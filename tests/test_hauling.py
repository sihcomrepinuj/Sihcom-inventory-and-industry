"""Tests for hauling.py — deficit calculation and build-capacity projection."""

from hauling import calculate_build_capacity, calculate_deficit


def test_calculate_deficit_all_at_station():
    """Materials already at build station show as ready."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    loc_index = {34: {1000: 150}}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["at_station"] == 100
    assert result[0]["elsewhere"] == {}
    assert result[0]["to_buy"] == 0


def test_calculate_deficit_split_locations():
    """Materials at different locations show where to haul from."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 500}]
    loc_index = {34: {1000: 200, 2000: 100}}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["at_station"] == 200
    assert result[0]["elsewhere"] == {2000: 100}
    assert result[0]["to_buy"] == 200


def test_calculate_deficit_nothing_owned():
    """No assets means everything must be bought."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 500}]
    loc_index = {}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["at_station"] == 0
    assert result[0]["elsewhere"] == {}
    assert result[0]["to_buy"] == 500


def test_calculate_deficit_volumes():
    """Volume calculations for hauling and buying."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 1000}]
    loc_index = {34: {1000: 300, 2000: 200}}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["elsewhere_volume"] == 200 * 0.01
    assert result[0]["to_buy_volume"] == 500 * 0.01


def test_calculate_deficit_multiple_elsewhere():
    """Materials scattered across multiple non-station locations."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 1000}]
    loc_index = {34: {1000: 100, 2000: 200, 3000: 300}}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["at_station"] == 100
    assert result[0]["elsewhere"] == {2000: 200, 3000: 300}
    assert result[0]["to_buy"] == 400


def test_calculate_deficit_more_than_needed():
    """Excess inventory doesn't go negative."""
    needed = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    loc_index = {34: {1000: 500}}
    volumes = {34: 0.01}
    result = calculate_deficit(needed, loc_index, build_station=1000, volumes=volumes)
    assert result[0]["at_station"] == 100
    assert result[0]["to_buy"] == 0
    assert result[0]["elsewhere_volume"] == 0.0


# ------------------------------------------------------------------
# calculate_build_capacity
# ------------------------------------------------------------------

def test_build_capacity_empty_materials():
    """No materials → None capacity (unbounded), empty constraints."""
    result = calculate_build_capacity([], {})
    assert result["capacity_runs"] is None
    assert result["constraints"] == []


def test_build_capacity_single_material_ample():
    """One material with plenty owned → capacity = owned // per_run."""
    materials = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    assets = {34: 1050}
    result = calculate_build_capacity(materials, assets)
    assert result["capacity_runs"] == 10
    assert len(result["constraints"]) == 1
    assert result["constraints"][0]["max_runs"] == 10
    assert result["constraints"][0]["owned"] == 1050
    assert result["constraints"][0]["per_run"] == 100


def test_build_capacity_zero_owned():
    """Material not in asset_index counts as zero → capacity = 0."""
    materials = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    assets = {}
    result = calculate_build_capacity(materials, assets)
    assert result["capacity_runs"] == 0
    assert result["constraints"][0]["owned"] == 0
    assert result["constraints"][0]["max_runs"] == 0


def test_build_capacity_bottleneck_sorted_first():
    """Tightest constraint dictates capacity and sorts first."""
    materials = [
        {"type_id": 34, "name": "Tritanium", "quantity": 100},
        {"type_id": 11399, "name": "Morphite", "quantity": 5},
        {"type_id": 40, "name": "Pyerite", "quantity": 50},
    ]
    assets = {34: 10_000, 11399: 12, 40: 5_000}
    result = calculate_build_capacity(materials, assets)
    # Morphite: 12 // 5 = 2 (tightest)
    # Tritanium: 10000 // 100 = 100
    # Pyerite: 5000 // 50 = 100
    assert result["capacity_runs"] == 2
    assert result["constraints"][0]["name"] == "Morphite"
    assert result["constraints"][0]["max_runs"] == 2
    assert result["constraints"][1]["max_runs"] == 100
    assert result["constraints"][2]["max_runs"] == 100


def test_build_capacity_tied_bottlenecks():
    """Two materials tied for tightest both appear with the same max_runs."""
    materials = [
        {"type_id": 1, "name": "A", "quantity": 10},
        {"type_id": 2, "name": "B", "quantity": 20},
    ]
    assets = {1: 30, 2: 60}  # both → 3 runs
    result = calculate_build_capacity(materials, assets)
    assert result["capacity_runs"] == 3
    assert result["constraints"][0]["max_runs"] == 3
    assert result["constraints"][1]["max_runs"] == 3


def test_build_capacity_skips_zero_quantity():
    """Materials with quantity <= 0 don't constrain capacity."""
    materials = [
        {"type_id": 1, "name": "Free", "quantity": 0},
        {"type_id": 2, "name": "Real", "quantity": 100},
    ]
    assets = {2: 500}
    result = calculate_build_capacity(materials, assets)
    assert result["capacity_runs"] == 5
    assert len(result["constraints"]) == 1
    assert result["constraints"][0]["name"] == "Real"


def test_build_capacity_floor_division():
    """Fractional capacity rounds down — can't half-build a run."""
    materials = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    assets = {34: 199}
    result = calculate_build_capacity(materials, assets)
    assert result["capacity_runs"] == 1  # 199 // 100 = 1, not 1.99
