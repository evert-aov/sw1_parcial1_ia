"""Avisos efímeros de revisión; MongoDB sigue siendo la fuente durable del historial."""
import asyncio
import json
from contextlib import asynccontextmanager
from threading import Lock


class ConversationEvents:
    """Difunde cambios del proceso a suscriptores internos sin consultar MongoDB periódicamente."""

    def __init__(self):
        """Inicializa suscriptores protegidos para publicaciones desde hilos del worker."""
        self._lock = Lock()
        self._subscribers = {}

    @asynccontextmanager
    async def subscribe(self):
        """Registra una cola acotada y la libera al cerrar la conexión interna."""
        queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers[queue] = asyncio.get_running_loop()
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers.pop(queue, None)

    def publish(self, room_id: str, revision: int) -> None:
        """Publica solamente sala y revisión después del commit; nunca texto ni adjuntos."""
        event = {'type': 'changed', 'diagramId': room_id, 'revision': revision}
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, event)
            except RuntimeError:
                pass  # Una conexión cerrada recuperará su historia al reconectar.

    def _enqueue(self, queue, event: dict) -> None:
        """Si un consumidor se atrasa, sustituye avisos por una orden de resincronización."""
        with self._lock:
            if queue not in self._subscribers:
                return
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({'type': 'reset'})
        else:
            queue.put_nowait(event)

    async def stream(self):
        """Emite SSE con recuperación inicial y latidos de transporte cada quince segundos."""
        async with self.subscribe() as queue:
            yield 'data: {"type":"reset"}\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f'data: {json.dumps(event)}\n\n'
                except TimeoutError:
                    yield ': keepalive\n\n'
