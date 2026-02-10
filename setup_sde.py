"""
setup_sde.py — Download and prepare the Fuzzwork SQLite SDE.

Run this once (and again whenever you want to update after a patch):
    python setup_sde.py

It will:
  1. Download sqlite-latest.sqlite.bz2 from fuzzwork.co.uk
  2. Decompress it into the data/ subdirectory
  3. Verify the database is usable

Requires: pip install requests
"""

import os
import sys
import bz2
import sqlite3
import shutil

# We avoid network imports at the top so the rest of the codebase can
# import helpers without requests installed.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SDE_URL = "https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"
SDE_BZ2 = os.path.join(DATA_DIR, "sqlite-latest.sqlite.bz2")
SDE_DB = os.path.join(DATA_DIR, "sqlite-latest.sqlite")


def download_sde():
    """Download the compressed SDE from Fuzzwork."""
    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' is required.  pip install requests")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"Downloading SDE from {SDE_URL} ...")
    print("(This is ~130 MB compressed, may take a few minutes)")

    resp = requests.get(SDE_URL, stream=True)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(SDE_BZ2, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                mb = downloaded / 1024 / 1024
                print(f"\r  {mb:.1f} MB ({pct:.0f}%)", end="", flush=True)

    print(f"\n  Saved to {SDE_BZ2}")


def decompress_sde():
    """Decompress the .bz2 file to .sqlite."""
    print("Decompressing ...")

    with bz2.open(SDE_BZ2, "rb") as src, open(SDE_DB, "wb") as dst:
        shutil.copyfileobj(src, dst)

    size_mb = os.path.getsize(SDE_DB) / 1024 / 1024
    print(f"  Extracted to {SDE_DB} ({size_mb:.0f} MB)")

    # Clean up compressed file
    os.remove(SDE_BZ2)
    print("  Removed compressed file.")


def verify_sde():
    """Quick sanity check on the database."""
    conn = sqlite3.connect(SDE_DB)
    cur = conn.cursor()

    # Check key tables exist
    tables_needed = [
        "invTypes",
        "industryActivityMaterials",
        "industryActivityProducts",
        "industryActivity",
    ]
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cur.fetchall()}

    all_ok = True
    for t in tables_needed:
        if t in existing:
            count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {count:,} rows")
        else:
            print(f"  WARNING: Table '{t}' not found!")
            all_ok = False

    conn.close()

    if all_ok:
        print("\nSDE is ready!")
    else:
        print("\nWARNING: Some tables are missing. The SDE may be incomplete.")


def main():
    print("=" * 60)
    print("EVE Online SDE Setup (Fuzzwork SQLite)")
    print("=" * 60)

    if os.path.exists(SDE_DB):
        print(f"\nExisting SDE found at {SDE_DB}")
        resp = input("Re-download and replace? [y/N]: ").strip().lower()
        if resp != "y":
            print("Keeping existing SDE.")
            verify_sde()
            return

    download_sde()
    decompress_sde()
    verify_sde()


if __name__ == "__main__":
    main()
