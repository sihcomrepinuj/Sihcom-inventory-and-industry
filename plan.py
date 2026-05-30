"""Pure-logic orchestration for the action-first build plan.

No Flask / ESI / DB imports — takes resolved trees + plain index dicts and
returns classified buckets. Tested like hauling.py.
"""
from dataclasses import dataclass, field


@dataclass
class Target:
    """One build-list target plus its already-resolved input tree."""
    type_id: int
    name: str
    blueprint_type_id: int | None
    needed: int                 # units of the product wanted (runs * qty_per_run)
    children: list  # list[MaterialNode] — the product's direct inputs


@dataclass
class ReqNode:
    type_id: int
    name: str
    total_needed: int = 0
    blueprint_type_id: int | None = None
    activity_id: int | None = None
    is_terminal: bool = True
    direct_inputs: set = field(default_factory=set)


def _accumulate(graph, type_id, name, qty, blueprint_type_id,
                activity_id, is_terminal, direct_inputs):
    node = graph.get(type_id)
    if node is None:
        node = ReqNode(type_id=type_id, name=name)
        graph[type_id] = node
    node.total_needed += qty
    # Metadata (name, blueprint, activity, terminal) is identical for a given type_id wherever it appears, so last-write-wins is equivalent to first-write.
    node.name = name
    node.blueprint_type_id = blueprint_type_id
    node.activity_id = activity_id
    node.is_terminal = is_terminal
    node.direct_inputs |= set(direct_inputs)


def _walk(graph, mat_node):
    direct_inputs = [c.type_id for c in mat_node.children]
    _accumulate(
        graph, mat_node.type_id, mat_node.name, mat_node.quantity_needed,
        mat_node.blueprint_type_id, mat_node.activity_id,
        mat_node.is_terminal, direct_inputs,
    )
    for child in mat_node.children:
        _walk(graph, child)


def merge_trees(targets) -> dict[int, "ReqNode"]:
    graph = {}
    for t in targets:
        _accumulate(
            graph, t.type_id, t.name, t.needed,
            t.blueprint_type_id, activity_id=1, is_terminal=False,
            direct_inputs=[c.type_id for c in t.children],
        )
        for child in t.children:
            _walk(graph, child)
    return graph
