"""Verifica la normalización acotada de cardinalidades antes de publicar propuestas."""
from copy import deepcopy
import pytest
from pydantic import ValidationError
from app.models import ModelReply


def graph(**connection):
    """Crea un grafo sintético con dos nodos para verificar identidad y cardinalidades."""
    return {'nodes': [{'id': 'source'}, {'id': 'target'}], 'connections': [
        {'id': 'edge', 'sourceNodeId': 'source', 'targetNodeId': 'target', **connection},
    ]}


def test_normalizes_aliases_without_mutation():
    """Preserva valores, identificadores y el objeto original al normalizar una propuesta."""
    ast = graph(sourceCardinality='0..*', targetCardinality='0..1')
    original = deepcopy(ast)
    reply = ModelReply(content='Proposal', proposedAst=ast)
    connection = reply.proposedAst['connections'][0]
    assert connection['sourceMultiplicity'] == '0..*'
    assert connection['targetMultiplicity'] == '0..1'
    assert 'sourceCardinality' not in connection
    assert 'targetCardinality' not in connection
    assert connection['id'] == 'edge'
    assert ast == original


def test_preserves_canonical_fields_and_equivalent_aliases():
    """Un alias redundante idéntico se retira sin cambiar el significado del grafo."""
    reply = ModelReply(content='Proposal', proposedAst=graph(sourceMultiplicity='1', sourceCardinality='1', targetMultiplicity='*'))
    connection = reply.proposedAst['connections'][0]
    assert connection['sourceMultiplicity'] == '1'
    assert connection['targetMultiplicity'] == '*'
    assert 'sourceCardinality' not in connection


@pytest.mark.parametrize('connection', [
    {'sourceCardinality': '1', 'sourceMultiplicity': '*'},
    {'targetCardinality': '*', 'targetMultiplicity': '1'},
    {'sourceCardinality': 1},
    {'targetCardinality': None},
])
def test_rejects_ambiguous_or_invalid_cardinality(connection):
    """No adivina cardinalidades cuando la respuesta es ambigua o contiene tipos incorrectos."""
    with pytest.raises(ValidationError):
        ModelReply(content='Proposal', proposedAst=graph(**connection))


def test_does_not_silently_drop_unknown_fields():
    """La adaptación no oculta campos desconocidos al validador autoritativo de NestJS."""
    reply = ModelReply(content='Proposal', proposedAst=graph(unsupported='value'))
    assert reply.proposedAst['connections'][0]['unsupported'] == 'value'
