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
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SDE_DB = os.path.join(DATA_DIR, "sqlite-latest.sqlite")
BUILD_FILE = os.path.join(DATA_DIR, "sde-build.txt")
CONVERTER_DIR = os.path.join(PROJECT_DIR, "tools", "eve-sde-converter")
SDE_WORK_DIR = os.path.join(CONVERTER_DIR, "sde")

CCP_BASE_URL = "https://developers.eveonline.com/static-data/tranquility"


def read_local_build() -> str | None:
    """Read the SDE build number we last installed. None if unknown."""
    if not os.path.exists(BUILD_FILE):
        return None
    try:
        with open(BUILD_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_local_build(build: str) -> None:
    """Record the SDE build number alongside the database."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BUILD_FILE, "w") as f:
        f.write(str(build))


def is_sde_current() -> bool:
    """Return True if the local SDE matches CCP's latest build.

    Fails open: if CCP is unreachable or the local build is unknown
    but the SDE file exists, returns True for the network-error case
    (don't block startup on transient errors) and False for the
    unknown-local case (force a refresh once so we get on the tracker).
    """
    local = read_local_build()
    try:
        latest = get_latest_build()
    except Exception:
        return True  # network error — use whatever's on disk
    if local is None:
        return False  # unknown local build → refresh once to record it
    return local == latest


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
    # Force UTF-8 for the child's stdio: the converter prints Unicode
    # glyphs that Windows' default cp1252 codec can't encode when stdout
    # is a pipe rather than a console.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "Load.py", "sqlite"],
        cwd=CONVERTER_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if result.returncode != 0:
        print(f"  Converter stderr:\n{result.stderr}")
        # Include the tail of stderr in the exception so failures are
        # diagnosable from the app's error page without combing logs.
        tail = (result.stderr or "").strip().splitlines()[-10:]
        snippet = "\n".join(tail) if tail else "(no stderr)"
        raise RuntimeError(
            f"eve-sde-converter failed (exit {result.returncode}):\n{snippet}"
        )
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
# Pinned to match the SHA committed in our .gitmodules — keep in sync
# when bumping the submodule via `git submodule update --remote`.
CONVERTER_REPO = "noirsoldats/eve-sde-converter"
CONVERTER_PINNED_SHA = "f1f03f3d4ae7c000994e8646e411b461f6ed7811"


def ensure_converter():
    """Ensure tools/eve-sde-converter/ is populated.

    Tries the git submodule path first (fast, works locally and in any
    env that has both git and a real .git/ directory). Falls back to a
    GitHub tarball download via stdlib urllib + tarfile so we don't
    depend on git being installed at runtime — Railway's runtime image
    strips git.
    """
    load_py = os.path.join(CONVERTER_DIR, "Load.py")
    if os.path.exists(load_py):
        return

    git = shutil.which("git")
    if git and os.path.isdir(os.path.join(PROJECT_DIR, ".git")):
        print("  Converter source missing — initializing submodule...", flush=True)
        sub = subprocess.run(
            [git, "submodule", "update", "--init", "--recursive", "tools/eve-sde-converter"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
        )
        if sub.returncode == 0 and os.path.exists(load_py):
            print("  Submodule initialized.")
            return
        print(
            f"  Submodule init failed (rc={sub.returncode}); falling back to tarball.",
            flush=True,
        )

    print(
        f"  Fetching converter tarball ({CONVERTER_PINNED_SHA[:7]})...",
        flush=True,
    )
    url = f"https://github.com/{CONVERTER_REPO}/archive/{CONVERTER_PINNED_SHA}.tar.gz"
    try:
        with urllib.request.urlopen(url) as resp:
            tarball = resp.read()
    except Exception as e:
        raise RuntimeError(f"Failed to download converter tarball from {url}: {e}")

    os.makedirs(os.path.dirname(CONVERTER_DIR), exist_ok=True)
    if os.path.exists(CONVERTER_DIR):
        shutil.rmtree(CONVERTER_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            tf.extractall(tmp)
        # GitHub archive tarballs wrap everything in <repo>-<sha>/
        entries = os.listdir(tmp)
        if len(entries) != 1:
            raise RuntimeError(f"Unexpected tarball layout: {entries}")
        shutil.move(os.path.join(tmp, entries[0]), CONVERTER_DIR)

    if not os.path.exists(load_py):
        raise RuntimeError(
            f"Tarball extracted but Load.py still missing at {load_py}"
        )
    print("  Converter source ready.")


def build_database():
    """Full pipeline: bootstrap converter, download, extract, convert, install, record build."""
    ensure_converter()
    build = get_latest_build()
    zip_path = download_sde(build)
    extract_sde(zip_path)
    write_converter_config()
    run_converter()
    install_database()
    write_local_build(build)


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
