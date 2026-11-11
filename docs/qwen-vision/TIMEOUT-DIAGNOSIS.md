# Diagnóstico de timeout de Qwen

## Consulta revisada

Se revisó el turno «Analiza el diagrama y dime que realiza el sw» del diagrama
`93cdd5a4-b3db-40f8-b4ad-78ea06c850e0`. MongoDB conservaba el turno Personal con
proveedor `ollama`, estado `failed` y código `generation_failed`.

La solicitud contenía el snapshot UML completo de 16 elementos y 18 relaciones, sin imagen
adjunta. El contexto reconstruido tenía 28 446 caracteres y cabía en el presupuesto de 48 000.
Las consultas MongoDB y las reproducciones de diagnóstico no modificaron el historial ni
aplicaron propuestas al diagrama.

## Evidencia y límites

| Comprobación | Resultado observado |
| --- | --- |
| `/api/tags` y `/api/ps` | HTTP 200; modelo disponible y cargado. |
| `/api/show` | `qwen3.8:latest`, 27,3B parámetros, Q4_K_M; capacidades completion, vision, tools y thinking. |
| Razonamiento declarado | Valores `false`, `low`, `medium`, `xhigh`; valor predeterminado `medium`. |
| Reproducción con petición anterior | `ReadTimeout` tras 90,28 segundos, antes de recibir el JSON completo. |
| Mismo contexto con `think: false` | HTTP 200 en 44,53 segundos; 10 059 tokens de entrada y 539 de salida. |
| Validación del resultado | `ModelReply` válido, intención `mentorship`, sin propuesta AST. |

El error histórico solo conservaba el código genérico: no se puede demostrar retrospectivamente
la excepción exacta de aquella ejecución. La reproducción sí demuestra un timeout con la
configuración anterior y una respuesta válida con el razonamiento opcional desactivado.
Son dos observaciones, no un benchmark: caché del prompt, carga del servidor y longitud de
salida influyen en los tiempos. No se garantiza que todo turno futuro termine antes de 90 segundos.
La prueba usó texto y snapshot; no evaluó una imagen ni las funciones de voz.

## Ajustes

- [ModelProviders](../../app/providers/models.py) envía `think: false` en las consultas Ollama
  de generación textual, imágenes, descripción, resúmenes y sugerencias. Conserva `stream: false`,
  el esquema JSON y la validación. El campo es parte del [contrato oficial de Ollama](https://docs.ollama.com/api/chat).
- [ConversationService](../../app/conversations.py) registra etapa, tipo de excepción y tiempo
  transcurrido junto al ID del turno y proveedor. No registra contenido privado ni el mensaje
  de la excepción. Esto diferencia `ReadTimeout` de errores de contexto o validación.
- No se amplió el timeout ni se recortó el snapshot para obtener una respuesta. La IA continúa
  proponiendo cambios pendientes; esta consulta conceptual no pidió modificar el diagrama.

## Activación

Reiniciar FastAPI para cargar los cambios, o comprobar que Uvicorn haya recargado los módulos.
Después, el autor puede reintentar el mensaje original desde el chat. La prueba aislada no
publicó su respuesta en la sala y no sustituyó el mensaje fallido.

## Verificación automatizada

Las regresiones comprueban `think: false` para los contratos estructurados de Ollama y verifican
que los logs de generación/transcripción contienen la etapa y `ReadTimeout`, sin filtrar
credenciales, contenido del mensaje ni Base64. Resultado de `uv run pytest -q`: **113 pruebas aprobadas**. Se observaron 71 advertencias
de deprecación de terceros (`google-genai` y `mongomock` en Python 3.14), sin fallos.
`git diff --check` y los 21 enlaces locales de los documentos revisados también pasaron.
