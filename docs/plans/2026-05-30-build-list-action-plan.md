# Build List + Action Plan Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the search-first flow with a persistent build list of targets that produces one aggregated, action-first plan ("ready to start now / in progress / blocked / buy") derived live from ESI assets + jobs.

**Architecture:** A new pure-logic module `plan.py` merges per-target material trees (from the existing `sde.resolve_material_chain`) into one requirement graph keyed by `type_id`, then classifies each node against a location asset index and active ESI jobs into four buckets. Flask routes and templates render it; the existing engine (`sde.py`, `esi.py`, `hauling.py`) is untouched and old pages survive as drill-downs. Build-list intent persists to a gitignored JSON file; execution state is never saved — it is recomputed from ESI every load.

**Tech Stack:** Python 3.12+, Flask, Jinja2, pytest, Preston (ESI), SQLite SDE. No new dependencies.

**Design doc:** `docs/plans/2026-05-30-build-list-action-plan-design.md`

**Conventions to match:**
- Pure functions with no Flask/ESI/DB imports go in `plan.py`, mirroring `hauling.py`. Test them like `tests/test_hauling.py` (plain dicts in, plain data out, no mocks).
- Flask routes stay thin; they fetch ESI/SDE data then delegate to pure functions.
- ESI job dicts (from `esi.fetch_industry_jobs`) use ESI field names: `product_type_id`, `blueprint_type_id`, `runs`, `activity_id`, `status` (`active`/`paused`/`ready`/`delivered`/`cancelled`/`reverted`), `facility_id`, `end_date`.

---

## Task 1: Build-list persistence (`build_list.py`)

A tiny module that loads/saves the list of targets to a JSON file. Pure file IO, no Flask.

**Files:**
- Create: `build_list.py`
- Test: `tests/test_build_list.py`

**Data shape** — one target:
```python
{"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
 "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None}
```
`buy_set` is a list of intermediate `type_id`s the user chose to buy instead of build. `build_station` is an optional facility_id.

**Step 1: Write the failing test**

```python
# tests/test_build_list.py
import build_list


def test_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    targets = [
        {"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
    ]
    build_list.save(targets, path)
    assert build_list.load(path) == targets


def test_load_missing_returns_empty(tmp_path):
    assert build_list.load(tmp_path / "nope.json") == []


def test_add_target_appends(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add(
        {"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
        path,
    )
    build_list.add(
        {"type_id": 17636, "name": "Phoenix", "qty": 3, "runs": 3,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
        path,
    )
    loaded = build_list.load(path)
    assert [t["type_id"] for t in loaded] == [671, 17636]


def test_remove_target_by_type_id(tmp_path):
    path = tmp_path / "bl.json"
    build_list.save(
        [{"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
          "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None}],
        path,
    )
    build_list.remove(671, path)
    assert build_list.load(path) == []
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_list.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'build_list'`

**Step 3: Write minimal implementation**

```python
# build_list.py
"""Persistence for the user's build list (target intent only — never progress)."""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "build_list.json"


def load(path=DEFAULT_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def save(targets: list[dict], path=DEFAULT_PATH) -> None:
    Path(path).write_text(json.dumps(targets, indent=2), encoding="utf-8")


def add(target: dict, path=DEFAULT_PATH) -> None:
    targets = load(path)
    targets.append(target)
    save(targets, path)


def remove(type_id: int, path=DEFAULT_PATH) -> None:
    targets = [t for t in load(path) if t["type_id"] != type_id]
    save(targets, path)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_build_list.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add build_list.py tests/test_build_list.py
git commit -m "feat: build-list persistence module"
```

---

## Task 2: Merge per-target trees into one requirement graph (`plan.py`)

Walk each target's resolved `MaterialNode` tree and accumulate a flat `{type_id: ReqNode}` graph, summing quantities for shared intermediates and recording each node's direct-input type_ids. This is the aggregation that makes a Rev and a Phoenix share cap parts instead of double-counting.

**Files:**
- Create: `plan.py`
- Test: `tests/test_plan.py`

**Design notes for the implementer:**
- `ReqNode` is the merged record for one `type_id`. `total_needed` sums every occurrence. `direct_inputs` is the set of child `type_id`s (same for a given blueprint wherever it appears, so recording it once is correct).
- `merge_trees` takes already-resolved trees so it stays pure and testable without the SDE. A thin caller in the Flask/CLI layer (Task 5/7) does the `resolve_material_chain` calls and passes the trees in.
- The top-level product of each target is **not** in its own resolved tree (resolve returns the product's *inputs*). The caller adds a synthetic product node; `merge_trees` accepts an explicit list of `(product_node_info, child_nodes)` pairs so the product is represented and its direct inputs are the top-level material type_ids.

**Step 1: Write the failing test**

```python
# tests/test_plan.py
from sde import MaterialNode
import plan


def _node(tid, name, qty, terminal, children=None, bp=None, act=1):
    return MaterialNode(
        type_id=tid, name=name, quantity_needed=qty,
        activity_id=None if terminal else act,
        activity_name=None if terminal else "Manufacturing",
        blueprint_type_id=bp, me_level=10,
        is_terminal=terminal, depth=0, children=children or [],
    )


def test_merge_single_target_indexes_every_node():
    # Hull (product) <- 30 Cap Parts <- 100 Tritanium
    trit = _node(34, "Tritanium", 100, terminal=True)
    parts = _node(1, "Cap Parts", 30, terminal=False, children=[trit], bp=1001)
    graph = plan.merge_trees([
        plan.Target(type_id=671, name="Revelation", blueprint_type_id=2001,
                    needed=5, children=[parts]),
    ])
    assert set(graph) == {671, 1, 34}
    assert graph[1].total_needed == 30
    assert graph[1].direct_inputs == {34}
    assert graph[671].direct_inputs == {1}
    assert graph[34].is_terminal is True


def test_merge_sums_shared_intermediate():
    parts_a = _node(1, "Cap Parts", 30, terminal=False,
                    children=[_node(34, "Tritanium", 100, terminal=True)], bp=1001)
    parts_b = _node(1, "Cap Parts", 24, terminal=False,
                    children=[_node(34, "Tritanium", 80, terminal=True)], bp=1001)
    graph = plan.merge_trees([
        plan.Target(671, "Revelation", 2001, 5, [parts_a]),
        plan.Target(17636, "Phoenix", 2002, 3, [parts_b]),
    ])
    assert graph[1].total_needed == 54          # 30 + 24, not duplicated
    assert graph[34].total_needed == 180        # 100 + 80
    assert set(graph[671].direct_inputs) == {1}
    assert set(graph[17636].direct_inputs) == {1}
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'plan'`

**Step 3: Write minimal implementation**

```python
# plan.py
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
    children: list             # list[MaterialNode] — the product's direct inputs


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


def merge_trees(targets: list[Target]) -> dict[int, ReqNode]:
    graph: dict[int, ReqNode] = {}
    for t in targets:
        _accumulate(
            graph, t.type_id, t.name, t.needed,
            t.blueprint_type_id, activity_id=1, is_terminal=False,
            direct_inputs=[c.type_id for c in t.children],
        )
        for child in t.children:
            _walk(graph, child)
    return graph
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan.py -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add plan.py tests/test_plan.py
git commit -m "feat: merge per-target material trees into one requirement graph"
```

---

## Task 3: The classifier — ready / in_progress / blocked / buy

The heart of the redesign. Given the merged graph plus a location asset index, active jobs, build station, and buy-set, classify every node into one of four buckets using the readiness rule from the design doc.

**Files:**
- Modify: `plan.py`
- Test: `tests/test_plan.py`

**Readiness rule (exact):**
```
owned   = qty owned at build_station (fallback: owned anywhere if build_station is None)
in_job  = sum of (job.runs-derived product qty) for active jobs whose product_type_id == node
shortfall = total_needed - owned - in_job

For a node WITH a blueprint and NOT in buy_set:
    shortfall <= 0                      -> satisfied (omit from all buckets)
    active job producing it exists      -> IN PROGRESS
    every direct input has shortfall<=0 -> READY
    else                                -> BLOCKED (missing = inputs with shortfall > 0)
A node that is terminal OR in buy_set, with shortfall > 0 -> BUY
```
Known simplification (document in a comment): "input has shortfall <= 0" is evaluated at the aggregate level — owning enough of an input to cover all its uses marks it available. Good enough for "can I start something now"; note it so a future refinement (per-branch reservation) is an informed choice, not a surprise.

**Step 1: Write the failing tests**

```python
# add to tests/test_plan.py

def _graph(*nodes):
    return {n.type_id: n for n in nodes}


def _req(tid, name, needed, terminal, inputs=(), bp=None):
    return plan.ReqNode(
        type_id=tid, name=name, total_needed=needed,
        blueprint_type_id=bp, activity_id=None if terminal else 1,
        is_terminal=terminal, direct_inputs=set(inputs),
    )


def test_classify_ready_when_inputs_owned():
    # Hull needs Cap Parts; we own all 30 cap parts -> hull is READY
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=True),
    )
    loc = {1: {60003760: 30}}
    out = plan.classify(g, loc, jobs=[], build_station=60003760, buy_set=set())
    assert [n["type_id"] for n in out["ready"]] == [671]
    assert out["blocked"] == []
    assert out["buy"] == []


def test_classify_blocked_lists_missing_inputs():
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    loc = {}  # own nothing
    out = plan.classify(g, loc, jobs=[], build_station=60003760, buy_set=set())
    # Cap Parts has no inputs owned -> blocked; hull blocked on cap parts;
    # Tritanium has no blueprint -> buy
    blocked_ids = {n["type_id"] for n in out["blocked"]}
    assert blocked_ids == {671, 1}
    cap = next(n for n in out["blocked"] if n["type_id"] == 1)
    assert cap["missing"] == [{"type_id": 34, "name": "Tritanium", "shortfall": 3000}]
    assert [n["type_id"] for n in out["buy"]] == [34]


def test_classify_in_progress_from_active_job():
    g = _graph(
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    loc = {34: {60003760: 3000}}  # trit on hand
    jobs = [{"product_type_id": 1, "runs": 30, "activity_id": 1,
             "status": "active", "end_date": "2026-05-31T00:00:00Z"}]
    out = plan.classify(g, loc, jobs=jobs, build_station=60003760, buy_set=set())
    assert [n["type_id"] for n in out["in_progress"]] == [1]
    assert out["ready"] == []   # already running, not offered again


def test_classify_buy_set_moves_node_to_buy():
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    out = plan.classify(g, {}, jobs=[], build_station=None, buy_set={1})
    # Cap Parts chosen to buy -> appears in buy, not blocked/ready
    assert 1 in {n["type_id"] for n in out["buy"]}
    assert 1 not in {n["type_id"] for n in out["ready"] + out["blocked"]}
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan.py -k classify -v`
Expected: FAIL with `AttributeError: module 'plan' has no attribute 'classify'`

**Step 3: Write minimal implementation**

```python
# add to plan.py

def _owned(loc_index, type_id, build_station) -> int:
    locs = loc_index.get(type_id, {})
    if build_station is not None:
        return locs.get(build_station, 0)
    return sum(locs.values())   # no station chosen -> count everywhere


def _in_job_qty(jobs, type_id) -> int:
    """Units of `type_id` currently being produced by active/ready jobs."""
    total = 0
    for j in jobs:
        if j.get("product_type_id") != type_id:
            continue
        if j.get("status") not in ("active", "ready", "paused"):
            continue
        total += j.get("runs", 0)   # product qty/run folded in upstream is approx;
                                    # runs is the conservative signal that "it's cooking"
    return total


def classify(graph, loc_index, jobs, build_station, buy_set):
    """Bucket every node into ready / in_progress / blocked / buy.

    NOTE: input availability is evaluated at the aggregate level (owning enough
    of an input to cover all its uses marks it available). Sufficient for
    'what can I start now'; per-branch reservation is a deliberate non-goal.
    """
    shortfall = {}
    has_active_job = {}
    for tid, node in graph.items():
        owned = _owned(loc_index, tid, build_station)
        in_job = _in_job_qty(jobs, tid)
        shortfall[tid] = node.total_needed - owned - in_job
        has_active_job[tid] = in_job > 0

    ready, in_progress, blocked, buy = [], [], [], []
    for tid, node in graph.items():
        if shortfall[tid] <= 0:
            continue  # satisfied
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
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan.py -v`
Expected: PASS (all tests, including Task 2's merge tests)

**Step 5: Commit**

```bash
git add plan.py tests/test_plan.py
git commit -m "feat: classify requirement graph into ready/in-progress/blocked/buy"
```

---

## Task 4: Buy-list deficit + profit rollup wiring (`plan.py`)

Enrich the `buy` bucket with haul/buy split (reuse `hauling.calculate_deficit`) and compute a build-list-wide profit rollup. Keep these as thin pure adapters so the route layer stays trivial.

**Files:**
- Modify: `plan.py`
- Test: `tests/test_plan.py`

**Step 1: Write the failing test**

```python
# add to tests/test_plan.py
import hauling


def test_buy_rows_get_haul_split():
    buy = [{"type_id": 34, "name": "Tritanium", "needed": 5000, "shortfall": 5000}]
    loc = {34: {60003760: 1000, 60008494: 2000}}  # 1000 at station, 2000 elsewhere
    volumes = {34: 0.01}
    enriched = plan.enrich_buy(buy, loc, build_station=60003760, volumes=volumes)
    row = enriched[0]
    assert row["at_station"] == 1000
    assert row["to_buy"] == 2000        # 5000 - 1000 - 2000(elsewhere)
    assert row["elsewhere"] == {60008494: 2000}
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan.py -k enrich_buy -v`
Expected: FAIL with `AttributeError: module 'plan' has no attribute 'enrich_buy'`

**Step 3: Write minimal implementation**

```python
# add to plan.py
import hauling


def enrich_buy(buy_rows, loc_index, build_station, volumes):
    """Attach at_station / elsewhere / to_buy split to buy rows.

    Reuses hauling.calculate_deficit so the haul math lives in exactly one place.
    When build_station is None, falls back to a buy-everything-not-owned view.
    """
    needed = [{"type_id": r["type_id"], "name": r["name"],
               "quantity": r["shortfall"]} for r in buy_rows]
    station = build_station if build_station is not None else 0
    deficit = hauling.calculate_deficit(needed, loc_index, station, volumes)
    by_id = {d["type_id"]: d for d in deficit}
    out = []
    for r in buy_rows:
        merged = {**r, **by_id.get(r["type_id"], {})}
        out.append(merged)
    return out
```

Profit rollup: the route layer already has `_compute_profit` in `app.py`. For the build-list rollup, sum each target's `_compute_profit(...)` material-cost and revenue. No new pure function is needed here — wire it in Task 5 and keep this task to the buy split. (YAGNI: don't build a second profit engine.)

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plan.py tests/test_plan.py
git commit -m "feat: enrich buy list with haul/buy split"
```

---

## Task 5: Flask routes — build list + plan

Wire the pure logic into the web app. `/` becomes the build list; `/plan` renders the four buckets; `/api/plan` returns JSON for the live refresh button.

**Files:**
- Modify: `app.py` (replace the `index()` route at `app.py:332-344`; add new routes near it; add a `_resolve_targets()` helper that turns build-list dicts into `plan.Target`s via `sde.resolve_material_chain`)
- Reference: `sde.resolve_material_chain` (`sde.py:431`), `sde.find_blueprint_for_product`, `sde.get_activity_products` (for product qty/run), `esi.fetch_industry_jobs`, `esi.build_location_asset_index`, `esi.get_cached_location_asset_index` (`esi.py:590`), `_get_station_list` (`app.py:120`), `get_sde`/`get_authed_preston_from_session` (`app.py:170`,`243`)

**Helper to add (orchestration — not pure, lives in app.py):**

```python
def _resolve_targets(sde, targets: list[dict]) -> list[plan.Target]:
    out = []
    for t in targets:
        bp_id = sde.find_blueprint_for_product(t["type_id"])
        if bp_id is None:
            continue  # not manufacturable; skip with a flash in the route
        children = resolve_material_chain(
            sde, bp_id, me_level=t.get("me", 10), runs=t.get("runs", 1),
            structure_bonus=t.get("structure_bonus", 0.0),
        )
        # product units = runs * qty_per_run (ammo=100/run, ships=1/run)
        qpr = sde.get_product_qty_per_run(bp_id) or 1
        out.append(plan.Target(
            type_id=t["type_id"], name=t["name"], blueprint_type_id=bp_id,
            needed=t.get("runs", 1) * qpr, children=children,
        ))
    return out
```
If `sde.get_product_qty_per_run` does not exist, add it next to `find_product_for_blueprint` in `sde.py` (one-line query against `industryActivityProducts.quantity WHERE typeID=? AND activityID=1`) with a test in `tests/test_sde.py`.

**Routes:**

```python
import build_list, plan

@app.route("/")
def index():
    targets = build_list.load()
    return render_template("build_list.html", targets=targets,
                           character_name=session.get("character_name"))

@app.route("/build-list/add", methods=["POST"])
def build_list_add():
    tid = int(request.form["type_id"])
    sde = get_sde()
    build_list.add({
        "type_id": tid, "name": sde.get_type_name(tid),
        "qty": int(request.form.get("qty", 1)),
        "runs": int(request.form.get("runs", 1)),
        "me": int(request.form.get("me", 10)),
        "structure_bonus": float(request.form.get("structure_bonus", 0)),
        "buy_set": [], "build_station": None,
    })
    return redirect(url_for("index"))

@app.route("/build-list/remove/<int:type_id>", methods=["POST"])
def build_list_remove(type_id):
    build_list.remove(type_id)
    return redirect(url_for("index"))

@app.route("/plan")
def plan_view():
    sde = get_sde()
    targets = build_list.load()
    graph = plan.merge_trees(_resolve_targets(sde, targets))
    buy_set = {tid for t in targets for tid in t.get("buy_set", [])}

    loc_index, jobs, build_station = {}, [], None
    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        loc_index = esi.get_cached_location_asset_index(p, character_id)
        jobs = esi.fetch_industry_jobs(p, character_id)
        stations = _get_station_list(p, character_id)
        build_station = stations[0]["id"] if stations else None
        session["refresh_token"] = p.refresh_token

    buckets = plan.classify(graph, loc_index, jobs, build_station, buy_set)
    volumes = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
    buckets["buy"] = plan.enrich_buy(buckets["buy"], loc_index, build_station, volumes)
    return render_template("plan.html", buckets=buckets,
                           targets=targets, authed=bool(p),
                           character_name=session.get("character_name"))

@app.route("/api/plan")
def api_plan():
    # Same body as plan_view but returns jsonify(buckets) for the live ⟳ button.
    ...
```

**Manual verification (no automated test for Flask wiring — verify by running):**

Run:
```bash
python -m pytest tests/ -v          # all pure-logic tests still green
python app.py                       # then open http://localhost:5000/
```
Expected: `/` shows an (empty) build list with an add form; adding a target then visiting `/plan` shows four bucket sections. Without ESI login, everything sits in Blocked/Buy with a login banner.

**Commit:**
```bash
git add app.py sde.py tests/test_sde.py templates/
git commit -m "feat: build-list and action-plan routes"
```

---

## Task 6: Templates — `build_list.html` and `plan.html`

**Files:**
- Create: `templates/build_list.html` (extends `base.html`)
- Create: `templates/plan.html` (extends `base.html`)
- Reference existing Jinja filters: `isk`, `ftime`, `commas`, `vol` (registered in `app.py`)

**`build_list.html`** — a table of `targets` (name, qty, me, runs, remove button), an add form (search field posting to `/build-list/add`; reuse the existing search partial/autocomplete from `index.html` if present), and a prominent `Build the plan →` link to `/plan`.

**`plan.html`** — four sections in this order, each hidden when empty:
1. **▶ Ready to start now** — `buckets.ready`: name, shortfall (qty to make), activity, drill-in link to `/blueprint/<blueprint_type_id>` or `/chain/<blueprint_type_id>`.
2. **⏳ In progress** — `buckets.in_progress`: name, shortfall, `end_date` (format with a small countdown or `ftime`).
3. **🔒 Blocked** — `buckets.blocked`: name, then its `missing` list (`{name} × {shortfall}`), each missing item linking to its own row/drill-in.
4. **🛒 Buy list** — `buckets.buy`: name, `to_buy`, `to_buy_volume` (`| vol`), and (if priced) line cost. Reuse the column styling from `shopping.html`/`chain_shopping.html`.
- A `⟳ refresh` button that re-fetches `/api/plan` and re-renders (progressive enhancement; page works without JS).
- A login banner when `not authed`.

**Verification:** `python app.py`, walk the flow in a browser. Confirm a target with owned inputs appears under Ready; remove its inputs (or log out) and confirm it drops to Blocked/Buy.

**Commit:**
```bash
git add templates/build_list.html templates/plan.html
git commit -m "feat: build-list and action-plan templates"
```

---

## Task 7: CLI parallel `plan` command (`eve_inventory.py`)

Mirror the web flow in the terminal so both surfaces match. Reads the same `build_list.json`.

**Files:**
- Modify: `eve_inventory.py` (add a `plan` command to the existing command router)
- Reference: existing command dispatch + `with SDE() as sde:` usage in `eve_inventory.py`

**Behaviour:** `python eve_inventory.py plan` loads `build_list.json`, resolves + merges + classifies (same `plan.*` functions), and prints the four sections as text tables. If ESI is configured/authed, fetch assets + jobs; otherwise print SDE-only (all Blocked/Buy) with a note.

**Verification:**
```bash
python eve_inventory.py plan
```
Expected: prints Ready / In progress / Blocked / Buy sections from the current `build_list.json`.

**Commit:**
```bash
git add eve_inventory.py
git commit -m "feat: CLI plan command mirroring the web action plan"
```

---

## Task 8: Final wiring — gitignore, docs, full test run

**Files:**
- Modify: `.gitignore` (add `build_list.json`)
- Modify: `README.md` and `context.md` (document the new flow: build list → plan; note old pages are now drill-downs)

**Steps:**
1. Add `build_list.json` to `.gitignore` (it is per-user intent, like `tokens.json`).
2. Update `README.md` Usage section: lead with the build-list/plan flow; keep the old commands documented under "drill-down / detail views."
3. Update `context.md` architecture section: add `plan.py` (pure classifier) and `build_list.py`, and the new routes.
4. Run the full suite:
   ```bash
   python -m pytest tests/ -v
   ```
   Expected: all prior tests plus the new `test_build_list.py` and `test_plan.py` green.
5. Manual smoke per @superpowers:verification-before-completion: add two targets that share an intermediate, open `/plan`, confirm the shared intermediate's `needed` is summed (not doubled).

**Commit:**
```bash
git add .gitignore README.md context.md
git commit -m "docs: document build-list/action-plan flow; ignore build_list.json"
```

---

## Notes for the executor

- **TDD strictly** on `build_list.py` and `plan.py` (Tasks 1–4) — they are pure and fully unit-testable; that is where correctness lives. Routes/templates/CLI (Tasks 5–7) are verified by running the app, mirroring how the existing Flask code is exercised.
- **Do not touch** `sde.py` chain logic, `esi.py`, or `hauling.py` except the one additive `get_product_qty_per_run` helper in Task 5 (only if it does not already exist).
- **DRY:** the buy split goes through `hauling.calculate_deficit`; profit reuses `_compute_profit`. No second copies.
- **YAGNI:** no saved progress, no DB, no profit rewrite, no per-branch input reservation. Those are explicit non-goals in the design.
- If `get_cached_location_asset_index` or `get_type_volumes` signatures differ from what Task 5 assumes, adapt the call — the engine functions are the source of truth, not this plan's paraphrase.
