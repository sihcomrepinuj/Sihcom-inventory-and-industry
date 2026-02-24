# SDE Switch & Location-Aware Hauling Plan — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Switch from Fuzzwork CSV SDE to CCP's official YAML SDE via eve-sde-converter, then add location-aware asset tracking with hauling plan view on shopping lists.

**Architecture:** Three sequential parts. Part 1 replaces the SDE data source (foundational). Part 2 adds location-aware asset indexing and a deficit view to shopping pages. Part 3 updates documentation. The SDE switch is a plumbing change — all existing `sde.py` queries stay identical because the converter produces the same SQLite schema. Location tracking adds a new index function alongside the existing flat one, preserving backward compatibility.

**Tech Stack:** Python 3.12+, SQLite, Flask, Preston (ESI), PyYAML (via eve-sde-converter), pytest

---

## Part 1: SDE Source Switch

### Task 1: Set up test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_sde.py`
- Modify: `requirements.txt`

**Step 1: Add pytest to requirements**

Add `pytest>=8.0` to `requirements.txt`:

```
flask>=3.0
gunicorn>=21.2
preston>=4.12
requests>=2.31
pytest>=8.0
```

**Step 2: Install dependencies**

Run: `pip install -r requirements.txt`

**Step 3: Create test directory and conftest**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
"""Shared test fixtures for Sihcom Industry Tracker."""

import os
import sys

# Add project root to path so tests can import sde, esi, etc.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

**Step 4: Write a smoke test against the current SDE database**

This test validates that our existing SDE database has the tables and data we depend on. It will serve as a regression test after the SDE switch.

Create `tests/test_sde.py`:

```python
"""Tests for sde.py — validates SDE queries work correctly.

These tests run against the actual SDE database (data/sqlite-latest.sqlite).
They verify that the schema and data we depend on are present and correct.
"""

import pytest
from sde import SDE, apply_me, calculate_materials, resolve_material_chain, flatten_material_tree


@pytest.fixture
def sde():
    """Provide an SDE instance for tests. Skips if database is missing."""
    try:
        s = SDE()
        yield s
        s.close()
    except FileNotFoundError:
        pytest.skip("SDE database not available — run setup_sde.py first")


# -- Schema validation --

def test_sde_has_required_tables(sde):
    """The SDE database must have the 4 tables our queries depend on."""
    cursor = sde.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row["name"] for row in cursor.fetchall()}
    assert "invTypes" in tables
    assert "industryActivityMaterials" in tables
    assert "industryActivityProducts" in tables
    assert "industryActivity" in tables


def test_invtypes_has_data(sde):
    """invTypes should have tens of thousands of rows."""
    count = sde.conn.execute("SELECT COUNT(*) as c FROM invTypes").fetchone()["c"]
    assert count > 10000


# -- Type lookups --

def test_get_type_name_tritanium(sde):
    """Tritanium (type_id=34) is a well-known mineral."""
    assert sde.get_type_name(34) == "Tritanium"


def test_get_type_names_bulk(sde):
    """Bulk lookup returns correct names."""
    names = sde.get_type_names([34, 35, 36])
    assert names[34] == "Tritanium"
    assert names[35] == "Pyerite"
    assert names[36] == "Mexallon"


def test_search_types_drake(sde):
    """Searching 'Drake' should find the Drake battlecruiser."""
    results = sde.search_types("Drake")
    names = [r["name"] for r in results]
    assert "Drake" in names


# -- Blueprint lookups --

def test_search_blueprints_drake(sde):
    """Searching blueprints for 'Drake' should find the Drake Blueprint."""
    results = sde.search_blueprints("Drake")
    product_names = [r["product_name"] for r in results]
    assert "Drake" in product_names


def test_find_blueprint_for_product_drake(sde):
    """Drake (type_id=24698) should have a manufacturing blueprint."""
    bp_id = sde.find_blueprint_for_product(24698)
    assert bp_id is not None


def test_find_product_for_blueprint(sde):
    """Drake Blueprint should produce a Drake."""
    bp_id = sde.find_blueprint_for_product(24698)
    product_id = sde.find_product_for_blueprint(bp_id)
    assert product_id == 24698


# -- Materials --

def test_get_manufacturing_materials_drake(sde):
    """Drake Blueprint should have manufacturing materials."""
    bp_id = sde.find_blueprint_for_product(24698)
    mats = sde.get_manufacturing_materials(bp_id)
    assert len(mats) > 0
    # Every material should have type_id, name, quantity, volume
    for mat in mats:
        assert "type_id" in mat
        assert "name" in mat
        assert "quantity" in mat
        assert mat["quantity"] > 0


def test_get_activity_time(sde):
    """Drake Blueprint should have a manufacturing time."""
    bp_id = sde.find_blueprint_for_product(24698)
    time_seconds = sde.get_activity_time(bp_id, 1)  # 1 = manufacturing
    assert time_seconds is not None
    assert time_seconds > 0


# -- ME calculation (pure functions, no SDE needed) --

def test_apply_me_zero():
    """ME 0 should return base * runs."""
    assert apply_me(100, me_level=0, runs=1) == 100
    assert apply_me(100, me_level=0, runs=10) == 1000


def test_apply_me_ten():
    """ME 10 should reduce by 10%."""
    result = apply_me(100, me_level=10, runs=1)
    assert result == 90


def test_apply_me_minimum_one_per_run():
    """Even with high ME, you need at least 1 per run."""
    result = apply_me(1, me_level=10, runs=5)
    assert result >= 5


def test_apply_me_structure_bonus():
    """Structure bonus stacks with ME."""
    no_bonus = apply_me(100, me_level=10, runs=1, structure_bonus=0)
    with_bonus = apply_me(100, me_level=10, runs=1, structure_bonus=1.0)
    assert with_bonus < no_bonus


# -- Chain resolution --

def test_resolve_material_chain_drake(sde):
    """Drake material chain should resolve to a non-empty tree."""
    bp_id = sde.find_blueprint_for_product(24698)
    tree = resolve_material_chain(sde, bp_id, me_level=10, runs=1)
    assert len(tree) > 0


def test_flatten_material_tree_drake(sde):
    """Flattened Drake chain should produce a non-empty shopping list."""
    bp_id = sde.find_blueprint_for_product(24698)
    tree = resolve_material_chain(sde, bp_id, me_level=10, runs=1)
    flat = flatten_material_tree(tree)
    assert len(flat) > 0
    for item in flat:
        assert "type_id" in item
        assert "name" in item
        assert "quantity" in item
        assert item["quantity"] > 0
```

**Step 5: Run tests to verify they pass against the current SDE**

Run: `pytest tests/test_sde.py -v`
Expected: All tests PASS (these validate the current working state)

**Step 6: Commit**

```bash
git add tests/ requirements.txt
git commit -m "Add test infrastructure and SDE regression tests"
```

---

### Task 2: Add eve-sde-converter as a git submodule

**Files:**
- Create: `tools/eve-sde-converter/` (git submodule)
- Modify: `.gitmodules` (auto-created by git)
- Modify: `.gitignore`

**Step 1: Add the submodule**

Run: `git submodule add https://github.com/noirsoldats/eve-sde-converter.git tools/eve-sde-converter`

**Step 2: Update .gitignore**

Add these lines to `.gitignore`:

```
# SDE conversion working files
sde/
*.zip
eve.db
```

**Step 3: Verify the submodule**

Run: `ls tools/eve-sde-converter/Load.py`
Expected: File exists

**Step 4: Commit**

```bash
git add .gitmodules tools/eve-sde-converter .gitignore
git commit -m "Add eve-sde-converter as git submodule for CCP YAML SDE"
```

---

### Task 3: Rewrite setup_sde.py

**Files:**
- Modify: `setup_sde.py` (full rewrite)

**Step 1: Write the new setup_sde.py**

Replace the entire file:

```python
"""
setup_sde.py — Download CCP's official YAML SDE and convert to SQLite.

Uses noirsoldats/eve-sde-converter (git submodule at tools/eve-sde-converter)
to parse CCP's YAML files into a SQLite database.

Run this once (and again after major EVE patches):
    python setup_sde.py

It will:
  1. Check the latest SDE build number from CCP
  2. Download the YAML SDE zip (~200 MB)
  3. Extract YAML files
  4. Convert to SQLite via eve-sde-converter
  5. Copy the result to data/sqlite-latest.sqlite
  6. Verify the database is usable
"""

import configparser
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SDE_DB = os.path.join(DATA_DIR, "sqlite-latest.sqlite")
CONVERTER_DIR = os.path.join(PROJECT_DIR, "tools", "eve-sde-converter")
SDE_WORK_DIR = os.path.join(CONVERTER_DIR, "sde")

CCP_BASE_URL = "https://developers.eveonline.com/static-data/tranquility"


def get_latest_build() -> str:
    """Fetch the latest SDE build number from CCP."""
    print("  Checking latest SDE version...", end=" ", flush=True)
    resp = requests.get(f"{CCP_BASE_URL}/latest.jsonl")
    resp.raise_for_status()
    # latest.jsonl contains a single JSON line with the build number
    data = json.loads(resp.text.strip().split("\n")[0])
    build = str(data.get("build_number", data.get("buildNumber", "")))
    print(f"build {build}")
    return build


def download_sde(build: str) -> str:
    """Download the YAML SDE zip from CCP. Returns path to zip file."""
    zip_name = f"eve-online-static-data-{build}-yaml.zip"
    zip_path = os.path.join(CONVERTER_DIR, zip_name)

    if os.path.exists(zip_path):
        print(f"  SDE zip already downloaded: {zip_name}")
        return zip_path

    url = f"{CCP_BASE_URL}/{zip_name}"
    print(f"  Downloading {zip_name}...", flush=True)
    resp = requests.get(url, stream=True)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                print(f"\r  Downloading {zip_name}... {pct}%", end="", flush=True)
    print(f"\r  Downloaded {zip_name} ({downloaded // 1024 // 1024} MB)")
    return zip_path


def extract_sde(zip_path: str):
    """Extract YAML files to the converter's sde/ directory."""
    if os.path.exists(SDE_WORK_DIR):
        shutil.rmtree(SDE_WORK_DIR)
    os.makedirs(SDE_WORK_DIR, exist_ok=True)

    print("  Extracting YAML files...", end=" ", flush=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(SDE_WORK_DIR)

    # The zip may contain a nested directory — flatten if needed
    entries = os.listdir(SDE_WORK_DIR)
    if len(entries) == 1 and os.path.isdir(os.path.join(SDE_WORK_DIR, entries[0])):
        nested = os.path.join(SDE_WORK_DIR, entries[0])
        for item in os.listdir(nested):
            shutil.move(os.path.join(nested, item), SDE_WORK_DIR)
        os.rmdir(nested)

    print("done")


def write_converter_config():
    """Write sdeloader.cfg for the converter pointing to our paths."""
    cfg_path = os.path.join(CONVERTER_DIR, "sdeloader.cfg")

    config = configparser.ConfigParser()
    config["Database"] = {
        "sqlite": f"sqlite:///{os.path.join(CONVERTER_DIR, 'eve.db')}",
    }
    config["Files"] = {
        "sourcePath": "sde",
        "destinationPath": "sdeoutput/",
    }
    with open(cfg_path, "w") as f:
        config.write(f)


def run_converter():
    """Run Load.py sqlite in the converter directory."""
    print("  Converting YAML to SQLite (this may take a few minutes)...")
    result = subprocess.run(
        [sys.executable, "Load.py", "sqlite"],
        cwd=CONVERTER_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Converter stderr:\n{result.stderr}")
        raise RuntimeError(f"eve-sde-converter failed with return code {result.returncode}")
    print("  Conversion complete.")


def install_database():
    """Copy eve.db to our data/ directory as sqlite-latest.sqlite."""
    os.makedirs(DATA_DIR, exist_ok=True)

    eve_db = os.path.join(CONVERTER_DIR, "eve.db")
    if not os.path.exists(eve_db):
        raise FileNotFoundError(f"Converter output not found at {eve_db}")

    if os.path.exists(SDE_DB):
        os.remove(SDE_DB)

    shutil.copy2(eve_db, SDE_DB)
    size_mb = os.path.getsize(SDE_DB) / 1024 / 1024
    print(f"  Database installed to {SDE_DB} ({size_mb:.1f} MB)")


def verify_sde():
    """Verify the database has the tables and data we need."""
    conn = sqlite3.connect(SDE_DB)
    required_tables = [
        "invTypes",
        "industryActivityMaterials",
        "industryActivityProducts",
        "industryActivity",
    ]
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cursor.fetchall()}

    all_ok = True
    for t in required_tables:
        if t in existing:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {count:,} rows")
        else:
            print(f"  WARNING: Table '{t}' not found!")
            all_ok = False

    conn.close()

    if all_ok:
        print("\nSDE is ready!")
    else:
        print("\nWARNING: Required tables missing. Check the converter output.")
    return all_ok


# Keep these names for backwards compatibility with app.py imports
def build_database():
    """Full pipeline: download, extract, convert, install."""
    build = get_latest_build()
    zip_path = download_sde(build)
    extract_sde(zip_path)
    write_converter_config()
    run_converter()
    install_database()


def download_sde_compat():
    """Backwards-compatible name used by app.py."""
    build_database()


def main():
    print("=" * 60)
    print("EVE Online SDE Setup (CCP YAML -> SQLite)")
    print("=" * 60)

    if not os.path.exists(os.path.join(CONVERTER_DIR, "Load.py")):
        print(
            f"\nERROR: eve-sde-converter not found at {CONVERTER_DIR}\n"
            "Run: git submodule update --init --recursive"
        )
        sys.exit(1)

    if os.path.exists(SDE_DB):
        print(f"\nExisting SDE found at {SDE_DB}")
        resp = input("Re-download and replace? [y/N]: ").strip().lower()
        if resp != "y":
            print("Keeping existing SDE.")
            verify_sde()
            return

    print()
    build_database()
    print()
    verify_sde()


if __name__ == "__main__":
    main()
```

**Step 2: Run the new setup to build the database**

Run: `python setup_sde.py`
Expected: Downloads CCP YAML SDE, converts to SQLite, installs to `data/sqlite-latest.sqlite`

Note: This step takes several minutes due to the ~200 MB download and YAML parsing. If this is your first time, you may need to run `git submodule update --init --recursive` first.

**Step 3: Run the SDE regression tests against the new database**

Run: `pytest tests/test_sde.py -v`
Expected: All tests PASS — confirming the new CCP-sourced database is schema-compatible

**Step 4: Commit**

```bash
git add setup_sde.py
git commit -m "Rewrite setup_sde.py to use CCP YAML SDE via eve-sde-converter"
```

---

### Task 4: Update sde.py docstrings and error messages

**Files:**
- Modify: `sde.py:1-13` (module docstring)
- Modify: `sde.py:47-66` (class docstring and error message)

**Step 1: Update module docstring**

Replace lines 1-13 of `sde.py`:

```python
"""
sde.py — Interface to the EVE Online SDE SQLite database.

The database is built from CCP's official YAML SDE using
noirsoldats/eve-sde-converter (git submodule at tools/eve-sde-converter).

Provides:
  - Type name lookups (much faster than ESI)
  - Blueprint material requirements
  - Product <-> blueprint resolution
  - Activity times and invention data
  - ME-adjusted material calculations

Run setup_sde.py to download and convert the latest SDE.
"""
```

**Step 2: Update class docstring**

Replace the SDE class docstring (lines 47-56):

```python
class SDE:
    """
    Interface to the EVE Online SDE SQLite database.

    Built from CCP's official YAML SDE via eve-sde-converter.

    Key tables used:
        invTypes                    - type_id <-> name mapping
        industryActivityMaterials   - blueprint materials per activity
        industryActivityProducts    - blueprint products per activity
        industryActivity            - activity times
    """
```

**Step 3: Update error message**

Replace the FileNotFoundError message (lines 62-66):

```python
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"SDE database not found at: {db_path}\n"
                "Run 'python setup_sde.py' to download and convert the CCP YAML SDE."
            )
```

**Step 4: Run tests**

Run: `pytest tests/test_sde.py -v`
Expected: All PASS (docstring changes only, no logic changes)

**Step 5: Commit**

```bash
git add sde.py
git commit -m "Update sde.py docstrings for CCP YAML SDE source"
```

---

### Task 5: Update app.py log messages

**Files:**
- Modify: `app.py:113-129` (ensure_sde_downloaded function)

**Step 1: Update log messages in ensure_sde_downloaded()**

Replace the function:

```python
def ensure_sde_downloaded():
    """Download and convert the CCP YAML SDE if it doesn't exist or is corrupt."""
    from sde import DEFAULT_SDE_PATH
    logger.info(f"Checking for SDE at: {DEFAULT_SDE_PATH}")

    if os.path.exists(DEFAULT_SDE_PATH):
        if _sde_is_valid(DEFAULT_SDE_PATH):
            logger.info("SDE found and valid.")
            return
        else:
            logger.warning("SDE file is corrupt — deleting and re-downloading...")
            os.remove(DEFAULT_SDE_PATH)

    logger.info("Downloading CCP YAML SDE and converting to SQLite...")
    from setup_sde import build_database
    build_database()
    logger.info("SDE ready.")
```

**Step 2: Verify app still starts**

Run: `python -c "from app import app; print('App imports OK')"`
Expected: Prints "App imports OK"

**Step 3: Commit**

```bash
git add app.py
git commit -m "Update app.py SDE log messages for CCP YAML source"
```

---

## Part 2: Location-Aware Hauling Plan

### Task 6: Add location-aware asset index with tests

**Files:**
- Create: `tests/test_esi.py`
- Modify: `esi.py:187-192` (add new function after build_asset_index)

**Step 1: Write the failing tests**

Create `tests/test_esi.py`:

```python
"""Tests for esi.py — asset indexing and location tracking."""

from esi import build_asset_index, build_location_asset_index


# -- Existing flat index (regression) --

def test_build_asset_index_basic():
    """Flat index sums quantities across all locations."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 200, "location_id": 2000},
        {"type_id": 35, "quantity": 50, "location_id": 1000},
    ]
    index = build_asset_index(assets)
    assert index[34] == 300
    assert index[35] == 50


def test_build_asset_index_empty():
    """Empty asset list produces empty index."""
    assert build_asset_index([]) == {}


# -- Location-aware index --

def test_build_location_asset_index_basic():
    """Location index preserves per-location quantities."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 200, "location_id": 2000},
        {"type_id": 35, "quantity": 50, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[34][1000] == 100
    assert index[34][2000] == 200
    assert index[35][1000] == 50


def test_build_location_asset_index_same_location():
    """Multiple stacks at same location are summed."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 50, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[34][1000] == 150


def test_build_location_asset_index_empty():
    """Empty asset list produces empty index."""
    assert build_location_asset_index([]) == {}


def test_build_location_asset_index_default_quantity():
    """Assets without explicit quantity default to 1 (e.g. ships)."""
    assets = [
        {"type_id": 587, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[587][1000] == 1
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_esi.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_location_asset_index'`

**Step 3: Implement build_location_asset_index**

Add after `build_asset_index()` in `esi.py` (after line 192):

```python
def build_location_asset_index(assets: list[dict]) -> dict[int, dict[int, int]]:
    """
    Build {type_id: {location_id: quantity}} from asset list.

    Preserves per-location quantities for location-aware shopping lists.
    """
    index: dict[int, dict[int, int]] = {}
    for a in assets:
        tid = a["type_id"]
        lid = a.get("location_id", 0)
        qty = a.get("quantity", 1)
        if tid not in index:
            index[tid] = defaultdict(int)
        index[tid][lid] += qty
    # Convert inner defaultdicts to plain dicts for clean serialization
    return {tid: dict(locs) for tid, locs in index.items()}
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_esi.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add tests/test_esi.py esi.py
git commit -m "Add build_location_asset_index for per-location asset tracking"
```

---

### Task 7: Add manufacturing station detection with tests

**Files:**
- Modify: `tests/test_esi.py` (add tests)
- Modify: `esi.py` (add function after build_location_asset_index)

**Step 1: Write the failing tests**

Append to `tests/test_esi.py`:

```python
from esi import extract_manufacturing_stations


def test_extract_manufacturing_stations_basic():
    """Extracts unique facility IDs from manufacturing jobs, ranked by frequency."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 1, "facility_id": 2000, "status": "active"},
        {"activity_id": 3, "facility_id": 3000, "status": "active"},  # TE research, not mfg
    ]
    stations = extract_manufacturing_stations(jobs)
    # 1000 appears twice, so it should be first
    assert stations[0] == 1000
    assert stations[1] == 2000
    assert len(stations) == 2  # 3000 is excluded (not manufacturing)


def test_extract_manufacturing_stations_includes_reactions():
    """Reaction jobs (activity_id=9) are also relevant build stations."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 9, "facility_id": 2000, "status": "active"},
    ]
    stations = extract_manufacturing_stations(jobs)
    assert 1000 in stations
    assert 2000 in stations


def test_extract_manufacturing_stations_empty():
    """No jobs means no stations."""
    assert extract_manufacturing_stations([]) == []


def test_extract_manufacturing_stations_completed_jobs():
    """Completed jobs still count — they reveal where you build."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "delivered"},
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
    ]
    stations = extract_manufacturing_stations(jobs)
    assert stations == [1000]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_esi.py::test_extract_manufacturing_stations_basic -v`
Expected: FAIL — `ImportError: cannot import name 'extract_manufacturing_stations'`

**Step 3: Implement extract_manufacturing_stations**

Add to `esi.py` after `build_location_asset_index`:

```python
def extract_manufacturing_stations(jobs: list[dict]) -> list[int]:
    """
    Extract unique manufacturing/reaction facility IDs from industry jobs.

    Returns facility IDs ranked by frequency (most-used first).
    Only includes manufacturing (activity_id=1) and reaction (activity_id=9) jobs.
    """
    from collections import Counter

    PRODUCTION_ACTIVITIES = {1, 9}  # Manufacturing, Reactions
    facility_counts: Counter = Counter()

    for job in jobs:
        if job.get("activity_id") in PRODUCTION_ACTIVITIES:
            fid = job.get("facility_id")
            if fid:
                facility_counts[fid] += 1

    # Return sorted by frequency (most used first)
    return [fid for fid, _ in facility_counts.most_common()]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_esi.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add tests/test_esi.py esi.py
git commit -m "Add extract_manufacturing_stations from industry jobs"
```

---

### Task 8: Add deficit calculation helper with tests

**Files:**
- Create: `tests/test_hauling.py`
- Create: `hauling.py`

This is the core logic that computes the three-group deficit view. Keeping it in a separate module makes it easy to test as a pure function.

**Step 1: Write the failing tests**

Create `tests/test_hauling.py`:

```python
"""Tests for hauling.py — deficit calculation for location-aware shopping."""

from hauling import calculate_deficit


def test_calculate_deficit_all_at_station():
    """Materials already at build station show as ready."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    loc_index = {34: {1000: 150}}  # 150 at station 1000
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["at_station"] == 100  # capped at needed
    assert result[0]["elsewhere"] == {}
    assert result[0]["to_buy"] == 0


def test_calculate_deficit_split_locations():
    """Materials at different locations show where to haul from."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 500}]
    loc_index = {34: {1000: 200, 2000: 100}}  # 200 at station, 100 elsewhere
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["at_station"] == 200
    assert result[0]["elsewhere"] == {2000: 100}
    assert result[0]["to_buy"] == 200  # 500 - 200 - 100


def test_calculate_deficit_nothing_owned():
    """No assets means everything must be bought."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 500}]
    loc_index = {}
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["at_station"] == 0
    assert result[0]["elsewhere"] == {}
    assert result[0]["to_buy"] == 500


def test_calculate_deficit_volumes():
    """Volume calculations for hauling and buying."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 1000}]
    loc_index = {34: {1000: 300, 2000: 200}}
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["elsewhere_volume"] == 200 * 0.01  # 2.0 m3 to haul
    assert result[0]["to_buy_volume"] == 500 * 0.01  # 5.0 m3 to buy


def test_calculate_deficit_multiple_elsewhere():
    """Materials scattered across multiple non-station locations."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 1000}]
    loc_index = {34: {1000: 100, 2000: 200, 3000: 300}}
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["at_station"] == 100
    assert result[0]["elsewhere"] == {2000: 200, 3000: 300}
    assert result[0]["to_buy"] == 400  # 1000 - 100 - 200 - 300


def test_calculate_deficit_more_than_needed():
    """Excess inventory doesn't go negative."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    loc_index = {34: {1000: 500}}
    volumes = {34: 0.01}

    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)

    assert result[0]["at_station"] == 100  # capped at needed
    assert result[0]["to_buy"] == 0
    assert result[0]["elsewhere_volume"] == 0.0
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hauling.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hauling'`

**Step 3: Implement hauling.py**

Create `hauling.py`:

```python
"""
hauling.py — Deficit calculation for location-aware shopping lists.

Given a list of needed materials, a location-aware asset index, and a
build station ID, calculates what's at the station, what needs to be
hauled from elsewhere, and what needs to be bought.
"""


def calculate_deficit(
    needed: list[dict],
    loc_index: dict[int, dict[int, int]],
    build_station: int,
    volumes: dict[int, float],
) -> list[dict]:
    """
    Calculate per-material deficit for a build station.

    Args:
        needed: List of {type_id, name, quantity} dicts (from shopping list)
        loc_index: {type_id: {location_id: quantity}} from build_location_asset_index
        build_station: The facility_id where we're manufacturing
        volumes: {type_id: volume_per_unit} for m3 calculations

    Returns:
        List of dicts per material:
            type_id, name, quantity_needed,
            at_station, elsewhere (dict of {loc_id: qty}),
            to_buy, elsewhere_volume, to_buy_volume
    """
    results = []
    for mat in needed:
        tid = mat["type_id"]
        qty_needed = mat["quantity"]
        unit_vol = volumes.get(tid, 0.0)

        # What's at the build station?
        type_locations = loc_index.get(tid, {})
        at_station = min(type_locations.get(build_station, 0), qty_needed)

        # What's at other locations?
        remaining_need = qty_needed - at_station
        elsewhere = {}
        elsewhere_total = 0
        for loc_id, loc_qty in type_locations.items():
            if loc_id == build_station:
                continue
            if remaining_need <= 0:
                break
            usable = min(loc_qty, remaining_need)
            elsewhere[loc_id] = usable
            elsewhere_total += usable
            remaining_need -= usable

        # What still needs to be bought?
        to_buy = max(0, qty_needed - at_station - elsewhere_total)

        results.append({
            "type_id": tid,
            "name": mat["name"],
            "quantity_needed": qty_needed,
            "at_station": at_station,
            "elsewhere": elsewhere,
            "elsewhere_volume": elsewhere_total * unit_vol,
            "to_buy": to_buy,
            "to_buy_volume": to_buy * unit_vol,
        })

    return results
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hauling.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add hauling.py tests/test_hauling.py
git commit -m "Add hauling deficit calculator for location-aware shopping"
```

---

### Task 9: Update asset caching to preserve raw assets

**Files:**
- Modify: `esi.py:511-536` (get_cached_asset_index and related)

**Step 1: Refactor caching to store raw assets**

The current cache stores only the flat index. We need the raw asset list so we can build both flat and location-aware indexes. Replace the asset caching section in `esi.py`:

```python
# {entity_id: (timestamp, raw_assets_list)}
_raw_asset_cache: dict[int, tuple[float, list[dict]]] = {}
ASSET_CACHE_TTL = 600  # 10 minutes


def _get_cached_raw_assets(
    p: Preston,
    entity_id: int,
    is_corp: bool = False,
) -> list[dict]:
    """Fetch and cache raw asset list for a character or corporation."""
    now = _time.monotonic()

    if entity_id in _raw_asset_cache:
        ts, assets = _raw_asset_cache[entity_id]
        if now - ts < ASSET_CACHE_TTL:
            return assets

    if is_corp:
        assets = fetch_corp_assets(p, entity_id)
    else:
        assets = fetch_assets(p, entity_id)

    _raw_asset_cache[entity_id] = (now, assets)
    return assets


def get_cached_asset_index(
    p: Preston,
    entity_id: int,
    is_corp: bool = False,
) -> dict[int, int]:
    """
    Fetch and cache flat asset index for a character or corporation.

    Returns {type_id: total_quantity} (location-unaware, for backward compat).
    """
    assets = _get_cached_raw_assets(p, entity_id, is_corp)
    return build_asset_index(assets)


def get_cached_location_asset_index(
    p: Preston,
    entity_id: int,
    is_corp: bool = False,
) -> dict[int, dict[int, int]]:
    """
    Fetch and cache location-aware asset index.

    Returns {type_id: {location_id: quantity}}.
    """
    assets = _get_cached_raw_assets(p, entity_id, is_corp)
    return build_location_asset_index(assets)


def prefetch_asset_index(
    p: Preston,
    entity_id: int,
    is_corp: bool = False,
) -> None:
    """Proactively fetch and cache assets in the background."""
    try:
        _get_cached_raw_assets(p, entity_id, is_corp)
    except Exception:
        pass
```

**Step 2: Run all existing tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add esi.py
git commit -m "Refactor asset caching to preserve raw assets for location indexing"
```

---

### Task 10: Add build station endpoint and shopping route location support

**Files:**
- Modify: `app.py` (add /api/stations endpoint, update shopping route)

**Step 1: Add the stations API endpoint**

Add after the auth routes section in `app.py`:

```python
@app.route("/api/stations")
@login_required
def api_stations():
    """Return the user's manufacturing stations ranked by usage."""
    p = get_authed_preston_from_session()
    if not p:
        return jsonify(error="Session expired"), 401

    character_id = int(session["character_id"])
    jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)

    from esi import extract_manufacturing_stations
    station_ids = extract_manufacturing_stations(jobs)

    # Resolve names
    stations = []
    for sid in station_ids[:10]:  # limit to top 10
        name = esi.resolve_location_name(p, sid, "other")
        stations.append({"id": sid, "name": name})

    session["refresh_token"] = p.refresh_token
    return jsonify(stations=stations)
```

**Step 2: Update the shopping route to support location parameter**

Modify the `shopping()` function in `app.py`. Add location handling after the asset fetch block. The key changes:

1. Read `location` query parameter
2. If set, use `get_cached_location_asset_index` and `calculate_deficit`
3. Pass location data to template
4. Fetch station list for the dropdown

Add these imports at the top of `app.py`:

```python
from hauling import calculate_deficit
```

Then update the shopping route to add location support. After the existing asset_index fetch (around line 629-633), add:

```python
    # Location-aware hauling (when build station is selected)
    build_station = request.args.get("location", type=int)
    station_list = []
    deficit_data = None

    if build_station:
        loc_index = esi.get_cached_location_asset_index(
            p, corporation_id if source == "corp" and corporation_id else character_id,
            is_corp=(source == "corp" and corporation_id is not None),
        )
        volumes = sde.get_type_volumes([m["type_id"] for m in materials]) if materials else {}
        deficit_data = calculate_deficit(
            [{"type_id": m["type_id"], "name": m["name"], "quantity": m["adjusted_quantity"]}
             for m in materials],
            loc_index, build_station, volumes,
        )

    # Always fetch station list for the dropdown (if logged in)
    try:
        jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
        raw_stations = esi.extract_manufacturing_stations(jobs)
        for sid in raw_stations[:10]:
            name = esi.resolve_location_name(p, sid, "other")
            station_list.append({"id": sid, "name": name})
    except Exception:
        pass  # Station list is optional
```

Update the `render_template` call to pass the new data:

```python
    return render_template(
        "shopping.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        materials=materials, total_buy_cost=total_buy_cost,
        total_buy_volume=total_buy_volume,
        character_name=session.get("character_name"),
        source=source,
        has_corp=corporation_id is not None,
        build_station=build_station,
        station_list=station_list,
        deficit_data=deficit_data,
    )
```

**Step 3: Verify app imports**

Run: `python -c "from app import app; print('OK')"`
Expected: Prints "OK"

**Step 4: Commit**

```bash
git add app.py
git commit -m "Add build station API and location support to shopping route"
```

---

### Task 11: Update shopping list template with station selector and deficit view

**Files:**
- Modify: `templates/shopping.html`

**Step 1: Add the build station dropdown to the form**

Add after the Assets `<select>` block (after line 35 in the current template), before the submit button:

```html
    <label>
        Build Station
        <select name="location">
            <option value="">All locations</option>
            {% for station in station_list %}
            <option value="{{ station.id }}" {% if build_station == station.id %}selected{% endif %}>{{ station.name }}</option>
            {% endfor %}
        </select>
    </label>
```

**Step 2: Add the deficit view below the existing table**

After the existing shopping table's closing `</table>` tag and before the "missing materials" paragraph, add a conditional block:

```html
{% if deficit_data %}
<h3>Hauling Plan</h3>

<table>
    <thead>
        <tr>
            <th>Material</th>
            <th class="text-right">Need</th>
            <th class="text-right">At Station</th>
            <th class="text-right">Haul (elsewhere)</th>
            <th class="text-right">Haul Vol m³</th>
            <th class="text-right">Buy</th>
            <th class="text-right">Buy Vol m³</th>
        </tr>
    </thead>
    <tbody>
        {% for d in deficit_data %}
        <tr>
            <td><a href="/market/{{ d.type_id }}">{{ d.name }}</a></td>
            <td class="text-right">{{ d.quantity_needed | commas }}</td>
            <td class="text-right">
                {% if d.at_station > 0 %}
                    <span class="status-ok">{{ d.at_station | commas }}</span>
                {% else %}
                    0
                {% endif %}
            </td>
            <td class="text-right">
                {% set haul_total = d.elsewhere.values() | sum %}
                {% if haul_total > 0 %}
                    {{ haul_total | commas }}
                    <small class="text-muted">
                        ({% for loc_id, qty in d.elsewhere.items() %}{{ qty | commas }}{% if not loop.last %}, {% endif %}{% endfor %})
                    </small>
                {% else %}
                    0
                {% endif %}
            </td>
            <td class="text-right">{{ d.elsewhere_volume | vol }}</td>
            <td class="text-right">
                {% if d.to_buy > 0 %}
                    <span class="status-need">{{ d.to_buy | commas }}</span>
                {% else %}
                    0
                {% endif %}
            </td>
            <td class="text-right">{{ d.to_buy_volume | vol }}</td>
        </tr>
        {% endfor %}
    </tbody>
    <tfoot>
        <tr>
            <td colspan="4" class="text-right grand-total">Total Haul Volume</td>
            <td class="text-right grand-total">{{ deficit_data | sum(attribute='elsewhere_volume') | vol }} m³</td>
            <td class="text-right grand-total">Total Buy Volume</td>
            <td class="text-right grand-total">{{ deficit_data | sum(attribute='to_buy_volume') | vol }} m³</td>
        </tr>
    </tfoot>
</table>
{% endif %}
```

**Step 3: Test manually in browser**

1. Run the app: `python app.py`
2. Log in via EVE SSO
3. Navigate to a shopping list (e.g. Drake)
4. Verify the Build Station dropdown appears
5. Select a station — verify the hauling plan table renders
6. Verify "All locations" shows the classic flat view

**Step 4: Commit**

```bash
git add templates/shopping.html
git commit -m "Add build station selector and hauling deficit view to shopping template"
```

---

### Task 12: Add location support to chain shopping route and template

**Files:**
- Modify: `app.py` (chain_shopping route)
- Modify: `templates/chain_shopping.html`

**Step 1: Update chain_shopping route**

Apply the same pattern as Task 10 to the `chain_shopping()` function in `app.py`. After the existing `asset_index` fetch, add:

```python
    build_station = request.args.get("location", type=int)
    station_list = []
    deficit_data = None

    if build_station:
        loc_index = esi.get_cached_location_asset_index(
            p, corporation_id if source == "corp" and corporation_id else character_id,
            is_corp=(source == "corp" and corporation_id is not None),
        )
        volumes_for_deficit = sde.get_type_volumes(
            [m["type_id"] for m in shopping_materials]
        ) if shopping_materials else {}
        deficit_data = calculate_deficit(
            shopping_materials, loc_index, build_station, volumes_for_deficit,
        )

    try:
        jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
        raw_stations = esi.extract_manufacturing_stations(jobs)
        for sid in raw_stations[:10]:
            name = esi.resolve_location_name(p, sid, "other")
            station_list.append({"id": sid, "name": name})
    except Exception:
        pass
```

Update the `render_template` call to pass:

```python
        build_station=build_station,
        station_list=station_list,
        deficit_data=deficit_data,
```

**Step 2: Update chain_shopping.html template**

Apply the same template changes as Task 11:
- Add Build Station dropdown to the form
- Add the hauling plan table (identical HTML block)

**Step 3: Test manually**

Same manual test flow as Task 11, but via the chain shopping page.

**Step 4: Commit**

```bash
git add app.py templates/chain_shopping.html
git commit -m "Add location-aware hauling to chain shopping page"
```

---

## Part 3: Context & Documentation Cleanup

### Task 13: Update context.md

**Files:**
- Modify: `context.md`

**Step 1: Update SDE references**

Replace all mentions of "Fuzzwork SQLite SDE", "Fuzzwork CSV", and "fuzzwork.co.uk" with references to CCP's official YAML SDE via eve-sde-converter. Key locations:

- Architecture section: Update `sde.py` description and `setup_sde.py` description
- Key Design Decisions section #1: Update the rationale
- Dependencies section: Add eve-sde-converter, PyYAML
- Files Generated at Runtime: Update the SDE row description

**Step 2: Replace Go Porting Guide**

Remove the entire "Go Porting Guide" section (everything from `## Go Porting Guide` through the end of the "Codebase Size" table). Replace with:

```markdown
## Go Reference

This Python app serves as a prototype and reference implementation. Corp mates building the consolidated Go-based industry tool can reference this codebase for domain logic, ME formulas, and chain resolution algorithms.
```

**Step 3: Update Potential Next Steps**

Remove "The following are relevant to the consolidated Go project:" framing. Add the hauling plan feature (now implemented). Keep the remaining items as general next steps.

**Step 4: Add hauling.py to the Architecture section**

Add `hauling.py` to the file list with description: "Deficit calculation for location-aware shopping lists"

**Step 5: Commit**

```bash
git add context.md
git commit -m "Update context.md: CCP SDE source, remove Go porting guide, add hauling docs"
```

---

### Task 14: Run full test suite and verify

**Step 1: Run all tests**

Run: `pytest tests/ -v`
Expected: All tests PASS

**Step 2: Start the app and do a manual smoke test**

Run: `python app.py`

Verify:
- Search works (e.g. search "Drake")
- Blueprint page loads with materials
- Chain page loads
- Shopping list works (flat mode, no location)
- Shopping list works (with build station selected)
- Chain shopping works (both modes)
- Profit page works

**Step 3: Final commit if any fixups needed**

```bash
git add -A
git commit -m "Final cleanup after SDE switch and hauling plan implementation"
```
