# SDE Source Switch & Location-Aware Hauling Plan

## Date
2026-02-23

## Summary

Three changes to the Sihcom Industry Tracker:

1. Switch from Fuzzwork CSV SDE to CCP's official YAML SDE via `noirsoldats/eve-sde-converter`
2. Add location-aware asset tracking and a hauling plan view to shopping lists
3. Clean up context.md: remove Go porting guide, update SDE references

## Approach

Sequential — SDE switch first (foundational), then location tracking on top.

---

## Part 1: SDE Source Switch

### Problem

The Fuzzwork CSV SDE is a community mirror of CCP's data. CCP now publishes the SDE directly as YAML, and `noirsoldats/eve-sde-converter` is an actively maintained Python 3.12+ tool that downloads and converts it to SQLite.

### Design

**Replace `setup_sde.py`** to use `eve-sde-converter` instead of downloading Fuzzwork CSVs.

The converter produces a SQLite database with identical table schemas to what we currently use:

| Table | Columns | Used by |
|-------|---------|---------|
| `invTypes` | typeID, typeName, volume, groupID, published, ... | Type lookups, search, volume |
| `industryActivityMaterials` | typeID, activityID, materialTypeID, quantity | Material requirements |
| `industryActivityProducts` | typeID, activityID, productTypeID, quantity | Blueprint/product resolution |
| `industryActivity` | typeID, activityID, time | Activity times |

**`sde.py` requires zero changes** — all SQL queries remain identical.

The converter also creates additional tables (dogma, universe, stations, skins, etc.) that are harmless and may be useful later.

### Changes

- `setup_sde.py` — rewrite to call `eve-sde-converter` instead of downloading Fuzzwork CSVs
- `requirements.txt` — add `eve-sde-converter` dependency (or vendor its core modules)
- `app.py` `ensure_sde_downloaded()` — update log messages from "Fuzzwork" to "CCP SDE"
- `sde.py` — update docstrings and error messages only (no logic changes)

### What stays the same

- All `sde.py` query logic
- Output path `data/sqlite-latest.sqlite`
- The `app.py` auto-download flow
- All web routes and templates

---

## Part 2: Location-Aware Hauling Plan

### Problem

`build_asset_index()` in `esi.py` flattens all assets to `{type_id: total_quantity}`, discarding `location_id`. Shopping lists show total inventory but not where it is. Users can't tell if materials are at their build station or scattered across the universe.

### Design

#### 2a. Location-aware asset index

New function in `esi.py`:

```python
def build_location_asset_index(assets: list[dict]) -> dict[int, dict[int, int]]:
    """Build {type_id: {location_id: quantity}} from asset list."""
```

- Preserves `location_id` from each ESI asset record
- The existing flat `build_asset_index()` stays for backward compatibility
- Cache the raw asset list so both index types can be built without re-fetching

#### 2b. Build station detection

Detect manufacturing stations from the user's industry jobs:

- `fetch_industry_jobs()` already returns `facility_id` per job
- New function: `get_manufacturing_stations(p, character_id)` — fetches jobs, extracts unique facility IDs, ranks by frequency (most-used first)
- Resolve names via existing `resolve_location_name()`
- Expose as a dropdown selector at the top of shopping list pages

#### 2c. Simple deficit view

When a build station is selected, each material shows:

1. **At build station** — quantity already at the selected location (ready to use)
2. **Elsewhere** — quantity at other locations, with location names and volume (needs hauling)
3. **Need to buy** — deficit after all owned inventory, with Jita cost and volume

Each group shows total m3 for hauling estimates.

#### 2d. Route changes

- Add `location` query parameter to `/shopping/<bp_id>` and `/chain/shopping/<bp_id>`
- When `location` is set, use `build_location_asset_index()` and render the deficit view
- When `location` is not set, shopping lists work exactly as they do now
- Build station selector populated from industry jobs, cached per session

### Changes

- `esi.py` — add `build_location_asset_index()`, `get_manufacturing_stations()`, update caching to preserve raw assets
- `app.py` — add location parameter to shopping routes, build station selector logic, deficit view data preparation
- `templates/shopping.html` — build station dropdown, three-group deficit layout
- `templates/chain_shopping.html` — same location-aware changes

### What stays the same

- All non-shopping routes (blueprint, chain, market, profit)
- The flat shopping list view (when no location is selected)
- All SDE logic
- ESI auth flow

---

## Part 3: Context & Documentation Cleanup

### Changes to `context.md`

- Replace "Fuzzwork SQLite SDE" references with "CCP official YAML SDE (via eve-sde-converter)"
- Update `setup_sde.py` description
- Remove the Go Porting Guide section (~100 lines)
- Replace with: "This Python app serves as a prototype and reference implementation. Corp mates building the consolidated Go-based industry tool can reference this codebase for domain logic, ME formulas, and chain resolution algorithms."
- Add `noirsoldats/eve-sde-converter` to Dependencies
- Update Potential Next Steps to include hauling plan, remove Go-specific framing

---

## Implementation Order

1. SDE switch (Part 1) — foundational, verify all existing features still work
2. Location-aware hauling (Part 2) — builds on stable SDE base
3. Context cleanup (Part 3) — documentation pass after code changes

## Dependencies

- `noirsoldats/eve-sde-converter` — Python 3.12+, PyYAML, downloads CCP YAML SDE
- No new dependencies for location tracking (uses existing ESI data)
