"""Contratos validados del chat, sus adjuntos y respuestas del modelo."""
import base64
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.proposal_contract import normalize_proposal_ast

Provider = Literal['ollama', 'vertex']
ChatScope = Literal['group', 'personal']
ChatChannel = Literal['chat', 'call']


class Contract(BaseModel):
    """Rechaza campos inesperados en los contratos públicos."""
    model_config = ConfigDict(extra='forbid')


class Actor(Contract):
    """Identidad autenticada enviada exclusivamente por NestJS."""
    userId: str
    displayName: str
    role: str


class SpeechRequest(Contract):
    """Fija la identidad vocal de Kanon; el texto se obtiene del mensaje persistido."""
    voice: Literal['marcelo'] = 'marcelo'


class Attachment(Contract):
    """Adjunto acotado para voz grabada o imagen; no contiene URLs externas."""
    kind: Literal['image', 'audio']
    mimeType: str
    data: str = Field(max_length=7_000_000)
    name: str = Field(default='attachment', max_length=160)

    @model_validator(mode='after')
    def validate_media(self):
        """Valida MIME y Base64 antes de guardar o enviar el contenido a un proveedor."""
        supported = {'image': {'image/png', 'image/jpeg', 'image/webp'},
                     'audio': {'audio/webm', 'audio/ogg', 'audio/wav', 'audio/mp4', 'audio/mpeg'}}
        self.mimeType = self.mimeType.split(';')[0]
        if self.mimeType not in supported[self.kind]:
            raise ValueError('Unsupported media type')
        try:
            raw = base64.b64decode(self.data, validate=True)
        except ValueError as exc:
            raise ValueError('Invalid base64 attachment') from exc
        if not raw or len(raw) > 5_000_000:
            raise ValueError('Attachment must contain 1 to 5000000 bytes')
        return self


class Submission(Contract):
    """Mensaje autenticado y snapshot autorizado del diagrama al enviar el turno."""
    clientMessageId: str = Field(min_length=8, max_length=80)
    sender: Actor
    content: str = Field(default='', max_length=12000)
    provider: Provider = 'ollama'
    language: Literal['es', 'en'] = 'es'
    askAssistant: bool = True
    scope: ChatScope = 'group'
    channel: ChatChannel = 'chat'
    enforceMention: bool = False
    attachment: Attachment | None = None
    diagram: dict = Field(default_factory=dict)
    diagramRevision: str

    @model_validator(mode='after')
    def validate_content(self):
        """Impide mensajes vacíos y snapshots que excedan el contexto aceptado."""
        import json
        if not self.content.strip() and not self.attachment:
            raise ValueError('A message or attachment is required')
        if len(json.dumps(self.diagram)) > 300000:
            raise ValueError('Diagram context is too large')
        return self


class SuggestionFinding(Contract):
    """Señal estructural detectada previamente sobre el diagrama para orientar a Kanon."""
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=600)


class SuggestionRequest(Contract):
    """Solicitud efímera de sugerencia autónoma sin escribir en el historial del chat."""
    sender: Actor
    provider: Provider = 'ollama'
    language: Literal['es', 'en'] = 'es'
    hasMultipleCollaborators: bool = False
    diagram: dict = Field(default_factory=dict)
    findings: list[SuggestionFinding] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def validate_diagram_budget(self):
        """Impide evaluar snapshots excesivos en tareas proactivas de segundo plano."""
        import json
        if len(json.dumps(self.diagram)) > 300000:
            raise ValueError('Diagram context is too large')
        return self


class SuggestionReply(Contract):
    """Consejo breve generado por Kanon para mostrar junto al icono del manual."""
    shouldSuggest: bool = True
    title: str = Field(default='', max_length=140)
    summary: str = Field(default='', max_length=600)
    starterPrompt: str = Field(default='', max_length=600)


class SuggestionAccept(Contract):
    """Siembra idempotente de una sugerencia aceptada dentro del chat personal o grupal del usuario."""
    clientSuggestionId: str = Field(min_length=8, max_length=80)
    sender: Actor
    title: str = Field(min_length=1, max_length=140)
    summary: str = Field(min_length=1, max_length=4000)
    language: str | None = Field(default=None, max_length=10)
    scope: ChatScope = 'personal'


class ModelReply(Contract):
    """Salida estructurada del mentor/modelador, siempre pendiente de aprobación humana."""
    content: str = Field(min_length=1, max_length=32000)
    intent: Literal['mentorship', 'modeling', 'hybrid'] = 'mentorship'
    changesSummary: str = Field(default='', max_length=4000)
    proposedAst: dict | None = None

    @field_validator('proposedAst', mode='before')
    @classmethod
    def normalize_proposal(cls, value: object) -> object:
        """Publica cardinalidades con nombres canónicos sin mutar el JSON original del proveedor."""
        return normalize_proposal_ast(value)

    @field_validator('changesSummary', mode='before')
    @classmethod
    def normalize_changes_summary(cls, value: object) -> object:
        """Convierte un resumen nulo del modelo en texto vacío sin admitir otros tipos inválidos."""
        return '' if value is None else value

    @model_validator(mode='after')
    def validate_proposal(self):
        """Verifica la estructura mínima y referencias del AST propuesto."""
        if self.proposedAst is not None:
            import json
            ast = self.proposedAst
            if len(json.dumps(ast)) > 300000:
                raise ValueError('Proposal exceeds document size limit')
            if not isinstance(ast.get('nodes'), list) or not isinstance(ast.get('connections'), list):
                raise ValueError('A proposal requires nodes and connections')
            if len(ast['nodes']) > 500 or len(ast['connections']) > 2000:
                raise ValueError('Proposal exceeds graph limits')
            ids = [n.get('id') for n in ast['nodes'] if isinstance(n, dict)]
            if len(ids) != len(ast['nodes']) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
                raise ValueError('Proposal node identifiers must be unique')
            for connection in ast['connections']:
                if not isinstance(connection, dict) or connection.get('sourceNodeId') not in ids or connection.get('targetNodeId') not in ids:
                    raise ValueError('Proposal contains a dangling connection')
        return self


class MultimodalReply(Contract):
    """Transcripción o descripción y respuesta producidas juntas a partir del adjunto original."""
    description: str = Field(min_length=1, max_length=16000)
    reply: ModelReply
