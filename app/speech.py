"""Síntesis opcional de respuestas guardadas, independiente de los turnos y del AST."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from app.config import Settings

SpeechVoice = Literal['marcelo']
VOICE_IDS = {'marcelo': 'es-BO-MarceloNeural'}
MAX_AUDIO_BYTES = 5_000_000


class SpeechUnavailable(Exception):
    """La voz falló sin invalidar el texto de la conversación."""


class SpeechBusy(Exception):
    """La capacidad de síntesis está ocupada; no se acumulan tareas ilimitadas."""


@dataclass
class SpeechAudio:
    """Audio completo y marcas temporales reales del proveedor, en milisegundos."""
    audio: bytes
    cues: list[dict] = field(default_factory=list)


class SpeechProvider(Protocol):
    """Puerto sustituible para sintetizar texto sin conocer salas ni persistencia."""

    async def synthesize(self, text: str, voice: SpeechVoice) -> SpeechAudio:
        """Produce audio MPEG acotado o informa un fallo del proveedor."""
        ...


class SpeechCache:
    """Guarda audio privado fuera del documento de historial y con vencimiento TTL."""

    def __init__(self, database, days: int):
        """Prepara la colección y el índice de expiración sin guardar secretos."""
        self.assets = database.conversation_speech
        self.days = days
        self.assets.create_index('expiresAt', expireAfterSeconds=0)

    def get(self, key: str) -> SpeechAudio | None:
        """Ignora entradas vencidas aunque Mongo aún no haya ejecutado su limpieza TTL."""
        asset = self.assets.find_one({'_id': key, 'expiresAt': {'$gt': datetime.now(timezone.utc)}})
        return SpeechAudio(bytes(asset['audio']), asset.get('cues', [])) if asset else None

    def put(self, key: str, asset: SpeechAudio) -> None:
        """Reemplaza una variante del audio con tamaño acotado y vencimiento renovado."""
        self.assets.replace_one({'_id': key}, {
            '_id': key, 'audio': asset.audio, 'cues': asset.cues,
            'expiresAt': datetime.now(timezone.utc) + timedelta(days=self.days),
        }, upsert=True)


class SpeechService:
    """Resuelve una respuesta de su sala, reutiliza variantes y acota el trabajo simultáneo."""

    def __init__(self, repository, provider: SpeechProvider, cache: SpeechCache, settings: Settings):
        """Inyecta puertos y límites; no inicia red ni inferencias al construir el servicio."""
        self.repository = repository
        self.provider = provider
        self.cache = cache
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}

    def spoken_text(self, room_id: str, message_id: str) -> str:
        """Valida pertenencia y estado; omite bloques de código y destinos de enlaces al leer."""
        message = next((item for item in self.repository.read(room_id)['messages']
                        if item['id'] == message_id), None)
        if message is None:
            raise LookupError('Message not found')
        if message.get('source') != 'assistant' or message.get('status') != 'completed':
            raise ValueError('Speech requires a completed assistant message')
        text = re.sub(r'```[\s\S]*?(?:```|$)', '', message['content'])
        text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'(?m)^\s{0,3}(?:#{1,6}|>|[-*+])\s+', '', text)
        text = re.sub(r'[`*_~]', '', text).strip()
        recipient = (message.get('replyToUser') or {}).get('displayName', '').strip()
        if recipient and text and recipient.casefold() not in text[:80].casefold():
            text = f'{recipient}, {text}'
        if not text or len(text) > self.settings.speech_max_chars:
            raise ValueError('Speech text is empty or exceeds the configured limit')
        return text

    async def render(self, room_id: str, message_id: str, voice: SpeechVoice) -> bytes:
        """Conserva el endpoint MPEG compatible usando el mismo artefacto de reproducción."""
        return (await self.playback(room_id, message_id, voice)).audio

    async def playback(self, room_id: str, message_id: str, voice: SpeechVoice = 'marcelo') -> SpeechAudio:
        """Solicita audio sin mutar el historial; comparte tareas locales y rechaza sobrecarga."""
        if voice not in VOICE_IDS:
            raise ValueError('Unsupported speech voice')
        text = await asyncio.to_thread(self.spoken_text, room_id, message_id)
        key = hashlib.sha256(f'v2:{room_id}:{message_id}:{voice}:{text}'.encode()).hexdigest()
        cached = await asyncio.to_thread(self.cache.get, key)
        if cached is not None:
            return cached
        task = self.tasks.get(key)
        if task is None:
            if len(self.tasks) >= self.settings.speech_max_concurrent:
                raise SpeechBusy()
            task = asyncio.create_task(self.generate(key, text, voice))
            self.tasks[key] = task
            task.add_done_callback(lambda completed: self.finish(key, completed))
        return await asyncio.shield(task)

    def finish(self, key: str, task: asyncio.Task) -> None:
        """Libera capacidad y consume errores incluso si el navegador canceló la descarga."""
        self.tasks.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def generate(self, key: str, text: str, voice: SpeechVoice) -> SpeechAudio:
        """Acota tiempo y tamaño; solo almacena audio completo y oculta detalles del proveedor."""
        stage = 'cache_read'
        try:
            async with asyncio.timeout(self.settings.speech_timeout_seconds):
                cached = await asyncio.to_thread(self.cache.get, key)
                if cached is not None:
                    return cached
                stage = 'synthesis'
                audio = await self.provider.synthesize(text, voice)
                # Compatibilidad con adaptadores existentes sin metadatos de alineación.
                if isinstance(audio, bytes):
                    audio = SpeechAudio(audio)
                if not audio.audio or len(audio.audio) > MAX_AUDIO_BYTES:
                    raise ValueError('Invalid audio size')
                stage = 'cache_write'
                await asyncio.to_thread(self.cache.put, key, audio)
                return audio
        except Exception as error:
            logging.getLogger(__name__).warning(
                'Speech unavailable stage=%s error_type=%s', stage, type(error).__name__)
            raise SpeechUnavailable() from error

    async def close(self) -> None:
        """Cancela las síntesis pendientes al finalizar el proceso ASGI."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
