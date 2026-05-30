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


def _owned(loc_index, type_id, build_station) -> int:
    locs = loc_index.get(type_id, {})
    if build_station is not None:
        return locs.get(build_station, 0)
    return sum(locs.values())   # no station chosen -> count everywhere


def _in_job_qty(jobs, type_id) -> int:
    """Units of `type_id` currently being produced by active/ready/paused jobs."""
    total = 0
    for j in jobs:
        if j.get("product_type_id") != type_id:
            continue
        # paused jobs still hold their reserved inputs, so treat them as in-flight (not Ready)
        if j.get("status") not in ("active", "ready", "paused"):
            continue
        total += j.get("runs", 0)
    return total


def _row(node, shortfall, jobs=None):
    row = {"type_id": node.type_id, "name": node.name,
           "needed": node.total_needed, "shortfall": shortfall,
           "blueprint_type_id": node.blueprint_type_id,
           "activity_id": node.activity_id}
    if jobs is not None:
        ends = [j.get("end_date") for j in jobs
                if j.get("product_type_id") == node.type_id]
        row["end_date"] = min([e for e in ends if e], default=None)
    return row


def classify(graph, loc_index, jobs, build_station, buy_set):
    """Bucket every node into ready / in_progress / blocked / buy.

    NOTE: input availability is evaluated at the aggregate level (owning enough
    of an input to cover all its uses marks it available). Sufficient for
    'what can I start now'; per-branch reservation is a deliberate non-goal.
    """
    shortfall = {}
    has_active_job = {}
    owned_short = {}
    for tid, node in graph.items():
        owned = _owned(loc_index, tid, build_station)
        in_job = _in_job_qty(jobs, tid)
        shortfall[tid] = node.total_needed - owned - in_job
        owned_short[tid] = node.total_needed - owned
        has_active_job[tid] = in_job > 0

    ready, in_progress, blocked, buy = [], [], [], []
    for tid, node in graph.items():
        # Satisfied = already owned in full. A node still short on units stays
        # visible even when an active job covers the gap (it lands in_progress).
        if owned_short[tid] <= 0:
            continue
        buildable = node.blueprint_type_id is not None and tid not in buy_set
        if not buildable:
            buy.append(_row(node, shortfall[tid]))
            continue
        if has_active_job[tid]:
            in_progress.append(_row(node, shortfall[tid], jobs=jobs))
            continue
        missing = [
            {"type_id": i, "name": graph[i].name if i in graph else str(i),
             "shortfall": shortfall.get(i, 0)}
            for i in sorted(node.direct_inputs)
            if shortfall.get(i, 0) > 0
        ]
        if missing:
            row = _row(node, shortfall[tid])
            row["missing"] = missing
            blocked.append(row)
        else:
            ready.append(_row(node, shortfall[tid]))
    return {"ready": ready, "in_progress": in_progress,
            "blocked": blocked, "buy": buy}
