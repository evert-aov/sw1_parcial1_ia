# Guía del desarrollador — FastAPI y conversación grupal con IA

El microservicio `ia/` concentra historial compartido, turnos recuperables, contexto, imágenes,
clips de voz y proveedores Ollama/Vertex/Gemini. Usa MongoDB para persistencia y recibe
solicitudes internas de NestJS. No administra JWT de usuarios ni escribe el documento en PostgreSQL.

Su arquitectura separa API, casos de uso, contratos, persistencia y adaptadores de proveedores.
El microkernel pertenece al editor Angular; aquí las extensiones se realizan mediante las
fronteras del servicio, sin dependencias entre implementaciones del frontend y Python.

## Contenido

| Capítulo | Tema |
| --- | --- |
| [00 — Arquitectura y configuración](00-ARQUITECTURA-Y-CONFIGURACION.md) | Dependencias, arranque y propiedad del entorno. |
| [01 — API interna y contratos](01-API-INTERNA-Y-CONTRATOS.md) | Autenticación de servicio, rutas y límites de entrada. |
| [02 — Conversaciones y contexto](02-CONVERSACIONES-CONTEXTO-Y-TURNOS.md) | Secuencias, worker, turnos y memoria acotada. |
| [03 — MongoDB y recuperación](03-PERSISTENCIA-MONGODB-Y-RECUPERACION.md) | Colecciones, CAS, lease e idempotencia. |
| [04 — Proveedores y medios](04-PROVEEDORES-Y-ENTRADAS-MULTIMODALES.md) | Qwen 14B, Vertex/Gemini, imagen y voz. |
| [05 — Propuestas UML](05-PROPUESTAS-UML-E-INTEGRACION.md) | Contrato canónico, revisión humana y aplicación en NestJS. |
| [06 — Pruebas y operación](06-PRUEBAS-Y-OPERACION.md) | Tests, diagnóstico y límites conocidos. |
| [07 — Directorios y flujos](07-MAPA-DE-DIRECTORIOS-Y-FLUJOS.md) | Responsabilidades y puntos de extensión. |

## Inicio rápido

Desde `ia/`, con MongoDB disponible y `.env` configurado:

```sh
uv sync
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Python debe satisfacer [pyproject.toml](../pyproject.toml), actualmente `>=3.12`.
[uv.lock](../uv.lock) registra la resolución de dependencias. `GET /health` confirma que
terminó el arranque; no comprueba una inferencia real. El token interno debe coincidir con
`AI_SERVICE_TOKEN` en NestJS, sin reutilizar `JWT_SECRET`.

## Verificación y contribución

```sh
uv run pytest -q
```

Las pruebas usan MongoMock y proveedores sustituidos. Verificar MongoDB y proveedores reales
por separado cuando el cambio lo requiera. No fijar una cantidad histórica de pruebas.
Mantener código en inglés, docstrings y guías en español, y métodos documentados.

El asistente se presenta con la identidad **Kanon** y opera sobre salas grupales y privadas
(`scope: group | personal`). Admite texto, imágenes, clips de voz transcritos, transcripción de
intervenciones de llamada grupal con palabra de activación «Kanon», sugerencias autónomas y
síntesis selectiva con Edge TTS. Consultar el [modo voz](../docs/voice-mode/README.md).

Consultar la [skill de IA](../.agents/skills/sw1-ai-service-architect/SKILL.md), el
[README operativo](../README.md) y las guías de
[NestJS](../../backend/guia-desarrollador/README.md) y
[Angular](../../frontend/guia-desarrollador/README.md).

La [memoria persistente de conversación](../docs/conversation-memory/README.md) combina
mensajes recientes, acuerdos resumidos y recuperación léxica de mensajes antiguos.

La entrega inmediata del chat, la voz fija de Kanon y los subtítulos alineados se describen en
[Kanon: sincronización y reporte](../docs/kanon-realtime/README.md).

La llamada usa [Gemini Live](../docs/gemini-live/README.md) con audio nativo; los clips e imágenes
usan Gemini multimodal y mantienen el flujo de propuestas revisadas del chat.
