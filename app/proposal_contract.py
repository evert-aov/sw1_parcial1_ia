"""Adaptación acotada de alias del modelo al contrato canónico de propuestas UML."""
from copy import deepcopy
import re


def _normalize_methods(nodes: list) -> None:
    """Normaliza operaciones separando el identificador limpio de los parámetros y el retorno."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        methods = node.get('methods')
        if not isinstance(methods, list):
            continue
        for m in methods:
            if not isinstance(m, dict):
                continue
            name = m.get('name')
            if isinstance(name, str):
                trimmed_name = name.strip()
                match = re.match(r'^([a-zA-Z0-9_]+)\s*\((.*?)\)(?:\s*:\s*(.+))?$', trimmed_name)
                if match:
                    m['name'] = match.group(1).strip()
                    if not m.get('parameters') and match.group(2) is not None:
                        m['parameters'] = match.group(2).strip()
                    if match.group(3) and (not m.get('returnType') or m.get('returnType') == 'void'):
                        m['returnType'] = match.group(3).strip()
            if 'parameters' not in m or m['parameters'] is None:
                m['parameters'] = ''
            elif isinstance(m['parameters'], str):
                p = m['parameters'].strip()
                if p.startswith('(') and p.endswith(')'):
                    m['parameters'] = p[1:-1].strip()
            if 'returnType' not in m or m['returnType'] is None or not str(m['returnType']).strip():
                m['returnType'] = 'void'


def _expand_association_classes(nodes: list, connections: list) -> tuple[list, list]:
    """Expande conexiones directas de tipo association_class entre dos clases regulares
    en la estructura canónica de 4 elementos (ancla, clase intermedia, enlace principal y enlace punteado)."""
    node_by_id = {n.get('id'): n for n in nodes if isinstance(n, dict) and n.get('id')}
    new_nodes = list(nodes)
    new_conns = []

    for conn in connections:
        if not isinstance(conn, dict):
            new_conns.append(conn)
            continue

        c_type = conn.get('type')
        s_id = conn.get('sourceNodeId')
        t_id = conn.get('targetNodeId')
        s_node = node_by_id.get(s_id)
        t_node = node_by_id.get(t_id)

        # Si es association_class o una relación muchos-a-muchos (* a * / N:M) directa sin clase intermedia
        sm = str(conn.get('sourceMultiplicity') or '').strip().lower()
        tm = str(conn.get('targetMultiplicity') or '').strip().lower()
        is_many_to_many = ('*' in sm or 'n' in sm) and ('*' in tm or 'n' in tm or 'm' in tm)
        has_anchor = bool(conn.get('assocAnchorNodeId'))
        has_assoc_node = any(
            isinstance(n, dict) and n.get('assocMainConnId') == conn.get('id')
            for n in nodes
        )

        should_expand = (
            s_node and t_node and
            not s_node.get('isAnchor') and not t_node.get('isAnchor') and
            s_id != t_id and
            (
                c_type == 'association_class' or
                (is_many_to_many and not has_anchor and not has_assoc_node)
            )
        )

        if should_expand:
            cid = conn.get('id', 'assoc')
            anchor_id = f"anchor_{cid}"
            assoc_node_id = f"node_{cid}_assoc"

            s_x = s_node.get('positionX', 100)
            s_y = s_node.get('positionY', 100)
            t_x = t_node.get('positionX', 300)
            t_y = t_node.get('positionY', 100)
            mid_x = int((s_x + t_x) / 2) if isinstance(s_x, (int, float)) and isinstance(t_x, (int, float)) else 200
            mid_y = int((s_y + t_y) / 2) if isinstance(s_y, (int, float)) and isinstance(t_y, (int, float)) else 150

            anchor_node = {
                'id': anchor_id,
                'name': '',
                'nodeType': 'class',
                'positionX': mid_x,
                'positionY': mid_y,
                'width': 0,
                'height': 0,
                'isAnchor': True,
                'attributes': [],
                'methods': [],
            }

            s_name = str(s_node.get('name', 'Source')).strip() or 'Source'
            t_name = str(t_node.get('name', 'Target')).strip() or 'Target'
            assoc_name = conn.get('name') or f"{s_name}_{t_name}"

            s_fk = s_name[:1].lower() + s_name[1:] + 'Id'
            t_fk = t_name[:1].lower() + t_name[1:] + 'Id'
            if s_fk.lower() == t_fk.lower():
                s_fk = f"{s_name.lower()}Id"
                t_fk = f"{t_name.lower()}Id"

            assoc_node = {
                'id': assoc_node_id,
                'name': assoc_name,
                'nodeType': 'class',
                'positionX': mid_x - 110,
                'positionY': mid_y + 120,
                'width': 220,
                'isAnchor': False,
                'attributes': [
                    {'name': s_fk, 'type': 'UUID'},
                    {'name': t_fk, 'type': 'UUID'},
                ],
                'methods': [],
                'assocMainConnId': cid,
            }

            main_conn = {
                **conn,
                'type': 'association',
                'sourceMultiplicity': conn.get('sourceMultiplicity') or '*',
                'targetMultiplicity': conn.get('targetMultiplicity') or '*',
                'assocAnchorNodeId': anchor_id,
            }

            dashed_conn = {
                'id': f"{cid}_dashed",
                'sourceNodeId': anchor_id,
                'targetNodeId': assoc_node_id,
                'sourceId': anchor_id,
                'targetId': f"{assoc_node_id}_top",
                'type': 'association_class',
                'lineStyle': 'straight',
                'name': '«link»',
            }

            node_by_id[anchor_id] = anchor_node
            node_by_id[assoc_node_id] = assoc_node
            new_nodes.extend([anchor_node, assoc_node])
            new_conns.extend([main_conn, dashed_conn])
        else:
            new_conns.append(conn)

    return new_nodes, new_conns


def normalize_proposal_ast(value: object) -> object:
    """Conserva el grafo, convierte alias de cardinalidad, normaliza firmas de métodos
    y expande clases de asociación directas a su estructura canónica."""
    if not isinstance(value, dict):
        return value
    ast = deepcopy(value)
    nodes = ast.get('nodes')
    if isinstance(nodes, list):
        _normalize_methods(nodes)

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

    if isinstance(nodes, list):
        expanded_nodes, expanded_conns = _expand_association_classes(nodes, connections)
        ast['nodes'] = expanded_nodes
        ast['connections'] = expanded_conns

    return ast
