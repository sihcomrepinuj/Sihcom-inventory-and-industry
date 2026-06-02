# Per-Product Build Station Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give each build-list product its own build station, render the action plan grouped one block per product (each evaluated against its own station), and turn the Materials flat view into an aggregated bill-of-materials with an owned-anywhere "to buy".

**Architecture:** Move `build_station` from a single global value onto each target in `build_list.json`. `_compute_plan` stops merging targets and instead loops, running the *unchanged* engine (`merge_trees`/`classify`/`enrich_buy`/`attach_haul_breakdown`) once per product against that product's station, returning a list of blocks. The Materials flat view drops the station/haul split for an aggregated total + owned-anywhere to-buy.

**Tech Stack:** Python 3.12+, Flask, Jinja2, pytest. No new dependencies.

**Design doc:** `docs/plans/2026-06-01-per-product-build-station-design.md`

**Current code to know (read before editing):**
- `plan.Target` dataclass (`plan.py`): fields `type_id, name, blueprint_type_id, needed, children`.
- `app._resolve_targets(sde, targets)` (`app.py:~375-409`): builds `plan.Target`s from build-list dicts.
- `app._compute_plan()` (`app.py:412-466`): currently merges all targets, classifies against ONE `build_station` resolved via `plan.resolve_build_station(data.get("build_station"), stations)`, returns `(buckets, targets, authed, station_ctx)`.
- `app.plan_view` (`517-524`), `app.api_plan` (`527-530`): consume the 4-tuple.
- `app.build_station` route (`498-514`): POST sets the GLOBAL station via `build_list.set_build_station`.
- `app.materials` route (`533-591`): flat branch uses location-aware `calculate_deficit` + station picker + haul.
- `templates/plan.html`: 4 fixed sections + a JS in-place refresh + `{% include "_station_picker.html" %}`.
- `templates/_station_picker.html`: global station `<select>` posting `station_id`/`next`/`view`.
- `templates/materials.html`: flat view with At-station/Haul/To-buy columns + the picker include.
- `build_list.py`: `load()` injects global `build_station`; `set_build_station(id)` sets it; `add_target` upserts.
- `plan.classify`'s `_owned` already sums across ALL locations when `build_station is None` — so a target with no station nets "owned anywhere" automatically.
- `esi.get_cached_asset_index(p, entity_id, is_corp)` → FLAT `{type_id: qty}` (owned anywhere) — for Materials.

---

## Task 1: Pure pieces — Target.build_station + owned-anywhere totals helper

**Files:**
- Modify: `plan.py`
- Test: `tests/test_plan.py`

**Step 1: Add failing tests** to `tests/test_plan.py`:

```python
def test_target_has_build_station_field_default_none():
    t = plan.Target(type_id=1, name="X", blueprint_type_id=2, needed=1, children=[])
    assert t.build_station is None


def test_target_build_station_settable():
    t = plan.Target(type_id=1, name="X", blueprint_type_id=2, needed=1, children=[],
                    build_station=60003760)
    assert t.build_station == 60003760


def test_attach_owned_totals_nets_owned_anywhere_and_volumes():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 5000},
            {"type_id": 35, "name": "Pyerite", "quantity": 1000}]
    owned = {34: 2000}                       # flat owned-anywhere index
    volumes = {34: 0.01, 35: 0.01}
    out = plan.attach_owned_totals(rows, owned, volumes)
    trit = next(r for r in out if r["type_id"] == 34)
    assert trit["total"] == 5000
    assert trit["owned"] == 2000
    assert trit["to_buy"] == 3000
    assert trit["total_volume"] == 50.0
    pyer = next(r for r in out if r["type_id"] == 35)
    assert pyer["to_buy"] == 1000            # nothing owned


def test_attach_owned_totals_never_negative_and_nonmutating():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    out = plan.attach_owned_totals(rows, {34: 999}, {34: 0.01})
    assert out[0]["to_buy"] == 0
    assert "total" not in rows[0]            # input untouched
```

**Step 2:** Run `python -m pytest tests/test_plan.py -k "target_has_build_station or target_build_station or attach_owned_totals" -v` → FAIL.

**Step 3: Implement** in `plan.py`:
- Add a field to the `Target` dataclass: `build_station: int | None = None` (place it last so existing positional constructions still work).
- Add the helper (it's the retired `attach_supply_columns`, re-added for the explicitly station-less Materials view):
```python
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
```

**Step 4:** Run the targeted tests → PASS; `python -m pytest -q` → green.

**Step 5: Commit:**
```bash
git add plan.py tests/test_plan.py
git commit -m "feat: Target.build_station field + attach_owned_totals helper"
```

---

## Task 2: build_list.py — per-target station

**Files:**
- Modify: `build_list.py`
- Test: `tests/test_build_list.py`

**Step 1: Add failing tests** to `tests/test_build_list.py`:

```python
def test_add_target_defaults_build_station_none(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    t = build_list.load(path)["targets"][0]
    assert t["build_station"] is None


def test_set_target_station_sets_only_that_target(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.add_target({"type_id": 17636, "name": "Phoenix", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.set_target_station(671, 60003760, path)
    targets = {t["type_id"]: t for t in build_list.load(path)["targets"]}
    assert targets[671]["build_station"] == 60003760
    assert targets[17636]["build_station"] is None


def test_set_target_station_clear(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.set_target_station(671, 60003760, path)
    build_list.set_target_station(671, None, path)
    assert build_list.load(path)["targets"][0]["build_station"] is None


def test_legacy_global_build_station_ignored(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('{"targets": [{"type_id": 671, "name": "Rev"}], '
                    '"buy_set": [], "build_station": 60003760}', encoding="utf-8")
    data = build_list.load(path)
    # old global value no longer surfaces; the target has no station
    assert data["targets"][0].get("build_station") is None
```

**Step 2:** Run `python -m pytest tests/test_build_list.py -k "target_station or defaults_build_station or legacy_global" -v` → FAIL.

**Step 3: Edit `build_list.py`:**
- In `add_target`, ensure stored targets include `"build_station": None` if absent: before saving, do `target.setdefault("build_station", None)`.
- In `load()`, after loading, normalize each target: `for t in data["targets"]: t.setdefault("build_station", None)`. Stop adding a top-level `build_station` (remove the `data.setdefault("build_station", None)` line). A legacy top-level value is simply not used (leave it in the dict harmlessly, or `data.pop("build_station", None)` — popping is cleaner; do that).
- Replace `set_build_station` with:
```python
def set_target_station(type_id: int, station_id: int | None, path=DEFAULT_PATH) -> None:
    """Set (or clear, with None) the build station for one target."""
    data = load(path)
    for t in data["targets"]:
        if t["type_id"] == type_id:
            t["build_station"] = station_id
            break
    save(data, path)
```
KEEP backward-compat: other modules still import `set_build_station`. Grep for `set_build_station` usages: the only caller is the `/build-station` route (updated in Task 3) and possibly tests. To keep the suite green at THIS commit, leave a thin shim `set_build_station = None`? No — instead, do NOT delete `set_build_station` yet; leave the old function in place (it writes a now-ignored top-level key) so nothing breaks, and remove it in Task 6 cleanup. Add `set_target_station` alongside. Update the build_list tests that asserted the old global default shape (e.g. `test_load_defaults_build_station_none`, `test_set_build_station_*`): since `load` no longer returns a top-level `build_station`, update/remove those assertions minimally (the global-station tests become per-target tests; delete the obsolete global ones).

**Step 4:** Run `python -m pytest tests/test_build_list.py -v` then `python -m pytest -q` → green (fix any test that asserted the old top-level `build_station`).

**Step 5: Commit:**
```bash
git add build_list.py tests/test_build_list.py
git commit -m "feat: per-target build_station in build_list (set_target_station)"
```

---

## Task 3: Plan grouped per product

**Files:**
- Modify: `app.py` (`_resolve_targets`, `_compute_plan`, `plan_view`, `api_plan`, `build_station` route)
- Modify: `templates/plan.html`, `templates/_station_picker.html`
- Test: `tests/test_app_plan.py`

**Step 1: `_resolve_targets`** — carry the station onto each Target: when constructing `plan.Target(...)`, add `build_station=t.get("build_station")`.

**Step 2: Rewrite `_compute_plan`** to return per-product blocks:
```python
def _compute_plan():
    """Compute the action plan as one block per product.

    Returns (blocks, authed, station_ctx). Each block:
      {type_id, name, needed, station, buckets}
    where buckets = {ready, in_progress, blocked, buy} classified against THAT
    product's station (None -> owned-anywhere). station_ctx = {"stations": [...]}
    is the shared list of station options for every picker.
    """
    sde = get_sde()
    data = build_list.load()
    buy_set = set(data["buy_set"])
    resolved = _resolve_targets(sde, data["targets"])

    loc_index, jobs, stations = {}, [], []
    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        corporation_id = session.get("corporation_id")
        if corporation_id:
            loc_index = esi.get_cached_location_asset_index(p, corporation_id, is_corp=True)
        else:
            loc_index = esi.get_cached_location_asset_index(p, character_id, is_corp=False)
        jobs = esi.fetch_industry_jobs(p, character_id)
        stations = _get_station_list(p, character_id, corporation_id)
        session["refresh_token"] = p.refresh_token

    blocks = []
    for t in resolved:
        graph = plan.merge_trees([t], buy_set)
        station = t.build_station
        buckets = plan.classify(graph, loc_index, jobs, station, buy_set)
        volumes = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
        buckets["buy"] = plan.enrich_buy(buckets["buy"], loc_index, station, volumes)
        loc_names = {}
        if p:
            elsewhere_ids = {lid for r in buckets["buy"] for lid in r.get("elsewhere", {})}
            loc_names = {lid: esi.get_cached_location_name(p, lid, "other")
                         for lid in elsewhere_ids}
        buckets["buy"] = plan.attach_haul_breakdown(buckets["buy"], loc_names)
        blocks.append({"type_id": t.type_id, "name": t.name, "needed": t.needed,
                       "station": station, "buckets": buckets})

    return blocks, bool(p), {"stations": stations}
```
Update `plan_view`: `blocks, authed, station_ctx = _compute_plan()` → render `plan.html` with `blocks`, `authed`, `station_ctx`.
Update `api_plan`: `blocks, _a, _s = _compute_plan(); return jsonify(blocks)`.

**Step 3: `/build-station` route → per-target:**
```python
@app.route("/build-station", methods=["POST"])
def build_station():
    """Persist the chosen build station for ONE product, then back to the plan."""
    try:
        type_id = int(request.form["type_id"])
        raw = request.form.get("station_id", "").strip()
        build_list.set_target_station(type_id, int(raw) if raw else None)
    except (KeyError, ValueError) as e:
        flash(f"Invalid input: {e}")
    return redirect(url_for("plan_view"))
```

**Step 4: Rewrite `templates/_station_picker.html`** to a per-product picker (receives `block` + `stations`):
```jinja
{% if stations %}
<form method="post" action="{{ url_for('build_station') }}" class="controls" style="margin:0 0 0.5rem;">
  <input type="hidden" name="type_id" value="{{ block.type_id }}">
  <label>Building at
    <select name="station_id" onchange="this.form.submit()">
      <option value="" {% if not block.station %}selected{% endif %}>&mdash; choose &mdash;</option>
      {% set ids = stations | map(attribute='id') | list %}
      {% for s in stations %}
        <option value="{{ s.id }}" {% if s.id == block.station %}selected{% endif %}>{{ s.name }}</option>
      {% endfor %}
      {% if block.station and block.station not in ids %}
        <option value="{{ block.station }}" selected>Saved station ({{ block.station }})</option>
      {% endif %}
    </select>
  </label>
  <noscript><button type="submit">Set</button></noscript>
</form>
{% endif %}
```

**Step 5: Rewrite `templates/plan.html`** to loop one block per product. For each `block` in `blocks`: render a product header (`{{ block.name }} &times; {{ block.needed | commas }}`), include the picker (`{% with block=block, stations=station_ctx.stations %}{% include "_station_picker.html" %}{% endwith %}`), then the four sub-sections (Ready / In progress / Blocked / Buy) reading `block.buckets.*`, each hidden when empty. Keep the existing column layouts and the `activity_names` map and the `commas`/`vol` filters. Keep the login banner (when `not authed`) and the empty-state (when `blocks` is empty). 
- **Refresh button:** change the JS to simply `location.reload()` (the page is now a variable number of blocks; in-place re-render is dropped). Replace the whole `{% block scripts %}` with a tiny handler: `document.getElementById('btn-refresh')?.addEventListener('click', () => location.reload());`. Remove the old row-builder JS.
- Section element `id`s that were unique (`sec-ready`, etc.) are no longer needed by JS; keep them out or suffix with `block.type_id` if you keep them for styling. The `#sec-ready` green-accent style: apply it via a class instead of an id so it can repeat (e.g. `.sec-ready`).

**Step 6: Tests** (`tests/test_app_plan.py`, stub `app.esi`): replace the old `_compute_plan` 4-tuple tests with block tests:
```python
def test_compute_plan_returns_one_block_per_product(monkeypatch, tmp_build_list):
    # seed two manufacturable targets; unauthed (no ESI) is fine for shape
    import build_list, app
    build_list.add_target({"type_id": <A>, "name": "A", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0})
    build_list.add_target({"type_id": <B>, "name": "B", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0})
    with app.app.test_request_context():
        blocks, authed, ctx = app._compute_plan()
    assert {b["type_id"] for b in blocks} == {<A>, <B>}
    assert all("buckets" in b for b in blocks)
```
(Use real manufacturable type_ids from the SDE, SDE-gated like the other route tests; if that's awkward, prefer a focused unit test of the per-block loop by monkeypatching `_resolve_targets` to return two `plan.Target`s and stubbing `app.esi`/`app.get_sde`. Pick whichever is stable.)
Also: `POST /build-station` with `type_id` + `station_id` updates only that target (assert via `build_list.load()`); GET `/plan` renders two product headers. Update/remove any test asserting the old 4-tuple or global picker.

**Step 7:** `python -c "import app"`; `python -m pytest -q` → green.

**Step 8: Commit:**
```bash
git add app.py templates/plan.html templates/_station_picker.html tests/test_app_plan.py
git commit -m "feat: action plan grouped per product, each with its own station"
```

---

## Task 4: Materials flat = aggregated BOM (owned-anywhere)

**Files:**
- Modify: `app.py` (`materials` route flat branch)
- Modify: `templates/materials.html`
- Test: `tests/test_app_plan.py` (+ remove obsolete consistency tests)

**Step 1: Rewrite the flat branch of `materials()`** to drop the station/haul/deficit and use the owned-anywhere helper:
```python
    if view == "flat":
        nodes = [child for t in resolved for child in t.children]
        flat = flatten_material_tree(nodes, buy_set)
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
        flat_rows = plan.attach_owned_totals(flat, owned_index, volumes)
    return render_template("materials.html", view=view, targets=resolved,
                           buy_set=buy_set, flat_rows=flat_rows, authed=authed,
                           has_targets=bool(targets),
                           character_name=session.get("character_name"))
```
(Remove `station_ctx`, `calculate_deficit`, `_get_station_list`, `resolve_build_station`, `get_cached_location_name`, and `attach_haul_breakdown` from this route. `get_cached_asset_index` is the FLAT index — confirm it's imported/available via `esi.`.)

**Step 2: Update `templates/materials.html` flat view:** remove the `{% include "_station_picker.html" %}` from the materials page (both views). Flat columns: **Item** (`r.name`), **Total required** (`r.total | commas`), **To buy** (`r.to_buy | commas`, shown when `authed`), **Volume** (`r.total_volume | vol`). When not authed, show only Item / Total required / Volume + the existing "log in" note (to-buy hidden). Remove At-station / Haul-from columns. Keep the empty-state and the Tree view untouched (but ensure the Tree view no longer includes the picker either).

**Step 3: Remove the now-obsolete consistency tests** in `tests/test_plan.py` / `tests/test_app_plan.py`: `test_materials_and_plan_share_buy_basis` and `test_plan_and_materials_agree_to_buy_with_assets_and_station` (plan is now per-product-per-station; materials is owned-anywhere — they intentionally differ). Add a small test: `/materials?view=flat` unauthed → 200, contains "Total required", does NOT contain "Haul from" or "At station".

**Step 4:** `python -c "import app"`; `python -m pytest -q` → green.

**Step 5: Commit:**
```bash
git add app.py templates/materials.html tests/test_plan.py tests/test_app_plan.py
git commit -m "feat: Materials flat = aggregated BOM with owned-anywhere to-buy"
```

---

## Task 5: CLI plan grouped per product

**Files:**
- Modify: `eve_inventory.py` (`cmd_plan`)

**Step 1:** Rewrite `cmd_plan`'s computation to loop per product (mirror `_compute_plan`): for each resolved target, `merge_trees([t])` → `classify(graph, loc_index, jobs, t.build_station, buy_set)` → `enrich_buy` → `attach_haul_breakdown`, and print a per-product block (header `Name x needed` + the Ready/Blocked/Buy sections with haul) honoring each target's saved station. `_resolve_targets` in the CLI must also set `build_station` from the target dict (mirror Task 3 Step 1). Drop the old single saved-station resolution. Keep ESI optional (unauthed → per-product owned-anywhere/blocked-buy). Update HELP wording: build station is per product, set on the web `/plan`.

**Step 2:** `python -c "import eve_inventory"`; manual: seed a temp `build_list.json` with two targets (one with a `build_station`, one without) and run `python eve_inventory.py plan` → prints two per-product blocks without error. Delete the temp file. `python -m pytest -q` → green.

**Step 3: Commit:**
```bash
git add eve_inventory.py
git commit -m "feat: CLI plan grouped per product, honoring each target's station"
```

---

## Task 6: Cleanup + docs + verification

**Files:**
- Modify: `plan.py` (remove unused `resolve_build_station`), `build_list.py` (remove unused `set_build_station`), `app.py` (remove any now-dead imports)
- Modify: `README.md`, `context.md`

**Steps:**
1. Grep the repo for `resolve_build_station` and `set_build_station`. After Tasks 3-5 they should have NO remaining callers in app/eve_inventory. Remove both functions and their now-orphaned tests (`test_resolve_build_station_*` in `tests/test_plan.py`). If any caller remains, STOP and fix it instead.
2. `README.md`: update the build-station section — station is now **per product** on the Plan (each product block has its own "Building at" picker, no global default), and the Materials flat view is an **aggregated bill of materials** (total required + to-buy own-anywhere + volume), with no station picker. Correct any wording that describes a single global station or the materials at-station/haul split.
3. `context.md`: note `Target.build_station`, `plan.attach_owned_totals` (and that `attach_supply_columns`/`resolve_build_station`/`set_build_station` were removed/replaced), `build_list.set_target_station`, the per-product `_compute_plan` (returns blocks) and `/build-station` (per-target), and the Materials-flat change. Update the test count (run `python -m pytest -q`).
4. Run `python -m pytest -q` (green) and `python -c "import app, eve_inventory"` (OK). Manual smoke: seed two targets at different stations, GET `/plan`, confirm two blocks each with its own picker; GET `/materials?view=flat`, confirm aggregated columns and no picker. Delete the temp build_list.json.

**Commit:**
```bash
git add plan.py build_list.py app.py README.md context.md tests/
git commit -m "chore: drop global-station code; document per-product stations"
```

---

## Notes for the executor

- **TDD** on the pure pieces (Task 1) and `build_list` (Task 2). The route/template restructure (Tasks 3-4) is verified by the Flask test client + running; the CLI (Task 5) by running.
- **DRY / engine unchanged:** `merge_trees`/`classify`/`enrich_buy`/`attach_haul_breakdown` are NOT modified — the feature calls them per product. The only new pure code is `attach_owned_totals` (Task 1).
- **YAGNI:** no cross-product owned-stock reservation; `buy_set` stays global; refresh is a plain reload (no per-block JS rebuild).
- **Keep the suite green at every commit.** Tasks 1-2 are additive (old global code still present); Tasks 3-5 switch the callers; Task 6 removes the now-dead global-station functions.
- Structure-name resolution (the "Structure <id>" issue) is a separate ESI-scope matter — NOT part of this plan.
