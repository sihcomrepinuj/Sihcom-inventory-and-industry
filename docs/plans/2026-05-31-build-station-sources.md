# Build Stations from Blueprint Locations — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the character-job-history station picker with one built from where you hold blueprints (character + corp), ranked by blueprint count, cached, with a job-history fallback.

**Architecture:** Add a pure `extract_blueprint_stations` ranker and a TTL-cached `get_cached_blueprints` fetch to `esi.py` (mirroring the existing asset cache). Rewrite `app._get_station_list` to gather character + corp blueprints, rank/resolve/cap them, and fall back to job history on empty/error. The function's return contract (`[{id, name}]`) is unchanged, so every downstream consumer (picker, `resolve_build_station`, plan, materials, `/build-station`) is untouched.

**Tech Stack:** Python 3.12+, Flask, pytest, Preston (ESI). No new dependencies.

**Design doc:** `docs/plans/2026-05-31-build-station-sources-design.md`

**Existing code to reuse (read before using):**
- `esi.extract_manufacturing_stations(jobs)` (esi.py:213) — Counter/`most_common` ranker; the model for the new helper. Tests at `tests/test_esi.py:69-106`.
- `esi.fetch_blueprints(p, character_id)` (esi.py:238) and `esi.fetch_corp_blueprints(p, corporation_id)` (esi.py:323) — paginated list of blueprint dicts (each has `location_id`).
- Asset cache pattern (esi.py:440-441, 554-573): `_raw_asset_cache: dict[int, tuple[float, list[dict]]]`, `ASSET_CACHE_TTL = 600`, `_get_cached_raw_assets` using `_time.monotonic()`. (`_time` is already imported.)
- `esi.get_cached_location_name(p, loc_id, "other")` (esi.py:604) — cached name resolution.
- `app._get_station_list(p, character_id)` (app.py:123-139) — the function being rewritten; callers: `_compute_plan` (app.py:~433), `materials` (app.py:~562), and the legacy `shopping`/`profit` routes (app.py:~811, ~1029).

---

## Task 1: `extract_blueprint_stations` pure ranker

**Files:**
- Modify: `esi.py`
- Test: `tests/test_esi.py`

**Step 1: Add failing tests** to `tests/test_esi.py` (match the `extract_manufacturing_stations` test style; add the import alongside the existing one):

```python
from esi import extract_blueprint_stations


def test_extract_blueprint_stations_ranks_by_count():
    """Unique blueprint locations, ranked by how many blueprints sit there."""
    bps = [
        {"type_id": 1, "location_id": 1000},
        {"type_id": 2, "location_id": 1000},
        {"type_id": 3, "location_id": 1000},
        {"type_id": 4, "location_id": 2000},
        {"type_id": 5, "location_id": 2000},
        {"type_id": 6, "location_id": 3000},
    ]
    stations = extract_blueprint_stations(bps)
    assert stations == [1000, 2000, 3000]   # 3, 2, 1 by count desc


def test_extract_blueprint_stations_empty():
    assert extract_blueprint_stations([]) == []


def test_extract_blueprint_stations_ignores_missing_location():
    bps = [{"type_id": 1}, {"type_id": 2, "location_id": 0}, {"type_id": 3, "location_id": 5000}]
    # missing key and falsy 0 are skipped; only 5000 remains
    assert extract_blueprint_stations(bps) == [5000]
```

**Step 2:** Run `python -m pytest tests/test_esi.py -k extract_blueprint -v` → FAIL (ImportError / not defined).

**Step 3: Implement** in `esi.py` (next to `extract_manufacturing_stations`):

```python
def extract_blueprint_stations(blueprints: list[dict]) -> list[int]:
    """Unique blueprint location IDs ranked by how many blueprints are at each.

    Blueprints are what you need on-site to install a manufacturing job, so their
    locations are the candidate build stations. Skips records with no usable
    location_id. Ranked most-blueprints-first (your main hub leads).
    """
    from collections import Counter

    counts: Counter = Counter()
    for bp in blueprints:
        lid = bp.get("location_id")
        if lid:
            counts[lid] += 1
    return [lid for lid, _ in counts.most_common()]
```

**Step 4:** Run `python -m pytest tests/test_esi.py -k extract_blueprint -v` → PASS; `python -m pytest -q` → green.

**Step 5: Commit:**
```bash
git add esi.py tests/test_esi.py
git commit -m "feat: extract_blueprint_stations ranks build sites by blueprint count"
```

---

## Task 2: `get_cached_blueprints` (TTL-cached fetch)

**Files:**
- Modify: `esi.py`
- Test: `tests/test_esi.py`

**Step 1: Add a failing cache test** to `tests/test_esi.py`:

```python
import esi as esi_mod


def test_get_cached_blueprints_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(p, character_id):
        calls["n"] += 1
        return [{"type_id": 1, "location_id": 1000}]

    esi_mod._raw_blueprint_cache.clear()
    monkeypatch.setattr(esi_mod, "fetch_blueprints", fake_fetch)

    a = esi_mod.get_cached_blueprints(None, 42, is_corp=False)
    b = esi_mod.get_cached_blueprints(None, 42, is_corp=False)
    assert a == b == [{"type_id": 1, "location_id": 1000}]
    assert calls["n"] == 1   # second call served from cache


def test_get_cached_blueprints_corp_uses_corp_fetch(monkeypatch):
    esi_mod._raw_blueprint_cache.clear()
    monkeypatch.setattr(esi_mod, "fetch_corp_blueprints",
                        lambda p, cid: [{"type_id": 9, "location_id": 7000}])
    out = esi_mod.get_cached_blueprints(None, 99, is_corp=True)
    assert out == [{"type_id": 9, "location_id": 7000}]
```

**Step 2:** Run `python -m pytest tests/test_esi.py -k get_cached_blueprints -v` → FAIL (no attribute).

**Step 3: Implement** in `esi.py` (near the asset cache, ~line 440 for the cache dict and near `_get_cached_raw_assets` for the function):

Add the module-level cache next to `_raw_asset_cache`:
```python
# {entity_id: (timestamp, blueprints)}
_raw_blueprint_cache: dict[int, tuple[float, list[dict]]] = {}
BLUEPRINT_CACHE_TTL = 600  # 10 minutes — mirrors ASSET_CACHE_TTL
```
And the cached accessor (mirror `_get_cached_raw_assets`):
```python
def get_cached_blueprints(
    p: Preston,
    entity_id: int,
    is_corp: bool = False,
) -> list[dict]:
    """Fetch and cache the raw blueprint list for a character or corporation."""
    now = _time.monotonic()
    if entity_id in _raw_blueprint_cache:
        ts, bps = _raw_blueprint_cache[entity_id]
        if now - ts < BLUEPRINT_CACHE_TTL:
            return bps
    bps = fetch_corp_blueprints(p, entity_id) if is_corp else fetch_blueprints(p, entity_id)
    _raw_blueprint_cache[entity_id] = (now, bps)
    return bps
```
NOTE: character and corp ids share one cache dict keyed by `entity_id`; that matches `_raw_asset_cache`'s behavior and EVE ids don't collide across char/corp. Keep it consistent.

**Step 4:** Run `python -m pytest tests/test_esi.py -k get_cached_blueprints -v` → PASS; `python -m pytest -q` → green.

**Step 5: Commit:**
```bash
git add esi.py tests/test_esi.py
git commit -m "feat: get_cached_blueprints TTL cache (mirrors asset cache)"
```

---

## Task 3: Rewrite `_get_station_list` to use blueprint locations

**Files:**
- Modify: `app.py` (`_get_station_list` + the two call sites in `_compute_plan` and `materials`)
- Test: `tests/test_app_plan.py`

**Step 1: Rewrite `_get_station_list`** (app.py:123-139) to take an optional `corporation_id`, build from blueprints, and fall back to job history:

```python
def _get_station_list(p: Preston, character_id: int,
                      corporation_id: int | None = None) -> list[dict]:
    """Build stations ranked by how many of your blueprints sit there.

    Uses character blueprints, plus corp blueprints when corporation_id is given,
    so the picker lists everywhere you could install a job. Falls back to
    manufacturing-job history when no blueprints are available. Returns up to 15
    stations as [{id, name}, ...]; empty list on total failure (picker is optional).
    """
    try:
        bps = esi.get_cached_blueprints(p, character_id, is_corp=False)
        if corporation_id:
            bps = bps + esi.get_cached_blueprints(p, corporation_id, is_corp=True)
        station_ids = esi.extract_blueprint_stations(bps)
        if not station_ids:
            # Fallback: where you've built before.
            jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
            station_ids = esi.extract_manufacturing_stations(jobs)
        stations = []
        for sid in station_ids[:15]:
            name = esi.get_cached_location_name(p, sid, "other")
            stations.append({"id": sid, "name": name})
        return stations
    except Exception:
        logger.debug("Could not fetch station list", exc_info=True)
        return []
```

**Step 2: Update the plan + materials call sites** to pass `corporation_id`:
- In `_compute_plan` (app.py:~433): `stations = _get_station_list(p, character_id, corporation_id)` (the local `corporation_id = session.get("corporation_id")` already exists just above).
- In `materials` (app.py:~562): `stations = _get_station_list(p, character_id, corporation_id)` (its `corporation_id = session.get("corporation_id")` is in scope in the `if p:` block).
- Leave the legacy `shopping`/`profit` callers as `_get_station_list(p, character_id)` — the new optional param defaults to None, so they keep working (now blueprint-based, char-only; the `[{id,name}]` contract is unchanged).

**Step 3: Add tests** to `tests/test_app_plan.py` (pure — all ESI stubbed via monkeypatch on `app.esi`; no network/SDE needed). Import `app` and use `monkeypatch.setattr`:

```python
def test_get_station_list_from_blueprints_ranked_and_named(monkeypatch):
    import app
    monkeypatch.setattr(app.esi, "get_cached_blueprints",
                        lambda p, eid, is_corp=False: (
                            [{"location_id": 1000}, {"location_id": 1000}, {"location_id": 2000}]
                            if not is_corp else [{"location_id": 2000}]))
    monkeypatch.setattr(app.esi, "get_cached_location_name",
                        lambda p, sid, t: f"Station {sid}")
    out = app._get_station_list(object(), 1, corporation_id=99)
    # char: 1000x2, 2000x1; corp adds 2000x1 -> 1000(2), 2000(2); tie -> insertion order
    ids = [s["id"] for s in out]
    assert set(ids) == {1000, 2000}
    assert out[0]["name"] == f"Station {ids[0]}"


def test_get_station_list_falls_back_to_jobs_when_no_blueprints(monkeypatch):
    import app
    monkeypatch.setattr(app.esi, "get_cached_blueprints", lambda p, eid, is_corp=False: [])
    monkeypatch.setattr(app.esi, "fetch_industry_jobs",
                        lambda p, cid, include_completed=False: [
                            {"activity_id": 1, "facility_id": 5000}])
    monkeypatch.setattr(app.esi, "get_cached_location_name", lambda p, sid, t: f"S{sid}")
    out = app._get_station_list(object(), 1, corporation_id=None)
    assert [s["id"] for s in out] == [5000]   # job-history fallback
```

**Step 4:** `python -c "import app"`; `python -m pytest -q` → green.

**Step 5: Commit:**
```bash
git add app.py tests/test_app_plan.py
git commit -m "feat: station picker lists blueprint locations (char + corp) with job fallback"
```

---

## Task 4: Docs + final verification

**Files:**
- Modify: `README.md`, `context.md`

**Steps:**
1. `README.md`: in the build-station section, document that the "Building at" picker now lists
   **every station where you hold blueprints** (character + corp), ranked by blueprint count,
   rather than only where you've built before — so you can build anywhere you have the BP. Note
   the fallback to job history when no blueprints are found, and that asset netting is unchanged
   (corp-when-in-corp).
2. `context.md`: note `esi.extract_blueprint_stations` and `esi.get_cached_blueprints` (+ the
   `_raw_blueprint_cache`/`BLUEPRINT_CACHE_TTL`), and that `_get_station_list` is now
   blueprint-based (char + corp) with a job-history fallback. Update the test count (run
   `python -m pytest -q` for the exact number).
3. Run `python -m pytest -q` → green; record the count for the docs.
4. Manual sanity: `python -c "import app, eve_inventory"` → OK. (The picker itself needs a live
   ESI login to populate, which can't be exercised offline — the unit tests cover the logic.)

**Commit:**
```bash
git add README.md context.md
git commit -m "docs: station picker now built from blueprint locations"
```

---

## Notes for the executor

- **TDD strictly** on `extract_blueprint_stations` (Task 1) and `get_cached_blueprints` (Task 2) — pure/cached, fully unit-testable. `_get_station_list` (Task 3) is testable by stubbing `app.esi.*` (no network).
- **DRY:** mirror `extract_manufacturing_stations` and `_get_cached_raw_assets` exactly — same Counter and same TTL-cache shape. Reuse `get_cached_location_name`.
- **YAGNI:** no structure-service detection, no ME/material filtering of stations, no asset-scope change. Just change where the station list comes from.
- **Contract unchanged:** `_get_station_list` still returns `[{id, name}]`, so the picker, `resolve_build_station`, plan, materials, and `/build-station` need no changes. Don't touch them.
- The CLI (`cmd_plan`) does not call `_get_station_list`; it honors the saved station and keeps its job-history fallback — leave it as-is (per design).
- Keep the suite green at every commit.
