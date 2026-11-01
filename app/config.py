"""Configuración del servicio sin exponer secretos en respuestas o registros."""
from pathlib import Path
from typing import Literal
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Lee exclusivamente ia/.env; las variables del proceso prevalecen sin depender del cwd."""
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / '.env',
        extra='ignore', case_sensitive=False,
    )
    ai_service_token: str = ''
    mongodb_uri: str = 'mongodb://127.0.0.1:27017/uml_studio_db'
    mongodb_database: str = 'uml_studio_db'
    ollama_base_url: str = 'http://127.0.0.1:11434'
    chat_ollama_model: str = 'qwen2.5-coder:14b'
    gemini_model: str = 'gemini-2.5-flash'
    gemini_live_model: str = 'gemini-3.8-live'
    gemini_live_voice: str = 'Charon'
    gemini_live_session_seconds: int = Field(default=600, ge=30, le=900)
    gemini_live_max_sessions: int = Field(default=4, ge=1, le=16)
    gemini_api_key: str = ''
    gcp_project_id: str = ''
    gcp_location: str = 'us-central1'
    chat_default_provider: Literal['ollama', 'vertex'] = Field(default='ollama', validation_alias=AliasChoices('CHAT_DEFAULT_PROVIDER', 'AI_PROVIDER'))
    chat_media_provider: Literal['vertex'] = 'vertex'
    chat_timeout_seconds: int = Field(default=90, ge=5, le=120)
    chat_context_chars: int = Field(default=48000, ge=4000, le=100000)
    chat_recent_messages: int = Field(default=12, ge=4, le=40)
    chat_memory_batch_messages: int = Field(default=12, ge=4, le=12)
    chat_retrieval_messages: int = Field(default=4, ge=0, le=8)
    chat_memory_timeout_seconds: int = Field(default=20, ge=5, le=30)
    speech_timeout_seconds: int = Field(default=30, ge=5, le=35)
    speech_max_chars: int = Field(default=12000, ge=100, le=32000)
    speech_max_concurrent: int = Field(default=2, ge=1, le=8)
    speech_cache_days: int = Field(default=7, ge=1, le=30)

    @property
    def service_token(self) -> str:
        """Exige el secreto interno explícito, independiente de la autenticación JWT de NestJS."""
        if self.ai_service_token.strip():
            return self.ai_service_token.strip()
        raise ValueError('AI_SERVICE_TOKEN must be configured')
