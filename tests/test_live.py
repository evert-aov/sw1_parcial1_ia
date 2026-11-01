"""Contratos Live con dobles: autorización interna, PCM acotado y persistencia compartida."""
import asyncio
import base64
from types import SimpleNamespace
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.live import LiveService, LiveStart, LiveTurnStore
from app.main import create_app
from app.repository import ConversationRepository


def setup_live():
    """Construye una identidad autenticada y un repositorio aislado por prueba."""
    repository = ConversationRepository(mongomock.MongoClient().db)
    start = LiveStart(sessionId=uuid4(), sender={'userId': 'user', 'displayName': 'Ada', 'role': 'EDITOR'},
                      diagram={'nodes': [], 'connections': []}, diagramRevision='revision')
    settings = Settings(_env_file=None, ai_service_token='test-token')
    service = SimpleNamespace(repository=repository)
    return LiveService(service, settings), repository, start


def test_live_route_rejects_missing_bridge_token():
    """El navegador no puede abrir una sesión interna sin el secreto del puente NestJS."""
    live, repository, _ = setup_live()
    app = create_app(SimpleNamespace(repository=repository), live.settings)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(f'/internal/conversations/{uuid4()}/live'):
                pass
        assert error.value.code == 1008


def test_live_turns_keep_identity_order_and_completed_transcripts():
    """Los turnos no pasan por el worker ni duplican TTS; preservan autor y secuencias."""
    _, repository, start = setup_live()
    store = LiveTurnStore(repository, 'room', start, 'live-model')
    messages = store.begin()
    messages[0].update(content='Kanon explica Pedido', transcription='Kanon explica Pedido')
    messages[1]['content'] = 'Pedido agrupa sus detalles.'
    store.finish()
    store.finish()
    saved = repository.read('room')['messages']
    assert [m['sequence'] for m in saved] == [1, 2]
    assert saved[1]['replyTo'] == saved[0]['id']
    assert saved[0]['sender']['userId'] == 'user'
    assert all(m['status'] == 'completed' and m['speechMode'] == 'live' and not m['streaming'] for m in saved)
    store.begin()
    store.messages[1]['content'] = 'Partial reply'
    store.finish(True)
    assert repository.read('room')['messages'][-1]['interrupted'] is True


@pytest.mark.parametrize('packet', [
    {'type': 'audio', 'data': 'invalid!'}, {'type': 'audio', 'data': 'YQ=='},
    {'type': 'audio', 'data': 'x' * 22001}, {'type': 'audio', 'data': 'YWI=', 'userId': 'forged'},
])
def test_live_pcm_rejects_malformed_frames(packet):
    """No transmite Base64 inválido, muestras incompletas ni campos fuera del contrato."""
    live, _, _ = setup_live()

    class Socket:
        async def receive_json(self):
            """Entrega el frame inválido seleccionado."""
            return packet

    async def scenario():
        """Un frame inválido falla antes de acceder al SDK."""
        with pytest.raises(ValueError):
            await live.send_audio(Socket(), None)
    asyncio.run(scenario())


def test_live_receiver_streams_pcm_and_commits_text_without_an_llm_worker():
    """Un turno nativo emite texto incremental y audio, luego confirma ambos mensajes."""
    live, repository, start = setup_live()
    store = LiveTurnStore(repository, 'room', start, 'live-model')
    events = []

    class Complete(Exception):
        """Detiene el doble al confirmar el primer turno."""

    class Socket:
        async def send_json(self, packet):
            """Registra eventos y finaliza la prueba tras la confirmación durable."""
            import copy
            events.append(copy.deepcopy(packet))
            if packet['type'] == 'turn_complete':
                raise Complete()

    class Session:
        async def receive(self):
            """Entrega una transcripción humana y una respuesta nativa de prueba."""
            yield SimpleNamespace(data=b'\x00\x00', server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text='Kanon, hola'),
                output_transcription=SimpleNamespace(text='Hola Ada'), interrupted=False, turn_complete=True))

    async def scenario():
        """Recibe el turno sin invocar proveedores reales."""
        with pytest.raises(Complete):
            await live.receive(Socket(), Session(), store)
    asyncio.run(scenario())
    assert [event['type'] for event in events] == ['message', 'message', 'audio', 'turn_complete']
    assert base64.b64decode(events[2]['data']) == b'\x00\x00'
    saved = repository.read('room')['messages']
    assert [message['content'] for message in saved] == ['Kanon, hola', 'Hola Ada']
    assert all(not message['streaming'] for message in saved)


def test_live_disconnect_releases_session_capacity_without_a_provider_error():
    """Colgar cancela la recepción paralela y devuelve el cupo sin anunciar un fallo de IA."""
    live, _, start = setup_live()
    packets = []
    released = []

    class Socket:
        async def send_json(self, packet):
            """Registra los eventos enviados al puente."""
            packets.append(packet)

        async def receive_json(self):
            """Simula que el usuario cierra su conexión después del saludo."""
            raise WebSocketDisconnect(1000)

    class Session:
        async def __aenter__(self):
            """Abre la sesión sustituida sin consumir una API real."""
            return self

        async def __aexit__(self, *args):
            """Registra la liberación del transporte del proveedor."""
            released.append(True)

        async def send_client_content(self, **kwargs):
            """Acepta el contexto inicial sin producir una respuesta."""

        async def receive(self):
            """Espera hasta que el cierre del puente cancele la tarea hermana."""
            await asyncio.sleep(60)
            yield None

    live.conversations.providers = SimpleNamespace(cloud=lambda: SimpleNamespace(
        aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kwargs: Session()))))
    asyncio.run(live.run(Socket(), 'room', start))
    assert packets == [{'type': 'ready'}]
    assert live.active == 0
    assert released == [True]


def test_live_receiver_executes_diagram_edit_tool():
    """Gemini Live ejecuta edit_diagram, difunde el AST actualizado y responde a la herramienta."""
    live, repository, start = setup_live()
    store = LiveTurnStore(repository, 'room', start, 'live-model')
    sent_packets = []
    tool_responses = []
    prompts = []

    class Done(Exception):
        pass

    class Socket:
        async def send_json(self, packet):
            sent_packets.append(packet)

    class Session:
        async def receive(self):
            yield SimpleNamespace(
                tool_call=SimpleNamespace(
                    function_calls=[
                        SimpleNamespace(
                            name='edit_diagram',
                            args={'instruction': 'Agregar clase Factura', 'summary': 'Clase Factura agregada'},
                            id='call-1',
                        )
                    ]
                ),
                server_content=None,
            )

        async def send_tool_response(self, function_responses=None):
            tool_responses.extend(function_responses)

        async def send_client_content(self, turns=None, turn_complete=False):
            prompts.append(turns)
            raise Done()

    fake_ast = {'type': 'class', 'nodes': [{'id': 'n1', 'name': 'Factura', 'nodeType': 'class'}], 'connections': []}
    live.conversations.providers = SimpleNamespace(
        generate=lambda provider, ctx: (
            SimpleNamespace(proposedAst=fake_ast, changesSummary='Clase Factura agregada', intent='modeling', content='Listo'),
            {},
        )
    )

    async def scenario():
        with pytest.raises(Done):
            await live.receive(Socket(), Session(), store)

    asyncio.run(scenario())
    assert any(p.get('type') == 'diagram_update' for p in sent_packets)
    assert sent_packets[0]['type'] == 'diagram_update'
    assert sent_packets[0]['ast'] == fake_ast
    assert len(tool_responses) == 1
    assert tool_responses[0].name == 'edit_diagram'
    assert len(prompts) == 1

