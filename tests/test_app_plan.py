"""Smoke tests for the build-list + action-plan Flask routes (Task 5).

These exercise the route wiring, not the pure logic (covered by test_plan.py /
test_build_list.py). The /plan route needs the SDE database, so tests that hit
it skip gracefully when data/sqlite-latest.sqlite is missing.
"""
import json

import pytest

import app as app_module
import build_list


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with the build list pointed at a temp file.

    Ensures tests never read or write a real build_list.json in the repo root.
    """
    tmp_list = tmp_path / "build_list.json"
    monkeypatch.setattr(build_list, "DEFAULT_PATH", tmp_list)
    # load/save/add/remove bind DEFAULT_PATH as a default arg at definition
    # time, so reassigning the module attribute alone is not enough — patch the
    # bound default of each so the routes write to the temp file, never the repo.
    for fn in (build_list.load, build_list.save, build_list.add, build_list.remove):
        monkeypatch.setattr(fn, "__defaults__", (tmp_list,))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _sde_available():
    try:
        from sde import SDE
        SDE().close()
        return True
    except Exception:
        return False


def test_index_empty_build_list(client):
    """GET / renders the (empty) build list and returns 200."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Build List" in resp.data


def test_add_then_index_shows_item(client):
    """POST /build-list/add persists, then GET / shows the item name."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    # Tritanium (type_id 34) — added by id; name resolved from SDE.
    resp = client.post(
        "/build-list/add",
        data={"type_id": "34", "qty": "5", "me": "10"},
    )
    assert resp.status_code == 302  # redirect back to index

    stored = json.loads(build_list.DEFAULT_PATH.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["type_id"] == 34
    assert stored[0]["name"] == "Tritanium"

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Tritanium" in resp.data


def test_remove_item(client):
    """POST /build-list/remove drops the target."""
    build_list.add({
        "type_id": 34, "name": "Tritanium", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0, "buy_set": [], "build_station": None,
    })
    resp = client.post("/build-list/remove/34")
    assert resp.status_code == 302
    assert build_list.load() == []


def test_plan_unauthed_returns_200(client):
    """GET /plan with no login computes a plan (all blocked/buy) and renders."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    # A manufacturable item so the graph isn't empty (Warrior I, a drone).
    build_list.add({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0, "buy_set": [], "build_station": None,
    })
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert b"Action Plan" in resp.data


def test_api_plan_unauthed_returns_json(client):
    """GET /api/plan returns the four buckets as JSON."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"ready", "in_progress", "blocked", "buy"}
