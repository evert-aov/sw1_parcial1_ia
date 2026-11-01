"""Casos de uso del historial compartido y ejecución serializada por sala."""
import json
import re
import time
import logging
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL
from app.models import Submission, Attachment, SuggestionRequest, SuggestionAccept
from app.context import build_context
from app.memory import ConversationMemory, eligible_messages


def timestamp() -> str:
    """Devuelve una fecha UTC interoperable con clientes JavaScript."""
    return datetime.now(timezone.utc).isoformat()


def mentions_kanon_tag(text: str) -> bool:
    """Detecta mención explícita @Kanon en mensajes escritos del chat grupal."""
    return bool(re.search(r'(?<!\w)@kanon\b', text or '', re.IGNORECASE))


def mentions_kanon_voice(text: str) -> bool:
    """Detecta invocación hablada a Kanon tolerando variantes fonéticas frecuentes del STT."""
    return bool(re.search(r'\b(?:@?kanon|canon|kannon)\b', text or '', re.IGNORECASE))


class ConversationService:
    """Orquesta mensajes, contexto y propuestas sin conocer HTTP ni Socket.IO."""

    def __init__(self, repository, providers, settings, memory: ConversationMemory | None = None):
        """Inyecta persistencia e inferencia para aislar pruebas y proveedores."""
        self.repository = repository
        self.providers = providers
        self.settings = settings
        self.memory = memory

    def _resolve_initial_ask(self, submission: Submission) -> bool:
        """Determina si el turno invoca inicialmente a Kanon según ámbito, canal y menciones."""
        if submission.scope == 'personal':
            return True
        if not submission.enforceMention:
            return submission.askAssistant
        if submission.attachment and submission.attachment.kind == 'audio':
            return mentions_kanon_tag(submission.content)
        if submission.channel == 'call':
            return mentions_kanon_voice(submission.content)
        return mentions_kanon_tag(submission.content)

    def submit(self, room_id: str, submission: Submission) -> dict:
        """Acepta una intervención exactamente una vez por autor e identificador de cliente."""
        message_id = str(uuid5(NAMESPACE_URL, f'{room_id}:{submission.sender.userId}:{submission.clientMessageId}'))
        payload = submission.model_dump()
        self.repository.store_request(message_id, room_id, payload)
        original = self.repository.request(message_id)
        if any(original.get(k) != payload.get(k) for k in ['content', 'provider', 'attachment', 'askAssistant', 'scope', 'channel', 'enforceMention', 'language']):
            raise ValueError('Message identifier already used for a different payload')
        effective_ask = self._resolve_initial_ask(submission)
        def append(room):
            """Inserta el mensaje y su orden dentro de una única modificación atómica."""
            existing = next((m for m in room['messages'] if m['id'] == message_id), None)
            if existing:
                return existing
            if len(room['messages']) + sum(m['status'] in ('queued', 'processing') for m in room['messages']) >= 998:
                raise ValueError('Conversation message capacity reached')
            if sum(m['status'] in ('queued', 'processing') for m in room['messages']) >= 20:
                raise ValueError('Conversation queue is full')
            attachment = submission.attachment
            message = {'id': message_id, 'sequence': room['nextSequence'], 'sender': submission.sender.model_dump(),
                       'content': submission.content.strip(), 'createdAt': timestamp(),
                       'source': attachment.kind if attachment else 'text',
                       'scope': submission.scope, 'channel': submission.channel,
                       'status': 'queued' if effective_ask or attachment else 'completed',
                       'askAssistant': effective_ask, 'provider': 'vertex' if attachment else submission.provider,
                       'attachment': {'mimeType': attachment.mimeType, 'name': attachment.name} if attachment else None}
            room['nextSequence'] += 1
            room['messages'].append(message)
            return message
        return self.repository.mutate(room_id, append)

    def history(self, room_id: str, before: int | None = None, limit: int = 100) -> dict:
        """Entrega una página de historial confirmado sin snapshots privados ni tokens de lease."""
        room = self.repository.read(room_id)
        messages = [m for m in room['messages'] if before is None or m['sequence'] < before]
        page = messages[-limit:]
        return {'schemaVersion': 1, 'sessionId': room_id, 'revision': room['revision'], 'messages': [
            {k: v for k, v in m.items() if k not in ('leaseToken', 'leaseUntil')} for m in page
        ], 'hasMore': len(messages) > limit}

    def claim(self, room_id: str) -> dict | None:
        """Reserva un solo turno por sala con lease recuperable tras reinicio del proceso."""
        def reserve(room):
            """Evita procesar dos turnos de la misma conversación simultáneamente."""
            active = next((m for m in room['messages'] if m['status'] == 'processing'), None)
            if active and active.get('leaseUntil', 0) > time.time():
                return None
            message = active or next((m for m in room['messages'] if m['status'] == 'queued'), None)
            if not message:
                return None
            message.update(status='processing', leaseToken=str(uuid4()), leaseUntil=time.time() + 300)
            return dict(message)
        return self.repository.mutate(room_id, reserve)

    def process(self, room_id: str) -> None:
        """Ejecuta un turno y publica resultado o error durables, sin perder el mensaje humano."""
        message = self.claim(room_id)
        if not message:
            return
        reply = None
        metadata = {}
        description = ''
        failure = None
        invoke_ai = bool(message.get('askAssistant'))
        try:
            request = self.repository.request(message['id'])
            attachment = Attachment.model_validate(request['attachment']) if request.get('attachment') else None
            known_invocation = request.get('scope') == 'personal' or (
                mentions_kanon_tag(request.get('content', '')) if request.get('enforceMention')
                else bool(request.get('askAssistant')))
            if attachment and not known_invocation:
                description = self.providers.describe_attachment(
                    attachment,
                    diagram=request.get('diagram'), language=request.get('language'),
                )
                if not description:
                    raise ValueError('Empty attachment transcription')
            if request.get('scope') == 'personal':
                invoke_ai = True
            elif request.get('enforceMention'):
                if (request.get('attachment') or {}).get('kind') == 'audio':
                    invoke_ai = mentions_kanon_tag(request.get('content', '')) or mentions_kanon_voice(description)
                elif request.get('channel') == 'call':
                    invoke_ai = mentions_kanon_voice(request.get('content', ''))
                else:
                    invoke_ai = mentions_kanon_tag(request.get('content', ''))
            prompt_message = {**message, 'content': request['content'] + ('\n' + description if description else '')}
            if invoke_ai:
                snapshot = self.repository.read(room_id)
                # Validar primero la parte obligatoria, evitando inferencias de resumen innecesarias.
                build_context([], prompt_message, request['diagram'], self.settings.chat_context_chars)
                memory = None
                try:
                    if self.memory is not None:
                        memory = self.memory.prepare(room_id, eligible_messages(snapshot['messages'], prompt_message),
                                                     'vertex' if attachment else message['provider'])
                except Exception:
                    # El historial sigue siendo utilizable si la caché derivada no está disponible.
                    logging.getLogger(__name__).warning('Conversation memory unavailable; using history retrieval')
                context = build_context(snapshot['messages'], prompt_message,
                                        request['diagram'], self.settings.chat_context_chars, memory,
                                        self.settings.chat_recent_messages, self.settings.chat_retrieval_messages)
                if attachment:
                    result, metadata = self.providers.generate_multimodal(context, attachment)
                    description, reply = description or result.description, result.reply
                else:
                    reply, metadata = self.providers.generate(message['provider'], context)
                metadata.update(contextRevision=snapshot['revision'], diagramRevision=request['diagramRevision'])
        except Exception:
            # Los errores del proveedor pueden contener URLs o claves; nunca publicarlos al cliente.
            failure = 'generation_failed'
        def finish(room):
            """Publica solo si el worker conserva su lease; descarta resultados duplicados o tardíos."""
            current = next(m for m in room['messages'] if m['id'] == message['id'])
            if current.get('leaseToken') != message['leaseToken'] or current['status'] != 'processing':
                return
            current.update(status='failed' if failure else 'completed', askAssistant=invoke_ai)
            current.pop('leaseToken', None)
            current.pop('leaseUntil', None)
            if description:
                current['transcription'] = description
                current['mediaProvider'] = 'vertex'
                current['content'] = request['content'] + ('\n' if request['content'] else '') + description
            if failure:
                current['errorCode'] = failure
            if reply:
                answer = {'id': str(uuid5(NAMESPACE_URL, message['id'] + ':response')),
                          'sequence': room['nextSequence'], 'sender': {'kind': 'assistant', 'displayName': 'Kanon'},
                          'content': reply.content, 'intent': reply.intent, 'status': 'completed',
                          'source': 'assistant', 'scope': message.get('scope', 'group'),
                          'createdAt': timestamp(), 'replyTo': message['id'],
                          'replyToUser': {
                              'userId': message['sender'].get('userId'),
                              'displayName': message['sender'].get('displayName', ''),
                          },
                          'replyToSource': message.get('source', 'text'),
                          'replyToChannel': message.get('channel', 'chat'),
                          **metadata}
                if reply.proposedAst is not None:
                    answer['proposal'] = {'id': answer['id'], 'status': 'pending',
                                          'baseRevision': request['diagramRevision'],
                                          'ast': reply.proposedAst, 'summary': reply.changesSummary}
                room['messages'].append(answer)
                room['nextSequence'] += 1
        try:
            self.repository.mutate(room_id, finish)
        except ValueError:
            # Una respuesta grande no debe dejar el turno en procesamiento indefinido.
            reply = None
            description = ''
            failure = 'conversation_capacity_reached'
            self.repository.mutate(room_id, finish)

    def retry(self, room_id: str, message_id: str, user_id: str) -> dict:
        """Reencola un turno fallido solo para su autor, conservando el mensaje original."""
        def requeue(room):
            """Cambia el estado sin duplicar la entrada ni alterar su secuencia."""
            message = next((m for m in room['messages'] if m['id'] == message_id), None)
            if not message or message['sender'].get('userId') != user_id:
                raise LookupError('Message not found')
            if message['status'] == 'failed':
                message['status'] = 'queued'
                message.pop('errorCode', None)
            return message
        return self.repository.mutate(room_id, requeue)

    def proposal(self, room_id: str, proposal_id: str, applied: bool = False) -> dict:
        """Consulta una propuesta o registra su aplicación confirmada por el backend autoritativo."""
        def update(room):
            """Mantiene la transición idempotente de pendiente a aplicada."""
            proposal = next((m['proposal'] for m in room['messages'] if m.get('proposal', {}).get('id') == proposal_id), None)
            if not proposal:
                raise LookupError('Proposal not found')
            if applied:
                proposal['status'] = 'applied'
            return proposal
        return self.repository.mutate(room_id, update)

    def suggest(self, request: SuggestionRequest) -> dict:
        """Evalúa el grafo y devuelve una sugerencia proactiva sin escribir en el chat."""
        nodes = [n for n in (request.diagram.get('nodes') or [])
                 if isinstance(n, dict) and not n.get('isAnchor') and (n.get('nodeType') or 'class') != 'note']
        if not nodes and not request.findings:
            return {'shouldSuggest': False, 'title': '', 'summary': '', 'starterPrompt': ''}
        payload = json.dumps({
            'language': request.language,
            'recipient': request.sender.displayName,
            'hasMultipleCollaborators': request.hasMultipleCollaborators,
            'findings': [f.model_dump() for f in request.findings],
            'diagram': {
                'type': request.diagram.get('type', 'class'),
                'nodes': nodes[:80],
                'connections': (request.diagram.get('connections') or [])[:160],
            },
        }, ensure_ascii=False)
        if hasattr(self.providers, 'suggest'):
            try:
                suggestion = self.providers.suggest(request.provider, payload)
                if not suggestion.shouldSuggest or not suggestion.summary.strip():
                    return {'shouldSuggest': False, 'title': '', 'summary': '', 'starterPrompt': ''}
                return suggestion.model_dump()
            except Exception:
                logging.getLogger(__name__).warning('Proactive suggestion inference unavailable; using local fallback')
        if request.findings:
            top = request.findings[0]
            title = 'Sugerencia de modelado de Kanon' if request.language == 'es' else 'Kanon Modeling Tip'
            prompt = (f'Kanon, ayúdame a resolver esto en mi diagrama: {top.message}'
                      if request.language == 'es' else
                      f'Kanon, help me address this in my diagram: {top.message}')
            return {'shouldSuggest': True, 'title': title, 'summary': top.message, 'starterPrompt': prompt}
        return {'shouldSuggest': False, 'title': '', 'summary': '', 'starterPrompt': ''}

    def accept_suggestion(self, room_id: str, accept: SuggestionAccept) -> dict:
        """Inserta idempotentemente una sugerencia aceptada como mensaje de Kanon en el chat personal o grupal."""
        message_id = str(uuid5(NAMESPACE_URL, f'{room_id}:suggestion:{accept.sender.userId}:{accept.clientSuggestionId}'))
        def append(room):
            """Añade el mensaje de mentoría sin duplicarlo ante reintentos de red."""
            existing = next((m for m in room['messages'] if m['id'] == message_id), None)
            if existing:
                return existing
            if len(room['messages']) >= 998:
                raise ValueError('Conversation message capacity reached')
            content = f"**{accept.sender.displayName}**, {accept.summary.strip()}"
            scope = accept.scope if accept.scope in ('group', 'personal') else room.get('scope', 'personal')
            message = {
                'id': message_id,
                'sequence': room['nextSequence'],
                'sender': {'kind': 'assistant', 'displayName': 'Kanon'},
                'content': content,
                'intent': 'mentorship',
                'status': 'completed',
                'source': 'assistant',
                'scope': scope,
                'createdAt': timestamp(),
                'suggestionTitle': accept.title.strip(),
                'replyToUser': {'userId': accept.sender.userId, 'displayName': accept.sender.displayName},
            }
            room['nextSequence'] += 1
            room['messages'].append(message)
            return message
        return self.repository.mutate(room_id, append)
