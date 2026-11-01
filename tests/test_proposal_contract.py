"""Verifica la normalización acotada de cardinalidades antes de publicar propuestas."""
from copy import deepcopy
import pytest
from pydantic import ValidationError
from app.models import ModelReply
from app.proposal_contract import normalize_proposal_ast


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


def test_normalizes_method_signatures_separating_name_and_parameters():
    """Separa el nombre de la función, parámetros y tipo de retorno si el modelo emite la firma junta."""
    ast = {
        'nodes': [
            {
                'id': 'c1',
                'name': 'Producto',
                'methods': [
                    {'name': 'actualizarStock(cantidad)'},
                    {'name': 'calcularPrecio(impuesto: double): BigDecimal'},
                    {'name': 'obtenerId()', 'parameters': '', 'returnType': 'UUID'},
                ],
            },
            {'id': 'c2', 'name': 'Cliente'},
        ],
        'connections': [
            {'id': 'e1', 'sourceNodeId': 'c1', 'targetNodeId': 'c2', 'type': 'association'},
        ],
    }
    reply = ModelReply(content='Proposal', proposedAst=ast)
    methods = reply.proposedAst['nodes'][0]['methods']
    assert methods[0]['name'] == 'actualizarStock'
    assert methods[0]['parameters'] == 'cantidad'
    assert methods[0]['returnType'] == 'void'

    assert methods[1]['name'] == 'calcularPrecio'
    assert methods[1]['parameters'] == 'impuesto: double'
    assert methods[1]['returnType'] == 'BigDecimal'

    assert methods[2]['name'] == 'obtenerId'
    assert methods[2]['parameters'] == ''
    assert methods[2]['returnType'] == 'UUID'


def test_expands_standalone_association_class_connection():
    """Expande una conexión association_class entre dos clases a la estructura canónica de 4 elementos."""
    ast = {
        'nodes': [
            {'id': 'estudiante', 'name': 'Estudiante', 'positionX': 100, 'positionY': 100},
            {'id': 'curso', 'name': 'Curso', 'positionX': 500, 'positionY': 100},
        ],
        'connections': [
            {
                'id': 'conn_inscripcion',
                'sourceNodeId': 'estudiante',
                'targetNodeId': 'curso',
                'type': 'association_class',
                'name': 'Inscripcion',
                'sourceMultiplicity': '*',
                'targetMultiplicity': '*',
            },
        ],
    }
    reply = ModelReply(content='Proposal', proposedAst=ast)
    nodes = reply.proposedAst['nodes']
    conns = reply.proposedAst['connections']

    # Debe contener Estudiante, Curso, el nodo ancla y la clase de asociación
    assert len(nodes) == 4
    anchor = next(n for n in nodes if n.get('isAnchor'))
    assoc = next(n for n in nodes if n.get('id') == 'node_conn_inscripcion_assoc')
    assert anchor['id'] == 'anchor_conn_inscripcion'
    assert anchor['positionX'] == 300
    assert assoc['name'] == 'Inscripcion'
    assert any(a['name'] == 'estudianteId' for a in assoc['attributes'])
    assert any(a['name'] == 'cursoId' for a in assoc['attributes'])

    # Las conexiones deben incluir la principal y la punteada «link»
    assert len(conns) == 2
    main_c = next(c for c in conns if c['id'] == 'conn_inscripcion')
    dashed_c = next(c for c in conns if c['id'] == 'conn_inscripcion_dashed')
    assert main_c['type'] == 'association'
    assert main_c['assocAnchorNodeId'] == anchor['id']
    assert dashed_c['type'] == 'association_class'
    assert dashed_c['sourceNodeId'] == anchor['id']
    assert dashed_c['targetNodeId'] == assoc['id']


def test_direct_many_to_many_association_expands_to_association_class():
    """Expande automáticamente una relación muchos-a-muchos (* a *) en clase asociación canónica."""
    ast = {
        'nodes': [
            {'id': 'c1', 'name': 'Pedido', 'positionX': 100, 'positionY': 200},
            {'id': 'c2', 'name': 'Producto', 'positionX': 400, 'positionY': 200},
        ],
        'connections': [
            {
                'id': 'conn_order_prod',
                'sourceNodeId': 'c1',
                'targetNodeId': 'c2',
                'sourceId': 'c1_right',
                'targetId': 'c2_left',
                'type': 'association',
                'sourceMultiplicity': '*',
                'targetMultiplicity': '*',
            }
        ],
    }
    normalized = normalize_proposal_ast(ast)
    nodes = normalized['nodes']
    conns = normalized['connections']

    assert len(nodes) == 4
    anchor = next(n for n in nodes if n.get('isAnchor') is True)
    assoc = next(n for n in nodes if n.get('assocMainConnId') == 'conn_order_prod')
    assert assoc['name'] == 'Pedido_Producto'

    assert len(conns) == 2
    main_c = next(c for c in conns if c['id'] == 'conn_order_prod')
    dashed_c = next(c for c in conns if c['id'] == 'conn_order_prod_dashed')
    assert main_c['assocAnchorNodeId'] == anchor['id']
    assert dashed_c['type'] == 'association_class'
    assert dashed_c['sourceNodeId'] == anchor['id']
    assert dashed_c['targetNodeId'] == assoc['id']

