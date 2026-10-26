"""Persistencia MongoDB con CAS para ordenar cada sala sin transacciones distribuidas."""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable
import logging
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import BSON


class ConversationRepository:
    """Separa el historial compacto de snapshots y binarios de cada solicitud."""

    def __init__(self, database, on_changed: Callable[[str, int], None] | None = None):
        """Recibe una base Mongo compatible para producción o pruebas de contrato."""
        self.rooms = database.conversation_sessions
        self.requests = database.ai_requests
        self.rooms.create_index("messages.status")
        self.on_changed = on_changed

    def read(self, room_id: str) -> dict:
        """Obtiene una sala completa o un estado inicial aún no persistido."""
        return self.rooms.find_one({'_id': room_id}) or {
            '_id': room_id, 'revision': 0, 'nextSequence': 1, 'messages': [],
        }

    def mutate(self, room_id: str, operation: Callable[[dict], object]):
        """Aplica un cambio por compare-and-swap y reintenta si otro proceso ganó la carrera."""
        for _ in range(30):
            previous = self.read(room_id)
            current = deepcopy(previous)
            result = operation(current)
            if current == previous:
                return result
            current['revision'] += 1
            if len(BSON.encode(current)) > 12_000_000:
                raise ValueError('Conversation capacity reached; export history before starting another diagram')
            try:
                if previous['revision'] == 0:
                    self.rooms.insert_one(current)
                    self._notify(room_id, current['revision'])
                    return result
                updated = self.rooms.replace_one({'_id': room_id, 'revision': previous['revision']}, current)
                if updated.modified_count:
                    self._notify(room_id, current['revision'])
                    return result
            except DuplicateKeyError:
                continue
        raise RuntimeError('Conversation is busy; retry with the same message identifier')

    def _notify(self, room_id: str, revision: int) -> None:
        """Aísla los avisos efímeros: un fallo de transporte no revierte una escritura confirmada."""
        if self.on_changed:
            try:
                self.on_changed(room_id, revision)
            except Exception:
                logging.getLogger(__name__).warning('Conversation notification unavailable')

    def store_request(self, request_id: str, room_id: str, payload: dict) -> None:
        """Conserva la primera versión del payload para reintentos idempotentes."""
        self.requests.update_one({'_id': request_id}, {'$setOnInsert': {
            'roomId': room_id, 'payload': payload, 'createdAt': datetime.now(timezone.utc),
        }}, upsert=True)

    def request(self, request_id: str) -> dict:
        """Recupera el snapshot privado y adjunto asociados a un mensaje."""
        result = self.requests.find_one({'_id': request_id})
        if not result:
            raise LookupError('Request not found')
        return result['payload']

    def pending_rooms(self) -> list[str]:
        """Lista salas con trabajo pendiente o susceptible de recuperación tras una caída."""
        return [row['_id'] for row in self.rooms.find(
            {'messages.status': {'$in': ['queued', 'processing']}}, {'_id': 1},
        ).limit(100)]


def create_repository(uri: str, database: str) -> tuple[MongoClient, ConversationRepository]:
    """Crea el cliente con timeout acotado y verifica conectividad antes de servir tráfico."""
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    return client, ConversationRepository(client[database])
