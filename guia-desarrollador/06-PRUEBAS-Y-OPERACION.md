# 06 — Pruebas y operación

[Volver al índice](README.md)

## Suite vigente

```sh
uv sync
uv run pytest -q
```

| Archivo | Cobertura principal |
| --- | --- |
| `tests/test_config.py` | Entorno propio, token y selección de proveedor. |
| `tests/test_conversations.py` | HTTP interno, autorización de servicio, contexto, concurrencia y recuperación. |
| `tests/test_providers.py` | Configuración Google/Ollama, respuesta y resumen estructurados. |
| `tests/test_events.py` | Avisos tras commit, fallos/reintentos, autenticación y desborde de colas. |
| `tests/test_memory.py` | Checkpoints, aislamiento, recuperación, fuentes, presupuesto y degradación controlada. |
| `tests/test_proposal_contract.py` | Alias de multiplicidad, conflictos y preservación del grafo. |

Las pruebas usan MongoMock, proveedores deterministas y TestClient. No requieren consumir
una API de pago para cada ejecución ni sustituyen pruebas de carga o conectividad real.
Al cambiar una frontera ejecutar también las suites backend/frontend correspondientes.

## Diagnóstico por síntoma

| Síntoma | Comprobación |
| --- | --- |
| Fallo de arranque | `AI_SERVICE_TOKEN` explícito y conexión MongoDB autenticada. |
| NestJS devuelve 503 | Proceso FastAPI, `AI_SERVICE_URL`, token compartido y timeout del puente. |
| `/health` responde pero falla IA | Modelo instalado, credencial, proyecto, región y límites del proveedor. |
| Vertex intenta una ruta con `projects/None` | Configurar proyecto en el cliente regional y reiniciar. |
| Estado `generation_failed` | Buscar `Conversation turn failed` por `request_id`: etapa, proveedor, tipo de excepción y duración; reintentar el turno original como autor tras corregir la causa. |
| Propuesta rechazada por NestJS | Revisar `error.issues` y contrato canónico; no asumir que una respuesta conversacional confirma aplicación. |
| Catálogo/historial consultados sin pausa | Revisar temporizadores y efectos del cliente Angular antes de atribuir consumo a Qwen. |

El frontend actual separa catálogo e historial, evita solicitudes solapadas y cancela el ciclo
al cerrar el panel. En el servidor, `/internal/models` no genera texto. Los logs de muchos
`GET /internal/models` por segundo indican un problema de solicitudes, no demuestran carga del modelo.

## Comprobación manual acotada

1. Iniciar MongoDB, FastAPI y NestJS con sus entornos independientes.
2. Verificar `GET /health`, sin inferir de ello que las credenciales cloud funcionan.
3. Abrir un diagrama autorizado y enviar texto sin consultar IA; comprobar historial común.
4. Solicitar una respuesta corta con el proveedor seleccionado y revisar su estado.
5. Probar imagen o clip con credenciales de medios disponibles.
6. Revisar y aplicar una propuesta en un diagrama de prueba; verificar permisos y revisión.

No publicar secretos en ejemplos o logs. Reiniciar después de cambiar `.env` y reabrir el panel
para renovar el catálogo. Las pruebas reales pueden consumir cuota; usar entradas pequeñas.

## Límites actuales

No hay llamadas, streaming de conversación en vivo, archivado automático del chat ni una cola externa.
La síntesis opcional de respuestas usa Edge TTS y una caché de audio con TTL; ver el
[modo voz](../docs/voice-mode/README.md).
Los límites del historial son explícitos; el servicio no conserva una conversación ilimitada.
La concurrencia por sala protege la publicación, no garantiza una sola inferencia tras un fallo.
Medir uso de memoria y latencia con datos reales antes de ampliar capacidad.

## Operación del flujo de avisos

Una conexión `GET /internal/conversation-events` permanece abierta por proceso NestJS. El
historial se consulta al abrir el panel, ante cambios y cada 30 segundos como respaldo visible.
Los latidos SSE no generan nuevas peticiones GET. Reiniciar FastAPI y NestJS tras instalar esta
versión; no se requieren variables nuevas. Un proxy debe permitir respuestas streaming sin
buffering y conservar la conexión entre latidos. Ver el
[reporte de notificaciones](../docs/chat-notifications/README.md).

## Diagnóstico seguro de turnos fallidos

`ConversationService.process` registra `request_id`, `provider`, `stage`, `error_type`
y `elapsed_ms` cuando un turno falla. Las etapas son `request`, `attachment_description`,
`context` y `generation`. La etapa de generación incluye la validación del resultado del modelo.
El registro no incluye el texto de la excepción, traceback, prompts, medios, URLs ni credenciales.
El contrato público mantiene `generation_failed`; los fallos históricos previos a este registro
no permiten reconstruir retrospectivamente su causa exacta.

Un `ReadTimeout` indica espera de lectura agotada. Con Ollama sin streaming, una inferencia
puede superar ese plazo antes de entregar el JSON final. Ver el
[caso reproducido y corrección](../docs/qwen-vision/TIMEOUT-DIAGNOSIS.md).
