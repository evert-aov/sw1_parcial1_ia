"""API interna autenticada y ciclo de vida del worker recuperable."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from app.config import Settings
from app.conversations import ConversationService
from app.models import SpeechRequest, Submission
from app.memory import ConversationMemory, MemoryRepository
from app.events import ConversationEvents
from app.speech import SpeechService, SpeechCache, SpeechBusy, SpeechUnavailable
from app.providers.edge_speech import EdgeSpeechProvider
from app.providers.models import ModelProviders
from app.repository import create_repository

logger = logging.getLogger(__name__)


def create_app(service=None, settings=None, speech_provider=None) -> FastAPI:
    """Construye la API con dependencias sustituibles, sin conexiones al importar el módulo."""
    config = settings or Settings()
    token = config.service_token
    events = ConversationEvents()

    def create_speech(repository):
        """Compone síntesis y caché sobre la base existente con un proveedor sustituible."""
        return SpeechService(repository, speech_provider or EdgeSpeechProvider(),
                             SpeechCache(repository.requests.database, config.speech_cache_days), config)

    @asynccontextmanager
    async def lifespan(application):
        """Abre Mongo y proveedores; ejecuta y detiene el worker junto al proceso ASGI."""
        if service is not None:
            service.repository.on_changed = events.publish
            application.state.conversations = service
            application.state.speech = create_speech(service.repository)
            try:
                yield
            finally:
                await application.state.speech.close()
            return
        client, repository = await asyncio.to_thread(
            create_repository, config.mongodb_uri, config.mongodb_database
        )
        providers = ModelProviders(config)
        repository.on_changed = events.publish
        application.state.conversations = ConversationService(
            repository, providers, config,
            ConversationMemory(MemoryRepository(repository.requests.database), providers, config),
        )

        application.state.speech = create_speech(repository)

        async def worker():
            """Recorre las salas y recupera turnos persistidos; un fallo no detiene otras salas."""
            while True:
                try:
                    rooms = await asyncio.to_thread(repository.pending_rooms)
                    semaphore = asyncio.Semaphore(4)

                    async def process_room(room_id):
                        """Limita inferencias paralelas entre salas conservando un turno por sala."""
                        async with semaphore:  # noqa: B023
                            await asyncio.to_thread(
                                application.state.conversations.process, room_id
                            )

                    await asyncio.gather(*(process_room(room_id) for room_id in rooms))
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Conversation worker failed; retrying without logging payloads"
                    )
                await asyncio.sleep(0.5)

        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await application.state.speech.close()
            providers.close()
            client.close()

    application = FastAPI(title="UML Conversation Service", lifespan=lifespan)

    def authorize(x_ai_service_token: str = Header(default="")):
        """Valida el secreto del gateway sin exponer las rutas internas a navegadores anónimos."""
        if not hmac.compare_digest(x_ai_service_token, token):
            raise HTTPException(401, "Invalid service credential")

    def conversations():
        """Entrega el servicio configurado durante el arranque ASGI."""
        return application.state.conversations

    @application.get('/internal/conversation-events', dependencies=[Depends(authorize)])
    def conversation_events():
        """Entrega avisos internos autenticados; reconexión o desborde exige recuperar el historial."""
        return StreamingResponse(events.stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @application.exception_handler(ValueError)
    async def invalid_request(request, error):
        """Convierte validaciones de dominio en errores sin volcar payloads adjuntos."""
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(error)})

    @application.exception_handler(LookupError)
    async def missing_resource(request, error):
        """Uniforma recursos ausentes sin filtrar documentos de otras salas."""
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @application.get("/health")
    def health():
        """Confirma que el servicio completó su inicialización."""
        return {"status": "ok"}

    @application.get("/internal/models", dependencies=[Depends(authorize)])
    def models(service=Depends(conversations)):  # noqa: B008
        """Devuelve capacidades configuradas y conectividad local."""
        return service.providers.catalog()

    @application.get(
        "/internal/conversations/{room_id}", dependencies=[Depends(authorize)]
    )
    def history(
        room_id: UUID,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=100),
        service=Depends(conversations),
    ):
        """Pagina el historial por secuencia estable para sincronización y reconexión."""
        return service.history(str(room_id), before, limit)

    @application.post(
        "/internal/conversations/{room_id}/messages",
        status_code=202,
        dependencies=[Depends(authorize)],
    )
    def submit(room_id: UUID, submission: Submission, service=Depends(conversations)):
        """Persiste y encola un mensaje; la respuesta IA se entrega por el historial compartido."""
        return service.submit(str(room_id), submission)

    @application.post(
        "/internal/conversations/{room_id}/messages/{message_id}/retry",
        dependencies=[Depends(authorize)],
    )
    def retry(
        room_id: UUID, message_id: UUID, user_id: UUID, service=Depends(conversations)
    ):
        """Solicita reintento explícito sin crear otra intervención."""
        return service.retry(str(room_id), str(message_id), str(user_id))

    @application.get(
        "/internal/conversations/{room_id}/messages/{message_id}/attachment",
        dependencies=[Depends(authorize)],
    )
    def attachment(room_id: UUID, message_id: UUID, service=Depends(conversations)):
        """Sirve el adjunto original únicamente si pertenece a la sala solicitada."""
        if not any(
            m["id"] == str(message_id)
            for m in service.repository.read(str(room_id))["messages"]
        ):
            raise HTTPException(404)
        import base64

        media = service.repository.request(str(message_id)).get("attachment")
        if not media:
            raise HTTPException(404)
        return Response(
            base64.b64decode(media["data"]),
            media_type=media["mimeType"],
            headers={"Cache-Control": "no-store"},
        )

    @application.post(
        "/internal/conversations/{room_id}/messages/{message_id}/speech",
        dependencies=[Depends(authorize)],
    )
    async def speech(room_id: UUID, message_id: UUID, request: SpeechRequest):
        """Devuelve audio privado del texto existente; fallar no cambia ni reintenta el turno IA."""
        try:
            audio = await application.state.speech.render(str(room_id), str(message_id), request.voice)
        except SpeechBusy:
            raise HTTPException(429, "Speech capacity is busy", headers={"Retry-After": "5"}) from None
        except SpeechUnavailable:
            raise HTTPException(503, "Speech is temporarily unavailable") from None
        return Response(audio, media_type="audio/mpeg", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @application.get(
        "/internal/conversations/{room_id}/proposals/{proposal_id}",
        dependencies=[Depends(authorize)],
    )
    def proposal(room_id: UUID, proposal_id: UUID, service=Depends(conversations)):
        """Obtiene la propuesta para validación y aplicación por NestJS."""
        return service.proposal(str(room_id), str(proposal_id))

    @application.post(
        "/internal/conversations/{room_id}/proposals/{proposal_id}/applied",
        dependencies=[Depends(authorize)],
    )
    def applied(room_id: UUID, proposal_id: UUID, service=Depends(conversations)):
        """Registra la confirmación de una aplicación completada por el backend."""
        return service.proposal(str(room_id), str(proposal_id), applied=True)

    return application
