"""Verifica avisos posteriores al commit, recuperación y límites sin servidores externos."""
import asyncio
from unittest.mock import Mock

import mongomock
from fastapi.testclient import TestClient

from app.config import Settings
from app.conversations import ConversationService
from app.events import ConversationEvents
from app.main import create_app
from app.models import ModelReply, Submission
from app.repository import ConversationRepository


def test_stream_starts_with_reset_and_transports_thread_publication():
    """Una conexión nueva pide resincronizar y recibe solo revisión/sala desde otro hilo."""
    async def scenario():
        """Ejercita el generador real con el mismo cambio de hilo usado por el worker."""
        events = ConversationEvents()
        stream = events.stream()
        assert '"reset"' in await anext(stream)
        await asyncio.to_thread(events.publish, 'room', 3)
        frame = await asyncio.wait_for(anext(stream), 1)
        assert '"diagramId": "room"' in frame and '"revision": 3' in frame
        assert 'content' not in frame
        await stream.aclose()
        assert not events._subscribers
    asyncio.run(scenario())


def test_slow_subscriber_is_bounded_and_reset_recovers_overflow():
    """El desborde no acumula memoria ilimitada ni simula una entrega garantizada."""
    async def scenario():
        """Satura una cola y comprueba la orden de recuperación durable."""
        events = ConversationEvents()
        async with events.subscribe() as queue:
            for revision in range(257):
                events.publish('room', revision)
            await asyncio.sleep(0)
            assert queue.qsize() == 1
            assert queue.get_nowait() == {'type': 'reset'}
        assert not events._subscribers
    asyncio.run(scenario())


def test_notifications_follow_commits_and_reads_or_noops_do_not_publish():
    """El observador puede leer la revisión ya confirmada; lecturas e idempotencia no notifican."""
    repository = ConversationRepository(mongomock.MongoClient().db)
    changes = []

    def receive(room, revision):
        """Comprueba persistencia antes de aceptar el aviso."""
        assert repository.read(room)['revision'] == revision
        changes.append((room, revision))

    repository.on_changed = receive
    repository.mutate('room', lambda room: room['messages'].append({'id': 'message'}))
    repository.read('room')
    repository.mutate('room', lambda room: None)
    assert changes == [('room', 1)]
    repository.on_changed = Mock(side_effect=RuntimeError('transport failed'))
    repository.mutate('room', lambda room: room['messages'].append({'id': 'other'}))
    assert repository.read('room')['revision'] == 2


def test_worker_success_failure_retry_and_applied_proposal_publish_changes():
    """La notificación cubre resultados asíncronos y estados, además de la aceptación HTTP."""
    repository = ConversationRepository(mongomock.MongoClient().db)
    revisions = []
    repository.on_changed = lambda room, revision: revisions.append(revision)
    providers = Mock()
    providers.generate.return_value = (ModelReply(content='Ready', proposedAst={'nodes': [], 'connections': []}), {})
    service = ConversationService(repository, providers, Settings(_env_file=None))
    request = Submission(clientMessageId='request-1', sender={'userId': 'user', 'displayName': 'Alice', 'role': 'EDITOR'},
                         content='Help', diagram={}, diagramRevision='rev')
    accepted = service.submit('room', request)
    assert revisions == [1]
    service.submit('room', request)
    assert revisions == [1]
    providers.generate.side_effect = RuntimeError('failed')
    service.process('room')
    assert revisions == [1, 2, 3]
    assert service.history('room')['messages'][0]['status'] == 'failed'
    service.retry('room', accepted['id'], 'user')
    providers.generate.side_effect = None
    service.process('room')
    assert revisions == list(range(1, 7))
    proposal = service.history('room')['messages'][-1]['proposal']['id']
    service.proposal('room', proposal, applied=True)
    assert revisions[-1] == 7


def test_event_stream_requires_internal_service_token():
    """La nueva ruta no entrega avisos internos al navegador sin el secreto del puente."""
    settings = Settings(_env_file=None, ai_service_token='test-token')
    service = ConversationService(ConversationRepository(mongomock.MongoClient().db), Mock(), settings)
    with TestClient(create_app(service, settings)) as client:
        assert client.get('/internal/conversation-events').status_code == 401
        assert client.get('/internal/conversation-events', headers={'x-ai-service-token': 'wrong'}).status_code == 401
