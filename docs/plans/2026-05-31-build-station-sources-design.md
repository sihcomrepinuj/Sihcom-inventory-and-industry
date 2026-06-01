# Build Stations from Blueprint Locations — Design

**Date:** 2026-05-31
**Status:** Approved, ready for planning
**Author:** Neeraj (with Claude)

## Problem

The "Building at" station picker only lists **one location** for many users because it is built
from the wrong source:

- `_get_station_list` derives stations from `esi.fetch_industry_jobs(p, character_id)` — i.e.
  **the character's personal industry-job history only**. It ignores **corporation** industry
  jobs entirely (despite the corp scope and an unused `fetch_corp_industry_jobs`), and it ignores
  **where you actually hold blueprints/assets** — it only knows "where have I built before."

So a user with blueprints and materials at several stations sees just the one place they've
personally run jobs. (Asset *netting* already uses corp assets when in a corp; it's specifically
the selectable **build stations** that are stuck on personal job history.)

## Decisions (from brainstorming)

- **Station source:** the list of build stations = **where you hold blueprints** — character +
  corp BPOs/BPCs (the EVE-accurate "where I could install a job").
- **Asset scope:** unchanged — corp assets when in a corp, personal otherwise. (We are only
  broadening *where you can pick to build*, not what counts as on-hand.)
- **Approach A:** replace the job-history station detection with blueprint-location detection,
  with a job-history fallback; everything downstream is untouched.

## Section 1 — Station list from blueprint locations

`_get_station_list` reads **where your blueprints are** instead of job history:
- Fetch **character blueprints** (`esi.fetch_blueprints`) and, when a corp id is available,
  **corp blueprints** (`esi.fetch_corp_blueprints`) — merged.
- Each blueprint record carries `location_id`; count blueprints per location and **rank by count
  descending** (your main hub floats to the top and is the default when nothing is saved).
- Resolve each `location_id` to a name (`esi.get_cached_location_name`); cap at ~15.
- **Fallback:** if blueprint extraction yields nothing or errors, fall back to today's
  job-history list (`extract_manufacturing_stations`) — the dropdown never goes empty or breaks.

Net effect: the picker lists everywhere you could install a job, character *and* corp.

## Section 2 — Caching + a pure helper

- **Caching:** blueprint lists can be hundreds of entries and `_get_station_list` runs on every
  Plan/Materials load, so cache the blueprint fetches per entity with a TTL (the pattern
  `esi.py` already uses for assets via `_get_cached_raw_assets`). Avoids re-paginating ESI per
  page view.
- **Pure helper:** `esi.extract_blueprint_stations(blueprints) -> list[int]` — unique
  `location_id`s ranked by blueprint count, descending. Pure and unit-testable (mirrors
  `extract_manufacturing_stations`). ESI fetching/name-resolution stays in the caching/route layer.

## Section 3 — Integration (one function changes)

The change lands entirely inside `_get_station_list`; its contract stays identical
(`[{id, name}]` ranked), so every consumer is untouched:

```
fetch_blueprints(char) + fetch_corp_blueprints(corp)   [esi.py, exist]
      │  (cached per entity, TTL)
      ▼
extract_blueprint_stations(bps) → [location_id ranked by count]   [NEW pure]
      │  resolve names (get_cached_location_name)
      ▼
_get_station_list() → [{id, name}]   ← same shape as today
      │  (fallback to extract_manufacturing_stations on empty/error)
      ▼
unchanged: picker · resolve_build_station · _compute_plan · materials · /build-station
```

Asset netting (corp-when-in-corp), the haul-from breakdown, and the plan/materials consistency
all stay as shipped.

**CLI:** `cmd_plan` already honors the saved station first (set on the web), so the picker change
flows through. Its fallback when nothing is saved stays job-history-based — noted, not expanded.

## Section 4 — Edge cases & testing

**Edge cases:**
- No blueprints (new char) → job-history fallback; if also empty, no picker (`build_station=None`,
  "owned anywhere" netting) — same graceful degradation as today.
- Blueprint in a container/ship (`location_id` is a container, not a station) → resolves to a
  generic name and sinks by count; capped out in practice. Known minor limitation, not
  over-engineered away.
- Corp scope missing / corp fetch errors → character blueprints still drive the list; corp set is
  best-effort.
- Lost docking access to a structure → name resolver already returns `Structure <id>`; still
  selectable.

**Testing:**
- Pure (`tests/test_esi.py`, like the `extract_manufacturing_stations` tests):
  `extract_blueprint_stations` — ranks by count desc, dedups, handles empty, ignores missing
  `location_id`.
- Caching: the cached blueprint fetch is not re-called within the TTL (mirror asset-cache tests).
- Fallback: `_get_station_list` returns the job-history list when blueprint extraction is empty
  (stub the fetchers).
- Regression: existing station-picker route tests stay green; the `[{id,name}]` contract is
  unchanged so nothing downstream needs new tests.

## Scope / non-goals

- No structure-service detection (can't cheaply know if a citadel has a manufacturing module).
- No per-blueprint ME / material-availability filtering of stations.
- No change to asset scope (corp-when-in-corp stays).
- CLI fallback station source unchanged (it honors the web-saved station).
