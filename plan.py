"""Pure-logic orchestration for the action-first build plan.

No Flask / ESI / DB imports — takes resolved trees + plain index dicts and
returns classified buckets. Tested like hauling.py.
"""
from dataclasses import dataclass, field

import hauling


@dataclass
class Target:
    """One build-list target plus its already-resolved input tree."""
    type_id: int
    name: str
    blueprint_type_id: int | None
    needed: int                 # units of the product wanted (runs * qty_per_run)
    children: list  # list[MaterialNode] — the product's direct inputs
    build_station: int | None = None  # per-product build station (None = unset)


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


def _walk(graph, mat_node, buy_set):
    direct_inputs = [c.type_id for c in mat_node.children]
    _accumulate(
        graph, mat_node.type_id, mat_node.name, mat_node.quantity_needed,
        mat_node.blueprint_type_id, mat_node.activity_id,
        mat_node.is_terminal, direct_inputs,
    )
    # A bought component is treated as a leaf: do not pull in its sub-materials
    # (mirrors sde.flatten_material_tree's buy_set handling).
    if mat_node.type_id in buy_set:
        return
    for child in mat_node.children:
        _walk(graph, child, buy_set)


def merge_trees(targets, buy_set=None) -> dict[int, "ReqNode"]:
    buy_set = buy_set or set()
    graph = {}
    for t in targets:
        _accumulate(
            graph, t.type_id, t.name, t.needed,
            t.blueprint_type_id, activity_id=1, is_terminal=False,
            direct_inputs=[c.type_id for c in t.children],
        )
        for child in t.children:
            _walk(graph, child, buy_set)
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


def enrich_buy(buy_rows, loc_index, build_station, volumes):
    """Attach at_station / elsewhere / to_buy split to buy rows.

    Reuses hauling.calculate_deficit so the haul math lives in exactly one place.
    When build_station is None, falls back to a buy-everything-not-owned view.

    Each merged row carries `needed` (the gross requirement, == node.total_needed),
    `shortfall` (net of station/job inventory — kept for reference/display), and the
    deficit fields `at_station` / `elsewhere` / `to_buy` (the actual amounts after
    considering ALL locations). The split is computed from gross `needed`, so
    station/elsewhere stock is netted exactly once. (For raw materials in_job is
    always 0, so gross need is the correct basis.)
    """
    needed = [{"type_id": r["type_id"], "name": r["name"],
               "quantity": r["needed"]} for r in buy_rows]
    # 0 is never a real EVE station id, so nothing routes to at_station.
    station = build_station if build_station is not None else 0
    deficit = hauling.calculate_deficit(needed, loc_index, station, volumes)
    by_id = {d["type_id"]: d for d in deficit}
    out = []
    for r in buy_rows:
        merged = {**r, **by_id.get(r["type_id"], {})}
        out.append(merged)
    return out


def attach_owned_totals(rows, owned_index, volumes):
    """Add total / owned / to_buy (+ volumes) using a FLAT owned-anywhere index.

    For the Materials flat view: nets gross requirement against everything you
    own across all locations (no station concept). rows: [{type_id,name,quantity}].
    Pure: returns new dicts, never mutates inputs.
    """
    out = []
    for r in rows:
        tid = r["type_id"]
        total = r["quantity"]
        owned = owned_index.get(tid, 0)
        to_buy = max(0, total - owned)
        unit_vol = volumes.get(tid, 0.0)
        out.append({
            **r, "total": total, "owned": owned, "to_buy": to_buy,
            "total_volume": total * unit_vol, "to_buy_volume": to_buy * unit_vol,
        })
    return out


def attach_haul_breakdown(rows, loc_names):
    """Add a display-ready `haul` list to each row from its `elsewhere` map.

    rows: dicts that may carry `elsewhere` {station_id: qty}.
    loc_names: {station_id: name}; a missing name falls back to str(id).
    Returns new dicts (pure); `haul` is sorted by qty descending.
    """
    out = []
    for r in rows:
        elsewhere = r.get("elsewhere") or {}
        haul = sorted(
            ({"name": loc_names.get(lid, str(lid)), "qty": qty}
             for lid, qty in elsewhere.items()),
            key=lambda h: (-h["qty"], h["name"]),
        )
        out.append({**r, "haul": haul})
    return out
