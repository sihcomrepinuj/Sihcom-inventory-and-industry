"""
hauling.py — Deficit calculation for location-aware shopping lists.

Given a list of needed materials, a location-aware asset index, and a
build station ID, calculates what's at the station, what needs to be
hauled from elsewhere, and what needs to be bought.
"""


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
