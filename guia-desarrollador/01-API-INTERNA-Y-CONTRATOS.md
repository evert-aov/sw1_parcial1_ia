# 01 — API interna y contratos

[Volver al índice](README.md)

## Frontera de confianza

Todas las rutas `/internal/*` exigen `x-ai-service-token`, comparado con el token configurado.
`/health` no exige ese encabezado. El navegador utiliza NestJS con JWT; no accede directamente
a estas rutas ni conoce el token de servicio. No abrir CORS para evitar el puente autorizado.

NestJS entrega `sender`, diagrama persistido y `diagramRevision` después de comprobar permisos.
FastAPI no consulta membresías PostgreSQL ni valida JWT. Un `userId` recibido aquí es identidad
atestiguada por el servicio llamante, no una credencial autónoma.

## Rutas

| Método y ruta | Resultado |
| --- | --- |
| `GET /health` | Estado del arranque. |
| `GET /internal/models` | Catálogo y proveedor inicial. |
| `GET /internal/conversation-events` | SSE autenticado de cambios de revisión; conexión persistente. |
| `GET /internal/conversations/{room_id}?before=...&limit=100` | Historial por secuencia. |
| `POST /internal/conversations/{room_id}/messages` | Aceptación persistida, HTTP 202. |
| `POST /internal/conversations/{room_id}/messages/{message_id}/retry?user_id=...` | Reintento explícito del autor. |
| `GET /internal/conversations/{room_id}/messages/{message_id}/attachment` | Bytes con MIME y `Cache-Control: no-store`. |
| `GET /internal/conversations/{room_id}/proposals/{proposal_id}` | Propuesta para el backend. |
| `POST /internal/conversations/{room_id}/proposals/{proposal_id}/applied` | Confirmación de aplicación. |
| `POST /internal/conversations/{room_id}/suggestions` | Evaluación efímera de sugerencia autónoma de Kanon (no muta el historial). |
| `POST /internal/conversations/{room_id}/suggestions/accept` | Siembra idempotente de una sugerencia aceptada en el chat personal. |

La sala usa el UUID del diagrama. La página admite entre 1 y 100 mensajes; `before` es una
secuencia positiva. Las respuestas internas no utilizan la envoltura `data` de NestJS.
El contrato ejecutable está en [main.py](../app/main.py).

## Modelos de entrada y salida

[models.py](../app/models.py) define contratos Pydantic con `extra='forbid'`.

| Modelo | Campos y reglas relevantes |
| --- | --- |
| `Actor` | `userId`, `displayName`, `role`. |
| `Submission` | `clientMessageId`, `sender`, `content`, `provider`, `askAssistant`, `scope` (`group` \| `personal`), `channel` (`chat` \| `call`), `language`, `attachment`, `diagram`, `diagramRevision`. |
| `Attachment` | `kind`, `mimeType`, Base64 `data`, `name`; sin URLs remotas. |
| `ModelReply` | `content`, `intent`, `changesSummary`, `proposedAst`. |
| `SuggestionEvaluationRequest` / `SuggestionAcceptSubmission` | Hallazgos estructurales locales y siembra idempotente en sala personal. |

En salas `personal`, **Kanon** responde siempre. En salas `group`, el servidor determina la
intervención de Kanon cuando el texto/imagen contiene `@Kanon` o cuando la transcripción de
audio o de llamada (`channel: "call"`) menciona «Kanon». Cada mensaje de audio o llamada expone
`transcription` para mostrarse en cursiva, y cada respuesta de Kanon registra `replyToUser` y
`replyToSource` para enfatizar el destinatario y gobernar la reproducción selectiva de voz.

`content` de entrada admite hasta 12000 caracteres y requiere texto no vacío o un adjunto.
`clientMessageId` admite de 8 a 80 caracteres; preservarlo al reintentar evita duplicados.
El snapshot serializado admite hasta 300000 caracteres, sujeto además al presupuesto de contexto.

Los adjuntos deben decodificar entre 1 y 5000000 bytes. La validación comprueba Base64 y MIME
admitido; no equivale a inspección completa del contenido binario ni a validación de duración de audio.
Las respuestas del modelo admiten hasta 32000 caracteres y resumen de hasta 4000.
`changesSummary: null` se convierte en cadena vacía; otros tipos inválidos se rechazan.

La API usa `401` para credencial interna inválida, `404` para recursos ausentes y `422` para
validaciones de entrada/dominio. NestJS adapta estos errores antes de responder al navegador.

## Audio de respuesta

`POST /internal/conversations/{room_id}/messages/{message_id}/speech` recibe exclusivamente
`{"voice":"marcelo"}` o `{}`. Exige token interno, verifica que el mensaje
pertenece a la sala y que es una respuesta completada. Devuelve bytes `audio/mpeg`,
`Cache-Control: no-store` y `X-Content-Type-Options: nosniff`. No acepta texto del navegador,
no genera otro mensaje y no modifica su secuencia, estado ni propuesta.

Errores: 404 (mensaje ajeno/ausente), 422 (voz, estado o texto inadmisible), 429 (capacidad
ocupada, `Retry-After: 5` en FastAPI) y 503 (fallo/timeout del proveedor). El cliente ofrece
reintento explícito; no debe iniciar un bucle de consultas de audio.

## Flujo interno de avisos

`GET /internal/conversation-events` exige el mismo token interno. Devuelve `text/event-stream`,
`Cache-Control: no-store` y `X-Accel-Buffering: no`. Al conectar emite `data: {"type":"reset"}`.
Tras cada mutación confirmada de una sala emite un objeto como:

```json
{"type":"changed","diagramId":"505dc0a9-8dd4-407c-b895-7ff8354ba5a0","revision":8}
```

Cada trama termina con dos saltos de línea. Los comentarios `: keepalive` mantienen la conexión
cada 15 segundos sin consultar MongoDB. Cada consumidor tiene una cola de hasta 256 avisos;
al desbordarse se reemplaza su contenido por un `reset`. El consumidor debe recuperar datos
por las rutas existentes después de conectar o recibir un reset. No hay cursor de replay,
`Last-Event-ID`, payloads de mensajes ni garantía de entrega durable.

El flujo pertenece al proceso FastAPI que atiende la conexión. Los cambios confirmados en
otro proceso se recuperan mediante el respaldo HTTP; una difusión inmediata entre procesos
requeriría un transporte compartido. Ver [reporte y operación](../docs/chat-notifications/README.md).


## Audio alineado de Kanon

La ruta autenticada `POST /internal/conversations/{room_id}/messages/{message_id}/speech-playback`
acepta la misma voz fija y devuelve `{audioBase64, mimeType, cues}`. Cada cue tiene `startMs`,
`endMs` y `text`: marcas `SentenceBoundary` reales de Edge TTS convertidas a milisegundos.
La caché `v2` guarda audio y marcas juntos; `/speech` reutiliza su audio. No se espera a la síntesis
para publicar texto ni se modifica el historial. Ver [contratos y reporte](../docs/kanon-realtime/README.md).

## WebSocket interno de Gemini Live

`WS /internal/conversations/{room_id}/live` requiere `x-ai-service-token` antes de aceptar la
conexión (cierre 1008 sin token válido). NestJS envía en los primeros 10 segundos `LiveStart`:
`sessionId`, `sender`, `diagram`, `diagramRevision` y `language`. Identidad y snapshot provienen
del backend autorizado, no del cuerpo del navegador. El mensaje inicial se limita a 350000 caracteres.

Después solo se aceptan `{type: "audio", data: "..."}` (PCM Base64) y `{type: "end"}`.
Los frames se limitan a 16000 bytes decodificados, número par de bytes y 96000 bytes/segundo.
Eventos de salida: `ready`, `message`, `audio`, `interrupted`, `turn_complete` y `error`.
Los errores públicos usan códigos `live_busy` o `live_unavailable`, sin detalles de credenciales.

`LiveTurnStore` reserva un par humano/asistente mediante CAS y confirma el texto al finalizar
el turno o ante una desconexión controlada. No guarda PCM ni pasa por el worker HTTP. El mensaje
lleva `speechMode: live`, `streaming` e `interrupted`; la secuencia sigue siendo la del historial.
Los fragmentos intermedios son efímeros. Un cierre abrupto del proceso puede dejar reservas sin
texto final; no se promete recuperación de audio ni reanudación automática de una sesión Live.
Ver [flujo completo y límites](../docs/gemini-live/README.md).
