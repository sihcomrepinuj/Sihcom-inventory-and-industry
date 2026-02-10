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
