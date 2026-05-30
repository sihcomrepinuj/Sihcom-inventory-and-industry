# Search-to-Add + Materials View Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add (1) search-by-name-and-add to the build list and (2) a dedicated `/materials` view with a whole-list material-chain tree, global build/buy toggles, and a flattened supply list (total required + to-buy).

**Architecture:** Evolve `build_list.json` to `{targets, buy_set}` with a global build/buy set; reuse existing engine functions (`resolve_material_chain`, `flatten_material_tree`, `get_cached_asset_index`) for the tree and flat views; add one small pure helper for the to-buy columns. Server-rendered, progressive enhancement (POST-to-toggle) — consistent with the action-plan flow shipped earlier.

**Tech Stack:** Python 3.12+, Flask, Jinja2, pytest, SQLite SDE. No new dependencies.

**Design doc:** `docs/plans/2026-05-30-search-and-buildbuy-design.md`

**Conventions:** Pure functions (no Flask/ESI/DB) live in `plan.py`/`hauling.py` and are tested like `tests/test_hauling.py`. Routes stay thin. Match existing Pico-CSS templates and the `commas`/`vol` Jinja filters.

---

## Task 1: Evolve `build_list.py` to a global buy_set data model

Change `build_list.json` from a bare target list to `{"targets": [...], "buy_set": [...]}`,
with a backward-compatible loader, and migrate all callers in the SAME task so the suite stays
green. This is the foundation everything else builds on.

**Files:**
- Modify: `build_list.py` (full rewrite below)
- Modify: `tests/test_build_list.py` (new API)
- Modify: `app.py` (call sites at lines ~338, ~405-407, ~448, ~463)
- Modify: `eve_inventory.py` (call site at ~443, and its buy_set aggregation)
- Check: `tests/test_app_plan.py`, `tests/test_cli_plan.py` (update any direct `build_list.*` usage / fixtures)

**Step 1: Rewrite `tests/test_build_list.py`** to the new API (load→dict, add_target/remove_target/toggle_buy):

```python
import build_list


def _target(tid, qty=1):
    return {"type_id": tid, "name": f"Item {tid}", "qty": qty, "runs": None,
            "me": 10, "structure_bonus": 0.0}


def test_load_missing_returns_empty_shape(tmp_path):
    assert build_list.load(tmp_path / "nope.json") == {"targets": [], "buy_set": []}


def test_load_legacy_bare_list_is_wrapped(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('[{"type_id": 671, "name": "Revelation"}]', encoding="utf-8")
    data = build_list.load(path)
    assert data["targets"] == [{"type_id": 671, "name": "Revelation"}]
    assert data["buy_set"] == []


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    data = {"targets": [_target(671, 5)], "buy_set": [2867]}
    build_list.save(data, path)
    assert build_list.load(path) == data


def test_add_target_appends_and_upserts(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671, 5), path)
    build_list.add_target(_target(17636, 3), path)
    assert [t["type_id"] for t in build_list.load(path)["targets"]] == [671, 17636]
    build_list.add_target(_target(671, 8), path)  # upsert
    targets = build_list.load(path)["targets"]
    assert len(targets) == 2
    assert next(t for t in targets if t["type_id"] == 671)["qty"] == 8


def test_remove_target(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671), path)
    build_list.remove_target(671, path)
    assert build_list.load(path)["targets"] == []


def test_toggle_buy_is_idempotent_flip(tmp_path):
    path = tmp_path / "bl.json"
    build_list.toggle_buy(2867, path)
    assert build_list.load(path)["buy_set"] == [2867]
    build_list.toggle_buy(2867, path)               # flip off
    assert build_list.load(path)["buy_set"] == []


def test_toggle_buy_preserves_targets(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671), path)
    build_list.toggle_buy(2867, path)
    data = build_list.load(path)
    assert [t["type_id"] for t in data["targets"]] == [671]
    assert data["buy_set"] == [2867]
```

**Step 2: Run** `python -m pytest tests/test_build_list.py -v` → FAIL (AttributeError: add_target etc.).

**Step 3: Rewrite `build_list.py`:**

```python
"""Persistence for the user's build list and global build/buy choices.

Stores intent only (targets + which components to buy instead of build),
never execution progress. Shape: {"targets": [...], "buy_set": [type_id, ...]}.
"""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "build_list.json"


def load(path=DEFAULT_PATH) -> dict:
    """Return {"targets": [...], "buy_set": [...]}.

    Backward-compatible: a legacy bare-list file loads as
    {"targets": <list>, "buy_set": []}.
    """
    p = Path(path)
    if not p.exists():
        return {"targets": [], "buy_set": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"targets": data, "buy_set": []}
    data.setdefault("targets", [])
    data.setdefault("buy_set", [])
    return data


def save(data: dict, path=DEFAULT_PATH) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_target(target: dict, path=DEFAULT_PATH) -> None:
    """Upsert a target by type_id (replace in place, else append)."""
    data = load(path)
    targets = data["targets"]
    for i, t in enumerate(targets):
        if t["type_id"] == target["type_id"]:
            targets[i] = target
            break
    else:
        targets.append(target)
    save(data, path)


def remove_target(type_id: int, path=DEFAULT_PATH) -> None:
    data = load(path)
    data["targets"] = [t for t in data["targets"] if t["type_id"] != type_id]
    save(data, path)


def toggle_buy(type_id: int, path=DEFAULT_PATH) -> None:
    """Flip a component's membership in the global buy_set."""
    data = load(path)
    bs = data["buy_set"]
    if type_id in bs:
        bs.remove(type_id)
    else:
        bs.append(type_id)
    save(data, path)
```

**Step 4: Migrate callers** (keep behavior identical, just adapt to the new shape):

- `app.py` `index()` (~338): `targets = build_list.load()["targets"]`
- `app.py` `_compute_plan()` (~405-407): replace the two lines with
  ```python
  data = build_list.load()
  targets = data["targets"]
  graph = plan.merge_trees(_resolve_targets(sde, targets))
  buy_set = set(data["buy_set"])
  ```
  (delete the old `buy_set = {tid for t in targets ...}` aggregation)
- `app.py` `build_list_add()` (~448): `build_list.add_target({...})`. ALSO drop the now-vestigial
  per-target `"buy_set": []` and `"build_station": None` keys from the stored dict (global buy_set
  replaces them). Keep `type_id, name, qty, runs(None), me, structure_bonus`.
- `app.py` `build_list_remove()` (~463): `build_list.remove_target(type_id)`
- `eve_inventory.py` `cmd_plan()` (~443): `data = build_list.load(); targets = data["targets"]`,
  and replace its `buy_set = {tid for t in targets ...}` aggregation with `buy_set = set(data["buy_set"])`.
- `tests/test_app_plan.py` / `tests/test_cli_plan.py`: update any direct `build_list.load()` /
  `build_list.add(...)` usage to the new API, and any fixture that monkeypatches bound defaults
  (the functions changed names: add→add_target, remove→remove_target). The empty-list assertions
  now compare against `{"targets": [], "buy_set": []}` shape where relevant.

**Step 5:** Run `python -m pytest -q` → all green (fix any caller the change broke).

**Step 6: Commit:**
```bash
git add build_list.py tests/test_build_list.py app.py eve_inventory.py tests/test_app_plan.py tests/test_cli_plan.py
git commit -m "refactor: global build/buy set in build_list.json {targets, buy_set}"
```

---

## Task 2: Search-to-add on the build-list page

Replace the manual Type ID field with a name search that lists manufacturable products, each
with an Add button.

**Files:**
- Modify: `sde.py` (add `search_manufacturable`)
- Test: `tests/test_sde.py`
- Modify: `app.py` `index()` route
- Modify: `templates/build_list.html`

**Step 1: Add a failing SDE test** to `tests/test_sde.py` (follow its skip-if-missing style):

```python
def test_search_manufacturable_finds_ships_excludes_raws(sde):
    results = sde.search_manufacturable("Drake")
    assert any(r["name"] == "Drake" for r in results)
    # Tritanium is a raw mineral with no manufacturing blueprint -> excluded
    trit = sde.search_manufacturable("Tritanium")
    assert all(r["name"] != "Tritanium" for r in trit)
```

**Step 2:** Run that test → FAIL (no attribute search_manufacturable).

**Step 3: Implement `search_manufacturable`** in `sde.py` (near `search_types`):

```python
def search_manufacturable(self, name: str, limit: int = 25) -> list[dict]:
    """Search published types that have a manufacturing blueprint (activityID=1).

    Returns [{type_id, name}] ordered by name. Excludes raw materials and other
    items that can't be built, so they can't be added as build targets.
    """
    rows = self.conn.execute(
        """
        SELECT DISTINCT it.typeID AS type_id, it.typeName AS name
        FROM invTypes it
        JOIN industryActivityProducts iap
          ON iap.productTypeID = it.typeID AND iap.activityID = 1
        WHERE it.typeName LIKE ? AND it.published = 1
        ORDER BY it.typeName
        LIMIT ?
        """,
        (f"%{name}%", limit),
    ).fetchall()
    return [{"type_id": r["type_id"], "name": r["name"]} for r in rows]
```

**Step 4:** Run the SDE test → PASS.

**Step 5: Update `index()`** in `app.py` to handle an optional search query:

```python
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    data = build_list.load()
    results = None
    if q:
        results = get_sde().search_manufacturable(q)
    return render_template("build_list.html", targets=data["targets"],
                           q=q, results=results,
                           character_name=session.get("character_name"))
```

**Step 6: Update `templates/build_list.html`** — replace the two-step Type ID block with:
- A search form: `GET /` with `name="q"`, value `{{ q }}`.
- When `results` is set, a results table; each row has the item name, small `qty` (default 1)
  and `me` (default 10, select 0-10) inputs, and an **Add** form posting to
  `{{ url_for('build_list_add') }}` with hidden `type_id` and `name`, plus the qty/me inputs.
  (`build_list_add` already reads type_id/name/qty/me/structure_bonus from the form;
  structure_bonus defaults to 0 server-side, so the row need not include it.)
- When `q` is set but `results` is empty: "No manufacturable items match '{{ q }}'."
- Keep the targets table and "Build the plan →" link unchanged.

**Step 7: Verify** — `python -c "import app"`; add a smoke assertion to `tests/test_app_plan.py`:
GET `/?q=drake` returns 200 and contains an "Add" button / the name "Drake" (SDE-gated). Run
`python -m pytest -q` (green).

**Step 8: Commit:**
```bash
git add sde.py tests/test_sde.py app.py templates/build_list.html tests/test_app_plan.py
git commit -m "feat: search manufacturable items by name and add to build list inline"
```

---

## Task 3: Pure helper for flat supply to-buy columns

**Files:**
- Modify: `plan.py` (add `attach_supply_columns`)
- Test: `tests/test_plan.py`

**Step 1: Add failing tests** to `tests/test_plan.py`:

```python
def test_attach_supply_columns_nets_owned_and_volumes():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 5000},
            {"type_id": 35, "name": "Pyerite", "quantity": 1000}]
    owned = {34: 2000}                      # own some trit, no pyerite
    volumes = {34: 0.01, 35: 0.01}
    out = plan.attach_supply_columns(rows, owned, volumes)
    trit = next(r for r in out if r["type_id"] == 34)
    assert trit["total"] == 5000
    assert trit["owned"] == 2000
    assert trit["to_buy"] == 3000
    assert trit["total_volume"] == 50.0     # 5000 * 0.01
    assert trit["to_buy_volume"] == 30.0    # 3000 * 0.01
    pyer = next(r for r in out if r["type_id"] == 35)
    assert pyer["to_buy"] == 1000           # nothing owned


def test_attach_supply_columns_owned_exceeds_need():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    out = plan.attach_supply_columns(rows, {34: 999}, {34: 0.01})
    assert out[0]["to_buy"] == 0            # never negative
```

**Step 2:** Run → FAIL (no attribute attach_supply_columns).

**Step 3: Implement** in `plan.py`:

```python
def attach_supply_columns(rows, owned_index, volumes):
    """Add total / owned / to_buy (+ volumes) to flat supply rows.

    rows: [{type_id, name, quantity}] from flatten_material_tree.
    owned_index: {type_id: owned_qty} (flat, summed across locations).
    volumes: {type_id: unit_volume_m3}.
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
            **r,
            "total": total,
            "owned": owned,
            "to_buy": to_buy,
            "total_volume": total * unit_vol,
            "to_buy_volume": to_buy * unit_vol,
        })
    return out
```

**Step 4:** Run `python -m pytest tests/test_plan.py -v` → PASS; `python -m pytest -q` green.

**Step 5: Commit:**
```bash
git add plan.py tests/test_plan.py
git commit -m "feat: attach_supply_columns pure helper for flat supply view"
```

---

## Task 4: `/materials` routes (tree, flat, toggle)

**Files:**
- Modify: `app.py` (new routes + a shared resolver)

**Step 1: Add routes** to `app.py` (reuse the existing `_resolve_targets`):

```python
@app.route("/materials")
def materials():
    view = request.args.get("view", "tree")
    sde = get_sde()
    data = build_list.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])
    resolved = _resolve_targets(sde, targets)   # list[plan.Target], each with .children

    flat_rows = None
    authed = False
    if view == "flat":
        nodes = [child for t in resolved for child in t.children]
        flat = flatten_material_tree(nodes, buy_set)        # [{type_id,name,quantity}]
        volumes = sde.get_type_volumes([r["type_id"] for r in flat])
        owned_index = {}
        p = get_authed_preston_from_session()
        if p:
            authed = True
            character_id = int(session["character_id"])
            corporation_id = session.get("corporation_id")
            if corporation_id:
                owned_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
            else:
                owned_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
            session["refresh_token"] = p.refresh_token
        flat_rows = plan.attach_supply_columns(flat, owned_index, volumes)

    return render_template("materials.html", view=view, targets=resolved,
                           buy_set=buy_set, flat_rows=flat_rows, authed=authed,
                           has_targets=bool(targets),
                           character_name=session.get("character_name"))


@app.route("/materials/toggle/<int:type_id>", methods=["POST"])
def materials_toggle(type_id):
    build_list.toggle_buy(type_id)
    view = request.form.get("view", "tree")
    return redirect(url_for("materials", view=view))
```

Confirm `flatten_material_tree` is imported in app.py (it's in sde.py; add to the existing
`from sde import ...` line if not already present).

**Step 2: Verify** `python -c "import app"` works. (Template added in Task 5; until then the
route would 500 on render — that's fine, Task 5 completes it. Do NOT add a smoke test for
/materials until the template exists.)

**Step 3: Commit:**
```bash
git add app.py
git commit -m "feat: /materials routes (tree/flat resolve + build/buy toggle)"
```

---

## Task 5: `materials.html` template + nav/links

**Files:**
- Create: `templates/materials.html`
- Modify: `templates/base.html` (nav link to Materials)
- Modify: `templates/build_list.html` and `templates/plan.html` (link to /materials)
- Modify: `tests/test_app_plan.py` (smoke tests now that the template exists)

**Template requirements (`materials.html`, extends base.html):**
- A **Tree ⇄ Flat** switch: two links, `?view=tree` and `?view=flat`, current one marked active.
- Empty state when `not has_targets`: "Your build list is empty — add targets first." with a link to `/`.

- **Tree view** (`view == 'tree'`): for each target in `targets` (a `plan.Target` with
  `.type_id, .name, .needed, .children`), render the product as a root line (name × needed,
  NO toggle — you never buy the final target), then a recursive Jinja macro over `.children`.
  Define `{% macro node(n, buy_set) %}`:
  - If `n.type_id in buy_set`: render `n.name × n.quantity_needed` marked **(buy)** + a toggle
    form button "Build instead" (POST `/materials/toggle/{{ n.type_id }}` with hidden
    `view=tree`). Do NOT recurse (it's a bought leaf).
  - Elif `n.is_terminal or not n.children`: render `n.name × n.quantity_needed` as a plain raw
    leaf (no toggle).
  - Else (buildable intermediate): render `n.name × n.quantity_needed` (· `n.activity_name`) +
    a toggle button "Buy instead" (POST toggle, hidden `view=tree`), then recurse
    `{{ node(c, buy_set) }}` for each child `c`. Indent children (nested `<ul>` or padding by depth).
  Use `commas` filter on quantities.

- **Flat view** (`view == 'flat'`): a table over `flat_rows` with columns Item,
  Total required (`r.total | commas`), Volume (`r.total_volume | vol`), and — only when
  `authed` — To buy (`r.to_buy | commas`) and Buy volume (`r.to_buy_volume | vol`). When not
  authed, show a short note that logging in adds the to-buy column. Reuse shopping.html column styling.

- **base.html:** add a "Materials" nav link next to "Plan".
- **build_list.html & plan.html:** add a link/button to `/materials` ("Materials & build/buy →").

**Smoke tests** (`tests/test_app_plan.py`, SDE-gated): seed a build list with one manufacturable
target; GET `/materials?view=tree` → 200 and contains the target name; GET `/materials?view=flat`
→ 200; `POST /materials/toggle/<id>` → 302 and flips the component into the buy_set (assert via
`build_list.load()["buy_set"]`); unauthed flat view does NOT contain the "To buy" header.

**Verify:** `python -m pytest -q` green; `python -c "import app"` OK.

**Commit:**
```bash
git add templates/materials.html templates/base.html templates/build_list.html templates/plan.html tests/test_app_plan.py
git commit -m "feat: materials.html tree/flat view with build/buy toggles + nav links"
```

---

## Task 6: CLI parity (optional flat supplies) + docs + final verification

Keep the CLI honest about build/buy (it already reads the global buy_set after Task 1) and
document the new features.

**Files:**
- Modify: `eve_inventory.py` (confirm cmd_plan uses global buy_set; OPTIONAL: a `materials`
  CLI subcommand printing the flat supply list — only if it stays small and mirrors the web
  flat view; otherwise skip and note it.)
- Modify: `README.md`, `context.md`

**Steps:**
1. Confirm `cmd_plan` already consumes `set(data["buy_set"])` from Task 1 (a component toggled
   "buy" shows as a buy line in the CLI plan). Add/keep a one-line note in HELP that build/buy
   is set on the web `/materials` view.
2. OPTIONAL `eve_inventory.py materials` command: load build list, resolve, `flatten_material_tree`
   with the global buy_set, print Item / Total required / Volume (and To buy when ESI available
   via `get_cached_asset_index` + `attach_supply_columns`). Reuse the same pure helpers. If this
   balloons, SKIP it and state so — the web view is the primary surface.
3. `README.md`: document search-to-add (search by name, click Add) and the Materials view
   (tree with build/buy toggles, flat supplies with total + to-buy), and that build/buy is a
   global per-component choice that drives the plan.
4. `context.md`: note the `build_list.json` shape change to `{targets, buy_set}`, the new
   `sde.search_manufacturable`, `plan.attach_supply_columns`, the `/materials` + toggle routes,
   and `materials.html`. Update the test-count line.
5. Run `python -m pytest -q` (green) and a manual smoke: add two targets sharing a component via
   search, open `/materials`, toggle that component to "buy", confirm the flat view lists it as a
   bought leaf and the plan's buy bucket now includes it.

**Commit:**
```bash
git add eve_inventory.py README.md context.md
git commit -m "docs: document search-to-add and materials build/buy view"
```

---

## Notes for the executor

- **TDD strictly** on `build_list.py` (Task 1) and `plan.attach_supply_columns` (Task 3) — pure,
  fully unit-testable. Routes/templates are verified by the Flask test client + running.
- **DRY:** reuse `_resolve_targets` (gives both the tree roots and the flat input), `flatten_material_tree`,
  `get_cached_asset_index`, `get_type_volumes`, `attach_supply_columns`. Do NOT reimplement flattening
  or netting.
- **YAGNI:** no per-target build/buy, no JS live-refresh, no haul split or profit on the materials
  view. Global buy_set, server round-trip toggles only.
- **Engine functions are the source of truth** — if a signature differs from this plan's paraphrase,
  adapt the call.
- Keep the suite green at every commit (Task 1 must migrate all callers in its own commit).
