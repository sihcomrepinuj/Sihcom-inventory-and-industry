"""Persistence for the user's build list and global build/buy choices.

Stores intent only (targets + which components to buy instead of build),
never execution progress. Shape: {"targets": [...], "buy_set": [type_id, ...]}.
"""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "build_list.json"


def load(path=DEFAULT_PATH) -> dict:
    """Return {"targets": [...], "buy_set": [...]}.

    Backward-compatible: a legacy bare-list file loads as
    {"targets": <list>, "buy_set": []}.
    """
    p = Path(path)
    if not p.exists():
        return {"targets": [], "buy_set": [], "build_station": None}
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"targets": data, "buy_set": [], "build_station": None}
    data.setdefault("targets", [])
    data.setdefault("buy_set", [])
    data.setdefault("build_station", None)
    return data


def save(data: dict, path=DEFAULT_PATH) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_target(target: dict, path=DEFAULT_PATH) -> None:
    """Upsert a target by type_id (replace in place, else append)."""
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


def set_build_station(station_id, path=DEFAULT_PATH) -> None:
    """Persist the global build station (an int facility id, or None to clear)."""
    data = load(path)
    data["build_station"] = station_id
    save(data, path)
