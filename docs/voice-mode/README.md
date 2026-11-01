# Modo voz del chat grupal — implementación y operación

El modo voz permite escuchar respuestas de la IA con Sofía o Marcelo, voces de español de
Bolivia. Se conserva el chat compartido: hablar produce un clip con transcripción; escribir
produce texto; la IA responde en el mismo hilo y el audio es una representación opcional de
esa respuesta. No hay una segunda conversación ni una llamada WebRTC.

## Uso en el editor

1. Abrir el panel de IA y esperar a que sincronice el historial.
2. Pulsar el altavoz, junto al micrófono y al selector de modelos.
3. Elegir **Sofía · Bolivia · Femenina** o **Marcelo · Bolivia · Masculina**.
4. Activar **Escucha automática** para oír las respuestas nuevas. También se puede pulsar
   **Escuchar respuesta** debajo de una respuesta anterior sin activar escucha automática.
5. Para pedir ayuda, activar **Consultar a la IA**. Escribir o grabar con el micrófono,
   detener la grabación, revisar el adjunto y pulsar **Enviar**.
6. **Detener audio** cancela reproducción, descarga y cola local. Silenciar la escucha evita
   reproducir nuevas respuestas automáticamente.

La escucha es individual y se inicia apagada al abrir o cambiar de sala. Quien no pueda
escuchar conserva el texto y puede seguir participando. Seleccionar una voz no cambia el
proveedor del modelo ni la opción Chat de equipo / Consultar a la IA. La captura conserva
su límite de un minuto y el envío explícito; no hay detección automática de silencio.

El texto aparece independientemente del audio. Si Edge falla, el mensaje sigue completado;
reintentar audio no vuelve a ejecutar Qwen/Vertex. La voz no aplica propuestas al lienzo:
la revisión humana y la autorización NestJS se mantienen.

## Arranque

Desde `ia/`:

```sh
uv sync
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Reiniciar o recompilar también NestJS y Angular para incorporar el endpoint y los controles.
`edge-tts` queda declarado en [pyproject.toml](../../pyproject.toml) y resuelto en
[uv.lock](../../uv.lock), versión instalada en esta revisión: **7.3.1**. No requiere otra
API key, pero utiliza el servicio remoto de Microsoft; el texto sintetizado sale hacia ese
servicio. Google sigue transcribiendo los clips aunque se elija Qwen para generar texto.

La configuración opcional reside exclusivamente en `ia/.env` y está en
[.env.example](../../.env.example):

| Variable | Predeterminado | Función |
| --- | --- | --- |
| `SPEECH_TIMEOUT_SECONDS` | 30 | Plazo de generación/caché, entre 5 y 35 segundos. |
| `SPEECH_MAX_CHARS` | 12000 | Máximo de texto hablado; no se trunca silenciosamente. |
| `SPEECH_MAX_CONCURRENT` | 2 | Síntesis distintas simultáneas por proceso, máximo 8. |
| `SPEECH_CACHE_DAYS` | 7 | Retención de audio en MongoDB, entre 1 y 30 días. |

No cambiar credenciales Google, MongoDB ni el JWT para activar voz. El puente conserva
`AI_SERVICE_URL` y `AI_SERVICE_TOKEN`; no incorpora configuración de proveedores.

## Flujo y responsabilidades

```mermaid
sequenceDiagram
    participant U as Participante Angular
    participant N as NestJS
    participant F as FastAPI
    participant M as MongoDB
    participant E as Microsoft Edge TTS
    U->>N: Sincronizar historial
    N->>F: Consultar conversación autorizada
    F-->>U: Respuesta en texto mediante NestJS
    Note over U: Escucha opcional, independiente del resto del equipo
    U->>N: POST mensaje/speech con voz
    N->>N: Verificar JWT y acceso al diagrama
    N->>F: Solicitar audio con token interno
    F->>M: Validar mensaje y consultar variante
    alt Audio ausente
        F->>E: Sintetizar texto con voz boliviana
        E-->>F: Audio MPEG
        F->>M: Guardar audio con TTL
    end
    F-->>N: Audio privado o error acotado
    N-->>U: Bytes audio/mpeg
    U->>U: Reproducir; detener antes de grabar
```

| Capa | Implementación | Responsabilidad |
| --- | --- | --- |
| Angular | `ChatSpeechService` | Preferencia local, deduplicación, cola, reproducción, cancelación y liberación de Blob. |
| Angular | `ChatMediaService` | Micrófono, cámara y adjuntos; sigue separado de TTS. |
| Core Angular | `AiAssistantService.speech()` | Contrato binario sin conocimiento del proveedor. |
| NestJS | `ConversationController` / `ConversationService` | DTO, JWT, acceso al diagrama y respuesta privada. |
| NestJS | `AiServiceClient` | Puente HTTP con 40 segundos para voz, 15 para el resto. |
| FastAPI | `SpeechService` | Resolver texto persistido, límites, deduplicar tareas y coordinar caché. |
| FastAPI | `SpeechCache` | Colección `conversation_speech`, separada del historial, con TTL. |
| FastAPI | `EdgeSpeechProvider` | Implementar `SpeechProvider` usando Edge TTS y cerrar el canal ante cancelación/error. |

Las voces públicas son `sofia` y `marcelo`; el adaptador las traduce a
`es-BO-SofiaNeural` y `es-BO-MarceloNeural`. El proveedor de síntesis puede sustituirse sin
cambiar la captura, el historial o los proveedores de inferencia.

## Contrato y límites

- Público: `POST /api/conversations/:diagramId/messages/:messageId/speech`, con JWT.
- Interno: `POST /internal/conversations/{room_id}/messages/{message_id}/speech`, con token.
- Cuerpo: `{"voice":"sofia"}` o `{"voice":"marcelo"}`. No admite texto arbitrario ni URLs.
- Éxito: HTTP 200, bytes `audio/mpeg`, `Cache-Control: no-store` y `nosniff`.
- Solo se leen respuestas completadas pertenecientes a la sala. Se omiten bloques de código
  y marcas de presentación sin modificar el mensaje original. No se lee el AST de la propuesta.
- Se limita cada audio a 5 MB. La caché usa sala, mensaje, voz y hash del texto; cada descarga
  revalida acceso y pertenencia aunque la variante exista.
- No se incrementa la secuencia del chat ni se agregan binarios a `conversation_sessions`.
- 404: mensaje ausente/ajeno; 422: entrada, estado o tamaño no admitidos; 429: capacidad
  ocupada; 503: síntesis no disponible. El puente no expone detalles internos del proveedor.
- Las solicitudes concurrentes de una variante comparten tarea dentro de un proceso. En
  despliegues con varios workers puede duplicarse una síntesis antes de completar la caché;
  no se promete deduplicación distribuida ni límite global de concurrencia.
- Cancelar una descarga no cancela trabajo compartido del servidor: puede terminar y cachearse,
  siempre bajo el timeout. Al apagar ASGI se cancelan las síntesis pendientes.

## Reproducción y recuperación

Solo una respuesta se descarga/reproduce a la vez por panel; la cola admite diez respuestas
pendientes. Activar escucha toma como punto inicial la secuencia actual, evitando reproducir
historial al abrir, paginar o resincronizar. Los mensajes humanos no se sintetizan automáticamente.

Grabar o escuchar un clip humano detiene el audio de IA. Las nuevas respuestas pueden esperar
hasta liberar el micrófono. Cerrar el panel, cambiar de sala o voz cancela la suscripción HTTP
local y revoca la URL. El cliente espera como máximo 45 segundos. No hay sondeo ni reintentos
TTS automáticos: ante fallo se pausa escucha y se conserva el texto para reintento explícito.

Un navegador puede bloquear la reproducción automática. Se informa al participante y se ofrece
**Escuchar respuesta**; el audio descargado se conserva para reproducirlo directamente dentro
del clic, sin repetir la descarga. La política depende del navegador. Los controles funcionan dentro del
panel existente, sin añadir una barra al lienzo. Los audios vencen por TTL; no hay archivado de
llamadas ni reproducción sincronizada simultánea entre participantes.

## Verificación

Las suites cubren autorización antes de síntesis, rechazo de texto arbitrario, dos variantes,
caché, TTL, aislamiento de salas, secuencia sin cambios, saturación, cancelación de oyentes,
timeout, límites, cierre del canal y ausencia de reintentos automáticos. Angular comprueba que
activar voz no vuelve a conectar historial ni consulta el catálogo de modelos.

Las pruebas automatizadas usan MongoMock, transporte de audio y reproductor sustituidos.
No demuestran una conversación completa con MongoDB/PostgreSQL reales. Se realizó además
una prueba independiente contra Microsoft: ambas voces aparecieron en el catálogo y generaron
MPEG MP3 válido con una frase inocua, sin mensajes ni credenciales de usuarios. Un primer
intento de Marcelo falló por conectividad; una repetición produjo audio correctamente.

### Resultado de esta revisión

| Verificación ejecutada | Resultado |
| --- | --- |
| Frontend: `npm test -- --watch=false` | 25 archivos, 159 pruebas aprobadas; controles microkernel aprobados. |
| Frontend: `npm run build` | Compilación de producción correcta, sin advertencias en esta ejecución. |
| Backend: `npm test -- --runInBand` | 24 suites, 188 pruebas aprobadas. |
| Backend: `npm run build` | Compilación correcta. |
| IA: `uv run pytest -q` | 43 pruebas aprobadas. |
| Edge TTS real | Sofía: 25632 bytes; Marcelo: 26784 bytes; ambos MP3 mono, 24 kHz y 48 kbps. |
| Firefox con datos simulados | Menú de voces visible a 1366 × 900 y 390 × 844, sin desbordamiento horizontal. |
| Documentación | Enlaces locales y correspondencia de rutas revisados. |

La suite Python informa 61 advertencias de deprecación provenientes de Google Gen AI y
MongoMock bajo Python 3.14; no son fallos de pruebas. El TestClient ASGI se ejecutó fuera del
sandbox porque dentro del aislamiento quedaba bloqueado. La comprobación visual no hace
peticiones a proveedores reales ni utiliza datos privados.
# Diagnóstico de reproducción

Consultar el [reporte de recuperación del audio de Kanon](SPEECH-RECOVERY.md) para los
reintentos acotados, registros seguros y verificación del fallo `503` de reproducción.

