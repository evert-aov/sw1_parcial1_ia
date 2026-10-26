"""Adaptación acotada de alias del modelo al contrato canónico de propuestas UML."""
from copy import deepcopy


def normalize_proposal_ast(value: object) -> object:
    """Conserva el grafo y convierte alias de cardinalidad sin adivinar valores contradictorios."""
    if not isinstance(value, dict):
        return value
    ast = deepcopy(value)
    connections = ast.get('connections')
    if not isinstance(connections, list):
        return ast
    aliases = {'sourceCardinality': 'sourceMultiplicity', 'targetCardinality': 'targetMultiplicity'}
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        for alias, canonical in aliases.items():
            if alias not in connection:
                continue
            if not isinstance(connection[alias], str):
                raise ValueError('Connection cardinality must be a string')
            if canonical in connection and connection[canonical] != connection[alias]:
                raise ValueError('Connection multiplicity has conflicting aliases')
            connection[canonical] = connection.pop(alias)
    return ast
