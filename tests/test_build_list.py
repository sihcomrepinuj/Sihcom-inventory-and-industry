import importlib

import build_list


def _target(tid, qty=1):
    return {"type_id": tid, "name": f"Item {tid}", "qty": qty, "runs": None,
            "me": 10, "structure_bonus": 0.0, "build_station": None}


def test_load_missing_returns_empty_shape(tmp_path):
    assert build_list.load(tmp_path / "nope.json") == {
        "targets": [], "buy_set": []}


def test_load_legacy_bare_list_is_wrapped(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('[{"type_id": 671, "name": "Revelation"}]', encoding="utf-8")
    data = build_list.load(path)
    # load normalizes each target with a per-target build_station (None)
    assert data["targets"] == [
        {"type_id": 671, "name": "Revelation", "build_station": None}]
    assert data["buy_set"] == []


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    data = {"targets": [_target(671, 5)], "buy_set": [2867]}
    build_list.save(data, path)
    assert build_list.load(path) == data


def test_add_target_appends_and_upserts(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671, 5), path)
    build_list.add_target(_target(17636, 3), path)
    assert [t["type_id"] for t in build_list.load(path)["targets"]] == [671, 17636]
    build_list.add_target(_target(671, 8), path)  # upsert
    targets = build_list.load(path)["targets"]
    assert len(targets) == 2
    assert next(t for t in targets if t["type_id"] == 671)["qty"] == 8


def test_remove_target(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671), path)
    build_list.remove_target(671, path)
    assert build_list.load(path)["targets"] == []


def test_toggle_buy_is_idempotent_flip(tmp_path):
    path = tmp_path / "bl.json"
    build_list.toggle_buy(2867, path)
    assert build_list.load(path)["buy_set"] == [2867]
    build_list.toggle_buy(2867, path)               # flip off
    assert build_list.load(path)["buy_set"] == []


def test_toggle_buy_preserves_targets(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target(_target(671), path)
    build_list.toggle_buy(2867, path)
    data = build_list.load(path)
    assert [t["type_id"] for t in data["targets"]] == [671]
    assert data["buy_set"] == [2867]


def test_add_target_defaults_build_station_none(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    t = build_list.load(path)["targets"][0]
    assert t["build_station"] is None


def test_set_target_station_sets_only_that_target(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.add_target({"type_id": 17636, "name": "Phoenix", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.set_target_station(671, 60003760, path)
    targets = {t["type_id"]: t for t in build_list.load(path)["targets"]}
    assert targets[671]["build_station"] == 60003760
    assert targets[17636]["build_station"] is None


def test_set_target_station_clear(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.set_target_station(671, 60003760, path)
    build_list.set_target_station(671, None, path)
    assert build_list.load(path)["targets"][0]["build_station"] is None


def test_legacy_global_build_station_ignored(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('{"targets": [{"type_id": 671, "name": "Rev"}], '
                    '"buy_set": [], "build_station": 60003760}', encoding="utf-8")
    data = build_list.load(path)
    # old global value no longer surfaces as a top-level key; target has no station
    assert "build_station" not in data
    assert data["targets"][0].get("build_station") is None


def test_save_creates_missing_parent_dir(tmp_path):
    # On a fresh Railway volume the target dir may not exist yet; save() makes it.
    path = tmp_path / "nested" / "dir" / "bl.json"
    build_list.save({"targets": [], "buy_set": []}, path)
    assert path.exists()


def test_data_dir_env_controls_default_path(tmp_path, monkeypatch):
    # DATA_DIR (e.g. a mounted volume) relocates build_list.json so it survives
    # redeploys. DEFAULT_PATH is read at import time, so reload under the env.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    try:
        importlib.reload(build_list)
        assert build_list.DEFAULT_PATH == tmp_path / "build_list.json"
    finally:
        monkeypatch.delenv("DATA_DIR", raising=False)
        importlib.reload(build_list)   # restore module-dir default for other tests
