"""
hauling.py — Deficit calculation for location-aware shopping lists,
and build-capacity projection from stockpile against per-run materials.
"""


def calculate_build_capacity(
    materials: list[dict],
    asset_index: dict[int, int],
) -> dict:
    """
    Project a stockpile against per-run materials to find build capacity.

    Args:
        materials: List of {type_id, name, quantity} representing the
            materials required for ONE RUN at the desired ME. Caller
            chooses whether to flatten the chain to raw materials or
            stop at direct components.
        asset_index: {type_id: owned_qty} — flat asset index.

    Returns:
        {
            "capacity_runs": int | None,  # None when materials is empty
                                          # or every material has zero need
            "constraints": [
                {type_id, name, owned, per_run, max_runs}, ...
            ]  # sorted ascending by max_runs (tightest first)
        }

    Notes:
        Materials with quantity <= 0 are skipped (don't constrain capacity).
        Materials missing from asset_index are treated as owned=0.
    """
    constraints = []
    for mat in materials:
        per_run = mat["quantity"]
        if per_run <= 0:
            continue
        owned = asset_index.get(mat["type_id"], 0)
        max_runs = owned // per_run
        constraints.append({
            "type_id": mat["type_id"],
            "name": mat["name"],
            "owned": owned,
            "per_run": per_run,
            "max_runs": max_runs,
        })

    if not constraints:
        return {"capacity_runs": None, "constraints": []}

    constraints.sort(key=lambda c: c["max_runs"])
    return {
        "capacity_runs": constraints[0]["max_runs"],
        "constraints": constraints,
    }


def calculate_deficit(
    needed: list[dict],
    loc_index: dict[int, dict[int, int]],
    build_station: int,
    volumes: dict[int, float],
) -> list[dict]:
    """
    Calculate per-material deficit for a build station.

    Args:
        needed: List of {type_id, name, quantity} dicts (from shopping list)
        loc_index: {type_id: {location_id: quantity}} from build_location_asset_index
        build_station: The facility_id where we're manufacturing
        volumes: {type_id: volume_per_unit} for m3 calculations

    Returns:
        List of dicts per material:
            type_id, name, quantity_needed,
            at_station, elsewhere (dict of {loc_id: qty}),
            to_buy, elsewhere_volume, to_buy_volume
    """
    results = []
    for mat in needed:
        tid = mat["type_id"]
        qty_needed = mat["quantity"]
        unit_vol = volumes.get(tid, 0.0)

        # What's at the build station?
        type_locations = loc_index.get(tid, {})
        at_station = min(type_locations.get(build_station, 0), qty_needed)

        # What's at other locations?
        remaining_need = qty_needed - at_station
        elsewhere = {}
        elsewhere_total = 0
        for loc_id, loc_qty in type_locations.items():
            if loc_id == build_station:
                continue
            if remaining_need <= 0:
                break
            usable = min(loc_qty, remaining_need)
            elsewhere[loc_id] = usable
            elsewhere_total += usable
            remaining_need -= usable

        # What still needs to be bought?
        to_buy = max(0, qty_needed - at_station - elsewhere_total)

        results.append({
            "type_id": tid,
            "name": mat["name"],
            "quantity_needed": qty_needed,
            "at_station": at_station,
            "elsewhere": elsewhere,
            "elsewhere_volume": elsewhere_total * unit_vol,
            "to_buy": to_buy,
            "to_buy_volume": to_buy * unit_vol,
        })

    return results
