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
    # The build_list functions bind DEFAULT_PATH as a default arg at definition
    # time, so reassigning the module attribute alone is not enough — patch the
    # bound default of each so the routes write to the temp file, never the repo.
    for fn in (build_list.load, build_list.save, build_list.add_target,
               build_list.remove_target, build_list.toggle_buy,
               build_list.set_build_station):
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
    """GET / renders the (empty) build list with the build-plan affordance."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Build List" in resp.data
    # The prominent call-to-action and the empty-state message both render.
    assert b"Build the plan" in resp.data
    assert b"empty" in resp.data


def test_index_search_shows_manufacturable_with_add(client):
    """GET /?q=drake lists the manufacturable Drake with an Add affordance."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    resp = client.get("/?q=drake")
    assert resp.status_code == 200
    assert b"Drake" in resp.data
    assert b"Add" in resp.data


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
    assert len(stored["targets"]) == 1
    assert stored["targets"][0]["type_id"] == 34
    assert stored["targets"][0]["name"] == "Tritanium"

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Tritanium" in resp.data


def test_remove_item(client):
    """POST /build-list/remove drops the target."""
    build_list.add_target({
        "type_id": 34, "name": "Tritanium", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.post("/build-list/remove/34")
    assert resp.status_code == 302
    assert build_list.load()["targets"] == []


def test_plan_unauthed_returns_200(client):
    """GET /plan with no login computes a plan (all blocked/buy) and renders."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    # A manufacturable item so the graph isn't empty (Warrior I, a drone).
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert b"Action Plan" in resp.data
    # Action-first section headers + the refresh affordance always render
    # (sections are present in the DOM; hidden only when their bucket is empty).
    assert b"Ready to start" in resp.data
    assert b"Refresh" in resp.data


def test_plan_unauthed_shows_login_banner(client):
    """Without ESI login, /plan surfaces the login banner explaining the limits."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert b"Not logged in" in resp.data
    assert b"/login" in resp.data


def test_api_plan_unauthed_returns_json(client):
    """GET /api/plan returns the four buckets as JSON."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"ready", "in_progress", "blocked", "buy"}


def test_build_station_persists(client):
    """POST /build-station saves the station id and redirects to the plan."""
    resp = client.post(
        "/build-station",
        data={"station_id": "60003760", "next": "plan_view"},
    )
    assert resp.status_code == 302
    assert build_list.load()["build_station"] == 60003760


def test_build_station_empty_clears(client):
    """An empty station_id clears the saved station (back to most-used default)."""
    build_list.set_build_station(60003760)
    resp = client.post("/build-station", data={"station_id": ""})
    assert resp.status_code == 302
    assert build_list.load()["build_station"] is None


def test_build_station_next_materials_redirects(client):
    """next=materials redirects to /materials carrying the view param."""
    resp = client.post(
        "/build-station",
        data={"station_id": "60003760", "next": "materials", "view": "flat"},
    )
    assert resp.status_code == 302
    assert "/materials?view=flat" in resp.headers["Location"]


def test_plan_unauthed_no_station_picker(client):
    """Unauthed /plan renders the buy table without a station picker (no stations)."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/plan")
    assert resp.status_code == 200
    # No auth → empty station list → the "Building at" picker is not rendered.
    assert b"Building at" not in resp.data


def test_materials_tree_view_renders(client):
    """GET /materials?view=tree renders the target chain with build/buy toggles.

    Warrior I has buildable intermediates (it's a manufactured drone whose inputs
    include manufacturable components), so a "Buy instead" toggle appears. This
    also exercises the recursive node() macro without a Jinja recursion error.
    """
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/materials?view=tree")
    assert resp.status_code == 200
    assert b"Materials" in resp.data
    assert b"Warrior I" in resp.data
    assert b"Buy instead" in resp.data


def test_materials_flat_view_unauthed_hides_to_buy(client):
    """GET /materials?view=flat (unauthed) shows totals but hides the to-buy column."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/materials?view=flat")
    assert resp.status_code == 200
    assert b"Total required" in resp.data
    # The owned/to-buy columns are gated behind auth.
    assert b"To buy" not in resp.data


def test_materials_toggle_flips_buy_set(client):
    """POST /materials/toggle/<id> flips buy_set membership and redirects.

    The toggle route doesn't validate chain membership, so an arbitrary type_id
    exercises the flip cleanly.
    """
    tid = 11399  # Morphite — an arbitrary component id; flip semantics only.
    resp = client.post(f"/materials/toggle/{tid}", data={"view": "tree"})
    assert resp.status_code == 302
    assert tid in build_list.load()["buy_set"]
    # Toggling again removes it.
    resp = client.post(f"/materials/toggle/{tid}", data={"view": "tree"})
    assert resp.status_code == 302
    assert tid not in build_list.load()["buy_set"]
