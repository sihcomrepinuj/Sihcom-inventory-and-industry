import build_list


def _target(tid, qty=1):
    return {"type_id": tid, "name": f"Item {tid}", "qty": qty, "runs": None,
            "me": 10, "structure_bonus": 0.0}


def test_load_missing_returns_empty_shape(tmp_path):
    assert build_list.load(tmp_path / "nope.json") == {
        "targets": [], "buy_set": [], "build_station": None}


def test_load_legacy_bare_list_is_wrapped(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('[{"type_id": 671, "name": "Revelation"}]', encoding="utf-8")
    data = build_list.load(path)
    assert data["targets"] == [{"type_id": 671, "name": "Revelation"}]
    assert data["buy_set"] == []


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    data = {"targets": [_target(671, 5)], "buy_set": [2867], "build_station": None}
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


def test_load_defaults_build_station_none(tmp_path):
    assert build_list.load(tmp_path / "nope.json")["build_station"] is None


def test_set_build_station_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    build_list.set_build_station(60003760, path)
    assert build_list.load(path)["build_station"] == 60003760


def test_set_build_station_preserves_targets_and_buyset(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add_target({"type_id": 671, "name": "Rev", "qty": 1, "runs": None,
                           "me": 10, "structure_bonus": 0.0}, path)
    build_list.toggle_buy(2867, path)
    build_list.set_build_station(60003760, path)
    data = build_list.load(path)
    assert [t["type_id"] for t in data["targets"]] == [671]
    assert data["buy_set"] == [2867]
    assert data["build_station"] == 60003760


def test_load_legacy_dict_without_station_defaults_none(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('{"targets": [], "buy_set": []}', encoding="utf-8")
    assert build_list.load(path)["build_station"] is None


def test_load_legacy_bare_list_has_build_station_none(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('[{"type_id": 671, "name": "Rev"}]', encoding="utf-8")
    data = build_list.load(path)
    assert data["targets"] == [{"type_id": 671, "name": "Rev"}]
    assert data["build_station"] is None
