# Microservicio de conversación e IA

FastAPI concentra la conversación grupal, contexto del mentor/modelador, Ollama **Qwen2.5-Coder 14B**,
Google Vertex/Gemini y procesamiento de imágenes/clips de voz. MongoDB conserva el historial.
NestJS conserva JWT, permisos, proyectos y aplicación del AST en PostgreSQL.

## Arranque local

Requisitos: Python 3.12 o posterior (verificado con 3.14), `uv`, MongoDB y el backend existente.

```bash
# Desde ia/, con MongoDB ya disponible y ia/.env configurado.
uv sync
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

No se instala ni descarga Qwen durante el arranque. Para habilitar el proveedor local:

```bash
ollama pull qwen2.5-coder:14b
```

El modelo necesita recursos suficientes; medir latencia y memoria en la máquina de despliegue.
La API no usa otro modelo ni envía texto a cloud cuando falla el proveedor seleccionado.

`GET /health` confirma el arranque. Las rutas `/internal/*` requieren `x-ai-service-token` y no
se deben publicar directamente en el navegador. FastAPI no requiere CORS abierto.
MongoDB debe estar disponible al arrancar; se falla explícitamente si no se puede conectar.

## Configuración y credenciales

FastAPI lee **exclusivamente `ia/.env`**, mediante una ruta absoluta independiente de la carpeta
desde la que se ejecute. Las variables del proceso prevalecen. Las credenciales `GEMINI_API_KEY`,
`GCP_PROJECT_ID` y `GCP_LOCATION`, los ajustes de Ollama y la conexión MongoDB pertenecen a este servicio.
NestJS conserva únicamente `AI_SERVICE_URL` y `AI_SERVICE_TOKEN` para el puente interno.
El token debe coincidir en ambos `.env`; es independiente del `JWT_SECRET`, que permanece solo en NestJS.
No copies sus valores al código ni al frontend. También se permite autenticación Google ADC.
El adaptador conserva compatibilidad con claves de Vertex Express y Gemini API.

Para Vertex regional con API key, configurar `GEMINI_API_KEY`, `GCP_PROJECT_ID` y `GCP_LOCATION`
del mismo proyecto autorizado. El cliente recibe los tres valores explícitamente: no basta con
enviar la región, ya que produciría una ruta con `projects/None`. Sin proyecto, una clave Vertex
usa el modo Express sin forzar región. Las claves de Gemini API siguen usando su cliente propio;
ADC continúa disponible cuando no se configura API key.

La configuración local selecciona **Vertex** como proveedor inicial con `gemini-2.5-flash`;
Qwen sigue disponible para texto. En Personal Kanon responde siempre; en Grupal se invoca
con `@Kanon` escrito o «Kanon» en un clip de voz. Los adjuntos se procesan con Gemini.

| Variable | Valor por defecto / propósito |
| --- | --- |
| `AI_SERVICE_TOKEN` | Obligatorio, secreto explícito del puente compartido con NestJS. |
| `MONGODB_URI` | Conexión privada definida en `ia/.env`, con autenticación en `admin`. |
| `MONGODB_DATABASE` | `uml_studio_db`; debe corresponder a la base seleccionada en la URI. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `CHAT_OLLAMA_MODEL` | `qwen2.5-coder:14b`; el antiguo `OLLAMA_MODEL` no reemplaza este valor. |
| `CHAT_DEFAULT_PROVIDER` | `ollama`; si no existe, se acepta `AI_PROVIDER` del entorno de FastAPI. Valores: `ollama`, `vertex`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` si no está definido en FastAPI. |
| `GEMINI_API_KEY` | Credencial privada de Vertex/Gemini; no se copia al backend ni al frontend. |
| `GCP_PROJECT_ID` | ID o número del proyecto autorizado; obligatorio para la ruta regional de Vertex. |
| `GCP_LOCATION` | `us-central1` |
| `CHAT_MEDIA_PROVIDER` | `vertex`: transcripción de clips y descripción de imágenes. |
| `CHAT_TIMEOUT_SECONDS` | 90, configurable de 5 a 120 por solicitud al proveedor. |
| `CHAT_CONTEXT_CHARS` | 48000; rechaza un snapshot que por sí solo no quepa. |
| `CHAT_RECENT_MESSAGES` | 12; ventana reciente entre 4 y 40 mensajes. |
| `CHAT_MEMORY_BATCH_MESSAGES` | 12; bloques resumidos entre 4 y 12 mensajes. |
| `CHAT_RETRIEVAL_MESSAGES` | 4; recuperación de 0 a 8 mensajes antiguos por consulta. |
| `CHAT_MEMORY_TIMEOUT_SECONDS` | 20; timeout del resumen entre 5 y 30 segundos. |

Ni el mensaje, ni el historial ni el snapshot pueden contener credenciales por diseño del contrato.
Los errores publicados no incluyen respuestas internas ni URLs de proveedores.

Las respuestas del modelo pasan por `ModelReply`: `changesSummary: null` se normaliza a `""`,
porque una respuesta conceptual puede no describir cambios. Se siguen rechazando resúmenes
con otros tipos, contenido vacío y referencias inválidas del grafo. El contrato publicado al
frontend mantiene el resumen como cadena.

Después de cambiar `.env`, reiniciar Uvicorn (el modo `--reload` no garantiza recargar ese archivo).
Cerrar y volver a abrir el panel para renovar el catálogo y la selección inicial de proveedor.

La configuración local usa MongoDB en `127.0.0.1:27017`, base `uml_studio_db` y `authSource=admin`.
La contraseña se conserva únicamente en `ia/.env`, no en este README ni en `.env.example`.
Usar la instancia existente; el `compose.yaml` de desarrollo no provisiona el usuario autenticado
ni debe iniciarse en el mismo puerto ocupado por esa instancia.

## Estructura

| Archivo | Responsabilidad |
| --- | --- |
| `app/main.py` | API interna autenticada, ciclo de vida y worker. |
| `app/config.py` | Entorno y configuración validada. |
| `app/models.py` | Contratos Pydantic, límites y validación de medios/propuestas. |
| `app/repository.py` | MongoDB y escritura compare-and-swap por revisión de sala. |
| `app/conversations.py` | Aceptación, turnos, lease, reintento y propuestas. |
| `app/context.py` | Ensamblaje del contexto reciente, memoria y recuperación bajo presupuesto. |
| `app/memory.py` | Resumen persistente por bloques, referencias de origen y búsqueda de mensajes antiguos. |
| `app/providers/models.py` | Adaptadores de inferencia y capacidades Ollama/Google. |
| `tests/` | Contratos, concurrencia, HTTP interno, contexto y recuperación. |

## API interna

Todas las salas usan el UUID del diagrama autorizado por NestJS.

| Método y ruta | Operación |
| --- | --- |
| `GET /internal/models` | Capacidades y disponibilidad configurada. |
| `GET /internal/conversations/{id}?before=...&limit=100` | Historial por secuencia. |
| `POST /internal/conversations/{id}/messages` | Entrada idempotente y encolado; devuelve 202. |
| `POST /internal/conversations/{id}/messages/{messageId}/retry?user_id=...` | Reintento por autor. |
| `GET /internal/conversations/{id}/messages/{messageId}/attachment` | Bytes del adjunto privado. |
| `GET /internal/conversations/{id}/proposals/{proposalId}` | Propuesta para validar en NestJS. |
| `POST /internal/conversations/{id}/proposals/{proposalId}/applied` | Confirmación idempotente del backend. |

No hay endpoints de escritura de AST. FastAPI nunca conecta a PostgreSQL.

## Persistencia y recuperación

`conversation_sessions` contiene la secuencia, revisión, mensajes y propuestas compactos.
`ai_requests` almacena el payload original, adjunto y snapshot por mensaje. `_id` es único;
`messages.status` tiene índice para localizar trabajo pendiente. No se necesitan transacciones
MongoDB multidocumento: el historial y las propuestas se modifican atómicamente en un documento.
Si una solicitud queda almacenada pero no se acepta el mensaje, el worker no la procesa.

Solo un lease puede confirmar un turno por sala. Vence en 300 segundos para recuperar reinicios.
El mensaje no desaparece cuando falla el proveedor. El autor puede solicitar reintento explícito.
Se pueden ejecutar cuatro salas en paralelo por proceso; la inferencia no bloquea el hilo ASGI.
Los metadatos de respuesta incluyen proveedor, modelo, uso de tokens cuando existe, revisión del
contexto y revisión del diagrama. Una propuesta siempre comienza en estado pendiente.

Límites: 5 MB por medio, 20 turnos pendientes por sala, unos 1.000 mensajes y 12 MB por historial.
Los medios se conservan con la solicitud; no hay limpieza automática en este incremento.

## Pruebas

```bash
uv run pytest -q
```

Se utiliza MongoMock para el contrato de repositorio, proveedores deterministas y TestClient ASGI.
No se afirma que estas pruebas sustituyan una validación de carga con MongoDB y proveedores reales.

Ver [reporte completo](../frontend/docs/chat-grupal/README.md) para flujos, frontend, permisos,
aplicación idempotente y limitaciones. Referencias técnicas:
[Ollama Chat](https://docs.ollama.com/api/chat),
[Google Gen AI SDK](https://googleapis.github.io/python-genai/),
[actualizaciones MongoDB](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/update/).

## Contrato de multiplicidades en propuestas UML

El prompt solicita `sourceMultiplicity` y `targetMultiplicity` como cadenas.
`app/proposal_contract.py` normaliza los alias `sourceCardinality` y `targetCardinality` antes
de publicar nuevas propuestas. Los valores ambiguos o de otro tipo se rechazan; no se altera
el JSON original del proveedor. El backend mantiene la validación autoritativa del DTO y adapta
los mismos alias al aplicar propuestas históricas ya guardadas.

Las pruebas en `tests/test_proposal_contract.py` verifican cardinalidades, preservación del
grafo, alias equivalentes, conflictos y ausencia de descarte silencioso de campos desconocidos.

## Guía del desarrollador

La guía detallada de arquitectura, contratos, proveedores, persistencia y pruebas está en
[guia-desarrollador/README.md](guia-desarrollador/README.md).
Las reglas para agentes están en [AGENTS.md](AGENTS.md).

## Modo voz del chat

Respuestas de chat con la voz masculina fija Marcelo (Bolivia), manteniendo un solo historial.
Consultar [arquitectura, uso y verificación](docs/voice-mode/README.md).

## Memoria de conversación

La IA combina mensajes recientes, resumen persistente de requisitos/decisiones y recuperación
textual del historial antiguo, aislados por diagrama y turno. Se activa automáticamente al
reiniciar FastAPI con este código; no requiere cambiar frontend, backend ni credenciales.
Los valores de memoria omitidos en `.env` toman los predeterminados indicados arriba.
La primera consulta que reúne suficientes mensajes previos crea `conversation_memory`.

El resumen puede consumir una inferencia adicional por bloque; no se ejecuta al abrir el panel.
No garantiza recordar cada detalle: los resúmenes son limitados y la búsqueda no es semántica.
Ver [funcionamiento, límites y reporte de verificación](docs/conversation-memory/README.md).

## Sincronización optimizada del chat

El chat usa avisos Socket.IO autorizados, alimentados por un flujo SSE interno desde FastAPI.
El panel recupera historial ante cambios y mantiene una comprobación de respaldo de 30 segundos
mientras la pestaña está visible. Consultar [contratos, operación y verificación](docs/chat-notifications/README.md).

## Gemini Live y medios originales

La llamada grupal puede conectar con **Gemini Live**, con consentimiento individual y la misma
clave Google que ya usa FastAPI. Reiniciar IA/backend/frontend tras actualizar; NestJS necesita
la dependencia `ws` del manifiesto. El navegador requiere micrófono y HTTPS o localhost.

- Modelo Live: `GEMINI_LIVE_MODEL=gemini-3.8-live` (respaldo: `gemini-live-2.5-flash-native-audio`).
- Duración máxima por sesión: `GEMINI_LIVE_SESSION_SECONDS=600` (30–900).
- Sesiones simultáneas por proceso: `GEMINI_LIVE_MAX_SESSIONS=4` (1–16).
- Voz nativa fija: Puck. Edge TTS conserva Marcelo para el chat.
- Imagen y audio usan Gemini; `@Kanon + clip` resuelve transcripción y respuesta en una solicitud de contenido.

Los valores nuevos tienen predeterminados; no hace falta duplicar la API key ni modificar
credenciales en NestJS. El modelo debe estar disponible para el proyecto/región configurados.
Consultar [reporte, diagramas y límites conocidos](docs/gemini-live/README.md).
