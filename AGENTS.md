# AGENTS.md — Microservicio FastAPI de conversación e IA

Aplicar la skill [sw1-ai-service-architect](.agents/skills/sw1-ai-service-architect/SKILL.md)
y consultar la [guía del desarrollador](guia-desarrollador/README.md). Las instrucciones
explícitas del usuario prevalecen.

## Reglas del servicio

1. **Separación**: API/lifespan en `app/main.py`, contratos en `models.py`, casos de uso en `conversations.py`, contexto, repositorio y proveedores en sus módulos.
2. **Entorno propio**: `Settings` lee `ia/.env` por ruta absoluta y variables del proceso. Nunca leer `backend/.env` ni usar `JWT_SECRET` como token interno.
3. **Propiedad**: MongoDB, historial, turnos y proveedores pertenecen a IA. NestJS conserva JWT, permisos y escritura del AST en PostgreSQL.
4. **Contratos**: Pydantic rechaza campos desconocidos; mantener límites de texto, adjuntos, contexto y propuestas. No registrar secretos, medios ni payloads privados.
5. **Recuperación**: preservar IDs idempotentes, secuencias, CAS y token de lease. No mantener la única copia de una solicitud en memoria.
6. **Proveedores**: Qwen2.5-Coder 14B en Ollama y Google mediante adaptadores. No descargar modelos al arrancar ni cambiar proveedor silenciosamente.
7. **Medios**: imagen y clips de voz mediante Google; elegir Qwen para texto no hace local el procesamiento de medios. No implementar llamadas/edge-tts sin nuevo alcance.
8. **Propuestas**: publicar pendiente; normalizar aliases conocidos sin mutar el original ni descartar campos. NestJS valida y confirma aplicación tras commit SQL.
9. **Documentación**: código y nombres en inglés, docstrings de todos los métodos implementados y guías en español. Actualizar contratos y documentación de los consumidores.
10. **Verificación**: para código ejecutar `uv run pytest -q`; para contratos, también las suites de NestJS/Angular afectadas. No confundir mocks con pruebas reales de infraestructura.

Consultar `pyproject.toml` y `uv.lock` para dependencias. Eliminar directorios vacíos tras
reorganizar; no incluir `.env`, `.venv` ni credenciales en ejemplos o commits.
