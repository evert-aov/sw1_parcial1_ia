---
name: sw1-ai-service-architect
description: >-
  Diseñar, implementar, depurar o revisar el microservicio FastAPI de conversación
  e IA de UML Collaborative Studio: chat grupal, MongoDB, contexto, turnos recuperables,
  Ollama Qwen2.5-Coder 14B, Vertex/Gemini, imágenes, clips de voz y propuestas UML.
---

# Arquitectura del servicio de conversación e IA

Aplicar esta skill al workspace `ia/` y a las fronteras que afecte la tarea. Las instrucciones
explícitas del usuario prevalecen. No autoriza cambios ajenos, despliegues ni commits.

## Referencias progresivas

La raíz de `ia/` está tres niveles arriba de esta carpeta.

| Necesidad | Referencia |
| --- | --- |
| Arranque y entorno | [Configuración](../../../guia-desarrollador/00-ARQUITECTURA-Y-CONFIGURACION.md), [pyproject.toml](../../../pyproject.toml), [uv.lock](../../../uv.lock). |
| Rutas y límites | [Contratos](../../../guia-desarrollador/01-API-INTERNA-Y-CONTRATOS.md). |
| Orden de mensajes y prompt | [Contexto y turnos](../../../guia-desarrollador/02-CONVERSACIONES-CONTEXTO-Y-TURNOS.md). |
| Idempotencia y concurrencia | [MongoDB y recuperación](../../../guia-desarrollador/03-PERSISTENCIA-MONGODB-Y-RECUPERACION.md). |
| Modelos y medios | [Proveedores](../../../guia-desarrollador/04-PROVEEDORES-Y-ENTRADAS-MULTIMODALES.md). |
| Aplicación del AST | [Propuestas](../../../guia-desarrollador/05-PROPUESTAS-UML-E-INTEGRACION.md), [guía NestJS](../../../../backend/guia-desarrollador/README.md). |
| Pruebas y estructura | [Operación](../../../guia-desarrollador/06-PRUEBAS-Y-OPERACION.md), [mapa](../../../guia-desarrollador/07-MAPA-DE-DIRECTORIOS-Y-FLUJOS.md). |

Leer los capítulos pertinentes y contrastar el comportamiento con código y pruebas.
Mantener código y nombres en inglés; docstrings de métodos y documentación en español.

## Propiedad y fronteras

- API y lifespan en `main.py`; casos de uso en `conversations.py`; contratos, contexto, repositorio y proveedores separados.
- `Settings` lee exclusivamente `ia/.env` y variables del proceso. No leer archivos del backend ni reutilizar `JWT_SECRET`.
- NestJS valida JWT y acceso, construye `sender` y snapshot. FastAPI autentica al servicio con `x-ai-service-token`; no expone rutas internas al navegador.
- MongoDB guarda conversación y solicitudes. PostgreSQL y aplicación del AST pertenecen a NestJS.
- El alcance actual es chat con texto, imagen y clips; llamadas, WebRTC y edge-tts están pendientes.

## Invariantes de conversación

Preservar `clientMessageId`, IDs derivados, secuencias y solicitud original. Mutar la sala con
CAS por revisión, no con lecturas/escrituras sin condición. Conservar token y vencimiento del
lease al recuperar turnos; una ejecución antigua no puede publicar sobre un nuevo propietario.
No afirmar una sola inferencia/cobro cuando la recuperación puede repetir llamadas al proveedor.

Respetar límites de cola, documento BSON, adjuntos y contexto. El historial debe usar mensajes
anteriores al turno y sus respuestas asociadas; no incorporar intervenciones humanas posteriores
ni truncar silenciosamente el AST.
Mantener los dobles de repositorio/proveedores sustituibles para pruebas sin servicios externos.

## Proveedores y propuestas

No descargar ni ejecutar Qwen al consultar el catálogo. No hacer fallback cloud/local implícito.
Para Vertex regional pasar proyecto y ubicación junto a la credencial; conservar los caminos
Express, Gemini API y ADC. El catálogo cloud indica configuración, no prueba autorización real.

Los medios se describen/transcriben con Google aunque el proveedor de generación sea Qwen.
Validar MIME, Base64 y tamaño, sin afirmar validación de duración que no existe.
Validar `ModelReply`; normalizar `changesSummary: null` a cadena vacía y únicamente los alias
conocidos de multiplicidad. No mutar el JSON original ni borrar campos desconocidos.

Publicar propuestas pendientes con revisión base. La validación estructural Python no reemplaza
el DTO autoritativo NestJS. Confirmar `applied` por el endpoint interno después del commit del
backend; no conectar Python a PostgreSQL ni escribir automáticamente el documento.

## Verificación y entrega

Para código ejecutar `uv run pytest -q`. Cubrir límites, concurrencia, lease vencido, reintentos,
credenciales y contratos; cambios de frontera requieren suites de ambos extremos. Las pruebas
con MongoMock o clientes sustituidos no demuestran conectividad, cuota ni rendimiento real.

Para documentación validar enlaces, comandos y afirmaciones contra la implementación.
Actualizar guía y consumidores cuando cambien contratos. No incluir secretos en logs, tests o
README; informar comandos ejecutados y limitaciones sin conteos históricos de pruebas.
