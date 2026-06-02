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
    """GET /materials?view=flat (unauthed) shows totals but hides location columns."""
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })
    resp = client.get("/materials?view=flat")
    assert resp.status_code == 200
    assert b"Total required" in resp.data
    # The location-aware columns (at-station / haul / to-buy) are gated behind auth.
    assert b"Haul from" not in resp.data
    assert b"To buy" not in resp.data


def test_materials_and_plan_share_buy_basis(client):
    """Regression: pins the empty-asset/shared-path buy basis.

    Both flow through the same flatten + calculate_deficit / enrich_buy path with
    an empty (unauthed) location index, so for a given material the gross need —
    and therefore the amount to buy — must match across the two screens.

    We assert at the function level (no brittle HTML scraping): the to_buy figure
    computed for the flat Materials view equals the plan's buy bucket to_buy for
    the same material type. Tritanium (34) is a stable Warrior I input.
    """
    if not _sde_available():
        pytest.skip("SDE database not available — run setup_sde.py first")
    import build_list as bl
    import hauling
    import plan as plan_mod
    from sde import flatten_material_tree

    build_list.add_target({
        "type_id": 2456, "name": "Warrior I", "qty": 1, "runs": 1, "me": 10,
        "structure_bonus": 0.0,
    })

    sde = app_module.get_sde()
    data = bl.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])

    # Materials flat basis: flatten + calculate_deficit with empty loc_index.
    resolved = app_module._resolve_targets(sde, targets)
    nodes = [child for t in resolved for child in t.children]
    flat = flatten_material_tree(nodes, buy_set)
    volumes = sde.get_type_volumes([r["type_id"] for r in flat])
    deficit = hauling.calculate_deficit(flat, {}, None, volumes)
    mat_buy = {d["type_id"]: d["to_buy"] for d in deficit}

    # Plan buy basis: merge_trees + classify + enrich_buy with empty loc_index.
    graph = plan_mod.merge_trees(resolved, buy_set)
    buckets = plan_mod.classify(graph, {}, [], None, buy_set)
    plan_vol = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
    enriched = plan_mod.enrich_buy(buckets["buy"], {}, None, plan_vol)
    plan_buy = {r["type_id"]: r["to_buy"] for r in enriched}

    # They must cover the same materials and agree on every to_buy figure.
    assert mat_buy, "expected at least one material to buy"
    assert set(mat_buy) == set(plan_buy)
    for tid, qty in mat_buy.items():
        assert plan_buy[tid] == qty, f"buy basis differs for type {tid}"


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


