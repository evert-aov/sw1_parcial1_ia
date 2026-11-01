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


def test_private_route_fixed_voice_and_unchanged_history(setup):
    """Exige token y pertenencia; la voz fija reutiliza la caché sin mutar la secuencia."""
    service, provider, room, message = setup
    before = service.repository.read(room)
    app = create_app(SimpleNamespace(repository=service.repository), service.settings, provider)
    path = f'/internal/conversations/{room}/messages/{message}/speech'
    with TestClient(app) as client:
        assert client.post(path, json={'voice': 'marcelo'}).status_code == 401
        headers = {'x-ai-service-token': 'test-token'}
        for voice in ['marcelo', 'marcelo', 'marcelo']:
            response = client.post(path, json={'voice': voice}, headers=headers)
            assert response.status_code == 200
            assert response.content == b'fake-mpeg'
            assert response.headers['content-type'] == 'audio/mpeg'
            assert response.headers['cache-control'] == 'no-store'
        assert client.post(path, json={'voice': 'other'}, headers=headers).status_code == 422
        assert client.post(path, json={'voice': 'marcelo', 'text': 'injected'}, headers=headers).status_code == 422
        wrong_room = path.replace(room, str(uuid4()))
        assert client.post(wrong_room, json={'voice': 'marcelo'}, headers=headers).status_code == 404
    assert provider.calls == [('Hola equipo', 'marcelo')]
    assert service.repository.read(room) == before


def test_provider_failure_is_sanitized_and_retry_does_not_regenerate_model(setup):
    """Un fallo TTS no modifica el mensaje; otra petición puede sintetizarlo sin inferencia LLM."""
    service, provider, room, message = setup
    provider.failure = True
    app = create_app(SimpleNamespace(repository=service.repository), service.settings, provider)
    with TestClient(app) as client:
        path = f'/internal/conversations/{room}/messages/{message}/speech'
        response = client.post(path, json={'voice': 'marcelo'}, headers={'x-ai-service-token': 'test-token'})
        assert response.status_code == 503
        assert 'private' not in response.text
    provider.failure = False
    assert asyncio.run(service.render(room, message, 'marcelo')) == b'fake-mpeg'
    assert service.repository.read(room)['revision'] == 1


def test_concurrent_readers_share_one_task_and_capacity_is_bounded(setup):
    """Dos participantes comparten una variante; se rechaza otra cuando el cupo está ocupado."""
    service, provider, room, message = setup
    service.settings.speech_max_concurrent = 1

    async def scenario():
        """Retiene una síntesis para comprobar deduplicación y cierre sin carreras temporales."""
        provider.gate, provider.entered = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(service.render(room, message, 'marcelo'))
        await provider.entered.wait()
        second = asyncio.create_task(service.render(room, message, 'marcelo'))
        with pytest.raises(SpeechBusy):
            other = str(uuid4())
            original = service.repository.read(room)['messages'][0]
            service.repository.rooms.update_one({'_id': room}, {'$push': {'messages': {**original, 'id': other}}})
            await service.render(room, other, 'marcelo')
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
        listener = asyncio.create_task(service.render(room, message, 'marcelo'))
        await provider.entered.wait()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        tasks = list(service.tasks.values())
        provider.gate.set()
        await asyncio.gather(*tasks)
        assert await service.render(room, message, 'marcelo') == b'fake-mpeg'
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
            await service.render(room, message, 'marcelo')
        await service.close()
    asyncio.run(scenario())
    service.repository.rooms.update_one({'_id': room}, {'$set': {'messages.0.source': 'text'}})
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'marcelo'))
    service.repository.rooms.update_one({'_id': room}, {'$set': {'messages.0.source': 'assistant', 'messages.0.content': '```json\n{}\n```'}})
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'marcelo'))


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

        def __init__(self, text, voice, boundary="SentenceBoundary", **kwargs):
            """Registra argumentos públicos de síntesis."""
            calls.append((text, voice))

        async def stream(self):
            """Simula metadatos y audio; registra liberación aun con un fallo de tamaño."""
            try:
                yield {'type': 'SentenceBoundary', 'text': 'Hola', 'offset': 5_000_000, 'duration': 8_000_000}
                yield {'type': 'audio', 'data': b'1234'}
                yield {'type': 'audio', 'data': b'5678'}
            finally:
                closed.append(True)

    monkeypatch.setattr(edge_speech.edge_tts, 'Communicate', Communication)
    asset = asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Hola', 'marcelo'))
    assert asset.audio == b'12345678'
    assert asset.cues == [{'startMs': 500, 'endMs': 1300, 'text': 'Hola'}]
    monkeypatch.setattr(edge_speech, 'MAX_AUDIO_BYTES', 6)
    with pytest.raises(ValueError):
        asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Hola', 'marcelo'))
    assert calls == [('Hola', 'es-BO-MarceloNeural'), ('Hola', 'es-BO-MarceloNeural')]
    assert closed == [True, True]


def test_speech_rejects_long_text_and_empty_provider_audio(setup):
    """No trunca respuestas largas ni almacena resultados vacíos del proveedor."""
    service, provider, room, message = setup
    service.settings.speech_max_chars = 3
    with pytest.raises(ValueError):
        asyncio.run(service.render(room, message, 'marcelo'))
    assert provider.calls == []
    service.settings.speech_max_chars = 12000

    async def empty(text, voice):
        """Simula un servicio que cierra el canal sin producir audio."""
        return b''
    provider.synthesize = empty
    with pytest.raises(SpeechUnavailable):
        asyncio.run(service.render(room, message, 'marcelo'))
    assert service.cache.assets.count_documents({}) == 0


def test_timed_playback_reuses_audio_and_rejects_voice_changes(setup):
    """Conserva una sola voz y comparte audio/cues entre el endpoint clásico y el sincronizado."""
    from app.speech import SpeechAudio
    service, provider, room, message = setup

    async def synthesize(text, voice):
        """Entrega marcas conocidas sin acceder a Edge TTS."""
        provider.calls.append((text, voice))
        return SpeechAudio(b'fake-mpeg', [{'startMs': 500, 'endMs': 1300, 'text': 'Hola equipo'}])

    provider.synthesize = synthesize
    app = create_app(SimpleNamespace(repository=service.repository), service.settings, provider)
    path = f'/internal/conversations/{room}/messages/{message}/speech-playback'
    headers = {'x-ai-service-token': 'test-token'}
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 401
        assert client.post(path, json={'voice': 'sofia'}, headers=headers).status_code == 422
        packet = client.post(path, json={}, headers=headers)
        assert packet.status_code == 200
        assert packet.headers['cache-control'] == 'no-store'
        assert packet.json()['cues'] == [{'startMs': 500, 'endMs': 1300, 'text': 'Hola equipo'}]
        import base64
        assert base64.b64decode(packet.json()['audioBase64']) == b'fake-mpeg'
        assert client.post(path.replace('speech-playback', 'speech'), json={}, headers=headers).content == b'fake-mpeg'
    assert len(provider.calls) == 1


def test_transport_retry_discards_partial_audio_and_cues(monkeypatch, caplog):
    """Una desconexión no mezcla audio incompleto con el segundo intento ni filtra datos."""
    from app.providers import edge_speech
    calls, closed = [], []

    class Communication:
        """Simula una desconexión tras recibir una parte del audio."""

        def __init__(self, text, voice, **options):
            """Registra límites de red y asigna un intento independiente."""
            self.attempt = len(calls)
            calls.append(options)

        async def stream(self):
            """Emite datos parciales en el primer intento y completos en el segundo."""
            try:
                yield {'type': 'SentenceBoundary', 'text': str(self.attempt), 'offset': 0, 'duration': 10000}
                yield {'type': 'audio', 'data': b'partial' if self.attempt == 0 else b'complete'}
                if self.attempt == 0:
                    raise edge_speech.aiohttp.ClientConnectionError('private-url-and-content')
            finally:
                closed.append(self.attempt)

    monkeypatch.setattr(edge_speech.edge_tts, 'Communicate', Communication)
    result = asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Private text', 'marcelo'))
    assert result.audio == b'complete'
    assert [cue['text'] for cue in result.cues] == ['1']
    assert closed == [0, 1]
    assert calls == [{'boundary': 'SentenceBoundary', 'connect_timeout': 5, 'receive_timeout': 10}] * 2
    assert 'ClientConnectionError' in caplog.text
    assert 'private-url-and-content' not in caplog.text
    assert 'Private text' not in caplog.text


@pytest.mark.parametrize('failure,expected_attempts', [('no_audio', 2), ('forbidden', 1), ('cancelled', 1)])
def test_transport_retry_is_bounded_and_respects_cancellation(monkeypatch, failure, expected_attempts):
    """Agota solo dos intentos transitorios; permisos y cancelación no se reintentan."""
    from app.providers import edge_speech
    calls = []
    errors = {
        'no_audio': edge_speech.edge_tts.exceptions.NoAudioReceived('private'),
        'forbidden': edge_speech.aiohttp.ClientResponseError(None, (), status=403),
        'cancelled': asyncio.CancelledError(),
    }

    async def fail(self, text, voice):
        """Devuelve el fallo seleccionado sin conectar al proveedor externo."""
        calls.append(voice)
        raise errors[failure]

    monkeypatch.setattr(edge_speech.EdgeSpeechProvider, 'synthesize_once', fail)
    with pytest.raises(type(errors[failure])):
        asyncio.run(edge_speech.EdgeSpeechProvider().synthesize('Hola', 'marcelo'))
    assert len(calls) == expected_attempts


def test_speech_logs_stage_without_private_provider_details(setup, caplog):
    """El diagnóstico distingue síntesis de persistencia sin exponer el mensaje ni la URL."""
    service, provider, room, message = setup
    provider.failure = True
    with pytest.raises(SpeechUnavailable):
        asyncio.run(service.playback(room, message))
    assert 'stage=synthesis error_type=RuntimeError' in caplog.text
    assert 'private provider diagnostic' not in caplog.text
    assert service.repository.read(room)['messages'][0]['status'] == 'completed'
    assert service.cache.assets.count_documents({}) == 0
