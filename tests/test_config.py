"""Verifica la propiedad del entorno y la separación entre autenticación del producto y servicios."""
from pathlib import Path
import pytest
from pymongo.uri_parser import parse_uri
from app.config import Settings


def test_default_env_belongs_to_fastapi_not_the_working_directory(monkeypatch, tmp_path):
    """La ruta del archivo sigue en FastAPI aunque el proceso se lance desde otra carpeta."""
    expected = Path(__file__).resolve().parents[1] / '.env'
    monkeypatch.chdir(tmp_path)
    assert Settings.model_config['env_file'] == expected


def test_backend_jwt_cannot_authenticate_the_ai_service(monkeypatch):
    """Un JWT del producto no reemplaza la credencial independiente del puente."""
    monkeypatch.delenv('AI_SERVICE_TOKEN', raising=False)
    monkeypatch.setenv('JWT_SECRET', 'backend-only-secret')
    config = Settings(_env_file=None)
    assert 'jwt_secret' not in Settings.model_fields
    with pytest.raises(ValueError, match='AI_SERVICE_TOKEN must be configured'):
        _ = config.service_token


def test_ai_env_loads_credentials_and_the_selected_mongo_database(monkeypatch, tmp_path):
    """Conserva signos de exclamación y la base/authSource sin incluir credenciales reales."""
    keys = ['MONGODB_URI', 'MONGODB_DATABASE', 'GEMINI_API_KEY', 'AI_SERVICE_TOKEN']
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / '.env'
    env_file.write_text('MONGODB_URI="mongodb://test:example!!!!@127.0.0.1:27017/uml_studio_db?authSource=admin"\n'
                        'MONGODB_DATABASE=uml_studio_db\nGEMINI_API_KEY=test-key\nAI_SERVICE_TOKEN=test-service-token\n')
    config = Settings(_env_file=env_file)
    parsed = parse_uri(config.mongodb_uri)
    assert parsed['database'] == config.mongodb_database == 'uml_studio_db'
    assert parsed['options']['authSource'] == 'admin'
    assert parsed['password'] == 'example!!!!'
    assert config.gemini_api_key == 'test-key'
    assert config.service_token == 'test-service-token'


def test_process_environment_overrides_only_fastapi_file(monkeypatch, tmp_path):
    """Permite configurar contenedores con variables del proceso sobre el archivo local."""
    env_file = tmp_path / '.env'
    env_file.write_text('AI_SERVICE_TOKEN=file-token\n')
    monkeypatch.setenv('AI_SERVICE_TOKEN', 'process-token')
    assert Settings(_env_file=env_file).service_token == 'process-token'
