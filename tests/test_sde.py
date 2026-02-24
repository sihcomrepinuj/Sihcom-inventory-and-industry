"""Tests for sde.py — validates SDE queries work correctly.

These tests run against the actual SDE database (data/sqlite-latest.sqlite).
They verify that the schema and data we depend on are present and correct.
"""

import pytest
from sde import SDE, apply_me, calculate_materials, resolve_material_chain, flatten_material_tree


@pytest.fixture
def sde():
    """Provide an SDE instance for tests. Skips if database is missing."""
    try:
        s = SDE()
        yield s
        s.close()
    except FileNotFoundError:
        pytest.skip("SDE database not available — run setup_sde.py first")


# -- Schema validation --

def test_sde_has_required_tables(sde):
    """The SDE database must have the 4 tables our queries depend on."""
    cursor = sde.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row["name"] for row in cursor.fetchall()}
    assert "invTypes" in tables
    assert "industryActivityMaterials" in tables
    assert "industryActivityProducts" in tables
    assert "industryActivity" in tables


def test_invtypes_has_data(sde):
    """invTypes should have tens of thousands of rows."""
    count = sde.conn.execute("SELECT COUNT(*) as c FROM invTypes").fetchone()["c"]
    assert count > 10000


# -- Type lookups --

def test_get_type_name_tritanium(sde):
    """Tritanium (type_id=34) is a well-known mineral."""
    assert sde.get_type_name(34) == "Tritanium"


def test_get_type_names_bulk(sde):
    """Bulk lookup returns correct names."""
    names = sde.get_type_names([34, 35, 36])
    assert names[34] == "Tritanium"
    assert names[35] == "Pyerite"
    assert names[36] == "Mexallon"


def test_search_types_drake(sde):
    """Searching 'Drake' should find the Drake battlecruiser."""
    results = sde.search_types("Drake")
    names = [r["name"] for r in results]
    assert "Drake" in names


# -- Blueprint lookups --

def test_search_blueprints_drake(sde):
    """Searching blueprints for 'Drake' should find the Drake Blueprint."""
    results = sde.search_blueprints("Drake")
    product_names = [r["product_name"] for r in results]
    assert "Drake" in product_names


def test_find_blueprint_for_product_drake(sde):
    """Drake (type_id=24698) should have a manufacturing blueprint."""
    bp_id = sde.find_blueprint_for_product(24698)
    assert bp_id is not None


def test_find_product_for_blueprint(sde):
    """Drake Blueprint should produce a Drake."""
    bp_id = sde.find_blueprint_for_product(24698)
    product_id = sde.find_product_for_blueprint(bp_id)
    assert product_id == 24698


# -- Materials --

def test_get_manufacturing_materials_drake(sde):
    """Drake Blueprint should have manufacturing materials."""
    bp_id = sde.find_blueprint_for_product(24698)
    mats = sde.get_manufacturing_materials(bp_id)
    assert len(mats) > 0
    for mat in mats:
        assert "type_id" in mat
        assert "name" in mat
        assert "quantity" in mat
        assert mat["quantity"] > 0


def test_get_activity_time(sde):
    """Drake Blueprint should have a manufacturing time."""
    bp_id = sde.find_blueprint_for_product(24698)
    time_seconds = sde.get_activity_time(bp_id, 1)
    assert time_seconds is not None
    assert time_seconds > 0


# -- ME calculation (pure functions, no SDE needed) --

def test_apply_me_zero():
    """ME 0 should return base * runs."""
    assert apply_me(100, me_level=0, runs=1) == 100
    assert apply_me(100, me_level=0, runs=10) == 1000


def test_apply_me_ten():
    """ME 10 should reduce by 10%."""
    result = apply_me(100, me_level=10, runs=1)
    assert result == 90


def test_apply_me_minimum_one_per_run():
    """Even with high ME, you need at least 1 per run."""
    result = apply_me(1, me_level=10, runs=5)
    assert result >= 5


def test_apply_me_structure_bonus():
    """Structure bonus stacks with ME."""
    no_bonus = apply_me(1000, me_level=10, runs=1, structure_bonus=0)
    with_bonus = apply_me(1000, me_level=10, runs=1, structure_bonus=1.0)
    assert with_bonus < no_bonus


# -- Chain resolution --

def test_resolve_material_chain_drake(sde):
    """Drake material chain should resolve to a non-empty tree."""
    bp_id = sde.find_blueprint_for_product(24698)
    tree = resolve_material_chain(sde, bp_id, me_level=10, runs=1)
    assert len(tree) > 0


def test_flatten_material_tree_drake(sde):
    """Flattened Drake chain should produce a non-empty shopping list."""
    bp_id = sde.find_blueprint_for_product(24698)
    tree = resolve_material_chain(sde, bp_id, me_level=10, runs=1)
    flat = flatten_material_tree(tree)
    assert len(flat) > 0
    for item in flat:
        assert "type_id" in item
        assert "name" in item
        assert "quantity" in item
        assert item["quantity"] > 0
