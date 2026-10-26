"""Regresiones de configuración cloud y validación de respuestas sin consumir cuota real."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import mongomock
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.conversations import ConversationService
from app.models import ModelReply, Submission
from app.providers.models import ModelProviders
from app.repository import ConversationRepository


@pytest.mark.parametrize(('key', 'project', 'expected'), [
    ('AQ.test-key', 'test-project', {'vertexai': True, 'api_key': 'AQ.test-key', 'project': 'test-project', 'location': 'us-central1'}),
    ('AQ.test-key', '', {'vertexai': True, 'api_key': 'AQ.test-key'}),
    ('test-gemini-key', 'unused-project', {'api_key': 'test-gemini-key'}),
    ('', 'adc-project', {'vertexai': True, 'project': 'adc-project', 'location': 'us-central1'}),
])
def test_cloud_authentication_routes(monkeypatch, key, project, expected):
    """Conserva proyecto y región para Vertex regional, sin introducirlos en Express o Gemini."""
    constructor = Mock()
    monkeypatch.setattr('app.providers.models.genai.Client', constructor)
    providers = ModelProviders(Settings(_env_file=None, gemini_api_key=key,
                                        gcp_project_id=project, gcp_location='us-central1'))
    try:
        first = providers.cloud()
        assert providers.cloud() is first
        assert constructor.call_count == 1
        options = dict(constructor.call_args.kwargs)
        assert options.pop('http_options').timeout == providers.settings.chat_timeout_seconds * 1000
        assert options == expected
    finally:
        providers.close()
    constructor.return_value.close.assert_called_once()


@pytest.mark.parametrize('summary', [None, '', 'Preserve this summary'])
def test_optional_summary_remains_a_string(summary):
    """Acepta el resumen nulo observado en Vertex y conserva resúmenes válidos sin alterarlos."""
    reply = ModelReply.model_validate_json(json.dumps({'content': 'OK', 'changesSummary': summary}))
    assert reply.changesSummary == ('' if summary is None else summary)
    assert isinstance(reply.model_dump()['changesSummary'], str)


@pytest.mark.parametrize('summary', [42, [], {}])
def test_invalid_summary_types_are_still_rejected(summary):
    """La normalización de null no permite números, listas ni objetos como resumen."""
    with pytest.raises(ValidationError):
        ModelReply(content='OK', changesSummary=summary)


def test_null_summary_does_not_weaken_other_validation():
    """Sigue rechazando respuestas vacías, campos extra y grafos con referencias inexistentes."""
    for changes in [
        {'content': ''},
        {'unexpected': 'value'},
        {'proposedAst': {'nodes': [], 'connections': [{'sourceNodeId': 'missing', 'targetNodeId': 'missing'}]}},
    ]:
        with pytest.raises(ValidationError):
            ModelReply.model_validate({'content': 'OK', 'changesSummary': None, **changes})


@pytest.mark.parametrize('provider', ['vertex', 'ollama'])
def test_generation_parses_nullable_summary(monkeypatch, provider):
    """Ambos proveedores aceptan el mismo contrato y conservan modelo y consumo de tokens."""
    payload = json.dumps({'content': 'OK', 'intent': 'mentorship', 'changesSummary': None, 'proposedAst': None})
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(
        text=payload, usage_metadata=SimpleNamespace(prompt_token_count=6, candidates_token_count=1))
    monkeypatch.setattr('app.providers.models.genai.Client', Mock(return_value=google))
    providers = ModelProviders(Settings(_env_file=None, gemini_api_key='AQ.test', gcp_project_id='test-project'))
    providers.http.close()
    providers.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'message': {'content': payload}, 'prompt_eval_count': 6, 'eval_count': 1})))
    try:
        reply, metadata = providers.generate(provider, 'Synthetic test')
        assert reply.content == 'OK'
        assert reply.changesSummary == ''
        assert reply.proposedAst is None
        assert metadata['provider'] == provider
        assert metadata['inputTokens'] == 6
        assert metadata['outputTokens'] == 1
    finally:
        providers.close()


def test_vertex_reply_reaches_shared_history(monkeypatch):
    """Un turno con el JSON real problemático termina completado y publica respuesta sin propuesta."""
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(
        text='{"content":"OK","intent":"mentorship","changesSummary":null,"proposedAst":null}',
        usage_metadata=None)
    monkeypatch.setattr('app.providers.models.genai.Client', Mock(return_value=google))
    settings = Settings(_env_file=None, gemini_api_key='AQ.test', gcp_project_id='test-project')
    providers = ModelProviders(settings)
    service = ConversationService(ConversationRepository(mongomock.MongoClient().db), providers, settings)
    try:
        request = Submission(clientMessageId='synthetic-test', sender={'userId': 'tester', 'displayName': 'Tester', 'role': 'EDITOR'},
                             content='Reply OK', provider='vertex', askAssistant=True,
                             diagram={'nodes': [], 'connections': []}, diagramRevision='test-revision')
        accepted = service.submit('test-room', request)
        service.process('test-room')
        messages = service.history('test-room')['messages']
        assert len(messages) == 2
        assert all(message['status'] == 'completed' for message in messages)
        assert messages[1]['content'] == 'OK'
        assert messages[1]['replyTo'] == accepted['id']
        assert messages[1]['provider'] == 'vertex'
        assert 'proposal' not in messages[1]
    finally:
        providers.close()


@pytest.mark.parametrize('provider', ['vertex', 'ollama'])
def test_memory_summary_uses_selected_provider_and_bounded_schema(monkeypatch, provider):
    """Ambos adaptadores resumen sin AST, con timeout propio y sin cambiar de modelo."""
    payload = json.dumps({'items': [{'kind': 'requirement', 'text': 'Conservar auditoría', 'sourceIds': ['m1']}]})
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text=payload)
    monkeypatch.setattr('app.providers.models.genai.Client', Mock(return_value=google))
    providers = ModelProviders(Settings(_env_file=None, gemini_api_key='AQ.test', gcp_project_id='test-project'))
    requests = []

    def respond(request):
        """Captura HTTP saliente sin acceso a Ollama ni consumo de cuota."""
        requests.append(request)
        return httpx.Response(200, json={'message': {'content': payload}})

    providers.http.close()
    providers.http = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        result = providers.summarize(provider, 'Synthetic history')
        assert result.items[0].sourceIds == ['m1']
        if provider == 'ollama':
            google.models.generate_content.assert_not_called()
            body = json.loads(requests[0].content)
            assert body['model'] == providers.settings.chat_ollama_model
            assert body['options']['num_predict'] == 2048
            assert requests[0].extensions['timeout']['read'] == providers.settings.chat_memory_timeout_seconds
            schema = body['format']
        else:
            assert not requests
            options = google.models.generate_content.call_args.kwargs
            assert options['model'] == providers.settings.gemini_model
            assert options['config'].http_options.timeout == providers.settings.chat_memory_timeout_seconds * 1000
            assert options['config'].http_options.retry_options.attempts == 1
            schema = options['config'].response_json_schema
        assert set(schema['properties']) == {'items'}
    finally:
        providers.close()
