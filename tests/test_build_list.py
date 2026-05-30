import build_list


def test_round_trip(tmp_path):
    path = tmp_path / "bl.json"
    targets = [
        {"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
    ]
    build_list.save(targets, path)
    assert build_list.load(path) == targets


def test_load_missing_returns_empty(tmp_path):
    assert build_list.load(tmp_path / "nope.json") == []


def test_add_target_appends(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add(
        {"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
        path,
    )
    build_list.add(
        {"type_id": 17636, "name": "Phoenix", "qty": 3, "runs": 3,
         "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None},
        path,
    )
    loaded = build_list.load(path)
    assert [t["type_id"] for t in loaded] == [671, 17636]


def test_add_upserts_same_type_id(tmp_path):
    path = tmp_path / "bl.json"
    build_list.add({"type_id": 671, "name": "Revelation", "qty": 5, "runs": None,
                    "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None}, path)
    build_list.add({"type_id": 671, "name": "Revelation", "qty": 8, "runs": None,
                    "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None}, path)
    loaded = build_list.load(path)
    assert len(loaded) == 1
    assert loaded[0]["qty"] == 8     # replaced, not appended


def test_remove_target_by_type_id(tmp_path):
    path = tmp_path / "bl.json"
    build_list.save(
        [{"type_id": 671, "name": "Revelation", "qty": 5, "runs": 5,
          "me": 10, "structure_bonus": 0.0, "buy_set": [], "build_station": None}],
        path,
    )
    build_list.remove(671, path)
    assert build_list.load(path) == []
