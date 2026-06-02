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
               build_list.set_target_station):
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
    """GET /api/plan returns one block per product, each with its buckets."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["type_id"] == 2456
    assert set(data[0]["buckets"].keys()) == {"ready", "in_progress", "blocked", "buy"}


def test_build_station_sets_per_target(client):
    """POST /build-station persists the station on ONE target, redirects to /plan."""
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.post(
        "/build-station",
        data={"type_id": "2456", "station_id": "60003760"},
    )
    assert resp.status_code == 302
    assert "/plan" in resp.headers["Location"]
    target = next(t for t in build_list.load()["targets"] if t["type_id"] == 2456)
    assert target["build_station"] == 60003760


def test_build_station_empty_clears_per_target(client):
    """An empty station_id clears that target's build_station."""
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    build_list.set_target_station(2456, 60003760)
    resp = client.post(
        "/build-station",
        data={"type_id": "2456", "station_id": ""},
    )
    assert resp.status_code == 302
    target = next(t for t in build_list.load()["targets"] if t["type_id"] == 2456)
    assert target["build_station"] is None


def test_compute_plan_returns_one_block_per_product(monkeypatch):
    """_compute_plan yields one block per resolved target, each with buckets."""
    import app
    import plan
    t1 = plan.Target(type_id=1, name="A", blueprint_type_id=10, needed=1,
                     children=[], build_station=111)
    t2 = plan.Target(type_id=2, name="B", blueprint_type_id=20, needed=1,
                     children=[], build_station=None)
    monkeypatch.setattr(app, "_resolve_targets", lambda sde, targets: [t1, t2])

    class _SDE:
        def get_type_volumes(self, ids):
            return {}

    monkeypatch.setattr(app, "get_sde", lambda: _SDE())
    monkeypatch.setattr(app, "get_authed_preston_from_session", lambda: None)
    monkeypatch.setattr(app.build_list, "load",
                        lambda: {"targets": [{"type_id": 1}, {"type_id": 2}], "buy_set": []})
    with app.app.test_request_context():
        blocks, authed, ctx = app._compute_plan()
    assert [b["type_id"] for b in blocks] == [1, 2]
    assert authed is False
    assert all("buckets" in b and set(b["buckets"]) == {"ready", "in_progress", "blocked", "buy"}
               for b in blocks)


def test_plan_unauthed_shows_both_product_names(client):
    """GET /plan (unauthed) with two targets renders both product blocks."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    build_list.add_target({
        "type_id": 2454, "name": "Hornet I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert b"Warrior I" in resp.data
    assert b"Hornet I" in resp.data


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


def test_materials_flat_view_unauthed_aggregated_no_station(client):
    """GET /materials?view=flat (unauthed) shows the aggregated BOM, no station/haul.

    The flat view is now a whole-list aggregated bill of materials (build station
    is per-product on the Plan page), so the old location-aware columns are gone
    entirely and the to-buy column is gated behind auth.
    """
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/materials?view=flat")
    assert resp.status_code == 200
    assert b"Total required" in resp.data
    # No station picker, no haul, no per-product on Materials.
    assert b"Haul from" not in resp.data
    assert b"At station" not in resp.data
    # To-buy is gated behind auth.
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


def test_get_station_list_from_blueprints_ranked_and_named(monkeypatch):
    import app
    monkeypatch.setattr(app.esi, "get_cached_blueprints",
                        lambda p, eid, is_corp=False: (
                            [{"location_id": 1000}, {"location_id": 1000}, {"location_id": 2000}]
                            if not is_corp else []))
    monkeypatch.setattr(app.esi, "get_cached_location_name",
                        lambda p, sid, t: f"Station {sid}")
    out = app._get_station_list(object(), 1, corporation_id=99)
    ids = [s["id"] for s in out]
    assert set(ids) == {1000, 2000}            # char 1000x2 + 2000x1
    assert out[0]["id"] == 1000                # 1000 has the most (2) -> ranked first
    assert out[0]["name"] == "Station 1000"


def test_get_station_list_falls_back_to_jobs_when_no_blueprints(monkeypatch):
    import app
    monkeypatch.setattr(app.esi, "get_cached_blueprints", lambda p, eid, is_corp=False: [])
    monkeypatch.setattr(app.esi, "fetch_industry_jobs",
                        lambda p, cid, include_completed=False: [
                            {"activity_id": 1, "facility_id": 5000}])
    monkeypatch.setattr(app.esi, "get_cached_location_name", lambda p, sid, t: f"S{sid}")
    out = app._get_station_list(object(), 1, corporation_id=None)
    assert [s["id"] for s in out] == [5000]    # job-history fallback


def test_get_station_list_caps_at_15(monkeypatch):
    import app
    monkeypatch.setattr(app.esi, "get_cached_blueprints",
                        lambda p, eid, is_corp=False: [{"location_id": i} for i in range(100, 130)])
    monkeypatch.setattr(app.esi, "get_cached_location_name", lambda p, sid, t: str(sid))
    out = app._get_station_list(object(), 1)
    assert len(out) == 15


def test_get_station_list_merges_char_and_corp_counts(monkeypatch):
    import app
    # Character holds 1 BP at 1000 and 1 at 2000; corp holds 2 BPs at 1000.
    # Merged: location 1000 -> 3 blueprints, 2000 -> 1, so 1000 ranks first.
    monkeypatch.setattr(app.esi, "get_cached_blueprints",
        lambda p, eid, is_corp=False: (
            [{"location_id": 2000}, {"location_id": 1000}]      # char
            if not is_corp else
            [{"location_id": 1000}, {"location_id": 1000}]))    # corp
    monkeypatch.setattr(app.esi, "get_cached_location_name", lambda p, sid, t: str(sid))
    out = app._get_station_list(object(), 1, corporation_id=99)
    assert [s["id"] for s in out] == [1000, 2000]   # 1000 (3) outranks 2000 (1)


