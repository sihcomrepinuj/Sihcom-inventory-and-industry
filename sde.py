"""
sde.py — Interface to the Fuzzwork SQLite SDE database.

Provides:
  - Type name lookups (much faster than ESI)
  - Blueprint material requirements
  - Product <-> blueprint resolution
  - Activity times and invention data
  - ME-adjusted material calculations

The SDE database can be obtained by running setup_sde.py or by downloading
directly from https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2
"""

import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

# Industry activity IDs
ACTIVITY_MANUFACTURING = 1
ACTIVITY_RESEARCHING_TE = 3
ACTIVITY_RESEARCHING_ME = 4
ACTIVITY_COPYING = 5
ACTIVITY_REVERSE_ENGINEERING = 7
ACTIVITY_INVENTION = 8
ACTIVITY_REACTIONS = 9

ACTIVITY_NAMES = {
    1: "Manufacturing",
    3: "Researching TE",
    4: "Researching ME",
    5: "Copying",
    7: "Reverse Engineering",
    8: "Invention",
    9: "Reactions",
    11: "Reactions",
}

# Default SDE location (relative to project root)
DEFAULT_SDE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "sqlite-latest.sqlite"
)


class SDE:
    """
    Interface to the Fuzzwork SDE SQLite database.

    Key tables used:
        invTypes                    - type_id <-> name mapping
        industryActivityMaterials   - blueprint materials per activity
        industryActivityProducts    - blueprint products per activity
        industryActivity            - activity times
    """

    def __init__(self, db_path: str | None = None):
        db_path = db_path or DEFAULT_SDE_PATH

        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"SDE database not found at: {db_path}\n"
                "Run 'python setup_sde.py' to download it, or download from:\n"
                "  https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"
            )

        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Type lookups
    # ------------------------------------------------------------------

    def get_type_name(self, type_id: int) -> str:
        """Look up a single type name."""
        row = self.conn.execute(
            "SELECT typeName FROM invTypes WHERE typeID = ?", (type_id,)
        ).fetchone()
        return row["typeName"] if row else f"Unknown ({type_id})"

    def get_type_names(self, type_ids: list[int]) -> dict[int, str]:
        """Bulk look up type names."""
        if not type_ids:
            return {}
        unique = list(set(type_ids))
        placeholders = ",".join("?" * len(unique))
        rows = self.conn.execute(
            f"SELECT typeID, typeName FROM invTypes WHERE typeID IN ({placeholders})",
            unique,
        ).fetchall()
        result = {r["typeID"]: r["typeName"] for r in rows}
        for tid in unique:
            if tid not in result:
                result[tid] = f"Unknown ({tid})"
        return result

    def search_types(self, name: str, limit: int = 25) -> list[dict]:
        """Search for types by partial name match."""
        rows = self.conn.execute(
            "SELECT typeID, typeName FROM invTypes "
            "WHERE typeName LIKE ? AND published = 1 "
            "ORDER BY typeName LIMIT ?",
            (f"%{name}%", limit),
        ).fetchall()
        return [{"type_id": r["typeID"], "name": r["typeName"]} for r in rows]

    # ------------------------------------------------------------------
    # Blueprint lookups
    # ------------------------------------------------------------------

    def find_blueprint_for_product(self, product_type_id: int) -> int | None:
        """Given a product type_id, find the blueprint type_id that makes it."""
        row = self.conn.execute(
            "SELECT typeID FROM industryActivityProducts "
            "WHERE productTypeID = ? AND activityID = 1",
            (product_type_id,),
        ).fetchone()
        return row["typeID"] if row else None

    def find_product_for_blueprint(self, blueprint_type_id: int) -> int | None:
        """Given a blueprint type_id, find what it manufactures."""
        row = self.conn.execute(
            "SELECT productTypeID FROM industryActivityProducts "
            "WHERE typeID = ? AND activityID = 1",
            (blueprint_type_id,),
        ).fetchone()
        return row["productTypeID"] if row else None

    def search_blueprints(self, name: str, limit: int = 25) -> list[dict]:
        """
        Search for blueprints by product name or blueprint name.

        Returns list of dicts with keys:
            blueprint_type_id, blueprint_name, product_type_id, product_name
        """
        rows = self.conn.execute(
            """
            SELECT bp.typeID     AS blueprint_type_id,
                   bp.typeName   AS blueprint_name,
                   iap.productTypeID AS product_type_id,
                   prod.typeName AS product_name
            FROM invTypes bp
            JOIN industryActivityProducts iap
                ON bp.typeID = iap.typeID AND iap.activityID = 1
            JOIN invTypes prod
                ON iap.productTypeID = prod.typeID
            WHERE (bp.typeName LIKE ? OR prod.typeName LIKE ?)
              AND bp.published = 1
            ORDER BY prod.typeName
            LIMIT ?
            """,
            (f"%{name}%", f"%{name}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def get_manufacturing_materials(self, blueprint_type_id: int) -> list[dict]:
        """
        Get the base manufacturing materials for a blueprint.

        Returns list of {type_id, name, quantity} dicts sorted by quantity desc.
        """
        rows = self.conn.execute(
            """
            SELECT iam.materialTypeID AS type_id,
                   it.typeName        AS name,
                   iam.quantity        AS quantity
            FROM industryActivityMaterials iam
            JOIN invTypes it ON iam.materialTypeID = it.typeID
            WHERE iam.typeID = ? AND iam.activityID = 1
            ORDER BY iam.quantity DESC
            """,
            (blueprint_type_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_activity_materials(
        self, blueprint_type_id: int, activity_id: int
    ) -> list[dict]:
        """Get materials for any industry activity."""
        rows = self.conn.execute(
            """
            SELECT iam.materialTypeID AS type_id,
                   it.typeName        AS name,
                   iam.quantity        AS quantity
            FROM industryActivityMaterials iam
            JOIN invTypes it ON iam.materialTypeID = it.typeID
            WHERE iam.typeID = ? AND iam.activityID = ?
            ORDER BY iam.quantity DESC
            """,
            (blueprint_type_id, activity_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_activity_time(
        self, blueprint_type_id: int, activity_id: int
    ) -> int | None:
        """Get the base time in seconds for an activity on a blueprint."""
        row = self.conn.execute(
            "SELECT time FROM industryActivity "
            "WHERE typeID = ? AND activityID = ?",
            (blueprint_type_id, activity_id),
        ).fetchone()
        return row["time"] if row else None

    def get_invention_products(self, blueprint_type_id: int) -> list[dict]:
        """Get possible invention outcomes for a T1 blueprint."""
        rows = self.conn.execute(
            """
            SELECT iap.productTypeID AS type_id,
                   it.typeName       AS name,
                   iap.quantity      AS quantity
            FROM industryActivityProducts iap
            JOIN invTypes it ON iap.productTypeID = it.typeID
            WHERE iap.typeID = ? AND iap.activityID = 8
            """,
            (blueprint_type_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Chain resolution helpers
    # ------------------------------------------------------------------

    def find_source_for_material(self, type_id: int) -> dict | None:
        """
        Check if a material can be produced (manufactured or reacted).

        Checks manufacturing (activityID=1) first, then reactions (activityID=9).

        Returns dict with keys:
            blueprint_type_id, activity_id, quantity_per_run
        or None if this is a terminal (raw) material.
        """
        for activity_id in (ACTIVITY_MANUFACTURING, ACTIVITY_REACTIONS):
            row = self.conn.execute(
                "SELECT typeID, quantity FROM industryActivityProducts "
                "WHERE productTypeID = ? AND activityID = ?",
                (type_id, activity_id),
            ).fetchone()
            if row:
                return {
                    "blueprint_type_id": row["typeID"],
                    "activity_id": activity_id,
                    "quantity_per_run": row["quantity"],
                }
        return None

    def get_materials_by_type_ids(self, type_ids: list[int]) -> dict[int, dict | None]:
        """
        Batch version of find_source_for_material for multiple type IDs.

        Reduces database queries by fetching all material sources in a single
        round trip instead of querying each material individually.

        Args:
            type_ids: List of material type IDs to look up

        Returns:
            Dict mapping type_id -> source info (same format as find_source_for_material)
            or None if the material has no blueprint/reaction formula.
        """
        if not type_ids:
            return {}

        # Initialize all type_ids as having no source (terminal materials)
        result = {tid: None for tid in type_ids}

        # Batch query for manufacturing sources (activityID=1)
        placeholders = ",".join("?" * len(type_ids))
        mfg_rows = self.conn.execute(
            f"""
            SELECT productTypeID, typeID, quantity
            FROM industryActivityProducts
            WHERE productTypeID IN ({placeholders}) AND activityID = 1
            """,
            type_ids,
        ).fetchall()

        for row in mfg_rows:
            result[row["productTypeID"]] = {
                "blueprint_type_id": row["typeID"],
                "activity_id": ACTIVITY_MANUFACTURING,
                "quantity_per_run": row["quantity"],
            }

        # Batch query for reaction sources (activityID=9) - only for materials not yet found
        remaining_ids = [tid for tid in type_ids if result[tid] is None]
        if remaining_ids:
            placeholders = ",".join("?" * len(remaining_ids))
            reaction_rows = self.conn.execute(
                f"""
                SELECT productTypeID, typeID, quantity
                FROM industryActivityProducts
                WHERE productTypeID IN ({placeholders}) AND activityID = 9
                """,
                remaining_ids,
            ).fetchall()

            for row in reaction_rows:
                result[row["productTypeID"]] = {
                    "blueprint_type_id": row["typeID"],
                    "activity_id": ACTIVITY_REACTIONS,
                    "quantity_per_run": row["quantity"],
                }

        return result


# ------------------------------------------------------------------
# ME Calculation helpers
# ------------------------------------------------------------------

def apply_me(
    base_quantity: int,
    me_level: int,
    runs: int = 1,
    structure_bonus: float = 0.0,
) -> int:
    """
    Calculate ME-adjusted material quantity.

    Post-Crius formula:
        adjusted = max(runs, ceil(round(
            runs * base_quantity * (1 - ME/100) * (1 - structure_bonus/100)
        , 2)))

    Args:
        base_quantity: Per-run quantity from the SDE
        me_level:      Material Efficiency 0-10
        runs:          Number of manufacturing runs
        structure_bonus: Structure material reduction % (e.g. 1.0 for
                         Raitaru, 4.2 for T2-rigged Raitaru)

    Returns:
        Total quantity needed across all runs
    """
    if base_quantity <= 0:
        return 0

    adjusted = (
        runs * base_quantity * (1 - me_level / 100) * (1 - structure_bonus / 100)
    )
    # Round to 2dp first to kill floating-point artefacts, then ceil,
    # then enforce the minimum-of-one-per-run rule.
    adjusted = max(runs, math.ceil(round(adjusted, 2)))
    return adjusted


def calculate_materials(
    sde: SDE,
    blueprint_type_id: int,
    me_level: int = 0,
    runs: int = 1,
    structure_bonus: float = 0.0,
) -> list[dict]:
    """
    Calculate ME-adjusted materials for manufacturing.

    Returns list of dicts:
        type_id, name, base_quantity, base_total, adjusted_quantity, saved
    """
    base_materials = sde.get_manufacturing_materials(blueprint_type_id)
    results = []
    for mat in base_materials:
        base_total = mat["quantity"] * runs
        adjusted = apply_me(mat["quantity"], me_level, runs, structure_bonus)
        results.append({
            "type_id": mat["type_id"],
            "name": mat["name"],
            "base_quantity": mat["quantity"],
            "base_total": base_total,
            "adjusted_quantity": adjusted,
            "saved": base_total - adjusted,
        })
    return results


# ------------------------------------------------------------------
# Material chain resolution
# ------------------------------------------------------------------

@dataclass
class MaterialNode:
    """A single node in a material dependency tree."""
    type_id: int
    name: str
    quantity_needed: int            # ME-adjusted total quantity
    activity_id: int | None         # 1=manufacturing, 9=reaction, None=raw
    activity_name: str | None
    blueprint_type_id: int | None   # BP/formula that makes this, None for raw
    me_level: int
    is_terminal: bool               # True = no blueprint exists (raw material)
    depth: int
    children: list['MaterialNode'] = field(default_factory=list)


def resolve_material_chain(
    sde: SDE,
    blueprint_type_id: int,
    me_level: int = 0,
    runs: int = 1,
    structure_bonus: float = 0.0,
    sub_me: int = 10,
    resolve_reactions: bool = True,
    max_depth: int = 10,
    _depth: int = 0,
    _cache: dict | None = None,
) -> list[MaterialNode]:
    """
    Recursively resolve the full material chain for a blueprint.

    Always resolves the complete tree. Build/buy decisions are made
    client-side or via flatten_material_tree(buy_set=...).

    Args:
        sde:               SDE instance
        blueprint_type_id: Blueprint type ID to resolve
        me_level:          ME for this specific blueprint
        runs:              Number of runs
        structure_bonus:   Structure material reduction %
        sub_me:            Default ME for sub-component blueprints (default 10)
        resolve_reactions: Whether to recurse into reactions (activityID=9)
        max_depth:         Safety limit for recursion depth
    """
    if _cache is None:
        _cache = {}

    if _depth > max_depth:
        # Safety: return remaining materials as terminal
        base_mats = sde.get_manufacturing_materials(blueprint_type_id)
        return [
            MaterialNode(
                type_id=m["type_id"], name=m["name"],
                quantity_needed=apply_me(m["quantity"], me_level, runs, structure_bonus),
                activity_id=None, activity_name=None,
                blueprint_type_id=None, me_level=me_level,
                is_terminal=True, depth=_depth,
            )
            for m in base_mats
        ]

    # Determine what activity this blueprint uses and get its materials
    # Check if the blueprint has reaction materials (activityID=9)
    reaction_mats = sde.get_activity_materials(blueprint_type_id, ACTIVITY_REACTIONS)
    if reaction_mats:
        # This is a reaction formula — no ME applies
        adjusted_mats = [
            {"type_id": m["type_id"], "name": m["name"],
             "adjusted_quantity": m["quantity"] * runs}
            for m in reaction_mats
        ]
    else:
        # Standard manufacturing blueprint — ME applies
        base_mats = sde.get_manufacturing_materials(blueprint_type_id)
        adjusted_mats = [
            {"type_id": m["type_id"], "name": m["name"],
             "adjusted_quantity": apply_me(m["quantity"], me_level, runs, structure_bonus)}
            for m in base_mats
        ]

    nodes = []
    # Pre fetch materials list for all type ids in the blueprint to minimize DB hits in the loop
    type_ids = [mat["type_id"] for mat in adjusted_mats]
    pre_fetched_materials = sde.get_materials_by_type_ids(type_ids)

    for mat in adjusted_mats:
        tid = mat["type_id"]
        qty = mat["adjusted_quantity"]

        # Memoized source lookup
        if tid not in _cache:
            _cache[tid] = pre_fetched_materials[tid]
        source = _cache[tid]

        if source is None:
            # Terminal raw material
            nodes.append(MaterialNode(
                type_id=tid, name=mat["name"],
                quantity_needed=qty,
                activity_id=None, activity_name=None,
                blueprint_type_id=None, me_level=0,
                is_terminal=True, depth=_depth,
            ))
        elif source["activity_id"] == ACTIVITY_REACTIONS and not resolve_reactions:
            # Reaction product but user chose not to resolve reactions
            nodes.append(MaterialNode(
                type_id=tid, name=mat["name"],
                quantity_needed=qty,
                activity_id=ACTIVITY_REACTIONS,
                activity_name="Reactions",
                blueprint_type_id=source["blueprint_type_id"],
                me_level=0, is_terminal=True, depth=_depth,
            ))
        else:
            # Resolvable component — recurse
            sub_activity = source["activity_id"]
            sub_bp = source["blueprint_type_id"]
            qty_per_run = source["quantity_per_run"]

            # How many runs of the sub-blueprint do we need?
            sub_runs = math.ceil(qty / qty_per_run)

            # Reactions have no ME; manufactured subs use sub_me
            effective_me = 0 if sub_activity == ACTIVITY_REACTIONS else sub_me

            children = resolve_material_chain(
                sde, sub_bp,
                me_level=effective_me,
                runs=sub_runs,
                structure_bonus=structure_bonus,
                sub_me=sub_me,
                resolve_reactions=resolve_reactions,
                max_depth=max_depth,
                _depth=_depth + 1,
                _cache=_cache,
            )

            nodes.append(MaterialNode(
                type_id=tid, name=mat["name"],
                quantity_needed=qty,
                activity_id=sub_activity,
                activity_name=ACTIVITY_NAMES.get(sub_activity, "Unknown"),
                blueprint_type_id=sub_bp,
                me_level=effective_me,
                is_terminal=False, depth=_depth,
                children=children,
            ))

    return nodes


def flatten_material_tree(
    nodes: list[MaterialNode],
    buy_set: set[int] | None = None,
) -> list[dict]:
    """
    Flatten a material tree into an aggregated shopping list.

    Args:
        nodes:   The material tree from resolve_material_chain()
        buy_set: Optional set of type_ids to BUY instead of build.
                 If a node's type_id is in buy_set, it's treated as a leaf
                 (the node itself goes on the list, not its children).
                 If None, resolves everything to terminal raw materials.

    Returns:
        List of {type_id, name, quantity} dicts, sorted by quantity desc.
    """
    totals: dict[int, dict] = {}

    def _walk(node_list: list[MaterialNode]):
        for node in node_list:
            # If this node is in the buy set, treat it as a leaf
            if buy_set is not None and node.type_id in buy_set:
                if node.type_id not in totals:
                    totals[node.type_id] = {
                        "type_id": node.type_id,
                        "name": node.name,
                        "quantity": 0,
                    }
                totals[node.type_id]["quantity"] += node.quantity_needed
            elif node.is_terminal or not node.children:
                # Terminal raw material
                if node.type_id not in totals:
                    totals[node.type_id] = {
                        "type_id": node.type_id,
                        "name": node.name,
                        "quantity": 0,
                    }
                totals[node.type_id]["quantity"] += node.quantity_needed
            else:
                # Intermediate — recurse into children
                _walk(node.children)

    _walk(nodes)
    return sorted(totals.values(), key=lambda x: x["quantity"], reverse=True)


def get_chain_summary(nodes: list[MaterialNode]) -> dict:
    """
    Get summary statistics for a material chain.

    Returns dict with:
        max_depth, total_intermediate_types, total_terminal_types,
        intermediates: [{type_id, name, quantity, activity_name, me_level}]
    """
    intermediates = []
    terminal_ids = set()
    max_depth = 0

    def _walk(node_list: list[MaterialNode]):
        nonlocal max_depth
        for node in node_list:
            max_depth = max(max_depth, node.depth)
            if node.is_terminal or not node.children:
                terminal_ids.add(node.type_id)
            else:
                intermediates.append({
                    "type_id": node.type_id,
                    "name": node.name,
                    "quantity": node.quantity_needed,
                    "activity_name": node.activity_name,
                    "me_level": node.me_level,
                })
                _walk(node.children)

    _walk(nodes)
    return {
        "max_depth": max_depth,
        "total_intermediate_types": len(intermediates),
        "total_terminal_types": len(terminal_ids),
        "intermediates": intermediates,
    }
