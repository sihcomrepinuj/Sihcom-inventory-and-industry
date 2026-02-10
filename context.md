# Sihcom Industry & Inventory Tracker — Project Context

## Owner

Neeraj — restaurant owner, EVE Online player (Black Omega Security corp), capital ship PvP (Revelation dreadnought). This tool supports his industry activities in EVE.

## Purpose

CLI tool for tracking EVE Online industry operations: assets, blueprints, manufacturing jobs, and material requirements. Designed to answer questions like "what do I need to build 5 Drakes at ME 10?" and "do I have the materials on hand?"

## Project Location

```
C:\Users\Neeraj\Documents\Sihcom Inventory and Industry\
```

## Architecture

Three-module design separating concerns:

```
eve_inventory.py    CLI entry point, display logic, command routing
    ├── sde.py      Offline blueprint/material data (Fuzzwork SQLite SDE)
    └── esi.py      Online character data (Preston library → ESI API)

setup_sde.py        One-time SDE download script (fuzzwork.co.uk)
```

### sde.py — Static Data (offline)
- Wraps the **Fuzzwork SQLite SDE** (`data/sqlite-latest.sqlite`)
- Provides: type name lookups, blueprint↔product resolution, manufacturing material requirements, activity times, invention data
- Key SDE tables: `invTypes`, `industryActivityMaterials`, `industryActivityProducts`, `industryActivity`
- Contains ME calculation logic: `apply_me()` and `calculate_materials()` implementing the post-Crius formula
- Blueprint material data is **not available via ESI** — the SDE is required
- Used as a context manager: `with SDE() as sde:`
- Zero network dependencies; works entirely from the local SQLite file

### esi.py — ESI API (online, authenticated)
- Uses the **Preston** library for EVE SSO + ESI REST calls
- Handles: SSO auth flow (local HTTP callback server on port 8888), token persistence/refresh
- Fetches: character assets (paginated), blueprints (paginated), industry jobs
- Location name resolution (stations, structures, solar systems)
- Config in `config.json`, tokens in `tokens.json`

### eve_inventory.py — CLI
- Two tiers of commands:
  - **SDE-only** (no auth): `search`, `materials`, `detail`, `mecomp`
  - **Authenticated** (SDE + ESI): `auth`, `assets`, `blueprints`, `jobs`, `shop`, `summary`
- `STRUCTURE_BONUS` env var for engineering complex material reduction %
- Interactive blueprint picker when search returns multiple matches

### setup_sde.py — SDE Bootstrap
- Downloads `sqlite-latest.sqlite.bz2` from fuzzwork.co.uk (~130 MB)
- Decompresses to `data/sqlite-latest.sqlite` (~400 MB)
- Verifies key tables exist and have data
- Should be re-run after major EVE patches

## Dependencies

- **preston** — Python ESI client (SSO, authenticated/unauthenticated API calls)
- **requests** — Used only by `setup_sde.py` for SDE download
- Python 3.11+ (Preston requirement)
- Standard library: `sqlite3`, `math`, `json`, `http.server`, `bz2`, `collections`

## Key Design Decisions

1. **Fuzzwork SQLite SDE over pyevelib/raw YAML**: Blueprint material recipes (`industryActivityMaterials`) aren't in ESI. The Fuzzwork SQLite conversion is the community standard — clean SQL, well-structured, used by virtually every serious EVE industry tool. `pyevelib` can auto-download CCP's YAML but doesn't parse it into the relational structure needed for material queries.

2. **SDE for type names instead of ESI**: Bulk `get_type_names()` via SQLite is instant vs ESI's `post_universe_names` which is rate-limited and requires network. ESI is only used for data that changes (assets, jobs, blueprints owned).

3. **Two-tier command structure**: Material lookups work without ESI credentials, lowering the barrier to entry. You can `mecomp revelation` immediately after downloading the SDE.

4. **ME formula**: `max(runs, ceil(round(runs * base * (1 - ME/100) * (1 - struct/100), 2)))`. The `round(..., 2)` eliminates floating-point artefacts before ceiling. The `max(runs, ...)` enforces the 1-per-run minimum.

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
```

Corp-level scopes are included but corp commands aren't yet implemented.

## Files Generated at Runtime

| File | Contents | Sensitive? |
|------|----------|-----------|
| `config.json` | ESI client_id, client_secret, callback_url | Yes |
| `tokens.json` | ESI refresh token | Yes |
| `data/sqlite-latest.sqlite` | Fuzzwork SDE (~400 MB) | No |

## Potential Next Steps

- **Corp-level commands**: Corp assets, corp blueprints, corp industry jobs (scopes already granted)
- **Market price integration**: ESI `get_markets_region_id_orders` for material cost estimation and profit calculations
- **CSV/Excel export**: Dump shopping lists or asset inventories to spreadsheet
- **Material chain resolution**: Recursively resolve T2 component blueprints to raw materials
- **Job scheduler**: Track industry slot usage and optimal job timing
- **Notification system**: Alert when jobs complete (polling or scheduled)
- **GUI**: Wrap the CLI in a simple Tkinter or web interface
