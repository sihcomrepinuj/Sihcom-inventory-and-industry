from sde import MaterialNode
import hauling
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


def test_merge_trees_prunes_bought_subtree():
    # Buying an intermediate must drop its sub-materials from the graph.
    trit = _node(34, "Tritanium", 100, terminal=True)
    parts = _node(1, "Cap Parts", 30, terminal=False, children=[trit], bp=1001)
    graph = plan.merge_trees(
        [plan.Target(671, "Revelation", 2001, 5, [parts])],
        buy_set={1},
    )
    assert 1 in graph        # bought component still present (you buy it)
    assert 34 not in graph   # its sub-material pruned (it's inside what you buy)


def test_merge_trees_default_no_buyset_keeps_full_tree():
    trit = _node(34, "Tritanium", 100, terminal=True)
    parts = _node(1, "Cap Parts", 30, terminal=False, children=[trit], bp=1001)
    graph = plan.merge_trees([plan.Target(671, "Revelation", 2001, 5, [parts])])
    assert set(graph) == {671, 1, 34}   # unchanged default behavior


def test_classify_via_merge_omits_bought_intermediate_children():
    # Integration: bought intermediate -> buy list has the component, NOT its raws.
    trit = _node(34, "Tritanium", 3000, terminal=True)
    parts = _node(1, "Cap Parts", 30, terminal=False, children=[trit], bp=1001)
    graph = plan.merge_trees(
        [plan.Target(671, "Revelation", 2001, 5, [parts])], buy_set={1})
    out = plan.classify(graph, {}, jobs=[], build_station=None, buy_set={1})
    buy_ids = {r["type_id"] for r in out["buy"]}
    assert 1 in buy_ids       # buy the cap parts
    assert 34 not in buy_ids  # do NOT also buy their tritanium


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


def test_classify_partial_owned_plus_job_is_in_progress():
    g = _graph(_req(1, "Cap Parts", 30, terminal=False, inputs=[34], bp=1001),
               _req(34, "Tritanium", 3000, terminal=True))
    loc = {1: {60003760: 10}, 34: {60003760: 3000}}  # own 10 of 30 cap parts
    jobs = [{"product_type_id": 1, "runs": 20, "activity_id": 1,
             "status": "active", "end_date": "2026-05-31T00:00:00Z"}]
    out = plan.classify(g, loc, jobs=jobs, build_station=60003760, buy_set=set())
    assert [n["type_id"] for n in out["in_progress"]] == [1]
    row = out["in_progress"][0]
    assert row["shortfall"] == 0          # 30 - 10 - 20
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


def test_buy_rows_get_haul_split():
    # need 5000 gross; 1000 at station, 2000 elsewhere -> buy 2000
    buy = [{"type_id": 34, "name": "Tritanium", "needed": 5000, "shortfall": 4000}]
    loc = {34: {60003760: 1000, 60008494: 2000}}
    volumes = {34: 0.01}
    enriched = plan.enrich_buy(buy, loc, build_station=60003760, volumes=volumes)
    row = enriched[0]
    assert row["at_station"] == 1000
    assert row["elsewhere"] == {60008494: 2000}
    assert row["to_buy"] == 2000          # 5000 - 1000 - 2000, station counted once
    assert row["needed"] == 5000          # original gross key preserved
    assert row["shortfall"] == 4000       # original key preserved


def test_enrich_buy_no_station_routes_all_to_buy_or_elsewhere():
    buy = [{"type_id": 34, "name": "Tritanium", "needed": 5000, "shortfall": 5000}]
    loc = {34: {60008494: 2000}}          # 2000 owned, no build station chosen
    enriched = plan.enrich_buy(buy, loc, build_station=None, volumes={34: 0.01})
    row = enriched[0]
    assert row["at_station"] == 0         # station 0 sentinel: nothing there
    assert row["to_buy"] == 3000          # 5000 - 2000 elsewhere


def test_enrich_buy_nothing_owned_buys_everything():
    buy = [{"type_id": 34, "name": "Tritanium", "needed": 5000, "shortfall": 5000}]
    enriched = plan.enrich_buy(buy, {}, build_station=60003760, volumes={34: 0.01})
    row = enriched[0]
    assert row["to_buy"] == 5000
    assert row["at_station"] == 0


def test_attach_supply_columns_nets_owned_and_volumes():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 5000},
            {"type_id": 35, "name": "Pyerite", "quantity": 1000}]
    owned = {34: 2000}                      # own some trit, no pyerite
    volumes = {34: 0.01, 35: 0.01}
    out = plan.attach_supply_columns(rows, owned, volumes)
    trit = next(r for r in out if r["type_id"] == 34)
    assert trit["total"] == 5000
    assert trit["owned"] == 2000
    assert trit["to_buy"] == 3000
    assert trit["total_volume"] == 50.0     # 5000 * 0.01
    assert trit["to_buy_volume"] == 30.0    # 3000 * 0.01
    pyer = next(r for r in out if r["type_id"] == 35)
    assert pyer["to_buy"] == 1000           # nothing owned


def test_attach_supply_columns_owned_exceeds_need():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    out = plan.attach_supply_columns(rows, {34: 999}, {34: 0.01})
    assert out[0]["to_buy"] == 0            # never negative


def test_attach_supply_columns_does_not_mutate_input():
    rows = [{"type_id": 34, "name": "Tritanium", "quantity": 100}]
    plan.attach_supply_columns(rows, {}, {})
    assert rows == [{"type_id": 34, "name": "Tritanium", "quantity": 100}]  # unchanged
