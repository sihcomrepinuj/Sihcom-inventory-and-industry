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
    ├── hauling.py  Deficit calculation for location-aware shopping lists
    ├── plan.py     Pure classifier for the action plan (no Flask/ESI/DB)
    └── build_list.py  Build-list persistence (build_list.json: {targets, buy_set, build_station})
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
- `get_product_qty_per_run(blueprint_type_id)` — units of product yielded per manufacturing run (1 for ships, 100 for ammo; `None` if no manufacturing product). Drives the qty -> runs derivation in the action plan.
- `get_manufacturing_materials()` and `get_activity_materials()` include `volume` from `invTypes` in query results
- `search_manufacturable(name, limit=25)` — search **published types that have a manufacturing blueprint** (activityID=1), returning `[{type_id, name}]`. Backs the home-page search-to-add so only buildable items can be added as targets (raw materials/non-buildables are excluded).
- Material chain resolution: `resolve_material_chain()` recursively resolves sub-components; `flatten_material_tree(nodes, buy_set=None)` aggregates to a shopping list — when a `buy_set` is given, components in it are treated as leaves (bought, not exploded into their own inputs)
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

### plan.py — Action Plan Classifier (pure logic)
- **No Flask / ESI / DB imports** — takes already-resolved material trees plus plain index dicts and returns classified buckets. Tested like `hauling.py`.
- `Target` dataclass — one build-list target plus its resolved input tree (`needed` = product units = runs * qty_per_run).
- `merge_trees(targets)` — walks every target's material tree and accumulates a single `{type_id: ReqNode}` requirement graph, summing `total_needed` across targets so a shared component/raw is one node (not one per target).
- `classify(graph, loc_index, jobs, build_station, buy_set)` — buckets every still-needed node into **ready** / **in_progress** / **blocked** / **buy**. Buildable nodes with all inputs on hand are ready; those covered by an active/ready/paused job are in_progress; buildable-but-input-short nodes are blocked (with the specific missing inputs and shortfalls); non-buildable or `buy_set` nodes are buy. Input availability is evaluated at the aggregate level (per-branch reservation is a deliberate non-goal).
- `enrich_buy(buy_rows, loc_index, build_station, volumes)` — attaches the at_station / elsewhere / to_buy split (and volume) to buy rows by reusing `hauling.calculate_deficit`, so haul math lives in one place. Falls back to a buy-everything-not-owned view when no build station is set.
- `resolve_build_station(saved, stations)` — picks the effective build station: the `saved` id wins if set, else the most-used station (`stations[0]["id"]`), else `None`. Used by `/plan`, the `/materials` flat view, and (replicated inline) the CLI to honor a saved build-station choice with a most-used fallback.
- `attach_haul_breakdown(rows, loc_names)` — pure helper that adds a display-ready `haul` list (`[{name, qty}]`, sorted by qty desc) to each row from its `elsewhere` {station_id: qty} map, resolving ids via `loc_names` (falling back to `str(id)`). Returns new dicts; never mutates inputs. Backs the "Haul from" column on the plan buy list and the `/materials` flat view, and the CLI.
- `attach_supply_columns` was **REMOVED**. The `/materials` flat supply view is now location-aware: it runs `hauling.calculate_deficit` (the same call the plan buy list uses) for the at_station / elsewhere / to_buy split, then `attach_haul_breakdown` for the "Haul from" names — so the flat view and the action plan agree on what to buy.

### build_list.py — Build-List Persistence
- Stores the user's **target intent + global build/buy choices only** (never progress) in `build_list.json` next to the code.
- Shape: `{"targets": [...], "buy_set": [type_id, ...], "build_station": <int|None>}`. `load()` is backward-compatible — a legacy bare-list file loads as `{"targets": <list>, "buy_set": [], "build_station": None}`, and a dict file lacking `build_station` defaults it to `None`.
- `load()` / `save()` — read/write the JSON dict.
- `add_target(target)` — **upsert by type_id**: replaces an existing target in place, else appends, so re-adding the same product updates rather than duplicates.
- `remove_target(type_id)` — deletes all matching targets.
- `toggle_buy(type_id)` — flips a component's membership in the global `buy_set` (the single build-vs-buy choice that applies across the whole list).
- `set_build_station(station_id)` — persists the global build station (an int facility id, or `None` to clear and fall back to most-used).
- A target dict carries `type_id`, `name`, `qty` (the driver), optional `runs` override, `me`, `structure_bonus`. Build/buy and the build station are **global** (top-level `buy_set` / `build_station`), not per-target.

### eve_inventory.py — CLI
- Two tiers of commands:
  - **SDE-only** (no auth): `search`, `materials`, `detail`, `mecomp`, `prices`, `chain`
  - **Authenticated** (SDE + ESI): `auth`, `assets`, `blueprints`, `jobs`, `shop`, `profit`, `summary`
  - **Build list**: `plan` — reads `build_list.json`, resolves + merges every target's tree, classifies into ready/in-progress/blocked/buy, and prints the buckets. Reads the global `buy_set` (`set(data["buy_set"])`), so a component toggled "buy" on the web `/materials` view shows up as a **buy line** here. Honors the saved `build_station` (set on the web picker; saved-wins-else-most-used precedence replicated inline since `extract_manufacturing_stations` returns bare ints), printing "Building at" and a per-material "Haul from" breakdown on the buy list. There is **no** CLI command to set the station. Uses ESI inventory/jobs only if a token already exists (never forces the SSO flow); without it everything lands in blocked/buy.
  - The CLI has **no build/buy toggle UI** and no build-list materials command — build vs buy is set on the web `/materials` view, and the flat supply view (total required + to-buy) is web-only. `HELP` notes this. (The unrelated SDE-only `materials <name>` command is a single-blueprint material breakdown, not the build-list view.)
- `_resolve_targets()` turns build-list dicts into `plan.Target`s: looks up the blueprint, derives runs from `qty` via `get_product_qty_per_run` (qty is the driver; `runs` is an optional advanced override), and resolves the material chain.
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
- Routes: `/` (build list, with inline **search-to-add** via `sde.search_manufacturable`), `/materials` (materials view), `/plan` (action plan), `/search` (blueprint/item search — formerly the home page), `/blueprint/<id>` (materials), `/chain/<id>` (full chain), `/shopping/<id>` (shopping list), `/chain/shopping/<id>` (chain shopping), `/market/<id>` (prices), `/profit/<id>` (profit analysis). Blueprint/chain/market/profit/shopping pages remain as drill-downs reached from the plan.
- Build-list routes: `/build-list/add` (POST — upserts a target via `build_list.add_target`), `/build-list/remove/<id>` (POST — `build_list.remove_target`)
- Materials routes: `/materials` (`?view=tree` shows per-component **build/buy** toggles; `?view=flat` shows the flattened aggregated supply list — now **location-aware**: `flatten_material_tree(nodes, buy_set)` then `hauling.calculate_deficit` (the same call the plan buy list uses) for the at_station / elsewhere / to_buy split, then `plan.attach_haul_breakdown` for the "Haul from" names, plus volume. So the flat view and the action plan agree on what to buy. The flat view also renders the shared "Building at" picker (`station_ctx`) and resolves the saved-or-most-used station via `plan.resolve_build_station`). `/materials/toggle/<id>` (POST) flips the global `buy_set` via `build_list.toggle_buy` and redirects back. Build/buy is a single global choice across the whole list and **drives the action plan** (a component set to buy moves from a build node to a buy line).
- Build-station route: `/build-station` (POST) persists the picker choice via `build_list.set_build_station` (empty value clears it, falling back to most-used) and redirects to the source page (`next` = `plan_view` or `materials`, preserving `view`). The Plan and Materials pages both render the shared `templates/_station_picker.html` partial, which posts here on `<select>` change.
- `_resolve_targets()` / `_compute_plan()` — shared by `/plan` and `/api/plan`; mirror the CLI's qty-driven derivation and corp-or-personal asset source. `_compute_plan` resolves the saved-or-most-used build station (`plan.resolve_build_station`), runs `classify` + `enrich_buy` (location-aware) and `attach_haul_breakdown`, and returns a `station_ctx` (`{"stations": [...], "selected": id|None}`) for the picker. Without ESI auth, `loc_index`/`jobs` are empty and there's no build station, so everything lands in blocked/buy.
- `/api/plan` — JSON form of the classified buckets
- `/api/stations` — returns user's manufacturing stations ranked by usage
- JSON API endpoints: `/api/materials/<id>`, `/api/chain/<id>`, `/api/profit/<id>` — used for live recalculation via JS
- `_compute_profit()` — shared helper for profit route and API (material cost split, revenue, margins, ISK/hr)
- Chain tree caching for performance (SDE data is static)
- ESI SSO via `/login`, `/callback`, `/logout`
- Interactive build/buy toggles on chain page (JS-driven shopping list updates)
- Jinja2 filters: `isk` (ISK formatting), `ftime` (time), `commas` (number formatting), `vol` (volume m3)
- All material/shopping tables include volume data
- Build Station selector on shopping lists (and a persisted "Building at" picker on the Plan + Materials pages) enables the location-aware hauling view (materials at station vs haul vs buy)

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
| `build_list.json` | Build target list (type_id, qty, ME, etc.) | No |
| `data/sqlite-latest.sqlite` | CCP YAML SDE converted to SQLite (~400 MB) | No |

`build_list.json` lives in `DATA_DIR` when that env var is set (e.g. a Railway
volume at `/data`), otherwise next to the code. On Railway the filesystem is
ephemeral, so `DATA_DIR` must point at a mounted volume for the build list to
survive redeploys (see README → Deployment). Web login is stored in the signed
Flask session cookie, not a file, so it persists across deploys as long as
`SECRET_KEY` is stable.

## Web Templates

| Template | Purpose |
|----------|---------|
| `base.html` | Layout shell (Pico CSS, nav, flash messages) |
| `build_list.html` | Build list (home page `/`): search-to-add + add/remove targets |
| `materials.html` | Materials view (`/materials`): tree with build/buy toggles + location-aware flat supply list (total required, at-station, haul-from, to-buy) with the "Building at" picker |
| `plan.html` | Action plan (`/plan`): ready / in-progress / blocked / buy buckets, with the "Building at" picker and "Haul from" column on the buy list |
| `_station_picker.html` | Shared "Building at" station picker partial (POSTs to `/build-station` on `<select>` change); included by `plan.html` and `materials.html` |
| `index.html` | Search page (`/search`) |
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

102 tests across 8 test files, run with `python -m pytest tests/ -v`:

| File | What It Covers | Tests |
|------|---------------|------|
| `tests/test_sde.py` | Schema validation, type lookups, blueprint resolution, materials, ME calculation (pure functions), chain resolution, `get_product_qty_per_run`, `search_manufacturable` (finds ships, excludes raws) | 19 |
| `tests/test_esi.py` | `build_asset_index` (flat), `build_location_asset_index` (per-location), `extract_manufacturing_stations` (frequency ranking) | 10 |
| `tests/test_hauling.py` | `calculate_deficit`: all-at-station, split locations, nothing owned, volumes, multiple elsewhere, excess inventory; `calculate_build_capacity` | 13 |
| `tests/test_setup_sde.py` | SDE download/conversion bootstrap helpers | 9 |
| `tests/test_plan.py` | Pure classifier: `merge_trees` (shared-component aggregation), `classify` (ready/in-progress/blocked/buy bucketing, missing-input detection, job/inventory netting, `buy_set` moves node to buy), `enrich_buy` (deficit split), `resolve_build_station` (saved-wins/most-used/none), `attach_haul_breakdown` (elsewhere→named haul list, sorting, no-mutation) | 23 |
| `tests/test_build_list.py` | Build-list persistence: load/save/`add_target` upsert-by-type_id/`remove_target`/`toggle_buy` (idempotent flip, preserves targets)/`set_build_station`, legacy bare-list + missing-`build_station` compat | 12 |
| `tests/test_app_plan.py` | Flask route wiring for the build list + action plan + materials view + station picker (`/`, `/build-list/add`, `/build-list/remove`, `/plan`, `/api/plan`, `/materials` tree/flat, `/materials/toggle/<id>`, `/build-station`) | 15 |
| `tests/test_cli_plan.py` | CLI `plan` command wiring (empty-list path, no SDE/ESI/network) | 1 |

SDE tests require `data/sqlite-latest.sqlite` (skip gracefully if missing). ESI, hauling, plan, and build-list tests are pure-function/route tests with no database or network dependencies.

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
