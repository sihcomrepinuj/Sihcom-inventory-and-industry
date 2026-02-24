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
