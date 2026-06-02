"""Persistence for the user's build list and global build/buy choices.

Stores intent only (targets + which components to buy instead of build),
never execution progress. Shape: {"targets": [...], "buy_set": [type_id, ...]},
where each target carries its own "build_station" (<int|None>).

The file lives in DATA_DIR when that env var is set (e.g. a Railway volume
mounted at /data), so the build list survives redeploys. It defaults to this
module's directory for local use and tests.
"""
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
DEFAULT_PATH = DATA_DIR / "build_list.json"


def load(path=DEFAULT_PATH) -> dict:
    """Return {"targets": [...], "buy_set": [...]}.

    Backward-compatible: a legacy bare-list file loads as
    {"targets": <list>, "buy_set": []}. Each target is normalized to carry a
    per-target "build_station" (defaulting to None). Any legacy top-level
    global "build_station" is dropped (the value now lives on each target).
    """
    p = Path(path)
    if not p.exists():
        return {"targets": [], "buy_set": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"targets": data, "buy_set": []}
    data.setdefault("targets", [])
    data.setdefault("buy_set", [])
    data.pop("build_station", None)
    for t in data["targets"]:
        t.setdefault("build_station", None)
    return data


def save(data: dict, path=DEFAULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_target(target: dict, path=DEFAULT_PATH) -> None:
    """Upsert a target by type_id (replace in place, else append)."""
    target.setdefault("build_station", None)
    data = load(path)
    targets = data["targets"]
    for i, t in enumerate(targets):
        if t["type_id"] == target["type_id"]:
            targets[i] = target
            break
    else:
        targets.append(target)
    save(data, path)


def remove_target(type_id: int, path=DEFAULT_PATH) -> None:
    data = load(path)
    data["targets"] = [t for t in data["targets"] if t["type_id"] != type_id]
    save(data, path)


def toggle_buy(type_id: int, path=DEFAULT_PATH) -> None:
    """Flip a component's membership in the global buy_set."""
    data = load(path)
    bs = data["buy_set"]
    if type_id in bs:
        bs.remove(type_id)
    else:
        bs.append(type_id)
    save(data, path)


def set_target_station(type_id: int, station_id: int | None, path=DEFAULT_PATH) -> None:
    """Set (or clear, with None) the build station for one target."""
    data = load(path)
    for t in data["targets"]:
        if t["type_id"] == type_id:
            t["build_station"] = station_id
            break
    save(data, path)
