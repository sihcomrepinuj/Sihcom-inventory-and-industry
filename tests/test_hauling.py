"""Tests for hauling.py — deficit calculation for location-aware shopping."""

from hauling import calculate_deficit


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
