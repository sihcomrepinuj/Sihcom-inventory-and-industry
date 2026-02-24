# Handoff & Next Steps — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Address uncommitted work-in-progress, fix known issues from code review, and implement the most impactful next-step features.

**Architecture:** This plan has three parts. Part 1 commits pre-existing uncommitted changes (volume columns, profit template). Part 2 fixes code quality issues identified during the SDE switch & hauling plan implementation. Part 3 implements the highest-priority next-step feature: resolving location IDs to human-readable station names in the hauling deficit view.

**Tech Stack:** Python 3.12+, SQLite, Flask, Preston (ESI), pytest

---

## Current State (as of 2026-02-24)

### What Was Completed

The SDE switch and location-aware hauling plan are fully implemented and pushed (`5c45ecb`):

- **SDE source** switched from Fuzzwork CSV to CCP's official YAML SDE via `noirsoldats/eve-sde-converter` (git submodule at `tools/eve-sde-converter/`)
- **Location-aware hauling plan** on both shopping and chain shopping pages — Build Station dropdown auto-populated from industry jobs, deficit table showing At Station / Haul / Buy with volumes
- **32 tests** passing (16 SDE, 10 ESI, 6 hauling)
- **context.md** fully updated

### Uncommitted Pre-Existing Changes

These changes exist in the working tree from a prior session and were NOT part of the SDE/hauling work:

| File | What Changed | Lines |
|------|-------------|-------|
| `eve_inventory.py` | Volume columns added to CLI `materials` and `chain` commands | +217/-16 |
| `templates/blueprint.html` | Volume column added to material table, profit link added, JS updated | +20/-4 |
| `templates/chain.html` | Volume column added to raw materials table | +19/-4 |
| `templates/profit.html` | **New file** (222 lines) — profit analysis template (untracked) | new |

### Known Issues from Code Review

During the hauling plan implementation, code reviewers identified these items:

1. **DRY violation**: Station-fetching logic duplicated between `api_stations()` and `shopping()`/`chain_shopping()` routes in `app.py`
2. **Sequential ESI calls**: Station name resolution does up to 10 sequential HTTP requests per page load (should batch or cache)
3. **Silent exception swallowing**: Station list fetch in shopping routes uses bare `except Exception: pass` without logging

---

## Part 1: Commit Pre-Existing Work

### Task 1: Review and commit volume column additions

**Files:**
- Review: `eve_inventory.py` (lines 133-170, 356-400)
- Review: `templates/blueprint.html`
- Review: `templates/chain.html`
- Review: `templates/profit.html` (new, 222 lines)

**Step 1: Review the unstaged changes**

Run: `git diff eve_inventory.py templates/blueprint.html templates/chain.html`

Verify:
- Volume columns are correctly added to CLI material tables
- Blueprint template has `Vol m³` column with JS live-update support
- Chain template has volume column in raw materials table
- No unintended changes mixed in

**Step 2: Review the new profit template**

Run: `python -c "from app import app; print('OK')"`

Read `templates/profit.html` and verify it matches the `profit()` route in `app.py` (line 777+).

**Step 3: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All 32 tests PASS

**Step 4: Commit**

```bash
git add eve_inventory.py templates/blueprint.html templates/chain.html templates/profit.html
git commit -m "Add volume columns to CLI and blueprint template, add profit template"
```

---

## Part 2: Code Quality Fixes

### Task 2: Extract shared station-fetching helper

**Files:**
- Modify: `app.py`

**Step 1: Write the helper function**

Add after the `get_cached_chain()` function (around line 57) in `app.py`:

```python
def _get_station_list(p: Preston, character_id: int) -> list[dict]:
    """Fetch manufacturing stations ranked by usage. Returns [{id, name}, ...]."""
    try:
        jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
        station_ids = esi.extract_manufacturing_stations(jobs)
        stations = []
        for sid in station_ids[:10]:
            name = esi.resolve_location_name(p, sid, "other")
            stations.append({"id": sid, "name": name})
        return stations
    except Exception:
        logger.debug("Could not fetch station list", exc_info=True)
        return []
```

**Step 2: Update `api_stations()` to use the helper**

Replace the body of `api_stations()` (around line 598):

```python
@app.route("/api/stations")
@login_required
def api_stations():
    """Return the user's manufacturing stations ranked by usage."""
    p = get_authed_preston_from_session()
    if not p:
        return jsonify(error="Session expired"), 401

    character_id = int(session["character_id"])
    stations = _get_station_list(p, character_id)

    session["refresh_token"] = p.refresh_token
    return jsonify(stations=stations)
```

**Step 3: Update `shopping()` to use the helper**

Replace the station-fetching block in `shopping()` (the `try/except` block around line 651-659):

```python
    station_list = _get_station_list(p, character_id)
```

**Step 4: Update `chain_shopping()` to use the helper**

Replace the station-fetching block in `chain_shopping()` (the `try/except` block around line 478-485):

```python
    station_list = _get_station_list(p, character_id)
```

**Step 5: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 6: Verify app imports**

Run: `python -c "from app import app; print('OK')"`

**Step 7: Commit**

```bash
git add app.py
git commit -m "Extract _get_station_list helper to DRY up station fetching"
```

---

### Task 3: Add station name caching

**Files:**
- Modify: `esi.py`

The station name resolution makes sequential ESI HTTP calls. Adding a simple TTL cache avoids re-resolving the same stations on every page load.

**Step 1: Add the cache**

Add after the `ASSET_CACHE_TTL` line (around line 441) in `esi.py`:

```python
# {location_id: (timestamp, name)}
_location_name_cache: dict[int, tuple[float, str]] = {}
LOCATION_NAME_CACHE_TTL = 3600  # 1 hour (station names rarely change)
```

**Step 2: Add a cached wrapper**

Add after `resolve_location_name()` (around line 394) in `esi.py`:

```python
def get_cached_location_name(
    p: Preston,
    location_id: int,
    location_type: str = "other",
) -> str:
    """Resolve location name with 1-hour TTL cache."""
    now = _time.monotonic()

    if location_id in _location_name_cache:
        ts, name = _location_name_cache[location_id]
        if now - ts < LOCATION_NAME_CACHE_TTL:
            return name

    name = resolve_location_name(p, location_id, location_type)
    _location_name_cache[location_id] = (now, name)
    return name
```

**Step 3: Update `_get_station_list()` in `app.py` to use the cached version**

In the `_get_station_list()` helper (from Task 2), replace:

```python
            name = esi.resolve_location_name(p, sid, "other")
```

With:

```python
            name = esi.get_cached_location_name(p, sid, "other")
```

**Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add esi.py app.py
git commit -m "Add location name caching to avoid repeated ESI lookups"
```

---

## Part 3: Hauling View Improvements

### Task 4: Show station names in hauling deficit "elsewhere" column

**Files:**
- Modify: `app.py` (shopping and chain_shopping routes)
- Modify: `templates/shopping.html`
- Modify: `templates/chain_shopping.html`

Currently the hauling plan "Haul (elsewhere)" column shows raw quantities like `(50, 30)` without identifying which locations they correspond to. This task resolves those location IDs to human-readable names.

**Step 1: Pass resolved location names to the template**

In the `shopping()` route in `app.py`, after the `calculate_deficit()` call (around line 649), add:

```python
        # Resolve location names for the deficit view
        if deficit_data:
            all_loc_ids = set()
            for d in deficit_data:
                all_loc_ids.update(d["elsewhere"].keys())
            loc_names = {}
            for lid in all_loc_ids:
                loc_names[lid] = esi.get_cached_location_name(p, lid, "other")
```

Then pass `loc_names` to the template:

```python
        loc_names=loc_names if deficit_data else {},
```

Do the same in `chain_shopping()`.

**Step 2: Update the hauling table templates**

In both `templates/shopping.html` and `templates/chain_shopping.html`, update the "Haul (elsewhere)" `<td>` to show location names:

Replace the inner loop:
```html
({% for loc_id, qty in d.elsewhere.items() %}{{ qty | commas }}{% if not loop.last %}, {% endif %}{% endfor %})
```

With:
```html
{% for loc_id, qty in d.elsewhere.items() %}
    <br><small>{{ loc_names.get(loc_id, loc_id) }}: {{ qty | commas }}</small>
{% endfor %}
```

**Step 3: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 4: Verify manually**

Run: `python app.py`
Navigate to a shopping list with a build station selected. Verify the "Haul" column shows station names instead of numeric IDs.

**Step 5: Commit**

```bash
git add app.py templates/shopping.html templates/chain_shopping.html
git commit -m "Show station names in hauling deficit elsewhere column"
```

---

### Task 5: Run full test suite and push

**Step 1: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

**Step 2: Push**

```bash
git push origin main
```
