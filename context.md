# Sihcom Industry & Inventory Tracker — Project Context

## Owner

Neeraj — restaurant owner, EVE Online player (Black Omega Security corp), capital ship PvP (Revelation dreadnought). This tool supports his industry activities in EVE.

## Purpose

CLI + web tool for tracking EVE Online industry operations: assets, blueprints, manufacturing jobs, material requirements, logistics volume, and profit analysis. Designed to answer questions like "what do I need to build 5 Drakes at ME 10?", "do I have the materials on hand?", "how much cargo space do I need to haul them?", and "is this build profitable?"

## Project Location

```
C:\Users\Neeraj\Documents\Sihcom Inventory and Industry\
```

## Architecture

Three-module design separating concerns, plus a Flask web interface:

```
eve_inventory.py    CLI entry point, display logic, command routing
app.py              Flask web interface (deployable to Railway)
    ├── sde.py      Offline blueprint/material data (CCP YAML SDE via eve-sde-converter)
    ├── esi.py      Online character data (Preston library -> ESI API)
    └── hauling.py  Deficit calculation for location-aware shopping lists
setup_sde.py        SDE download/conversion script (CCP YAML -> SQLite)
templates/          Jinja2 templates for the web interface
tests/              pytest test suite
```

### sde.py — Static Data (offline)
- Wraps the **CCP YAML SDE** (converted to SQLite via eve-sde-converter, stored at `data/sqlite-latest.sqlite`)
- Provides: type name lookups, blueprint<->product resolution, manufacturing material requirements, activity times, invention data, **item volumes**
- Key SDE tables: `invTypes`, `industryActivityMaterials`, `industryActivityProducts`, `industryActivity`
- Contains ME calculation logic: `apply_me()` and `calculate_materials()` implementing the post-Crius formula
- `calculate_materials()` returns per-material `volume` (unit m3) and `total_volume` (quantity * unit volume)
- `get_type_volumes(type_ids)` — batch volume lookup for aggregated/flattened material lists
- `get_manufacturing_materials()` and `get_activity_materials()` include `volume` from `invTypes` in query results
- Material chain resolution: `resolve_material_chain()` recursively resolves sub-components; `flatten_material_tree()` aggregates to a shopping list
- `MaterialNode` dataclass represents tree nodes (type_id, name, quantity, activity, children)
- Blueprint material data is **not available via ESI** — the SDE is required
- Used as a context manager: `with SDE() as sde:`
- Zero network dependencies; works entirely from the local SQLite file

### esi.py — ESI API (online, authenticated)
- Uses the **Preston** library for EVE SSO + ESI REST calls
- Handles: SSO auth flow (local HTTP callback server on port 8888), token persistence/refresh
- Fetches: character assets (paginated), blueprints (paginated), industry jobs
- Market data: `get_bulk_market_data()` for Jita prices, `get_type_market_data()` for detailed order book
- Asset caching: `get_cached_asset_index()` for corp/personal asset lookups (TTL-based in-memory cache)
- Concurrent market fetching with `ThreadPoolExecutor` (10 workers)
- Location name resolution (stations, structures, solar systems)
- Config in `config.json`, tokens in `tokens.json`
- Location-aware asset indexing: `build_location_asset_index()` returns `{type_id: {location_id: quantity}}`
- Manufacturing station detection: `extract_manufacturing_stations()` ranks build stations from industry jobs
- Raw asset caching: `_get_cached_raw_assets()` stores raw ESI data, builds flat or location-aware indexes on demand

### hauling.py — Deficit Calculation
- Pure function `calculate_deficit()` computes per-material breakdown: at_station, elsewhere (needs hauling), to_buy
- Inputs: needed materials list, location-aware asset index, build station ID, type volumes
- Returns volume calculations for both haul and buy quantities
- Used by shopping and chain shopping routes when a build station is selected

### eve_inventory.py — CLI
- Two tiers of commands:
  - **SDE-only** (no auth): `search`, `materials`, `detail`, `mecomp`, `prices`, `chain`
  - **Authenticated** (SDE + ESI): `auth`, `assets`, `blueprints`, `jobs`, `shop`, `profit`, `summary`
- Environment variables:
  - `STRUCTURE_BONUS` — engineering complex material reduction %
  - `MARKET_REGION` — market price region (default: 10000002 = The Forge/Jita)
  - `BROKER_FEE` — broker fee % (default: 1.5)
  - `SALES_TAX` — sales tax % (default: 3.6)
  - `MATERIAL_COST_PCT` — cost basis for owned materials as % of Jita sell (default: 100)
- Interactive blueprint picker when search returns multiple matches
- All material tables include **volume columns** (total m3 for materials, buy volume for shopping)

### app.py — Flask Web Interface
- Reuses sde.py and esi.py for all data operations
- Routes: `/` (search), `/blueprint/<id>` (materials), `/chain/<id>` (full chain), `/shopping/<id>` (shopping list), `/chain/shopping/<id>` (chain shopping), `/market/<id>` (prices), `/profit/<id>` (profit analysis)
- `/api/stations` — returns user's manufacturing stations ranked by usage
- JSON API endpoints: `/api/materials/<id>`, `/api/chain/<id>`, `/api/profit/<id>` — used for live recalculation via JS
- `_compute_profit()` — shared helper for profit route and API (material cost split, revenue, margins, ISK/hr)
- Chain tree caching for performance (SDE data is static)
- ESI SSO via `/login`, `/callback`, `/logout`
- Interactive build/buy toggles on chain page (JS-driven shopping list updates)
- Jinja2 filters: `isk` (ISK formatting), `ftime` (time), `commas` (number formatting), `vol` (volume m3)
- All material/shopping tables include volume data
- Build Station selector on shopping lists enables location-aware hauling view (materials at station vs haul vs buy)

### setup_sde.py — SDE Bootstrap
- Downloads CCP's official YAML SDE from `developers.eveonline.com`
- Uses **eve-sde-converter** (git submodule at `tools/eve-sde-converter/`) to convert YAML to SQLite
- Installs result to `data/sqlite-latest.sqlite`
- Verifies key tables exist and have data
- Should be re-run after major EVE patches

## Dependencies

- **flask** — Web interface
- **preston** — Python ESI client (SSO, authenticated/unauthenticated API calls)
- **requests** — Used by `setup_sde.py` for SDE download and by `esi.py` for market data
- **gunicorn** — Production WSGI server (Railway deployment)
- **pytest** — Test framework
- **eve-sde-converter** — Git submodule (`tools/eve-sde-converter/`) for CCP YAML SDE to SQLite conversion
- Python 3.12+ (eve-sde-converter requirement)
- Standard library: `sqlite3`, `math`, `json`, `http.server`, `collections`, `concurrent.futures`, `dataclasses`

## Key Design Decisions

1. **CCP YAML SDE via eve-sde-converter**: Blueprint material recipes (`industryActivityMaterials`) aren't in ESI. We use CCP's official YAML SDE, converted to SQLite via `noirsoldats/eve-sde-converter` (a maintained Python tool). This produces identical relational tables to the former Fuzzwork SQLite SDE. The converter is included as a git submodule at `tools/eve-sde-converter/`.

2. **SDE for type names instead of ESI**: Bulk `get_type_names()` via SQLite is instant vs ESI's `post_universe_names` which is rate-limited and requires network. ESI is only used for data that changes (assets, jobs, blueprints owned).

3. **Two-tier command structure**: Material lookups work without ESI credentials, lowering the barrier to entry. You can `mecomp revelation` immediately after downloading the SDE.

4. **ME formula**: `max(runs, ceil(round(runs * base * (1 - ME/100) * (1 - struct/100), 2)))`. The `round(..., 2)` eliminates floating-point artefacts before ceiling. The `max(runs, ...)` enforces the 1-per-run minimum.

5. **Volume from invTypes**: Packaged volume (`invTypes.volume`) is fetched alongside material queries. `calculate_materials()` attaches `volume` and `total_volume` per material. For chain/flatten flows where materials are aggregated, `get_type_volumes()` provides a batch lookup. This enables logistics planning (hauling estimates) alongside cost analysis.

6. **Profit cost split**: Owned materials are costed at a configurable % of Jita sell (`MATERIAL_COST_PCT`), so you can model "I bought these at 90% Jita". Materials you don't have are costed at full Jita sell. Revenue shows both sell order (broker + tax) and instant sell (tax only) scenarios.

7. **Location-aware hauling**: Raw asset caching (`_raw_asset_cache`) stores the full ESI asset list, then builds either flat (`{type_id: qty}`) or location-aware (`{type_id: {location_id: qty}}`) indexes on demand. Build stations are detected from industry jobs (manufacturing + reactions, ranked by frequency). The deficit calculator (`hauling.py`) is a pure function with no ESI/SDE dependencies, making it easy to test.

## ESI Scopes Used

```
esi-assets.read_assets.v1
esi-characters.read_blueprints.v1
esi-industry.read_character_jobs.v1
esi-markets.structure_markets.v1
esi-universe.read_structures.v1
esi-characters.read_corporation_roles.v1
esi-assets.read_corporation_assets.v1
esi-corporations.read_blueprints.v1
esi-industry.read_corporation_jobs.v1
esi-markets.read_character_orders.v1
esi-markets.read_corporation_orders.v1
```

Corp-level scopes are included and corp asset lookups are implemented (used by shopping lists and profit analysis with source toggle).

## Files Generated at Runtime

| File | Contents | Sensitive? |
|------|----------|-----------|
| `config.json` | ESI client_id, client_secret, callback_url | Yes |
| `tokens.json` | ESI refresh token | Yes |
| `data/sqlite-latest.sqlite` | CCP YAML SDE converted to SQLite (~400 MB) | No |

## Web Templates

| Template | Purpose |
|----------|---------|
| `base.html` | Layout shell (Pico CSS, nav, flash messages) |
| `index.html` | Search page |
| `blueprint.html` | Material requirements with live ME recalc (JS), links to chain/shopping/profit |
| `chain.html` | Full material chain with tree/flat views and build/buy toggles |
| `shopping.html` | Shopping list vs character/corp assets |
| `chain_shopping.html` | Chain-resolved shopping list vs assets |
| `market.html` | Market price detail (buy/sell/spread/volume) |
| `profit.html` | Profit analysis: material cost split, revenue breakdown, margin/ISK-hr |

## Profit Calculation Reference

The profit calculator compares material cost against product sell price. Key formulas:

```
# Revenue after fees (per unit)
net_sell = sell_min * (1 - broker_rate - tax_rate)    # sell order: broker + tax
net_buy  = buy_max  * (1 - tax_rate)                  # instant sell: tax only

# Material cost (per line item)
owned_qty   = min(have, needed)
buy_qty     = max(0, needed - have)
owned_cost  = owned_qty * jita_sell * (MATERIAL_COST_PCT / 100)
buy_cost    = buy_qty * jita_sell
line_cost   = owned_cost + buy_cost

# Multi-output blueprints (ammo = 100/run, ships = 1/run)
total_product_qty = qty_per_run * runs  # from industryActivityProducts.quantity

# Profit and margin
profit = revenue - material_cost
margin = profit / revenue * 100
isk_hr = profit / (base_time_seconds / 3600 * runs)
```

## Test Suite

32 tests across 3 test files, run with `python -m pytest tests/ -v`:

| File | Tests | What It Covers |
|------|-------|---------------|
| `tests/test_sde.py` | 16 | Schema validation, type lookups, blueprint resolution, materials, ME calculation (pure functions), chain resolution |
| `tests/test_esi.py` | 10 | `build_asset_index` (flat), `build_location_asset_index` (per-location), `extract_manufacturing_stations` (frequency ranking) |
| `tests/test_hauling.py` | 6 | `calculate_deficit`: all-at-station, split locations, nothing owned, volumes, multiple elsewhere, excess inventory |

SDE tests require `data/sqlite-latest.sqlite` (skip gracefully if missing). ESI and hauling tests are pure-function tests with no database or network dependencies.

## Go Reference

This Python app serves as a prototype and reference implementation. Corp mates building the consolidated Go-based industry tool can reference this codebase for domain logic, ME formulas, and chain resolution algorithms.

## Uncommitted Work-in-Progress

These changes exist in the working tree from a prior session and are **not yet committed**:

| File | Change | Status |
|------|--------|--------|
| `eve_inventory.py` | Volume columns added to CLI `materials` and `chain` commands | Modified |
| `templates/blueprint.html` | Volume column, profit link, JS live-update for volumes | Modified |
| `templates/chain.html` | Volume column in raw materials table | Modified |
| `templates/profit.html` | Full profit analysis template (222 lines) | New/untracked |

These should be reviewed and committed. See `docs/plans/2026-02-24-handoff-and-next-steps.md` Task 1.

## Known Code Quality Issues

Identified during the hauling plan implementation code review (see plan doc Tasks 2-3 for fixes):

1. **DRY violation**: Station-fetching logic duplicated in `api_stations()`, `shopping()`, and `chain_shopping()` in `app.py` — extract to `_get_station_list()` helper
2. **Sequential ESI calls**: Station name resolution makes up to 10 HTTP requests per page load — add `get_cached_location_name()` with TTL cache
3. **Silent exceptions**: Station list fetch uses bare `except Exception: pass` — add `logger.debug()` for debuggability

## Potential Next Steps

- **Hauling view: station names**: Resolve location IDs to human-readable names in the hauling deficit "elsewhere" column (see plan doc Task 4)
- **Job installation cost**: Add SDE-based job cost index to profit calculations (currently material-only)
- **CSV/Excel export**: Dump shopping lists or profit analyses to spreadsheet
- **Multi-blueprint profit comparison**: Compare profitability across multiple items side-by-side
- **Invention calculator**: Success probability, expected cost per successful invention
- **Job scheduler**: Track industry slot usage and optimal job timing
- **Notification system**: Alert when jobs complete
- **PI integration**: Planetary interaction material tracking
