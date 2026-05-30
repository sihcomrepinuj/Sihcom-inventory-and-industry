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
