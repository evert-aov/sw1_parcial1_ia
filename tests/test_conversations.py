"""Pruebas de persistencia, identidad, recuperación y contratos sin inferencias facturables."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import json
import mongomock
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.conversations import ConversationService
from app.context import build_context
from app.main import create_app
from app.models import Submission, ModelReply, Attachment
from app.repository import ConversationRepository

ROOM = str(uuid4())
USER = str(uuid4())


class Providers:
    """Doble determinista que registra contexto y simula fallos del proveedor."""
    fail = False
    contexts = None

    def generate(self, provider, context):
        """Registra contexto o falla antes de publicar la respuesta."""
        if self.fail:
            raise RuntimeError('private provider secret')
        self.contexts = json.loads(context)
        return ModelReply(content='Hello', proposedAst={'nodes': [], 'connections': []}), {'model': 'test', 'provider': provider}

    def describe_attachment(self, attachment):
        """Simula una transcripción sin pedir acceso a audio real."""
        return 'Recorded question'

    def catalog(self):
        """Entrega capacidades constantes para probar la ruta autenticada."""
        return {'models': []}


@pytest.fixture
def service():
    """Crea persistencia independiente por prueba con el mismo contrato de MongoDB."""
    return ConversationService(ConversationRepository(mongomock.MongoClient().db), Providers(), Settings(_env_file=None, ai_service_token='test-token'))


def submission(**changes):
    """Construye una intervención autenticada que cada prueba puede especializar."""
    return Submission.model_validate({
        'clientMessageId': str(uuid4()), 'sender': {'userId': USER, 'displayName': 'Alice', 'role': 'EDITOR'},
        'content': 'Help with this diagram', 'diagramRevision': '2026-10-22T00:00:00.000Z',
        'diagram': {'nodes': [], 'connections': []}, **changes,
    })


def test_duplicate_submission_and_payload_conflict(service):
    """Una retransmisión no duplica mensajes ni permite reutilizar ID con otro contenido."""
    request = submission()
    assert service.submit(ROOM, request) == service.submit(ROOM, request)
    with pytest.raises(ValueError):
        service.submit(ROOM, request.model_copy(update={'content': 'Different'}))
    assert len(service.history(ROOM)['messages']) == 1


def test_concurrent_messages_have_unique_sequences(service):
    """CAS conserva un orden único cuando varios autores publican concurrentemente."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: service.submit(ROOM, submission(askAssistant=False)), range(16)))
    assert [m['sequence'] for m in service.history(ROOM)['messages']] == list(range(1, 17))


def test_one_active_lease_and_recovery(service):
    """Solo un worker reclama una sala y un lease vencido puede recuperarse."""
    service.submit(ROOM, submission())
    first = service.claim(ROOM)
    assert service.claim(ROOM) is None
    service.repository.rooms.update_one({'_id': ROOM}, {'$set': {'messages.0.leaseUntil': 0}})
    assert service.claim(ROOM)['leaseToken'] != first['leaseToken']


def test_response_proposal_and_global_individual_context(service):
    """Conserva autores y respuestas previas sin aplicar propuestas automáticamente."""
    first = service.submit(ROOM, submission())
    second = service.submit(ROOM, submission(sender={'userId': str(uuid4()), 'displayName': 'Bob', 'role': 'VIEWER'}))
    service.process(ROOM)
    service.process(ROOM)
    messages = service.history(ROOM)['messages']
    assert len(messages) == 4
    assert messages[2]['proposal']['status'] == 'pending'
    assert service.providers.contexts['sender']['displayName'] == 'Bob'
    assert any(m['sender'].get('displayName') == 'Alice' for m in service.providers.contexts['sharedHistory'])
    assert any(m['content'] == 'Hello' for m in service.providers.contexts['sharedHistory'])
    assert messages[3]['replyTo'] == second['id']
    service.process(ROOM)
    assert len(service.history(ROOM)['messages']) == 4


def test_media_retry_does_not_duplicate_transcription(service):
    """Reintentar inferencia conserva una sola transcripción y no publica errores privados."""
    message = service.submit(ROOM, submission(attachment={'kind': 'audio', 'mimeType': 'audio/webm;codecs=opus', 'data': 'YWJj'}))
    service.providers.fail = True
    service.process(ROOM)
    assert service.history(ROOM)['messages'][0]['errorCode'] == 'generation_failed'
    with pytest.raises(LookupError):
        service.retry(ROOM, message['id'], 'another-user')
    service.providers.fail = False
    service.retry(ROOM, message['id'], USER)
    service.process(ROOM)
    assert service.history(ROOM)['messages'][0]['content'].count('Recorded question') == 1


def test_human_voice_transcribes_without_invoking_ai(service):
    """Un clip entre personas enriquece el mismo historial sin disparar respuesta IA."""
    service.submit(ROOM, submission(askAssistant=False, attachment={'kind': 'audio', 'mimeType': 'audio/webm', 'data': 'YWJj'}))
    service.process(ROOM)
    assert len(service.history(ROOM)['messages']) == 1
    assert service.history(ROOM)['messages'][0]['transcription'] == 'Recorded question'
    assert service.providers.contexts is None


def test_http_requires_service_credential_and_isolates_attachments(service):
    """Las rutas internas y los adjuntos rechazan accesos sin secreto o de otra sala."""
    app = create_app(service, service.settings)
    with TestClient(app) as client:
        assert client.get('/internal/models').status_code == 401
        headers = {'x-ai-service-token': 'test-token'}
        request = submission(attachment={'kind': 'image', 'mimeType': 'image/png', 'data': 'YWJj'})
        response = client.post(f'/internal/conversations/{ROOM}/messages', json=request.model_dump(), headers=headers)
        assert response.status_code == 202
        message_id = response.json()['id']
        assert client.get(f'/internal/conversations/{ROOM}/messages/{message_id}/attachment', headers=headers).content == b'abc'
        assert client.get(f'/internal/conversations/{uuid4()}/messages/{message_id}/attachment', headers=headers).status_code == 404
        assert client.get(f'/internal/conversations/{ROOM}?limit=101', headers=headers).status_code == 422


def test_validation_rejects_unsafe_media_and_dangling_graph():
    """Valida los formatos aceptados y la integridad referencial antes de presentar propuestas."""
    with pytest.raises(ValueError):
        Attachment(kind='image', mimeType='image/svg+xml', data='YWJj')
    with pytest.raises(ValueError):
        ModelReply(content='x', proposedAst={'nodes': [], 'connections': [{'sourceNodeId': 'missing', 'targetNodeId': 'missing'}]})


def test_context_budget_and_author_references(service):
    """El presupuesto incluye sobrecarga JSON y referencias personales sin duplicar texto."""
    messages = [{'id': str(i), 'sequence': i, 'sender': {'userId': USER}, 'content': 'x' * 80} for i in range(1, 20)]
    current = {'id': '20', 'sequence': 20, 'sender': {'userId': USER}, 'content': 'Question'}
    context = build_context(messages, current, {}, 500)
    assert len(context) <= 500
    assert json.loads(context)['individualMessageSequences']
    with pytest.raises(ValueError):
        build_context(messages, current, {'name': 'x' * 500}, 500)


def test_history_pagination_and_sanitization(service):
    """La paginación no repite mensajes y oculta tokens internos de procesamiento."""
    for _ in range(4):
        service.submit(ROOM, submission())
    service.claim(ROOM)
    recent = service.history(ROOM, limit=2)
    earlier = service.history(ROOM, before=recent['messages'][0]['sequence'], limit=2)
    assert recent['hasMore']
    assert not earlier['hasMore']
    assert [m['sequence'] for m in earlier['messages'] + recent['messages']] == [1, 2, 3, 4]
    assert 'leaseToken' not in earlier['messages'][0]
