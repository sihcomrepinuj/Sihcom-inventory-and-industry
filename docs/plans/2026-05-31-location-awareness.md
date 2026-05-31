# Location Awareness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make "where to build" and "where my stuff is" visible and controllable — a persisted build-station picker on the Plan and Materials views, and a per-station haul-from breakdown in the buy list, with the Materials flat view unified onto the same location-aware deficit math.

**Architecture:** Persist a global `build_station` in `build_list.json`. Reuse the proven `/shopping` pattern (`hauling.calculate_deficit` → resolve `elsewhere` IDs to names via `esi.get_cached_location_name`). Two small pure helpers in `plan.py` (`resolve_build_station`, `attach_haul_breakdown`) keep logic testable; routes do the ESI fetches; templates render a shared station picker + a haul-from column.

**Tech Stack:** Python 3.12+, Flask, Jinja2, pytest, SQLite SDE. No new dependencies.

**Design doc:** `docs/plans/2026-05-31-location-awareness-design.md`

**Key existing code to reuse (read before using):**
- `hauling.calculate_deficit(needed, loc_index, build_station, volumes)` → rows with `type_id, name, quantity_needed, at_station, elsewhere {loc_id: qty}, elsewhere_volume, to_buy, to_buy_volume`.
- `esi.get_cached_location_asset_index(p, entity_id, is_corp=False)` → `{type_id: {loc_id: qty}}`.
- `esi.get_cached_location_name(p, loc_id, "other")` → station name (cached, falls back to a string).
- `app._get_station_list(p, character_id)` → `[{id, name}, ...]` ranked by use.
- The `/shopping` route (`app.py:~790-811`) is the reference implementation of deficit + name resolution.
- `_compute_plan` (`app.py:403-440`) returns `(buckets, targets, authed)`; its buy bucket already ran through `enrich_buy` → `calculate_deficit`, so buy rows already carry `elsewhere`.
- `materials()` route (`app.py:487-520`) currently uses the FLAT `get_cached_asset_index` + `plan.attach_supply_columns`.

---

## Task 1: Persist a global build_station in build_list.py

**Files:**
- Modify: `build_list.py`
- Test: `tests/test_build_list.py`

**Step 1: Add failing tests** to `tests/test_build_list.py`:

```python
def test_load_defaults_build_station_none(tmp_path):
    assert build_list.load(tmp_path / "nope.json")["build_station"] is None


def test_set_build_station_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    build_list.set_build_station(60003760, path)
    assert build_list.load(path)["build_station"] == 60003760


def test_set_build_station_preserves_targets_and_buyset(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.toggle_buy(2867, path)
    build_list.set_build_station(60003760, path)
    data = build_list.load(path)
    assert [t["type_id"] for t in data["targets"]] == [671]
    assert data["buy_set"] == [2867]
    assert data["build_station"] == 60003760


def test_load_legacy_dict_without_station_defaults_none(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('{"targets": [], "buy_set": []}', encoding="utf-8")
    assert build_list.load(path)["build_station"] is None
```

**Step 2:** Run `python -m pytest tests/test_build_list.py -v` → the 4 new tests FAIL (KeyError 'build_station' / no attribute set_build_station).

**Step 3: Edit `build_list.py`:**
- In `load()`, after the existing `setdefault` calls, add: `data.setdefault("build_station", None)`. Also update the bare-list branch to include it: `return {"targets": data, "buy_set": [], "build_station": None}`, and the missing-file return to `{"targets": [], "buy_set": [], "build_station": None}`.
- Add:
```python
def set_build_station(station_id, path=DEFAULT_PATH) -> None:
    """Persist the global build station (an int facility id, or None to clear)."""
    data = load(path)
    data["build_station"] = station_id
    save(data, path)
```

**Step 4:** Run `python -m pytest tests/test_build_list.py -v` → all pass. Run `python -m pytest -q` → green (existing build_list tests still pass; the round-trip test that compares whole dicts now includes build_station — verify `test_save_load_round_trip` still passes since it saves a dict that may lack build_station; if it now fails because load adds the key, update that test's expected dict to include `"build_station": None` OR have it compare targets/buy_set only. Adjust minimally.)

**Step 5: Commit:**
```bash
git add build_list.py tests/test_build_list.py
git commit -m "feat: persist global build_station in build_list.json"
```

---

## Task 2: Pure helpers in plan.py (resolve_build_station, attach_haul_breakdown)

**Files:**
- Modify: `plan.py`
- Test: `tests/test_plan.py`

**Step 1: Add failing tests** to `tests/test_plan.py`:

```python
def test_resolve_build_station_saved_wins():
    stations = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    assert plan.resolve_build_station(2, stations) == 2


def test_resolve_build_station_falls_back_to_most_used():
    stations = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    assert plan.resolve_build_station(None, stations) == 1   # most-used first


def test_resolve_build_station_none_when_no_stations():
    assert plan.resolve_build_station(None, []) is None


def test_attach_haul_breakdown_names_and_sorts():
    rows = [{"type_id": 34, "name": "Tritanium", "to_buy": 100,
             "elsewhere": {60003760: 200, 60008494: 500}}]
    names = {60003760: "Sotiyo", 60008494: "Athanor"}
    out = plan.attach_haul_breakdown(rows, names)
    assert out[0]["haul"] == [
        {"name": "Athanor", "qty": 500},   # sorted by qty desc
        {"name": "Sotiyo", "qty": 200},
    ]


def test_attach_haul_breakdown_missing_name_falls_back_to_id():
    rows = [{"type_id": 34, "name": "Tritanium", "elsewhere": {999: 10}}]
    out = plan.attach_haul_breakdown(rows, {})
    assert out[0]["haul"] == [{"name": "999", "qty": 10}]


def test_attach_haul_breakdown_empty_and_nonmutating():
    rows = [{"type_id": 34, "name": "Tritanium", "elsewhere": {}}]
    out = plan.attach_haul_breakdown(rows, {})
    assert out[0]["haul"] == []
    assert "haul" not in rows[0]    # original not mutated
```

**Step 2:** Run `python -m pytest tests/test_plan.py -k "resolve_build_station or attach_haul" -v` → FAIL.

**Step 3: Add to `plan.py`** (near the other pure helpers):

```python
def resolve_build_station(saved, stations):
    """Pick the effective build station.

    saved (int|None) wins if set; else the most-used station (stations[0]);
    else None. stations: [{"id":..., "name":...}] ranked by use.
    """
    if saved is not None:
        return saved
    if stations:
        return stations[0]["id"]
    return None


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
            key=lambda h: h["qty"], reverse=True,
        )
        out.append({**r, "haul": haul})
    return out
```

**Step 4:** Run `python -m pytest tests/test_plan.py -v` (all pass) and `python -m pytest -q` (green).

**Step 5: Commit:**
```bash
git add plan.py tests/test_plan.py
git commit -m "feat: resolve_build_station + attach_haul_breakdown pure helpers"
```

---

## Task 3: Station picker + haul-from in the Plan

**Files:**
- Modify: `app.py` (`_compute_plan`, `plan_view`, add `/build-station` route)
- Create: `templates/_station_picker.html` (shared partial)
- Modify: `templates/plan.html`
- Test: `tests/test_app_plan.py`

**Step 1: Update `_compute_plan`** (`app.py:403-440`) to resolve the saved station, build the station list, resolve haul names, and attach the haul breakdown. Change its return to include station context:

```python
def _compute_plan():
    sde = get_sde()
    data = build_list.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])
    graph = plan.merge_trees(_resolve_targets(sde, targets), buy_set)

    loc_index, jobs, build_station = {}, [], None
    stations, loc_names = [], {}
    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        corporation_id = session.get("corporation_id")
        if corporation_id:
            loc_index = esi.get_cached_location_asset_index(p, corporation_id, is_corp=True)
        else:
            loc_index = esi.get_cached_location_asset_index(p, character_id, is_corp=False)
        jobs = esi.fetch_industry_jobs(p, character_id)
        stations = _get_station_list(p, character_id)
        build_station = plan.resolve_build_station(data.get("build_station"), stations)
        session["refresh_token"] = p.refresh_token

    buckets = plan.classify(graph, loc_index, jobs, build_station, buy_set)
    volumes = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
    buckets["buy"] = plan.enrich_buy(buckets["buy"], loc_index, build_station, volumes)
    # Resolve haul-from station names and bake a display-ready `haul` list onto each buy row.
    if p:
        elsewhere_ids = {lid for r in buckets["buy"] for lid in r.get("elsewhere", {})}
        loc_names = {lid: esi.get_cached_location_name(p, lid, "other") for lid in elsewhere_ids}
    buckets["buy"] = plan.attach_haul_breakdown(buckets["buy"], loc_names)

    station_ctx = {"stations": stations, "selected": build_station}
    return buckets, targets, bool(p), station_ctx
```

Update `plan_view` to unpack the 4-tuple and pass `station_ctx`:
```python
@app.route("/plan")
def plan_view():
    buckets, targets, authed, station_ctx = _compute_plan()
    return render_template("plan.html", buckets=buckets, targets=targets, authed=authed,
                           station_ctx=station_ctx,
                           character_name=session.get("character_name"))
```
Update `api_plan` to unpack 4 values (it ignores the extras): `buckets, _t, _a, _s = _compute_plan()`.

**Step 2: Add the `/build-station` route** (near the other build-list routes):
```python
@app.route("/build-station", methods=["POST"])
def build_station():
    raw = request.form.get("station_id", "").strip()
    build_list.set_build_station(int(raw) if raw else None)
    nxt = request.form.get("next", "plan_view")
    # nxt is an endpoint name we control; default to the plan.
    target = nxt if nxt in ("plan_view", "materials") else "plan_view"
    if target == "materials":
        return redirect(url_for("materials", view=request.form.get("view", "flat")))
    return redirect(url_for("plan_view"))
```

**Step 3: Create `templates/_station_picker.html`** — a shared partial rendering the "Building at" `<select>`. It receives `station_ctx` and a `next` endpoint name + optional `view`:
```jinja
{% if station_ctx and station_ctx.stations %}
<form method="post" action="{{ url_for('build_station') }}" class="controls" style="margin:0 0 0.5rem;">
  <input type="hidden" name="next" value="{{ next }}">
  {% if view %}<input type="hidden" name="view" value="{{ view }}">{% endif %}
  <label>Building at
    <select name="station_id" onchange="this.form.submit()">
      {% set ids = station_ctx.stations | map(attribute='id') | list %}
      {% for s in station_ctx.stations %}
        <option value="{{ s.id }}" {% if s.id == station_ctx.selected %}selected{% endif %}>{{ s.name }}</option>
      {% endfor %}
      {% if station_ctx.selected and station_ctx.selected not in ids %}
        <option value="{{ station_ctx.selected }}" selected>Saved station ({{ station_ctx.selected }})</option>
      {% endif %}
    </select>
  </label>
  <noscript><button type="submit">Set</button></noscript>
</form>
{% endif %}
```
(`onchange` auto-submits; `<noscript>` keeps it usable without JS.)

**Step 4: Update `templates/plan.html`:**
- Near the top header, include the picker: `{% include "_station_picker.html" with context %}` after setting `{% set next = "plan_view" %}` (or pass via the include). Simplest: `{% with next="plan_view" %}{% include "_station_picker.html" %}{% endwith %}`.
- Buy table (`plan.html:121-140`): add a **Haul from** column between "To buy" and "At station". Header `<th>Haul from</th>`; cell renders `r.haul`:
  ```jinja
  <td>{% for h in r.haul %}{{ h.qty | commas }} @ {{ h.name }}{% if not loop.last %}, {% endif %}{% else %}&mdash;{% endfor %}</td>
  ```
- Update the JS buy-row builder (the `rowBuy`/refresh function, ~`plan.html:230`) to also render the `haul` cell from `r.haul` (a JSON-clean list of `{name, qty}`) — iterate and join `qty + ' @ ' + esc(name)`, falling back to "—". (Add a `// keep in sync with the Jinja buy row` note, matching the existing sync comment.)

**Step 5: Smoke tests** (`tests/test_app_plan.py`, SDE-gated; the station picker only shows when authed, which tests can't easily reach — so assert the unauthed path still renders 200 and the buy table has no "Haul from" station data; and assert `POST /build-station` persists):
```python
def test_build_station_route_persists(client, tmp_build_list):
    r = client.post("/build-station", data={"station_id": "60003760", "next": "plan_view"})
    assert r.status_code == 302
    import build_list
    assert build_list.load()["build_station"] == 60003760

def test_build_station_clear(client, tmp_build_list):
    client.post("/build-station", data={"station_id": "", "next": "plan_view"})
    import build_list
    assert build_list.load()["build_station"] is None
```
(Use the existing fixtures that isolate build_list to tmp_path. Confirm the fixture names; adapt.)

**Step 6:** `python -c "import app"`; `python -m pytest -q` green.

**Step 7: Commit:**
```bash
git add app.py templates/_station_picker.html templates/plan.html tests/test_app_plan.py
git commit -m "feat: build-station picker + haul-from breakdown in the plan"
```

---

## Task 4: Materials flat view — location-aware + consistent

**Files:**
- Modify: `app.py` (`materials` route)
- Modify: `templates/materials.html`
- Modify: `plan.py` + `tests/test_plan.py` (REMOVE `attach_supply_columns` + its 3 tests)
- Test: `tests/test_app_plan.py`

**Step 1: Rewrite the flat branch of `materials()`** to use the location-aware deficit path (mirroring `_compute_plan`/`/shopping`):
```python
@app.route("/materials")
def materials():
    view = request.args.get("view", "tree")
    sde = get_sde()
    data = build_list.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])
    resolved = _resolve_targets(sde, targets)

    flat_rows, authed, station_ctx = None, False, {"stations": [], "selected": None}
    if view == "flat":
        nodes = [child for t in resolved for child in t.children]
        flat = flatten_material_tree(nodes, buy_set)            # [{type_id,name,quantity}]
        volumes = sde.get_type_volumes([r["type_id"] for r in flat])
        loc_index, build_station, loc_names, stations = {}, None, {}, []
        p = get_authed_preston_from_session()
        if p:
            authed = True
            character_id = int(session["character_id"])
            corporation_id = session.get("corporation_id")
            if corporation_id:
                loc_index = esi.get_cached_location_asset_index(p, corporation_id, is_corp=True)
            else:
                loc_index = esi.get_cached_location_asset_index(p, character_id, is_corp=False)
            stations = _get_station_list(p, character_id)
            build_station = plan.resolve_build_station(data.get("build_station"), stations)
            session["refresh_token"] = p.refresh_token
        deficit = calculate_deficit(flat, loc_index, build_station, volumes)
        if p:
            elsewhere_ids = {lid for d in deficit for lid in d["elsewhere"]}
            loc_names = {lid: esi.get_cached_location_name(p, lid, "other") for lid in elsewhere_ids}
        rows = plan.attach_haul_breakdown(deficit, loc_names)
        for r in rows:                                          # add total_volume for the Volume column
            r["total_volume"] = r["quantity_needed"] * volumes.get(r["type_id"], 0.0)
        flat_rows = rows
        station_ctx = {"stations": stations, "selected": build_station}

    return render_template("materials.html", view=view, targets=resolved,
                           buy_set=buy_set, flat_rows=flat_rows, authed=authed,
                           station_ctx=station_ctx, has_targets=bool(targets),
                           character_name=session.get("character_name"))
```
Confirm `calculate_deficit` is imported in app.py (it's used by `/shopping`, so likely already imported — verify the `from hauling import ...` line).

**Step 2: Remove `plan.attach_supply_columns`** and its 3 tests in `tests/test_plan.py` (`test_attach_supply_columns_*`). Grep to confirm no other caller remains (only the materials route used it).

**Step 3: Update `templates/materials.html` flat table:**
- Include the station picker at the top of the flat view: `{% with next="materials", view="flat" %}{% include "_station_picker.html" %}{% endwith %}`.
- Columns: **Item** (`r.name`), **Total required** (`r.quantity_needed | commas`), **Volume** (`r.total_volume | vol`) — always. When `authed`, ALSO: **At station** (`r.at_station | commas`), **Haul from** (render `r.haul` like the plan's buy table), **To buy** (`r.to_buy | commas`). When not authed: keep the existing "Log in to see how much you still need to buy." note and show only Total required + Volume.
- Empty `flat_rows` → "Nothing to supply." (unchanged).

**Step 4: Tests** (`tests/test_app_plan.py`, SDE-gated):
- `/materials?view=flat` unauthed → 200, contains "Total required", NOT "Haul from"/"To buy".
- Consistency regression: with a seeded build list, the set of `to_buy` per type in the materials flat (unauthed: to_buy == total) equals the plan's buy `to_buy` for the same types. (Unauthed both have empty loc_index so both equal the gross need — assert they match. This pins that the two screens use the same path.)
- Confirm removing attach_supply_columns didn't break imports: `python -c "import app"`.

**Step 5:** `python -m pytest -q` green (note: total test count drops by 3 from removing attach_supply_columns tests, then rises with new ones).

**Step 6: Commit:**
```bash
git add app.py templates/materials.html plan.py tests/test_plan.py tests/test_app_plan.py
git commit -m "feat: location-aware Materials flat view; unify on calculate_deficit"
```

---

## Task 5: CLI honors saved build_station + prints haul-from

**Files:**
- Modify: `eve_inventory.py` (`cmd_plan`)

**Step 1:** In `cmd_plan`, where it currently derives the build station from `extract_manufacturing_stations(station_jobs)`, change to honor the saved choice first:
```python
saved = data.get("build_station")
stations = esi.extract_manufacturing_stations(station_jobs)  # list[int]
build_station = saved if saved is not None else (stations[0] if stations else None)
```
(`data = build_list.load()` already exists in cmd_plan; `data.get("build_station")` is now available.)

**Step 2:** After classify/enrich_buy, resolve haul names and print them in the BUY LIST section. For each buy row with `elsewhere`, resolve names via `esi.get_cached_location_name(p, lid, "other")` (only when authed `p` exists), and print e.g. `Haul: 500 @ Sotiyo, 200 @ Athanor` under/next to the row. Reuse `plan.attach_haul_breakdown(buy_rows, loc_names)` for the named list, then print `r["haul"]`. Keep the existing To Buy / Vol / At Station columns; add the haul info compactly. Match the existing print style.

**Step 3:** Add a one-line HELP note: the build station is set on the web `/plan` or `/materials` page; the CLI honors the saved choice.

**Step 4: Verify** by running (seed a temp build_list.json with a `build_station` and a target): `python eve_inventory.py plan` prints without error and shows the saved station's at-station vs haul split. Delete the temp file. `python -m pytest -q` green; `python -c "import eve_inventory"` OK.

**Step 5: Commit:**
```bash
git add eve_inventory.py
git commit -m "feat: CLI plan honors saved build station and prints haul-from"
```

---

## Task 6: Docs + final verification

**Files:**
- Modify: `README.md`, `context.md`

**Steps:**
1. `README.md`: document the build-station picker (visible, persisted, defaults to most-used) on the Plan and Materials pages, and the per-station **Haul from** breakdown in the buy list / materials flat (own-elsewhere vs buy). Note the Materials flat view is now location-aware and agrees with the plan. Remove/replace the earlier note that said flat nets "all stock regardless of location" — it is now at-station/haul/buy like the plan (correct the prior honesty note).
2. `context.md`: update the `build_list.json` shape to `{targets, buy_set, build_station}`; add `build_list.set_build_station`, `plan.resolve_build_station`, `plan.attach_haul_breakdown`; note `plan.attach_supply_columns` was REMOVED (materials flat now uses `calculate_deficit`); add the `/build-station` route and `_station_picker.html`; update the test count.
3. Run `python -m pytest -q` (green).
4. Manual smoke (seed temp build_list.json with two targets sharing a component + a `build_station`): open `/plan` and `/materials?view=flat` mentally via the test client or note they render; confirm the buy list shows a Haul from column and the picker reflects the saved station. Delete the temp file; confirm no stray build_list.json staged.

**Commit:**
```bash
git add README.md context.md
git commit -m "docs: document build-station picker and haul-from location awareness"
```

---

## Notes for the executor

- **TDD strictly** on `build_list.set_build_station` (Task 1) and the `plan.py` pure helpers (Task 2). Routes/templates/CLI are verified by the Flask test client + running.
- **DRY:** the `/shopping` route is your reference for `calculate_deficit` + name resolution — mirror it, don't reinvent. The station picker is a single shared partial used by both plan.html and materials.html. The `haul` list is produced by one helper (`attach_haul_breakdown`) used by plan, materials, and CLI.
- **YAGNI:** no manual station entry, no per-target stations, no hauling optimization. One global build_station.
- **Consistency is the point:** after Task 4, the Plan buy list and Materials flat "to buy" use the same `calculate_deficit` path against the same station — keep them that way.
- **Engine functions are the source of truth** — if a signature differs from this plan's paraphrase, adapt the call.
- Keep the suite green at every commit.
