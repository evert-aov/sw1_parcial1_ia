"""Adaptador de Microsoft Edge TTS para la voz masculina boliviana de Kanon."""
import asyncio
import logging
import aiohttp
import edge_tts
from contextlib import aclosing

from app.speech import MAX_AUDIO_BYTES, VOICE_IDS, SpeechVoice, SpeechAudio


class EdgeSpeechProvider:
    """Convierte texto en MPEG mediante el servicio remoto, sin ejecutar modelos locales."""

    async def synthesize(self, text: str, voice: SpeechVoice) -> SpeechAudio:
        """Reintenta una vez fallos transitorios, bajo el plazo global de SpeechService."""
        for attempt in range(2):
            try:
                return await self.synthesize_once(text, voice)
            except (aiohttp.ClientError, TimeoutError,
                    edge_tts.exceptions.NoAudioReceived, edge_tts.exceptions.WebSocketError) as error:
                if attempt or (isinstance(error, aiohttp.ClientResponseError)
                               and error.status not in {408, 429, 500, 502, 503, 504}):
                    raise
                # No registrar str(error): puede incluir URLs privadas del proveedor.
                logging.getLogger(__name__).warning(
                    'Speech transport retry error_type=%s', type(error).__name__)
                await asyncio.sleep(0.5)
        raise RuntimeError('Speech attempts exhausted')

    async def synthesize_once(self, text: str, voice: SpeechVoice) -> SpeechAudio:
        """Descarta fragmentos incompletos y cierra cada conexión, también al cancelar."""
        audio = bytearray()
        cues = []
        communication = edge_tts.Communicate(
            text, VOICE_IDS[voice], boundary="SentenceBoundary",
            connect_timeout=5, receive_timeout=10,
        )
        async with aclosing(communication.stream()) as stream:
            async for chunk in stream:
                if chunk['type'] == 'audio':
                    audio.extend(chunk['data'])
                    if len(audio) > MAX_AUDIO_BYTES:
                        raise ValueError('Speech audio exceeds the size limit')
                elif chunk['type'] == 'SentenceBoundary':
                    cues.append({
                        'startMs': chunk['offset'] / 10_000,
                        'endMs': (chunk['offset'] + chunk['duration']) / 10_000,
                        'text': chunk['text'],
                    })
        return SpeechAudio(bytes(audio), cues)
