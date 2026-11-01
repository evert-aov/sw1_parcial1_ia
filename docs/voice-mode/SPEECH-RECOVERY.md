# Recuperación de fallos de audio de Kanon

[Volver al modo voz](README.md) · [Guía de proveedores](../../guia-desarrollador/04-PROVEEDORES-Y-ENTRADAS-MULTIMODALES.md)

## Incidente y diagnóstico

El 28 de octubre de 2026 se reportó un `503` en `speech-playback` después de recibir
texto. La consulta del turno señalado en MongoDB confirmó tanto el mensaje humano como
la respuesta del asistente en estado `completed`, sin `errorCode`. La síntesis directa de
esa respuesta (1562 caracteres, una vez retirada la presentación Markdown) produjo
598464 bytes y 16 marcas de frase en aproximadamente 5 segundos.

El error original no pudo reproducirse durante esa comprobación. Los registros anteriores
ocultaban su causa, por lo que no permiten atribuirlo con certeza a timeout, desconexión
o persistencia. El texto «Processing failed. Your message is saved.» corresponde a un
estado `failed` del turno; no es el mensaje de error de reproducción. No se cambiaron
estados históricos para esconder ese aviso ni se regeneró la respuesta del modelo.

## Cambio aplicado

- El adaptador Edge TTS reintenta **una sola vez** ante fallos transitorios de transporte,
  ausencia de audio, errores WebSocket, HTTP 408/429 o los errores 500/502/503/504.
- Cada intento crea una conexión y buffers nuevos. Los fragmentos incompletos y sus marcas
  temporales se descartan para evitar frases repetidas o subtítulos desfasados.
- La conexión tiene un límite de 5 segundos y una lectura inactiva de 10 segundos.
  La pausa entre intentos es de 0,5 segundos. Ambos intentos siguen sujetos al plazo total
  de `SPEECH_TIMEOUT_SECONDS` (30 segundos por defecto), sin ampliar el plazo del backend.
- Cancelaciones, errores de permisos y exceso de tamaño no se reintentan.
- El registro interno informa `stage=cache_read|synthesis|cache_write` y la clase de error.
  No imprime excepciones completas, URL, credenciales, texto ni audio.
- El contrato HTTP y la voz masculina boliviana Marcelo permanecen iguales. Los errores
  definitivos conservan el `503`; solo se cachea audio completo y nunca se reconsulta al LLM.

## Verificación

`UV_CACHE_DIR=/tmp/uml-uv-cache uv run pytest -q`: 82 pruebas aprobadas; 71 advertencias
de deprecación de terceros (`google-genai` y `mongomock` en Python 3.14).
Las regresiones cubren reintento, descarte de audio parcial, límite de intentos,
cancelación, permisos y diagnóstico sin datos privados.

Además de las pruebas con dobles, una síntesis real posterior al cambio con la voz Marcelo
generó 26784 bytes y una marca de frase. Esto comprueba disponibilidad puntual; no garantiza
que el proveedor remoto esté disponible continuamente.

La ruta `speech-playback` también se verificó en una instancia FastAPI aislada con MongoDB
y Edge TTS reales: HTTP 200, 598464 bytes, 16 marcas y 3,43 segundos para la respuesta
reportada. La revisión del historial permaneció intacta; únicamente se almacenó su audio
en la caché. La petición al proceso ya abierto en el puerto 8000 agotó 40 segundos, por lo
que esa instancia requiere una comprobación tras reiniciarla; no se detuvo automáticamente.

## Recuperación desde la interfaz

Si el servicio no utiliza recarga automática, reiniciar FastAPI para cargar el adaptador.
Usar «Escuchar respuesta» sobre el texto existente. No volver a enviar el mensaje solo
para recuperar el audio. Si reaparece un `503`, revisar el registro `Speech unavailable`
del microservicio: ahora distingue la etapa y el tipo de fallo sin exponer datos privados.
