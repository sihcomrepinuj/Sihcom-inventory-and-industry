"""Persistence for the user's build list (target intent only — never progress)."""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "build_list.json"


def load(path=DEFAULT_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def save(targets: list[dict], path=DEFAULT_PATH) -> None:
    Path(path).write_text(json.dumps(targets, indent=2), encoding="utf-8")


def add(target: dict, path=DEFAULT_PATH) -> None:
    """Upsert by type_id: replace an existing target in place, else append.

    Keeps the build list a set keyed by type_id (remove() already deletes all
    matches), so adding the same product twice updates it rather than creating a
    duplicate row.
    """
    targets = load(path)
    for i, t in enumerate(targets):
        if t["type_id"] == target["type_id"]:
            targets[i] = target
            break
    else:
        targets.append(target)
    save(targets, path)


def remove(type_id: int, path=DEFAULT_PATH) -> None:
    targets = [t for t in load(path) if t["type_id"] != type_id]
    save(targets, path)
