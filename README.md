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

### Build List + Action Plan (start here)

The primary workflow is **build-list-first**: you maintain a list of *what you
want to build*, then open an **action plan** that tells you what to do next.

1. **Maintain a build list of targets.** Each target is a manufacturable
   item plus a desired **quantity** (units to build), with optional ME and
   structure-bonus settings.
   - Web: the home page (`/`) is the build list — add/remove targets there.
     **Search-to-add:** type a ship/item name into the search box on the home
     page and click **Add** on a result — no type_id needed. The search only
     lists manufacturable items, so anything you can add is something you can
     build.
   - CLI: targets live in `build_list.json` (auto-created, per-user, gitignored).
     The web "add" form writes to it; you can also hand-edit it. Its shape is
     `{"targets": [...], "buy_set": [...]}` —
     `targets` are the items to build and `buy_set` is the global list of
     component type_ids you've chosen to buy rather than build. Each target also
     carries its own `build_station` (the facility id you're building *that
     product* at, set via the per-product web picker; `null` means "owned
     anywhere"). There is no global top-level build station.

   **Quantity is the driver.** You specify how many *units* of the product you
   want; the number of manufacturing *runs* is derived from the blueprint's
   per-run output (e.g. ammo yields 100/run, ships 1/run), rounding runs up so
   you build at least the requested units.

2. **Open the action plan.** It produces **one block per product** — each
   target's full material tree classified against *that product's own* build
   station — reading your live ESI assets and industry jobs. Within each block
   everything lands in four buckets:
   - **READY TO START NOW** — buildable items whose inputs are all on hand.
   - **IN PROGRESS** — items currently covered by an active/ready/paused job
     (shows when the job completes).
   - **BLOCKED** — buildable items waiting on inputs you don't yet have (and
     lists exactly which inputs, and how short).
   - **BUY LIST** — non-manufacturable items (or things you've chosen to buy)
     for that product. The "to buy" quantity accounts for stock you already
     hold (both at that product's build station and at your other stations), so
     it reflects only what you actually need to purchase. Per material it shows
     a **"Haul from" breakdown**: how much is already **at the build station**,
     how much you **own elsewhere and can haul** (with the station names and
     quantities), and how much to **buy** — plus the buy volume.

   **Building at (per-product build-station picker).** On the web Plan page
   **each product block has its own "Building at" dropdown** — there is no
   global default. The dropdown lists **every station where you hold blueprints**
   (your character's BPOs/BPCs plus your corp's when you're in a corp), **ranked
   by how many blueprints sit at each** so your main blueprint hub leads — rather
   than only the facilities where you've run industry jobs before. It shows up to
   **15** stations, and **falls back** to your manufacturing-job history when no
   blueprints are found (e.g. a brand-new character). Choosing one persists to
   that target's `build_station` in `build_list.json` and drives only that
   product's block: it's the station the "at the build station" stock is netted
   against, and everything you own elsewhere shows up under "Haul from".
   **Leaving a product unset** ("owned anywhere") still nets its inputs against
   everything you own, but with nothing designated as "at station" your owned
   stock shows up under "Haul from" — pick a station to see what's already
   on-site there versus what you'd haul in. Asset netting is unchanged —
   your corp's stock is netted when you're in a corp, your character's otherwise.
   (The picker is web-only; the CLI honors whatever station each target has saved.)

   ```bash
   python eve_inventory.py plan
   ```
   ```
   # or in the web UI:
   /plan
   ```

   **Degrades gracefully without ESI auth.** With no saved token, the plan has
   no inventory or job data, so everything that isn't already known to be on
   hand lands in **blocked** / **buy**. Run `auth` to enable the inventory and
   job checks. (The CLI never forces the SSO browser flow from `plan` — it only
   uses ESI if a token already exists.)

   **The CLI `plan` groups per product.** It prints one block per target, each
   classified against that target's saved `build_station` from `build_list.json`
   (set on the web picker) — printing "Building at" and the per-material "Haul
   from" breakdown for that product, or netting owned-anywhere when the target's
   station is unset. There is no CLI command to *set* the station — choose it on
   the web.

### Materials view (build vs buy) — web only

The web **Materials view** (`/materials`) sits between the build list and the
plan and is where you decide, per component, whether to **build** it or **buy**
it. This is a *global* choice that applies across your whole build list — a
component is either built or bought everywhere it appears, not per target.

It has two modes:

- **Tree** — every target's full material tree with a **build / buy** toggle on
  each component. Toggling a component to "buy" adds its type_id to the global
  `buy_set`; toggling back removes it. (Toggling posts to the server and
  reloads — there is no client-side JavaScript toggle.)
- **Flat supply list** — the trees flattened and aggregated into a single
  **bill of materials**. There is **no station picker** here: it nets your
  gross requirement against everything you own **anywhere**. Per item it shows
  the **total required**, (when logged in) the **to-buy** after subtracting
  everything you own across all locations, and the **volume** — no at-station or
  haul columns. Without ESI login to-buy equals the total required. A raw
  material shared by several targets aggregates to a single summed line.

These build/buy choices **drive the action plan**: a component set to "buy"
moves out of its build node and shows up as a **buy line** in `plan` (web and
CLI alike). The CLI reads the same global `buy_set`, but has no toggle UI — set
build/buy on the web `/materials` view (the flat supply view is web-only too).

### Detail / drill-down views

The per-blueprint commands below are the **detail views** you reach into from
the plan once you've decided what to work on — material breakdowns, full
component chains, market prices, profit, and shopping lists for a single item.

#### SDE-only commands (no auth needed)

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

#### Authenticated commands

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

### Web interface

The Flask app mirrors the same flow:

- `/` — **build list** (search-to-add + add/remove targets; the home page)
- `/materials` — **materials view** (`?view=tree` build/buy toggles,
  `?view=flat` aggregated bill of materials: total required + to-buy
  (owned-anywhere) + volume; no station picker)
- `/plan` — **action plan**, one block per product (ready / in-progress /
  blocked / buy), each with its own "Building at" picker and per-material "Haul
  from" breakdown
- `/build-station` (POST) — persists the chosen build station for **one product**
  (the per-product picker on the Plan page)
- `/search` — blueprint/item search (formerly the home page)
- `/blueprint/<id>`, `/chain/<id>`, `/shopping/<id>`, `/chain/shopping/<id>`,
  `/market/<id>`, `/profit/<id>` — the per-item drill-down pages, unchanged.

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

## Deployment (Railway) — making your data persist

Railway containers have an **ephemeral filesystem**: every deploy starts a fresh
container, so anything written to disk at runtime is lost on the next deploy. The
web app's only runtime-written file is `build_list.json` (your targets, build/buy
choices, and each target's saved build station). Without a persistent volume, **your build list
resets on every deploy**.

To make it survive deploys:

1. **Add a Volume** to the Railway service and mount it at `/data`
   (Railway dashboard → service → *Volumes* → mount path `/data`).
2. **Set `DATA_DIR=/data`** as a service environment variable. The app writes
   `build_list.json` to `DATA_DIR` when set, falling back to the app directory
   locally. (`DATA_DIR` is read at startup.)

Recommended environment variables for the deployed web app:

| Variable | Purpose |
|----------|---------|
| `DATA_DIR` | Directory for `build_list.json` — point at a mounted volume (e.g. `/data`) so the build list survives redeploys. |
| `SECRET_KEY` | Flask session signing key. **Set a long random value.** The default (`dev-secret-change-me`) is public, so anyone could forge a logged-in session — always override it in production. |
| `ESI_CLIENT_ID`, `ESI_CLIENT_SECRET`, `ESI_CALLBACK_URL`, `ESI_USER_AGENT` | ESI app credentials (the web app reads these from the env, not `config.json`). |

Your EVE login itself is stored in the signed Flask **session cookie**, so it
survives deploys as long as `SECRET_KEY` is stable (which it is once you set it).

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
