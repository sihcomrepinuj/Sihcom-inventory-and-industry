"""
eve_inventory.py — Main CLI for Sihcom Industry Tracker.

Usage:
    python eve_inventory.py <command> [arguments]

Commands (no auth required — SDE only):
    search <name>                    Search items/blueprints by name
    materials <name> [me] [runs]     Show materials for a blueprint
    detail <name>                    Full blueprint info (all activities)
    mecomp <name> [runs]             ME 0-10 comparison table
    prices <name>                    Market prices (buy/sell/volume)

Commands (require ESI auth):
    auth                             Run SSO authentication
    assets                           List all character assets
    blueprints                       List all blueprints with ME/TE
    jobs                             List industry jobs
    shop <name> [me] [runs]          Shopping list (materials vs assets)
    summary                          Full industry dashboard

Environment:
    STRUCTURE_BONUS   Structure material bonus % (default: 0)
    MARKET_REGION     Region ID for market prices (default: 10000002 = The Forge/Jita)
"""

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sde import (
    SDE, ACTIVITY_NAMES, ACTIVITY_MANUFACTURING, ACTIVITY_RESEARCHING_ME,
    ACTIVITY_RESEARCHING_TE, ACTIVITY_COPYING, ACTIVITY_INVENTION,
    ACTIVITY_REACTIONS, calculate_materials, apply_me,
    resolve_material_chain, flatten_material_tree, get_chain_summary,
    MaterialNode,
)
import esi


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def pick_blueprint(sde: SDE, search_term: str) -> dict | None:
    """Search for a blueprint, prompt user if multiple matches."""
    results = sde.search_blueprints(search_term)
    if not results:
        print(f"\nNo blueprints found matching '{search_term}'")
        return None

    if len(results) == 1:
        return results[0]

    print(f"\nMultiple blueprints match '{search_term}':")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r['product_name']} ({r['blueprint_name']})")
    print()

    try:
        choice = int(input("Select (number): ")) - 1
        if 0 <= choice < len(results):
            return results[choice]
    except (ValueError, EOFError):
        pass

    print("Using first result.")
    return results[0]


def fmt_time(seconds: int) -> str:
    """Format seconds as Xh Ym."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def fmt_isk(value: float) -> str:
    """Format an ISK value. Returns '-' for zero."""
    if value == 0:
        return "-"
    return f"{value:,.2f}"


# ------------------------------------------------------------------
# SDE-only commands
# ------------------------------------------------------------------

def cmd_search(sde: SDE, args: list[str]):
    term = " ".join(args)
    if not term:
        print("Usage: eve_inventory.py search <name>")
        return
    results = sde.search_types(term)
    print(f"\nSearch results for '{term}':")
    for r in results:
        print(f"  {r['type_id']:>8}  {r['name']}")


def cmd_materials(sde: SDE, args: list[str], structure_bonus: float,
                  region_id: int):
    if not args:
        print("Usage: eve_inventory.py materials <name> [me] [runs]")
        return
    term = args[0]
    me = int(args[1]) if len(args) > 1 else 10
    runs = int(args[2]) if len(args) > 2 else 1

    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]
    print(f"\n{'='*90}")
    print("MATERIAL REQUIREMENTS")
    print(f"{'='*90}")
    print(f"\n  Blueprint: {bp['blueprint_name']}")
    print(f"  Product:   {bp['product_name']}")
    print(f"  ME Level:  {me}")
    print(f"  Runs:      {runs}")
    if structure_bonus > 0:
        print(f"  Structure: -{structure_bonus}% materials")

    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)
    if not materials:
        print("\n  No manufacturing materials found.")
        return

    # Fetch market prices
    print("  Fetching market prices...")
    type_ids = [mat["type_id"] for mat in materials]
    prices = esi.get_bulk_market_data(type_ids, region_id)

    hdr = (f"{'Material':<30} {'Per Run':>10} {'Total(ME'+str(me)+')':>12}"
           f" {'Saved':>8} {'Jita Sell':>14} {'Total Cost':>16}")
    print(f"\n  {hdr}")
    print(f"  {'-'*30} {'-'*10} {'-'*12} {'-'*8} {'-'*14} {'-'*16}")

    grand_total = 0.0
    for mat in materials:
        sell_price = prices[mat["type_id"]]["sell_min"]
        line_cost = sell_price * mat["adjusted_quantity"]
        grand_total += line_cost

        print(
            f"  {mat['name']:<30} "
            f"{mat['base_quantity']:>10,} "
            f"{mat['adjusted_quantity']:>12,} "
            f"{mat['saved']:>8,} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16}"
        )

    print(f"\n  {'Estimated material cost:':>78} {fmt_isk(grand_total):>16} ISK")

    base_time = sde.get_activity_time(bp_id, ACTIVITY_MANUFACTURING)
    if base_time:
        print(f"\n  Base manufacturing time (per run): {fmt_time(base_time)}")

    inv_products = sde.get_invention_products(bp_id)
    if inv_products:
        print(f"\n  --- Invention Outcomes ---")
        for p in inv_products:
            print(f"    -> {p['name']}")
        inv_mats = sde.get_activity_materials(bp_id, ACTIVITY_INVENTION)
        if inv_mats:
            print(f"\n  Invention materials:")
            for m in inv_mats:
                print(f"    {m['quantity']:>8,}x  {m['name']}")


def cmd_detail(sde: SDE, args: list[str]):
    if not args:
        print("Usage: eve_inventory.py detail <name>")
        return
    term = " ".join(args)
    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]

    print(f"\n{'='*70}")
    print("BLUEPRINT DETAIL")
    print(f"{'='*70}")
    print(f"\n  Blueprint: {bp['blueprint_name']}")
    print(f"  Product:   {bp['product_name']} (type_id: {bp['product_type_id']})")

    for act_id, act_name in [
        (ACTIVITY_MANUFACTURING, "Manufacturing"),
        (ACTIVITY_RESEARCHING_ME, "ME Research"),
        (ACTIVITY_RESEARCHING_TE, "TE Research"),
        (ACTIVITY_COPYING, "Copying"),
        (ACTIVITY_INVENTION, "Invention"),
        (ACTIVITY_REACTIONS, "Reactions"),
    ]:
        mats = sde.get_activity_materials(bp_id, act_id)
        base_time = sde.get_activity_time(bp_id, act_id)
        if mats or base_time:
            print(f"\n  --- {act_name} ---")
            if base_time:
                print(f"  Base time: {fmt_time(base_time)}")
            for m in mats:
                print(f"    {m['quantity']:>10,}x  {m['name']}")

    inv_products = sde.get_invention_products(bp_id)
    if inv_products:
        print(f"\n  --- Invention Outcomes ---")
        for p in inv_products:
            print(f"    -> {p['name']} x{p['quantity']}")


def cmd_mecomp(sde: SDE, args: list[str], structure_bonus: float):
    if not args:
        print("Usage: eve_inventory.py mecomp <name> [runs]")
        return
    term = args[0]
    runs = int(args[1]) if len(args) > 1 else 1

    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]
    base_mats = sde.get_manufacturing_materials(bp_id)
    if not base_mats:
        print("\n  No manufacturing materials found.")
        return

    print(f"\n{'='*70}")
    print("ME COMPARISON TABLE")
    print(f"{'='*70}")
    print(f"\n  Blueprint: {bp['blueprint_name']} ({runs} run{'s' if runs != 1 else ''})")
    if structure_bonus > 0:
        print(f"  Structure bonus: -{structure_bonus}%")

    me_levels = list(range(11))
    header = f"  {'Material':<28}"
    for me in me_levels:
        header += f" {'ME'+str(me):>8}"
    print(f"\n{header}")
    print(f"  {'-'*28}" + " --------" * len(me_levels))

    for mat in base_mats:
        row = f"  {mat['name']:<28}"
        for me in me_levels:
            qty = apply_me(mat["quantity"], me, runs, structure_bonus)
            row += f" {qty:>8,}"
        print(row)


def cmd_prices(sde: SDE, args: list[str], region_id: int):
    if not args:
        print("Usage: eve_inventory.py prices <name>")
        return

    term = " ".join(args)
    results = sde.search_types(term)
    if not results:
        print(f"\nNo items found matching '{term}'")
        return

    if len(results) == 1:
        target = results[0]
    else:
        print(f"\nMultiple items match '{term}':")
        for i, r in enumerate(results[:15], 1):
            print(f"  {i}. {r['name']}")
        print()
        try:
            choice = int(input("Select (number): ")) - 1
            if 0 <= choice < len(results):
                target = results[choice]
            else:
                target = results[0]
                print("Using first result.")
        except (ValueError, EOFError):
            target = results[0]
            print("Using first result.")

    type_id = target["type_id"]
    print(f"\n{'='*60}")
    print("MARKET PRICES")
    print(f"{'='*60}")
    print(f"\n  Item:   {target['name']} (type_id: {type_id})")
    print(f"  Region: {region_id}")

    print("  Fetching market data...")
    data = esi.get_type_market_data(type_id, region_id)

    if data["sell_orders"] == 0 and data["buy_orders"] == 0:
        print("\n  No orders found in this region.")
        return

    print(f"\n  {'Sell (lowest):':<20} {fmt_isk(data['sell_min']):>18} ISK")
    print(f"  {'Buy (highest):':<20} {fmt_isk(data['buy_max']):>18} ISK")
    spread = data["sell_min"] - data["buy_max"]
    print(f"  {'Spread:':<20} {fmt_isk(spread):>18} ISK")
    if data["sell_min"] > 0:
        spread_pct = spread / data["sell_min"] * 100
        print(f"  {'Spread %:':<20} {spread_pct:>17.1f}%")
    print()
    print(f"  {'Sell volume:':<20} {data['sell_volume']:>18,}")
    print(f"  {'Buy volume:':<20} {data['buy_volume']:>18,}")
    print(f"  {'Sell orders:':<20} {data['sell_orders']:>18,}")
    print(f"  {'Buy orders:':<20} {data['buy_orders']:>18,}")


def cmd_chain(sde: SDE, args: list[str], structure_bonus: float,
              region_id: int):
    if not args:
        print("Usage: eve_inventory.py chain <name> [me] [runs]")
        return
    term = args[0]
    me = int(args[1]) if len(args) > 1 else 10
    runs = int(args[2]) if len(args) > 2 else 1

    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]
    print(f"\n{'='*90}")
    print("FULL MATERIAL CHAIN")
    print(f"{'='*90}")
    print(f"\n  Blueprint: {bp['blueprint_name']}")
    print(f"  Product:   {bp['product_name']}")
    print(f"  ME Level:  {me} (sub-components: ME 10)")
    print(f"  Runs:      {runs}")
    if structure_bonus > 0:
        print(f"  Structure: -{structure_bonus}% materials")

    print("\n  Resolving material chain...")
    tree = resolve_material_chain(
        sde, bp_id, me, runs, structure_bonus,
        sub_me=10, resolve_reactions=True,
    )
    summary = get_chain_summary(tree)

    # Print tree
    print(f"\n  --- Material Tree ---")
    print(f"  (depth: {summary['max_depth'] + 1}, "
          f"{summary['total_intermediate_types']} intermediates, "
          f"{summary['total_terminal_types']} raw materials)\n")

    def print_tree(nodes: list[MaterialNode], indent: int = 2):
        for node in nodes:
            prefix = " " * indent
            if node.children:
                label = f"({node.activity_name}, ME {node.me_level})"
                print(f"{prefix}{node.quantity_needed:>10,}x  {node.name:<35} {label}")
                print_tree(node.children, indent + 4)
            else:
                print(f"{prefix}{node.quantity_needed:>10,}x  {node.name}")

    print_tree(tree)

    # Aggregated raw materials with prices
    raw_materials = flatten_material_tree(tree)

    print(f"\n  Fetching market prices...")
    type_ids = [m["type_id"] for m in raw_materials]
    prices = esi.get_bulk_market_data(type_ids, region_id) if type_ids else {}

    hdr = f"{'Material':<35} {'Total Needed':>14} {'Jita Sell':>14} {'Total Cost':>16}"
    print(f"\n  --- Aggregated Raw Materials ---\n")
    print(f"  {hdr}")
    print(f"  {'-'*35} {'-'*14} {'-'*14} {'-'*16}")

    grand_total = 0.0
    for mat in raw_materials:
        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        line_cost = sell_price * mat["quantity"]
        grand_total += line_cost
        print(
            f"  {mat['name']:<35} "
            f"{mat['quantity']:>14,} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16}"
        )

    print(f"\n  {'Total raw material cost:':>65} {fmt_isk(grand_total):>16} ISK")


# ------------------------------------------------------------------
# Authenticated commands
# ------------------------------------------------------------------

def cmd_auth():
    config = esi.load_config()
    p = esi.authenticate(config)
    info = p.whoami()
    print(f"Authenticated as: {info.get('name', 'Unknown')}")


def cmd_assets(p, sde: SDE, character_id: int):
    print(f"\n{'='*70}")
    print("CHARACTER ASSETS")
    print(f"{'='*70}")

    assets = esi.fetch_assets(p, character_id)
    if not assets:
        print("No assets found.")
        return

    type_ids = list(set(a["type_id"] for a in assets))
    names = sde.get_type_names(type_ids)

    by_location: dict[int, list] = defaultdict(list)
    for a in assets:
        by_location[a["location_id"]].append(a)

    print(f"\nTotal items: {len(assets)}")
    print(f"Across {len(by_location)} locations\n")

    for loc_id, items in sorted(by_location.items()):
        loc_type = items[0].get("location_type", "unknown")
        loc_name = esi.resolve_location_name(p, loc_id, loc_type)

        print(f"\n--- {loc_name} ({len(items)} items) ---")
        sorted_items = sorted(items, key=lambda x: names.get(x["type_id"], ""))
        for item in sorted_items[:50]:
            name = names.get(item["type_id"], f"Type {item['type_id']}")
            qty = item.get("quantity", 1)
            flag = item.get("location_flag", "")
            singleton = " (assembled)" if item.get("is_singleton") else ""
            print(f"  {qty:>8,}x  {name:<45} [{flag}]{singleton}")
        if len(sorted_items) > 50:
            print(f"  ... and {len(sorted_items) - 50} more items")


def cmd_blueprints(p, sde: SDE, character_id: int):
    print(f"\n{'='*70}")
    print("BLUEPRINTS")
    print(f"{'='*70}")

    blueprints = esi.fetch_blueprints(p, character_id)
    if not blueprints:
        print("No blueprints found.")
        return

    type_ids = list(set(bp["type_id"] for bp in blueprints))
    names = sde.get_type_names(type_ids)

    bpos = [bp for bp in blueprints if bp.get("quantity", 0) != -2]
    bpcs = [bp for bp in blueprints if bp.get("quantity", 0) == -2]

    print(f"\nTotal blueprints: {len(blueprints)}")
    print(f"  BPOs: {len(bpos)}")
    print(f"  BPCs: {len(bpcs)}")

    if bpos:
        print(f"\n--- Original Blueprints (BPOs) ---")
        print(f"  {'Name':<45} {'ME':>4} {'TE':>4} {'Runs':>8}")
        print(f"  {'-'*45} {'---':>4} {'---':>4} {'-------':>8}")
        for bp in sorted(bpos, key=lambda x: names.get(x["type_id"], "")):
            name = names.get(bp["type_id"], f"Type {bp['type_id']}")
            me = bp.get("material_efficiency", 0)
            te = bp.get("time_efficiency", 0)
            runs = bp.get("runs", -1)
            runs_str = "inf" if runs == -1 else str(runs)
            print(f"  {name:<45} {me:>4} {te:>4} {runs_str:>8}")

    if bpcs:
        print(f"\n--- Blueprint Copies (BPCs) ---")
        print(f"  {'Name':<45} {'ME':>4} {'TE':>4} {'Runs':>8}")
        print(f"  {'-'*45} {'---':>4} {'---':>4} {'-------':>8}")
        for bp in sorted(bpcs, key=lambda x: names.get(x["type_id"], "")):
            name = names.get(bp["type_id"], f"Type {bp['type_id']}")
            me = bp.get("material_efficiency", 0)
            te = bp.get("time_efficiency", 0)
            runs = bp.get("runs", 0)
            print(f"  {name:<45} {me:>4} {te:>4} {runs:>8}")


def cmd_jobs(p, sde: SDE, character_id: int):
    print(f"\n{'='*70}")
    print("INDUSTRY JOBS")
    print(f"{'='*70}")

    jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
    if not jobs:
        print("No industry jobs found.")
        return

    type_ids = set()
    for j in jobs:
        type_ids.add(j.get("blueprint_type_id", 0))
        type_ids.add(j.get("product_type_id", 0))
    names = sde.get_type_names(list(type_ids))

    active = [j for j in jobs if j.get("status") == "active"]
    delivered = [j for j in jobs if j.get("status") == "delivered"]

    now = datetime.now(timezone.utc)

    print(f"\nTotal jobs: {len(jobs)}  (Active: {len(active)}, "
          f"Delivered: {len(delivered)})")

    if active:
        print(f"\n--- Active Jobs ---")
        for job in active:
            bp_name = names.get(job.get("blueprint_type_id", 0), "Unknown")
            product_name = names.get(job.get("product_type_id", 0), "Unknown")
            activity = ACTIVITY_NAMES.get(job.get("activity_id", 0), "Unknown")
            runs = job.get("runs", 1)
            cost = job.get("cost", 0)

            end_str = job.get("end_date", "")
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                remaining = end_dt - now
                if remaining.total_seconds() > 0:
                    time_left = fmt_time(int(remaining.total_seconds()))
                    time_left += " remaining"
                else:
                    time_left = "READY for delivery"
            except Exception:
                time_left = "unknown"

            print(f"\n  {activity}: {bp_name}")
            print(f"    Product: {product_name} x{runs}")
            print(f"    Cost: {cost:,.0f} ISK | {time_left}")

    if delivered:
        print(f"\n--- Recently Delivered ({len(delivered)} jobs) ---")
        for job in delivered[:10]:
            bp_name = names.get(job.get("blueprint_type_id", 0), "Unknown")
            activity = ACTIVITY_NAMES.get(job.get("activity_id", 0), "Unknown")
            runs = job.get("runs", 1)
            print(f"  {activity}: {bp_name} x{runs}")


def cmd_shop(p, sde: SDE, character_id: int, args: list[str],
             structure_bonus: float, region_id: int):
    if not args:
        print("Usage: eve_inventory.py shop <name> [me] [runs]")
        return

    term = args[0]
    me = int(args[1]) if len(args) > 1 else 10
    runs = int(args[2]) if len(args) > 2 else 1

    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]
    print(f"\n{'='*90}")
    print("SHOPPING LIST")
    print(f"{'='*90}")
    print(f"\n  Building: {bp['product_name']} x{runs} (ME {me})")
    if structure_bonus > 0:
        print(f"  Structure bonus: -{structure_bonus}%")

    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)
    if not materials:
        print("\n  No materials found.")
        return

    print("  Fetching assets...")
    assets = esi.fetch_assets(p, character_id)
    asset_index = esi.build_asset_index(assets)

    print("  Fetching market prices...")
    type_ids = [mat["type_id"] for mat in materials]
    prices = esi.get_bulk_market_data(type_ids, region_id)

    hdr = (f"{'Material':<30} {'Need':>10} {'Have':>10} {'Buy':>10}"
           f" {'Jita Sell':>14} {'Est. Cost':>16} {'':>6}")
    print(f"\n  {hdr}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}"
          f" {'-'*14} {'-'*16} {'-'*6}")

    missing_count = 0
    total_buy_cost = 0.0
    for mat in materials:
        needed = mat["adjusted_quantity"]
        have = asset_index.get(mat["type_id"], 0)
        deficit = max(0, needed - have)
        status = "  OK" if deficit == 0 else "  NEED"
        if deficit > 0:
            missing_count += 1

        sell_price = prices[mat["type_id"]]["sell_min"]
        line_cost = sell_price * deficit
        total_buy_cost += line_cost

        print(
            f"  {mat['name']:<30} "
            f"{needed:>10,} "
            f"{have:>10,} "
            f"{deficit:>10,} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16} "
            f"{status:>6}"
        )

    print()
    if missing_count == 0:
        print("  All materials on hand. Ready to build!")
    else:
        print(f"  Missing {missing_count} material(s). See 'Buy' column above.")
        print(f"  Estimated buy cost: {fmt_isk(total_buy_cost)} ISK")


def cmd_summary(p, sde: SDE, character_id: int):
    char_name = esi.get_character_name(p)

    print(f"\n{'='*70}")
    print(f"INDUSTRY SUMMARY — {char_name}")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    # Assets
    assets = esi.fetch_assets(p, character_id)
    print(f"\nAssets: {len(assets)} items across "
          f"{len(set(a['location_id'] for a in assets))} locations")

    # Blueprints
    blueprints = esi.fetch_blueprints(p, character_id)
    bpos = [bp for bp in blueprints if bp.get("quantity", 0) != -2]
    bpcs = [bp for bp in blueprints if bp.get("quantity", 0) == -2]
    print(f"Blueprints: {len(bpos)} BPOs, {len(bpcs)} BPCs")

    # Flag under-researched BPOs
    low_me = [bp for bp in bpos if bp.get("material_efficiency", 0) < 10]
    if low_me:
        type_ids = [bp["type_id"] for bp in low_me]
        names = sde.get_type_names(type_ids)
        print(f"\n  BPOs below ME 10 ({len(low_me)}):")
        for bp in sorted(low_me, key=lambda x: x.get("material_efficiency", 0))[:10]:
            name = names.get(bp["type_id"], f"Type {bp['type_id']}")
            me = bp.get("material_efficiency", 0)
            print(f"    ME {me:>2}: {name}")
        if len(low_me) > 10:
            print(f"    ... and {len(low_me) - 10} more")

    # Industry jobs
    jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
    active_jobs = [j for j in jobs if j.get("status") == "active"]

    now = datetime.now(timezone.utc)
    ready, in_progress = [], []
    for job in active_jobs:
        end_str = job.get("end_date", "")
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            (ready if end_dt <= now else in_progress).append(job)
        except Exception:
            in_progress.append(job)

    print(f"\nIndustry: {len(active_jobs)} active "
          f"({len(ready)} ready, {len(in_progress)} in progress)")

    if active_jobs:
        activity_counts: dict[str, int] = defaultdict(int)
        total_cost = 0
        for job in active_jobs:
            act = ACTIVITY_NAMES.get(job.get("activity_id", 0), "Unknown")
            activity_counts[act] += 1
            total_cost += job.get("cost", 0)

        print(f"\n  Job breakdown:")
        for act, count in sorted(activity_counts.items()):
            print(f"    {act}: {count}")
        print(f"  Total job costs: {total_cost:,.0f} ISK")

        if ready:
            type_ids = [j.get("product_type_id", 0) for j in ready]
            names = sde.get_type_names(type_ids)
            print(f"\n  Jobs ready for delivery:")
            for job in ready:
                product = names.get(job.get("product_type_id", 0), "Unknown")
                act = ACTIVITY_NAMES.get(job.get("activity_id", 0), "")
                print(f"    - {act}: {product} x{job.get('runs', 1)}")

        if in_progress:
            soonest = min(in_progress, key=lambda j: j.get("end_date", "9999"))
            end_str = soonest.get("end_date", "")
            type_ids = [soonest.get("product_type_id", 0)]
            names = sde.get_type_names(type_ids)
            product = names.get(type_ids[0], "Unknown")
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                remaining = end_dt - now
                print(f"\n  Next completion: {product} in "
                      f"{fmt_time(int(remaining.total_seconds()))}")
            except Exception:
                pass


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

HELP = """
Sihcom Industry & Inventory Tracker

SDE-only commands (no auth needed):
  search <name>                    Search items/blueprints
  materials <name> [me] [runs]     Material requirements + market prices
  detail <name>                    Full blueprint info
  mecomp <name> [runs]             ME 0-10 comparison table
  prices <name>                    Market prices (buy/sell/volume)
  chain <name> [me] [runs]         Full material chain (resolve subcomponents)

ESI commands (require auth):
  auth                             SSO authentication
  assets                           Character assets
  blueprints                       Blueprints with ME/TE
  jobs                             Industry jobs
  shop <name> [me] [runs]          Shopping list vs assets + prices
  summary                          Full industry dashboard

Environment:
  STRUCTURE_BONUS   Structure material bonus % (default: 0)
  MARKET_REGION     Region ID for prices (default: 10000002 = The Forge/Jita)

Examples:
  python eve_inventory.py materials "Antimatter Charge M" 10 100
  python eve_inventory.py prices tritanium
  python eve_inventory.py chain "Heavy Pulse Laser II" 10 1
  python eve_inventory.py shop drake 10 5
  python eve_inventory.py mecomp revelation
  python eve_inventory.py summary
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "-h", "--help"):
        print(HELP)
        return

    command = sys.argv[1].lower()
    args = sys.argv[2:]
    structure_bonus = float(os.environ.get("STRUCTURE_BONUS", "0"))
    region_id = esi.get_market_region()

    # Auth-only command
    if command == "auth":
        cmd_auth()
        return

    # SDE-only commands (no authentication needed)
    sde_commands = {"search", "materials", "detail", "mecomp", "prices", "chain"}
    if command in sde_commands:
        with SDE() as sde:
            if command == "search":
                cmd_search(sde, args)
            elif command == "materials":
                cmd_materials(sde, args, structure_bonus, region_id)
            elif command == "detail":
                cmd_detail(sde, args)
            elif command == "mecomp":
                cmd_mecomp(sde, args, structure_bonus)
            elif command == "prices":
                cmd_prices(sde, args, region_id)
            elif command == "chain":
                cmd_chain(sde, args, structure_bonus, region_id)
        return

    # Authenticated commands (need both SDE and ESI)
    config = esi.load_config()
    p = esi.get_authed_preston(config)
    character_id = esi.get_character_id(p)

    with SDE() as sde:
        if command == "assets":
            cmd_assets(p, sde, character_id)
        elif command == "blueprints":
            cmd_blueprints(p, sde, character_id)
        elif command == "jobs":
            cmd_jobs(p, sde, character_id)
        elif command == "shop":
            cmd_shop(p, sde, character_id, args, structure_bonus, region_id)
        elif command == "summary":
            cmd_summary(p, sde, character_id)
        else:
            print(f"Unknown command: {command}")
            print(HELP)


if __name__ == "__main__":
    main()
