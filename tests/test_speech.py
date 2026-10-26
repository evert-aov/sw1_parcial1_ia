"""Regresiones de voz privada, límites y recuperación sin consumir proveedores reales."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository import ConversationRepository
from app.speech import SpeechBusy, SpeechCache, SpeechService, SpeechUnavailable


class Provider:
    """Doble de síntesis con control de concurrencia y fallos."""

    def __init__(self):
        """Inicializa contadores sin crear conexiones de red."""
        self.calls = []
        self.failure = False
        self.gate = None
        self.entered = None

    async def synthesize(self, text, voice):
        """Produce audio de prueba o espera una barrera para simular síntesis en curso."""
        self.calls.append((text, voice))
        if self.entered:
            self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise RuntimeError('private provider diagnostic')
        return b'fake-mpeg'


@pytest.fixture
def setup():
    """Compone un historial aislado, un adaptador doble y la caché Mongo compatible."""
    repository = ConversationRepository(mongomock.MongoClient().db)
    room, message = str(uuid4()), str(uuid4())
    repository.rooms.insert_one({'_id': room, 'revision': 1, 'nextSequence': 2, 'messages': [
        {'id': message, 'sequence': 1, 'source': 'assistant', 'status': 'completed',
         'content': '**Hola** [equipo](https://example.invalid)\n```json\n{}\n```'}]})
    settings = Settings(_env_file=None, ai_service_token='test-token')
    provider = Provider()
    cache = SpeechCache(repository.requests.database, 7)
    service = SpeechService(repository, provider, cache, settings)
    return service, provider, room, message


def test_private_route_voice_variants_and_unchanged_history(setup):
    """Exige token y pertenencia; ambas voces reutilizan variantes sin mutar la secuencia."""
    service, provider, room, message = setup
    before = service.repository.read(room)
    app = create_app(SimpleNamespace(repository=service.repository), service.settings, provider)
    path = f'/internal/conversations/{room}/messages/{message}/speech'
    with TestClient(app) as client:
        assert client.post(path, json={'voice': 'sofia'}).status_code == 401
        headers = {'x-ai-service-token': 'test-token'}
        for voice in ['sofia', 'sofia', 'marcelo']:
            response = client.post(path, json={'voice': voice}, headers=headers)
            assert response.status_code == 200
            assert response.content == b'fake-mpeg'
            assert response.headers['content-type'] == 'audio/mpeg'
            assert response.headers['cache-control'] == 'no-store'
        assert client.post(path, json={'voice': 'other'}, headers=headers).status_code == 422
        assert client.post(path, json={'voice': 'sofia', 'text': 'injected'}, headers=headers).status_code == 422
        wrong_room = path.replace(room, str(uuid4()))
        assert client.post(wrong_room, json={'voice': 'sofia'}, headers=headers).status_code == 404
    assert provider.calls == [('Hola equipo', 'sofia'), ('Hola equipo', 'marcelo')]
    assert service.repository.read(room) == before


def test_provider_failure_is_sanitized_and_retry_does_not_regenerate_model(setup):
    """Un fallo TTS no modifica el mensaje; otra petición puede sintetizarlo sin inferencia LLM."""
    service, provider, room, message = setup
    provider.failure = True
    app = create_app(SimpleNamespace(repository=service.repository), service.settings, provider)
    with TestClient(app) as client:
        path = f'/internal/conversations/{room}/messages/{message}/speech'
        response = client.post(path, json={'voice': 'sofia'}, headers={'x-ai-service-token': 'test-token'})
        assert response.status_code == 503
        assert 'private' not in response.text
    provider.failure = False
    assert asyncio.run(service.render(room, message, 'sofia')) == b'fake-mpeg'
    assert service.repository.read(room)['revision'] == 1


def test_concurrent_readers_share_one_task_and_capacity_is_bounded(setup):
    """Dos participantes comparten una variante; se rechaza otra cuando el cupo está ocupado."""
    service, provider, room, message = setup
    service.settings.speech_max_concurrent = 1

    async def scenario():
        """Retiene una síntesis para comprobar deduplicación y cierre sin carreras temporales."""
        provider.gate, provider.entered = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(service.render(room, message, 'sofia'))
        await provider.entered.wait()
        second = asyncio.create_task(service.render(room, message, 'sofia'))
        with pytest.raises(SpeechBusy):
            await service.render(room, message, 'marcelo')
        provider.gate.set()
        assert await first == await second == b'fake-mpeg'
        await service.close()
    asyncio.run(scenario())
    assert len(provider.calls) == 1


def test_cancelled_listener_does_not_cancel_shared_synthesis(setup):
    """Cancelar un cliente permite terminar y cachear la variante para otro participante."""
    service, provider, room, message = setup

    async def scenario():
        """Cancela al solicitante mientras el proveedor mantiene el trabajo compartido."""
        provider.gate, provider.entered = asyncio.Event(), asyncio.Event()
        listener = asyncio.create_task(service.render(room, message, 'sofia'))
        await provider.entered.wait()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        tasks = list(service.tasks.values())
        provider.gate.set()
        await asyncio.gather(*tasks)
        assert await service.render(room, message, 'sofia') == b'fake-mpeg'
        await service.close()
    asyncio.run(scenario())
    assert len(provider.calls) == 1


def test_timeout_and_invalid_message_limits(setup):
    """La espera tiene límite y los mensajes humanos o vacíos nunca se envían a TTS."""
    service, provider, room, message = setup
    service.settings.speech_timeout_seconds = 0.01

    async def scenario():
        """Simula un proveedor bloqueado sin esperar el timeout de producción."""
        provider.gate = asyncio.Event()
        with pytest.raises(SpeechUnavailable):
            await service.render(room, message, 'sofia')
        await service.close()
    asyncio.run(scenario())
    service.repository.rooms.update_one({'_id': room}, {'$set': {'messages.0.source': 'text'}})
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'sofia'))
    service.repository.rooms.update_one({'_id': room}, {'$set': {'messages.0.source': 'assistant', 'messages.0.content': '```json\n{}\n```'}})
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'sofia'))


def test_expired_cache_is_not_served(setup):
    """El vencimiento se comprueba al leer aunque la limpieza TTL esté pendiente."""
    service, _, _, _ = setup
    service.cache.assets.insert_one({'_id': 'expired', 'audio': b'old', 'expiresAt': datetime.now(timezone.utc) - timedelta(days=1)})
    assert service.cache.get('expired') is None


def test_adapter_selects_bolivian_voice_and_closes_oversized_stream(monkeypatch):
    """El adaptador usa IDs reales y cierra el canal al exceder el presupuesto binario."""
    from app.providers import edge_speech
    calls, closed = [], []

    class Communication:
        """Doble del transporte Edge con fragmentos y cierre verificables."""

        def __init__(self, text, voice):
            """Registra argumentos públicos de síntesis."""
            calls.append((text, voice))

        async def stream(self):
            """Simula metadatos y audio; registra liberación aun con un fallo de tamaño."""
            try:
                yield {'type': 'WordBoundary', 'text': 'Hola'}
                yield {'type': 'audio', 'data': b'1234'}
                yield {'type': 'audio', 'data': b'5678'}
            finally:
                closed.append(True)

    monkeypatch.setattr(edge_speech.edge_tts, 'Communicate', Communication)
    assert asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Hola', 'sofia')) == b'12345678'
    monkeypatch.setattr(edge_speech, 'MAX_AUDIO_BYTES', 6)
    with pytest.raises(ValueError):
        asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Hola', 'marcelo'))
    assert calls == [('Hola', 'es-BO-SofiaNeural'), ('Hola', 'es-BO-MarceloNeural')]
    assert closed == [True, True]


def test_speech_rejects_long_text_and_empty_provider_audio(setup):
    """No trunca respuestas largas ni almacena resultados vacíos del proveedor."""
    service, provider, room, message = setup
    service.settings.speech_max_chars = 3
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'sofia'))
    assert provider.calls == []
    service.settings.speech_max_chars = 12000

    async def empty(text, voice):
        """Simula un servicio que cierra el canal sin producir audio."""
        return b''
    provider.synthesize = empty
    with pytest.raises(SpeechUnavailable):
        asyncio.run(service.render(room, message, 'sofia'))
    assert service.cache.assets.count_documents({}) == 0
