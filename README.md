# Sihcom Industry & Inventory Tracker

EVE Online industry tool using **Preston** (ESI API) + **Fuzzwork SDE** (SQLite) for material requirements, asset tracking, and manufacturing planning.

## Directory

```
C:\Users\Neeraj\Documents\Sihcom Inventory and Industry\
├── eve_inventory.py    # Main CLI entry point
├── esi.py              # Preston/ESI wrapper (auth, assets, jobs)
├── sde.py              # SDE database interface (materials, blueprints)
├── setup_sde.py        # Downloads and extracts the Fuzzwork SDE
├── config.json         # Your ESI credentials (auto-created on first run)
├── tokens.json         # Saved refresh token (auto-created after auth)
└── data/
    └── sqlite-latest.sqlite   # Fuzzwork SDE (created by setup_sde.py)
```

## Setup

### 1. Install Python dependencies

```
pip install preston requests
```

### 2. Download the SDE

```
cd "C:\Users\Neeraj\Documents\Sihcom Inventory and Industry"
python setup_sde.py
```

This downloads the Fuzzwork SQLite SDE (~130 MB compressed, ~400 MB extracted) into the `data/` subdirectory. Re-run after EVE patches to get updated data.

### 3. Register an ESI application

1. Go to https://developers.eveonline.com/
2. **Create New Application**
3. Set **Connection Type** to "Authentication & API Access"
4. **Callback URL**: `http://localhost:8888/callback`
5. Add these **Scopes**:
   - `esi-assets.read_assets.v1`
   - `esi-characters.read_blueprints.v1`
   - `esi-industry.read_character_jobs.v1`
   - `esi-markets.structure_markets.v1`
   - `esi-universe.read_structures.v1`
   - `esi-characters.read_corporation_roles.v1`
   - `esi-assets.read_corporation_assets.v1`
   - `esi-corporations.read_blueprints.v1`
   - `esi-industry.read_corporation_jobs.v1`
6. Copy your **Client ID** and **Secret Key**

### 4. Configure

Run once to generate the template:
```
python eve_inventory.py
```

Edit `config.json`:
```json
{
  "client_id": "your_client_id",
  "client_secret": "your_secret_key",
  "callback_url": "http://localhost:8888/callback",
  "user_agent": "Sihcom Industry Tracker (your EVE name)"
}
```

### 5. Authenticate

```
python eve_inventory.py auth
```

Opens a URL — paste in browser, log in via EVE SSO, authorize. The callback is captured automatically. Your refresh token saves to `tokens.json`.

## Usage

### SDE-only commands (no auth needed)

```bash
# Search for items/blueprints
python eve_inventory.py search "Hammerhead"

# Material requirements (ME 10, 5 runs)
python eve_inventory.py materials drake 10 5

# Full blueprint detail (all activities)
python eve_inventory.py detail "Hammerhead II"

# ME comparison table (see material savings ME 0-10)
python eve_inventory.py mecomp revelation

# ME comparison for 10 runs
python eve_inventory.py mecomp "Antimatter Charge M" 10
```

### Authenticated commands

```bash
# Full industry dashboard
python eve_inventory.py summary

# Character assets
python eve_inventory.py assets

# Blueprints with ME/TE levels
python eve_inventory.py blueprints

# Active & recent industry jobs
python eve_inventory.py jobs

# Shopping list: what do I need to buy?
python eve_inventory.py shop drake 10 5
```

### Structure bonuses

Set the `STRUCTURE_BONUS` environment variable for engineering complex bonuses:

```bash
# Raitaru (1% material reduction)
set STRUCTURE_BONUS=1
python eve_inventory.py materials drake 10 5

# T2-rigged Raitaru (1% base + 4.2% rig = 5.2% total... but it's multiplicative)
# Just set the combined effective bonus
set STRUCTURE_BONUS=4.2
```

## Architecture

The code is split into three modules:

| File | Purpose | Dependencies |
|------|---------|-------------|
| `sde.py` | Blueprint recipes, type names, ME math | sqlite3 (stdlib) |
| `esi.py` | ESI authentication, asset/blueprint/job fetching | preston |
| `eve_inventory.py` | CLI commands, display logic | sde.py, esi.py |

**SDE-only commands** (`materials`, `detail`, `mecomp`, `search`) work entirely offline from the SQLite database — no ESI auth needed. This means you can look up material requirements without even configuring ESI credentials.

**Authenticated commands** combine ESI data (your actual assets, blueprints, jobs) with SDE data (type names, material recipes) for things like shopping lists and the industry dashboard.

## How material calculations work

The post-Crius formula:

```
adjusted = max(runs, ceil(round(
    runs * base_quantity * (1 - ME/100) * (1 - structure_bonus/100)
, 2)))
```

- `base_quantity` comes from `industryActivityMaterials` in the SDE
- `ME` is 0-10 (from your blueprint's material_efficiency)
- The `round(..., 2)` step eliminates floating-point artefacts
- `max(runs, ...)` enforces the minimum of 1 unit per run per material

## Notes

- **Blueprint material data is in the SDE, not ESI.** CCP hasn't added industry recipe endpoints to ESI yet, so the Fuzzwork SQLite conversion of the SDE is essential.
- **`quantity = -2`** in the blueprints endpoint means BPC; `-1` or positive means BPO.
- **Don't commit `tokens.json`** — it contains your refresh token.
- Re-run `setup_sde.py` after major EVE patches to get updated blueprints/materials.
