"""Adaptador de Microsoft Edge TTS para las dos voces bolivianas admitidas."""
import edge_tts
from contextlib import aclosing

from app.speech import MAX_AUDIO_BYTES, VOICE_IDS, SpeechVoice


class EdgeSpeechProvider:
    """Convierte texto en MPEG mediante el servicio remoto, sin ejecutar modelos locales."""

    async def synthesize(self, text: str, voice: SpeechVoice) -> bytes:
        """Recoge fragmentos acotados y deja propagar cancelaciones al cliente de red."""
        audio = bytearray()
        async with aclosing(edge_tts.Communicate(text, VOICE_IDS[voice]).stream()) as stream:
            async for chunk in stream:
                if chunk['type'] == 'audio':
                    audio.extend(chunk['data'])
                    if len(audio) > MAX_AUDIO_BYTES:
                        raise ValueError('Speech audio exceeds the size limit')
        return bytes(audio)
