"""Regresiones de memoria persistente, presupuesto y aislamiento sin usar modelos reales."""
import json
from copy import deepcopy
from unittest.mock import Mock

import mongomock
import pytest

from app.config import Settings
from app.context import build_context
from app.conversations import ConversationService
from app.memory import ConversationMemory, MemoryRepository, MemorySummary, eligible_messages, retrieve
from app.models import ModelReply, Submission
from app.repository import ConversationRepository


def message(sequence, content='Comentario cotidiano', **changes):
    """Construye mensajes con identidad y secuencia estables para comprobar procedencia."""
    return {'id': f'm{sequence}', 'sequence': sequence, 'content': content,
            'sender': {'userId': 'alice', 'displayName': 'Alice'}, 'status': 'completed', **changes}


def settings(**changes):
    """Usa bloques pequeños y configuración sintética sin credenciales del entorno local."""
    return Settings(_env_file=None, chat_recent_messages=4, chat_memory_batch_messages=4, **changes)


class Summarizer:
    """Doble que conserva una decisión temprana y registra las entradas recibidas."""

    def __init__(self):
        """Inicializa un registro independiente por instancia."""
        self.calls = []

    def summarize(self, provider, context):
        """Devuelve citas verificables de los mensajes recibidos o de la memoria previa."""
        payload = json.loads(context)
        self.calls.append((provider, payload))
        items = payload['previousMemory'] or [
            {'kind': 'decision', 'text': payload['messages'][0]['content'][:240],
             'sourceIds': [payload['messages'][0]['id']]}]
        return MemorySummary(items=items)


@pytest.fixture
def memory():
    """Provee MongoMock y un resumidor determinista para cada prueba."""
    return ConversationMemory(MemoryRepository(mongomock.MongoClient().db), Summarizer(), settings())


def test_checkpoint_is_persistent_incremental_and_lazy(memory):
    """Reutiliza memoria tras reiniciar y solo resume cuando se completa otro bloque antiguo."""
    messages = [message(i) for i in range(1, 13)]
    assert memory.prepare('room', messages[:7], 'ollama') is None
    assert not memory.providers.calls
    first = memory.prepare('room', messages[:8], 'ollama')
    assert first['messageCount'] == 4
    assert first['method'] == 'model'
    restarted = ConversationMemory(memory.repository, Summarizer(), settings())
    assert restarted.prepare('room', messages[:11], 'vertex') == first
    assert not restarted.providers.calls
    second = restarted.prepare('room', messages, 'vertex')
    assert second['messageCount'] == 8
    assert restarted.providers.calls[0][0] == 'vertex'
    assert [m['sequence'] for m in restarted.providers.calls[0][1]['messages']] == [5, 6, 7, 8]
    assert second['items'][0]['sourceIds'] == ['m1']


def test_room_and_causal_prefix_isolation(memory):
    """Un turno antiguo no recibe memoria futura ni reutiliza el resumen de otra sala."""
    messages = [message(i, f'Decidimos requisito {i}') for i in range(1, 17)]
    future = memory.prepare('room', messages, 'vertex')
    earlier = memory.prepare('room', messages[:8], 'vertex')
    other = memory.prepare('other', messages[:8], 'vertex')
    assert len({future['_id'], earlier['_id'], other['_id']}) == 3
    assert earlier['messageCount'] == 4
    assert all(int(source[1:]) <= 4 for item in earlier['items'] for source in item['sourceIds'])
    assert memory.repository.checkpoints.count_documents({}) == 3


def test_source_correction_invalidates_checkpoint_without_overwrite(memory):
    """Cambiar una transcripción invalida su hash; una escritura tardía no reemplaza un checkpoint."""
    messages = [message(i) for i in range(1, 9)]
    first = memory.prepare('room', messages, 'ollama')
    messages[0]['content'] = 'Corrección de transcripción'
    second = memory.prepare('room', messages, 'ollama')
    assert second['_id'] != first['_id']
    assert memory.repository.store({**first, 'items': []})['items'] == first['items']
    assert second['items'][0]['text'] == 'Corrección de transcripción'


@pytest.mark.parametrize('failure', ['timeout', 'unknown_source', 'empty'])
def test_invalid_summary_uses_persistent_extracts(memory, failure):
    """Tiempo agotado, ausencia de contenido y referencias inventadas degradan a citas originales."""
    if failure == 'timeout':
        memory.providers.summarize = Mock(side_effect=TimeoutError('private secret'))
    else:
        memory.providers.summarize = Mock(return_value=MemorySummary(items=[] if failure == 'empty' else [
            {'kind': 'decision', 'text': 'Invented', 'sourceIds': ['future-message']}]))
    messages = [message(i, 'No debemos eliminar los pedidos') for i in range(1, 9)]
    result = memory.prepare('room', messages, 'vertex')
    assert result['method'] == 'extractive'
    assert result['items']
    assert all(item['kind'] == 'context' and item['text'] == messages[0]['content'] for item in result['items'])
    memory.prepare('room', messages, 'vertex')
    memory.providers.summarize.assert_called_once()


def test_large_existing_history_requires_only_one_summary(memory):
    """La adopción en salas antiguas no genera una ráfaga de inferencias ni envía adjuntos privados."""
    messages = [message(i, 'Requisito: conservar auditoría ' + 'x' * 10000,
                        attachment={'data': 'private'}, proposal={'ast': 'private'}) for i in range(1, 601)]
    original = deepcopy(messages)
    checkpoint = memory.prepare('room', messages, 'vertex')
    assert len(memory.providers.calls) == 1
    payload = memory.providers.calls[0][1]
    assert len(payload['messages']) == 4
    assert all(len(record['content']) <= 800 and record['excerpted'] for record in payload['messages'])
    assert len(json.dumps(payload)) < 16000
    assert 'private' not in json.dumps(payload)
    assert messages == original
    assert checkpoint['messageCount'] == 596


def test_eligibility_excludes_pending_failed_and_future_humans():
    """Solo mensajes completados previos y sus respuestas asociadas pueden alimentar memoria."""
    messages = [message(1), message(2, status='failed'), message(3, status='queued'),
                message(5, 'Future human'), message(6, 'Previous response', replyTo='m1',
                sender={'kind': 'assistant'}), message(7, 'Future response', replyTo='m5')]
    assert [m['id'] for m in eligible_messages(messages, message(4))] == ['m1', 'm6']


def test_retrieval_recovers_old_terms_and_keeps_author_attribution():
    """Una consulta recupera acuerdos fuera de la ventana reciente y sus autores autenticados."""
    messages = [message(1, 'Decidimos mantener auditoría de pedidos', sender={'userId': 'bob'}),
                message(2, 'Descripción de imagen: auditoría de facturas', source='image')]
    messages += [message(i) for i in range(3, 20)]
    assert retrieve(messages, 'AUDITORIA', 2)[0]['id'] in ('m1', 'm2')
    assert retrieve(messages, 'Sin coincidencias extraterrestres', 4) == []
    current = message(20, '¿Qué acordamos sobre auditoria?')
    result = json.loads(build_context(messages, current, {'nodes': []}, 4000, recent_count=4))
    assert [m['sequence'] for m in result['sharedHistory']] == [16, 17, 18, 19]
    recalled = result['retrievedHistory']
    assert {item['sourceId'] for item in recalled} == {'m1', 'm2'}
    assert next(item for item in recalled if item['sourceId'] == 'm1')['sender']['userId'] == 'bob'
    assert 'auditoría de facturas' in recalled[1]['content']


@pytest.mark.parametrize('limit', [500, 4000, 48000])
def test_three_context_layers_respect_exact_budget_and_preserve_snapshot(memory, limit):
    """Incluye escapes, autores y metadatos en el presupuesto; el AST permanece completo."""
    messages = [message(i, 'Requisito auditoría: ' + '\\"\n' * 1600) for i in range(1, 17)]
    checkpoint = memory.prepare('room', messages, 'vertex')
    current = message(17, 'Auditoria')
    diagram = {'nodes': [{'id': 'existing-id'}], 'connections': []}
    encoded = build_context(messages, current, diagram, limit, checkpoint, recent_count=4)
    assert len(encoded) <= limit
    context = json.loads(encoded)
    assert context['diagram'] == diagram
    assert context['currentMessage'] == current['content']
    assert context['sender'] == current['sender']
    for item in context['sharedHistory'] + context.get('retrievedHistory', []):
        original = next(source for source in messages if source['id'] == item['sourceId'])
        assert item['excerpted'] == (len(item['content']) < len(original['content']))
        assert item['content'] in original['content']
    if limit == 48000:
        assert context['conversationMemory']['items']
        assert context['retrievedHistory']
        assert context['sharedHistory']
        assert context['conversationMemory']['items'][0]['sources'][0]['sender']['userId'] == 'alice'


def test_snapshot_is_never_silently_truncated():
    """Un snapshot obligatorio demasiado grande se rechaza antes de consultar proveedores."""
    with pytest.raises(ValueError, match='Diagram exceeds'):
        build_context([], message(1), {'nodes': ['x' * 4000]}, 4000)


def test_unknown_memory_sources_are_not_injected():
    """La capa de contexto descarta referencias ajenas al conjunto elegible del turno."""
    checkpoint = {'method': 'model', 'items': [{'kind': 'decision', 'text': 'Future', 'sourceIds': ['m99']}]}
    context = json.loads(build_context([message(1)], message(2), {}, 4000, checkpoint))
    assert 'conversationMemory' not in context


@pytest.mark.parametrize('cache_failure', [False, True])
def test_service_integrates_memory_without_changing_history_or_public_reply(cache_failure):
    """El caso de uso combina memoria e historial y sigue respondiendo si falla su caché derivada."""
    database = mongomock.MongoClient().db
    providers = Summarizer()
    providers.generate = Mock(return_value=(ModelReply(content='Respuesta'), {'provider': 'vertex'}))
    memory = ConversationMemory(MemoryRepository(database), providers, settings())
    service = ConversationService(ConversationRepository(database), providers, settings(), memory)
    for i in range(8):
        service.submit('room', Submission(clientMessageId=f'input-id-{i}', sender={'userId': 'alice', 'displayName': 'Alice', 'role': 'EDITOR'},
            content=f'Decidimos auditoría {i}', askAssistant=False, diagram={}, diagramRevision='rev'))
    original = deepcopy(service.repository.read('room')['messages'])
    if cache_failure:
        service.memory.repository.latest = Mock(side_effect=RuntimeError('private cache data'))
    accepted = service.submit('room', Submission(clientMessageId='question', sender={'userId': 'alice', 'displayName': 'Alice', 'role': 'EDITOR'},
        content='¿Qué decidimos sobre auditoría?', askAssistant=True, provider='vertex', diagram={}, diagramRevision='rev'))
    service.process('room')
    stored = service.repository.read('room')['messages']
    assert stored[:8] == original
    assert stored[-2]['status'] == 'completed'
    assert stored[-1]['replyTo'] == accepted['id']
    context = json.loads(providers.generate.call_args.args[1])
    assert bool(context.get('conversationMemory')) is not cache_failure
    assert context['retrievedHistory']
    assert len(context['sharedHistory']) == 4


def test_application_factory_enables_persistent_memory(monkeypatch):
    """El arranque normal compone memoria persistente sin exigir cambios a los clientes HTTP."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    database = mongomock.MongoClient().db
    repository = ConversationRepository(database)
    providers = Mock()
    monkeypatch.setattr('app.main.create_repository', Mock(return_value=(Mock(), repository)))
    monkeypatch.setattr('app.main.ModelProviders', Mock(return_value=providers))
    app = create_app(settings=settings(ai_service_token='test-token'))
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        service = app.state.conversations
        assert isinstance(service.memory, ConversationMemory)
        assert service.memory.providers is providers
        assert service.memory.repository.checkpoints.database is repository.requests.database
        providers.summarize.assert_not_called()
    providers.close.assert_called_once()
