"""Tests for esi.py — asset indexing and location tracking."""

import esi as esi_mod
from esi import build_asset_index, build_location_asset_index


# -- Existing flat index (regression) --

def test_build_asset_index_basic():
    """Flat index sums quantities across all locations."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 200, "location_id": 2000},
        {"type_id": 35, "quantity": 50, "location_id": 1000},
    ]
    index = build_asset_index(assets)
    assert index[34] == 300
    assert index[35] == 50


def test_build_asset_index_empty():
    """Empty asset list produces empty index."""
    assert build_asset_index([]) == {}


# -- Location-aware index --

def test_build_location_asset_index_basic():
    """Location index preserves per-location quantities."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 200, "location_id": 2000},
        {"type_id": 35, "quantity": 50, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[34][1000] == 100
    assert index[34][2000] == 200
    assert index[35][1000] == 50


def test_build_location_asset_index_same_location():
    """Multiple stacks at same location are summed."""
    assets = [
        {"type_id": 34, "quantity": 100, "location_id": 1000},
        {"type_id": 34, "quantity": 50, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[34][1000] == 150


def test_build_location_asset_index_empty():
    """Empty asset list produces empty index."""
    assert build_location_asset_index([]) == {}


def test_build_location_asset_index_default_quantity():
    """Assets without explicit quantity default to 1 (e.g. ships)."""
    assets = [
        {"type_id": 587, "location_id": 1000},
    ]
    index = build_location_asset_index(assets)
    assert index[587][1000] == 1


# -- Manufacturing station detection --

from esi import extract_manufacturing_stations


def test_extract_manufacturing_stations_basic():
    """Extracts unique facility IDs from manufacturing jobs, ranked by frequency."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 1, "facility_id": 2000, "status": "active"},
        {"activity_id": 3, "facility_id": 3000, "status": "active"},  # TE research, not mfg
    ]
    stations = extract_manufacturing_stations(jobs)
    assert stations[0] == 1000
    assert stations[1] == 2000
    assert len(stations) == 2


def test_extract_manufacturing_stations_includes_reactions():
    """Reaction jobs (activity_id=9) are also relevant build stations."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
        {"activity_id": 9, "facility_id": 2000, "status": "active"},
    ]
    stations = extract_manufacturing_stations(jobs)
    assert 1000 in stations
    assert 2000 in stations


def test_extract_manufacturing_stations_empty():
    """No jobs means no stations."""
    assert extract_manufacturing_stations([]) == []


def test_extract_manufacturing_stations_completed_jobs():
    """Completed jobs still count — they reveal where you build."""
    jobs = [
        {"activity_id": 1, "facility_id": 1000, "status": "delivered"},
        {"activity_id": 1, "facility_id": 1000, "status": "active"},
    ]
    stations = extract_manufacturing_stations(jobs)
    assert stations == [1000]


# -- Blueprint station detection --

from esi import extract_blueprint_stations


def test_extract_blueprint_stations_ranks_by_count():
    """Unique blueprint locations, ranked by how many blueprints sit there."""
    bps = [
        {"type_id": 1, "location_id": 1000},
        {"type_id": 2, "location_id": 1000},
        {"type_id": 3, "location_id": 1000},
        {"type_id": 4, "location_id": 2000},
        {"type_id": 5, "location_id": 2000},
        {"type_id": 6, "location_id": 3000},
    ]
    stations = extract_blueprint_stations(bps)
    assert stations == [1000, 2000, 3000]   # 3, 2, 1 by count desc


def test_extract_blueprint_stations_empty():
    assert extract_blueprint_stations([]) == []


def test_extract_blueprint_stations_ignores_missing_location():
    bps = [{"type_id": 1}, {"type_id": 2, "location_id": 0}, {"type_id": 3, "location_id": 5000}]
    # missing key and falsy 0 are skipped; only 5000 remains
    assert extract_blueprint_stations(bps) == [5000]


def test_get_cached_blueprints_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(p, character_id):
        calls["n"] += 1
        return [{"type_id": 1, "location_id": 1000}]

    esi_mod._raw_blueprint_cache.clear()
    monkeypatch.setattr(esi_mod, "fetch_blueprints", fake_fetch)

    a = esi_mod.get_cached_blueprints(None, 42, is_corp=False)
    b = esi_mod.get_cached_blueprints(None, 42, is_corp=False)
    assert a == b == [{"type_id": 1, "location_id": 1000}]
    assert calls["n"] == 1   # second call served from cache


def test_get_cached_blueprints_corp_uses_corp_fetch(monkeypatch):
    esi_mod._raw_blueprint_cache.clear()
    monkeypatch.setattr(esi_mod, "fetch_corp_blueprints",
                        lambda p, cid: [{"type_id": 9, "location_id": 7000}])
    out = esi_mod.get_cached_blueprints(None, 99, is_corp=True)
    assert out == [{"type_id": 9, "location_id": 7000}]
