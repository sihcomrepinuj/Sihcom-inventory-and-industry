"""
setup_sde.py — Download and build a minimal SDE from Fuzzwork CSV files.

Run this once (and again whenever you want to update after a patch):
    python setup_sde.py

It will:
  1. Download 4 CSV files from fuzzwork.co.uk (~2 MB total)
  2. Build a SQLite database with just the tables we need (~18 MB)
  3. Verify the database is usable

Requires: pip install requests
"""

import csv
import io
import os
import sys
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SDE_DB = os.path.join(DATA_DIR, "sqlite-latest.sqlite")

BASE_URL = "https://www.fuzzwork.co.uk/dump/latest"

# Tables we need and their CSV URLs
TABLES = {
    "invTypes": {
        "url": f"{BASE_URL}/invTypes.csv",
        "columns": [
            ("typeID", "INTEGER PRIMARY KEY"),
            ("groupID", "INTEGER"),
            ("typeName", "TEXT"),
            ("description", "TEXT"),
            ("mass", "REAL"),
            ("volume", "REAL"),
            ("capacity", "REAL"),
            ("portionSize", "INTEGER"),
            ("raceID", "INTEGER"),
            ("basePrice", "REAL"),
            ("published", "INTEGER"),
            ("marketGroupID", "INTEGER"),
            ("iconID", "INTEGER"),
            ("soundID", "INTEGER"),
            ("graphicID", "INTEGER"),
        ],
    },
    "industryActivityMaterials": {
        "url": f"{BASE_URL}/industryActivityMaterials.csv",
        "columns": [
            ("typeID", "INTEGER"),
            ("activityID", "INTEGER"),
            ("materialTypeID", "INTEGER"),
            ("quantity", "INTEGER"),
        ],
    },
    "industryActivityProducts": {
        "url": f"{BASE_URL}/industryActivityProducts.csv",
        "columns": [
            ("typeID", "INTEGER"),
            ("activityID", "INTEGER"),
            ("productTypeID", "INTEGER"),
            ("quantity", "INTEGER"),
        ],
    },
    "industryActivity": {
        "url": f"{BASE_URL}/industryActivity.csv",
        "columns": [
            ("typeID", "INTEGER"),
            ("activityID", "INTEGER"),
            ("time", "INTEGER"),
        ],
    },
}


def download_csv(url: str) -> str:
    """Download a CSV file and return its content as a string."""
    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' is required.  pip install requests")
        sys.exit(1)

    print(f"  Downloading {url.split('/')[-1]}...", end=" ", flush=True)
    resp = requests.get(url)
    resp.raise_for_status()
    size_kb = len(resp.content) / 1024
    print(f"({size_kb:.0f} KB)")
    return resp.text


def build_database():
    """Download CSVs and build a minimal SQLite database."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # Remove existing database if present
    if os.path.exists(SDE_DB):
        os.remove(SDE_DB)

    conn = sqlite3.connect(SDE_DB)
    cur = conn.cursor()

    for table_name, spec in TABLES.items():
        # Create table
        col_defs = ", ".join(f"{name} {dtype}" for name, dtype in spec["columns"])
        cur.execute(f"CREATE TABLE {table_name} ({col_defs})")

        # Download and import CSV
        csv_text = download_csv(spec["url"])
        reader = csv.DictReader(io.StringIO(csv_text))

        col_names = [name for name, _ in spec["columns"]]
        placeholders = ", ".join("?" * len(col_names))
        insert_sql = f"INSERT INTO {table_name} ({', '.join(col_names)}) VALUES ({placeholders})"

        rows = []
        for row in reader:
            values = []
            for col_name, col_type in spec["columns"]:
                raw = row.get(col_name, "")
                if raw == "" or raw == "None":
                    values.append(None)
                elif "INTEGER" in col_type:
                    values.append(int(raw))
                elif "REAL" in col_type:
                    values.append(float(raw))
                else:
                    values.append(raw)
            rows.append(values)

        cur.executemany(insert_sql, rows)
        print(f"  {table_name}: {len(rows):,} rows imported")

    # Create indexes for fast lookups
    print("  Creating indexes...")
    cur.execute("CREATE INDEX idx_invTypes_name ON invTypes(typeName)")
    cur.execute("CREATE INDEX idx_iam_typeID ON industryActivityMaterials(typeID, activityID)")
    cur.execute("CREATE INDEX idx_iap_typeID ON industryActivityProducts(typeID, activityID)")
    cur.execute("CREATE INDEX idx_iap_product ON industryActivityProducts(productTypeID)")
    cur.execute("CREATE INDEX idx_ia_typeID ON industryActivity(typeID, activityID)")

    conn.commit()
    conn.close()

    size_mb = os.path.getsize(SDE_DB) / 1024 / 1024
    print(f"\n  Database saved to {SDE_DB} ({size_mb:.1f} MB)")


def verify_sde():
    """Quick sanity check on the database."""
    conn = sqlite3.connect(SDE_DB)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cur.fetchall()}

    all_ok = True
    for t in TABLES:
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


# Keep these names for backwards compatibility with app.py imports
def download_sde():
    """Download CSVs and build the database."""
    build_database()


def decompress_sde():
    """No-op — kept for backwards compatibility. The new approach doesn't use bz2."""
    pass


def main():
    print("=" * 60)
    print("EVE Online SDE Setup (Fuzzwork CSV -> SQLite)")
    print("=" * 60)

    if os.path.exists(SDE_DB):
        print(f"\nExisting SDE found at {SDE_DB}")
        resp = input("Re-download and replace? [y/N]: ").strip().lower()
        if resp != "y":
            print("Keeping existing SDE.")
            verify_sde()
            return

    build_database()
    verify_sde()


if __name__ == "__main__":
    main()
