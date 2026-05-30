from sde import MaterialNode
import plan


def _node(tid, name, qty, terminal, children=None, bp=None, act=1):
    return MaterialNode(
        type_id=tid, name=name, quantity_needed=qty,
        activity_id=None if terminal else act,
        activity_name=None if terminal else "Manufacturing",
        blueprint_type_id=bp, me_level=10,
        is_terminal=terminal, depth=0, children=children or [],
    )


def test_merge_single_target_indexes_every_node():
    # Hull (product) <- 30 Cap Parts <- 100 Tritanium
    trit = _node(34, "Tritanium", 100, terminal=True)
    parts = _node(1, "Cap Parts", 30, terminal=False, children=[trit], bp=1001)
    graph = plan.merge_trees([
        plan.Target(type_id=671, name="Revelation", blueprint_type_id=2001,
                    needed=5, children=[parts]),
    ])
    assert set(graph) == {671, 1, 34}
    assert graph[1].total_needed == 30
    assert graph[1].direct_inputs == {34}
    assert graph[671].direct_inputs == {1}
    assert graph[34].is_terminal is True


def test_merge_sums_shared_intermediate():
    parts_a = _node(1, "Cap Parts", 30, terminal=False,
                    children=[_node(34, "Tritanium", 100, terminal=True)], bp=1001)
    parts_b = _node(1, "Cap Parts", 24, terminal=False,
                    children=[_node(34, "Tritanium", 80, terminal=True)], bp=1001)
    graph = plan.merge_trees([
        plan.Target(671, "Revelation", 2001, 5, [parts_a]),
        plan.Target(17636, "Phoenix", 2002, 3, [parts_b]),
    ])
    assert graph[1].total_needed == 54          # 30 + 24, not duplicated
    assert graph[34].total_needed == 180        # 100 + 80
    assert set(graph[671].direct_inputs) == {1}
    assert set(graph[17636].direct_inputs) == {1}


def test_merge_diamond_within_single_tree():
    # Product <- CompA (needs 10 Trit) and CompB (needs 5 Trit); Trit shared.
    comp_a = _node(2, "CompA", 1, terminal=False,
                   children=[_node(34, "Tritanium", 10, terminal=True)], bp=1001)
    comp_b = _node(3, "CompB", 1, terminal=False,
                   children=[_node(34, "Tritanium", 5, terminal=True)], bp=1002)
    graph = plan.merge_trees([
        plan.Target(671, "Revelation", 2001, 1, [comp_a, comp_b]),
    ])
    assert graph[34].total_needed == 15          # 10 + 5 from both branches
    assert graph[34].is_terminal is True
    assert set(graph[671].direct_inputs) == {2, 3}


def _graph(*nodes):
    return {n.type_id: n for n in nodes}


def _req(tid, name, needed, terminal, inputs=(), bp=None):
    return plan.ReqNode(
        type_id=tid, name=name, total_needed=needed,
        blueprint_type_id=bp, activity_id=None if terminal else 1,
        is_terminal=terminal, direct_inputs=set(inputs),
    )


def test_classify_ready_when_inputs_owned():
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=True),
    )
    loc = {1: {60003760: 30}}
    out = plan.classify(g, loc, jobs=[], build_station=60003760, buy_set=set())
    assert [n["type_id"] for n in out["ready"]] == [671]
    assert out["blocked"] == []
    assert out["buy"] == []


def test_classify_blocked_lists_missing_inputs():
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    loc = {}  # own nothing
    out = plan.classify(g, loc, jobs=[], build_station=60003760, buy_set=set())
    blocked_ids = {n["type_id"] for n in out["blocked"]}
    assert blocked_ids == {671, 1}
    cap = next(n for n in out["blocked"] if n["type_id"] == 1)
    assert cap["missing"] == [{"type_id": 34, "name": "Tritanium", "shortfall": 3000}]
    assert [n["type_id"] for n in out["buy"]] == [34]


def test_classify_in_progress_from_active_job():
    g = _graph(
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    loc = {34: {60003760: 3000}}
    jobs = [{"product_type_id": 1, "runs": 30, "activity_id": 1,
             "status": "active", "end_date": "2026-05-31T00:00:00Z"}]
    out = plan.classify(g, loc, jobs=jobs, build_station=60003760, buy_set=set())
    assert [n["type_id"] for n in out["in_progress"]] == [1]
    assert out["ready"] == []


def test_classify_buy_set_moves_node_to_buy():
    g = _graph(
        _req(671, "Revelation", 5, terminal=False, inputs=[1], bp=2001),
        _req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
        _req(34, "Tritanium", 3000, terminal=True),
    )
    out = plan.classify(g, {}, jobs=[], build_station=None, buy_set={1})
    assert 1 in {n["type_id"] for n in out["buy"]}
    assert 1 not in {n["type_id"] for n in out["ready"] + out["blocked"]}
