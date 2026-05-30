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
    plan                             Action plan for the build list (uses ESI if authed)

Environment:
    STRUCTURE_BONUS   Structure material bonus % (default: 0)
    MARKET_REGION     Region ID for market prices (default: 10000002 = The Forge/Jita)
"""

import math
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
import build_list
import plan


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
           f" {'Saved':>8} {'Vol m³':>12} {'Jita Sell':>14} {'Total Cost':>16}")
    print(f"\n  {hdr}")
    print(f"  {'-'*30} {'-'*10} {'-'*12} {'-'*8} {'-'*12} {'-'*14} {'-'*16}")

    grand_total = 0.0
    grand_volume = 0.0
    for mat in materials:
        sell_price = prices[mat["type_id"]]["sell_min"]
        line_cost = sell_price * mat["adjusted_quantity"]
        grand_total += line_cost
        total_vol = mat.get("total_volume", 0.0)
        grand_volume += total_vol

        print(
            f"  {mat['name']:<30} "
            f"{mat['base_quantity']:>10,} "
            f"{mat['adjusted_quantity']:>12,} "
            f"{mat['saved']:>8,} "
            f"{total_vol:>12,.2f} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16}"
        )

    print(f"\n  {'Total volume:':>62} {grand_volume:>12,.2f} m³")
    print(f"  {'Estimated material cost:':>90} {fmt_isk(grand_total):>16} ISK")

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

    # Aggregated raw materials with prices and volumes
    raw_materials = flatten_material_tree(tree)

    print(f"\n  Fetching market prices...")
    type_ids = [m["type_id"] for m in raw_materials]
    prices = esi.get_bulk_market_data(type_ids, region_id) if type_ids else {}
    volumes = sde.get_type_volumes(type_ids) if type_ids else {}

    hdr = (f"{'Material':<35} {'Total Needed':>14} {'Vol m³':>12}"
           f" {'Jita Sell':>14} {'Total Cost':>16}")
    print(f"\n  --- Aggregated Raw Materials ---\n")
    print(f"  {hdr}")
    print(f"  {'-'*35} {'-'*14} {'-'*12} {'-'*14} {'-'*16}")

    grand_total = 0.0
    grand_volume = 0.0
    for mat in raw_materials:
        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        line_cost = sell_price * mat["quantity"]
        grand_total += line_cost
        unit_vol = volumes.get(mat["type_id"], 0.0)
        total_vol = unit_vol * mat["quantity"]
        grand_volume += total_vol
        print(
            f"  {mat['name']:<35} "
            f"{mat['quantity']:>14,} "
            f"{total_vol:>12,.2f} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16}"
        )

    print(f"\n  {'Total volume:':>63} {grand_volume:>12,.2f} m³")
    print(f"  {'Total raw material cost:':>77} {fmt_isk(grand_total):>16} ISK")


# ------------------------------------------------------------------
# Build list + action plan
# ------------------------------------------------------------------

def _resolve_targets(sde: SDE, targets: list[dict]) -> list:
    """Turn build-list dicts into plan.Target objects with resolved input trees.

    Mirrors app._resolve_targets: qty (product units) drives runs; runs is an
    optional override. Skips targets with no manufacturing blueprint.
    """
    out = []
    for t in targets:
        bp_id = sde.find_blueprint_for_product(t["type_id"])
        if bp_id is None:
            print(f"  Skipping '{t['name']}' — not manufacturable.")
            continue
        qpr = sde.get_product_qty_per_run(bp_id) or 1
        qty = t.get("qty", 1)
        runs = t.get("runs")
        if not runs:
            runs = max(1, math.ceil(qty / qpr))
        needed = runs * qpr
        children = resolve_material_chain(
            sde, bp_id,
            me_level=t.get("me", 10),
            runs=runs,
            structure_bonus=t.get("structure_bonus", 0.0),
        )
        # TODO: merge_trees() labels every top-level product as activity_id=1
        # (manufacturing). Reaction-built products are therefore shown as
        # manufacturing. Passing the true activity would require a new
        # plan.Target.activity_id field (out of scope for this task).
        out.append(plan.Target(
            type_id=t["type_id"], name=t["name"], blueprint_type_id=bp_id,
            needed=needed, children=children,
        ))
    return out


def cmd_plan():
    targets = build_list.load()
    if not targets:
        print("Build list is empty. "
              "Add targets via the web UI or build_list.json.")
        return

    with SDE() as sde:
        resolved = _resolve_targets(sde, targets)
        graph = plan.merge_trees(resolved)
        buy_set = {tid for t in targets for tid in t.get("buy_set", [])}

        loc_index: dict = {}
        jobs: list = []
        build_station = None

        # Use ESI only if it's already configured AND a saved token exists —
        # never force the SSO browser flow from `plan`.
        if os.path.exists(esi.CONFIG_FILE) and esi.load_refresh_token():
            try:
                p = esi.get_authed_preston()
                character_id = esi.get_character_id(p)
                corporation_id = esi.get_corporation_id(p, character_id)
                if corporation_id:
                    loc_index = esi.get_cached_location_asset_index(
                        p, corporation_id, is_corp=True,
                    )
                else:
                    loc_index = esi.get_cached_location_asset_index(
                        p, character_id, is_corp=False,
                    )
                # Active-only jobs feed classify (_in_job_qty counts only
                # active/ready/paused). Station ranking uses a completed-
                # inclusive history so we find the usual build station even
                # with no active jobs — mirrors app._get_station_list.
                jobs = esi.fetch_industry_jobs(p, character_id)
                station_jobs = esi.fetch_industry_jobs(
                    p, character_id, include_completed=True,
                )
                stations = esi.extract_manufacturing_stations(station_jobs)
                build_station = stations[0] if stations else None
            except Exception as e:
                print(f"  ESI lookup failed ({e}); continuing without it.")
                loc_index, jobs, build_station = {}, [], None
        else:
            print("  No ESI auth — without it everything shows as "
                  "blocked/buy. Run 'auth' to enable inventory/job checks.")

        buckets = plan.classify(graph, loc_index, jobs, build_station, buy_set)
        volumes = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
        buckets["buy"] = plan.enrich_buy(
            buckets["buy"], loc_index, build_station, volumes,
        )

    print(f"\n{'='*90}")
    print("ACTION PLAN")
    print(f"{'='*90}")
    print(f"\n  Plan: {len(targets)} target{'s' if len(targets) != 1 else ''}")

    ready = buckets["ready"]
    in_progress = buckets["in_progress"]
    blocked = buckets["blocked"]
    buy = buckets["buy"]

    if ready:
        print(f"\n  --- READY TO START NOW ---\n")
        print(f"  {'Name':<35} {'Qty to Make':>12} {'Activity':<16}")
        print(f"  {'-'*35} {'-'*12} {'-'*16}")
        for r in ready:
            act = ACTIVITY_NAMES.get(r.get("activity_id", 0), "Manufacturing")
            print(f"  {r['name']:<35} {r['shortfall']:>12,} {act:<16}")

    if in_progress:
        print(f"\n  --- IN PROGRESS ---\n")
        print(f"  {'Name':<35} {'Qty Short':>12} {'Completes':<22}")
        print(f"  {'-'*35} {'-'*12} {'-'*22}")
        for r in in_progress:
            ends = r.get("end_date") or "-"
            print(f"  {r['name']:<35} {r['shortfall']:>12,} {ends:<22}")

    if blocked:
        print(f"\n  --- BLOCKED ---\n")
        for r in blocked:
            print(f"  {r['name']}")
            needs = ", ".join(
                f"{m['name']} x{m['shortfall']:,}" for m in r.get("missing", [])
            )
            print(f"    needs: {needs}")

    if buy:
        print(f"\n  --- BUY LIST ---\n")
        print(f"  {'Name':<35} {'To Buy':>12} {'Vol m³':>12} {'At Station':>12}")
        print(f"  {'-'*35} {'-'*12} {'-'*12} {'-'*12}")
        for r in buy:
            to_buy = r.get("to_buy", r.get("shortfall", 0))
            vol = r.get("to_buy_volume", 0.0)
            at_station = r.get("at_station", 0)
            print(f"  {r['name']:<35} {to_buy:>12,} "
                  f"{vol:>12,.2f} {at_station:>12,}")

    if not (ready or in_progress or blocked or buy):
        print("\n  Nothing to do — everything needed is already on hand.")


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
           f" {'Buy Vol m³':>12} {'Jita Sell':>14} {'Est. Cost':>16} {'':>6}")
    print(f"\n  {hdr}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}"
          f" {'-'*12} {'-'*14} {'-'*16} {'-'*6}")

    missing_count = 0
    total_buy_cost = 0.0
    total_buy_volume = 0.0
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
        unit_vol = mat.get("volume", 0.0)
        buy_vol = unit_vol * deficit
        total_buy_volume += buy_vol

        print(
            f"  {mat['name']:<30} "
            f"{needed:>10,} "
            f"{have:>10,} "
            f"{deficit:>10,} "
            f"{buy_vol:>12,.2f} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16} "
            f"{status:>6}"
        )

    print()
    if missing_count == 0:
        print("  All materials on hand. Ready to build!")
    else:
        print(f"  Missing {missing_count} material(s). See 'Buy' column above.")
        print(f"  Total buy volume: {total_buy_volume:,.2f} m³")
        print(f"  Estimated buy cost: {fmt_isk(total_buy_cost)} ISK")


def cmd_profit(p, sde: SDE, character_id: int, args: list[str],
               structure_bonus: float, region_id: int,
               broker_fee: float, sales_tax: float,
               material_cost_pct: float):
    if not args:
        print("Usage: eve_inventory.py profit <name> [me] [runs]")
        return

    term = args[0]
    me = int(args[1]) if len(args) > 1 else 10
    runs = int(args[2]) if len(args) > 2 else 1

    bp = pick_blueprint(sde, term)
    if not bp:
        return

    bp_id = bp["blueprint_type_id"]
    product_id = bp["product_type_id"]

    # Get product quantity per run (e.g. 100 for ammo, 1 for ships)
    prod_row = sde.conn.execute(
        "SELECT quantity FROM industryActivityProducts "
        "WHERE typeID = ? AND activityID = 1",
        (bp_id,),
    ).fetchone()
    qty_per_run = prod_row["quantity"] if prod_row else 1
    total_product_qty = qty_per_run * runs

    print(f"\n{'='*100}")
    print("PROFIT ANALYSIS")
    print(f"{'='*100}")
    print(f"\n  Blueprint: {bp['blueprint_name']}")
    print(f"  Product:   {bp['product_name']} x{total_product_qty}"
          f" ({runs} run{'s' if runs != 1 else ''}"
          f"{f', {qty_per_run}/run' if qty_per_run > 1 else ''})")
    print(f"  ME Level:  {me}")
    if structure_bonus > 0:
        print(f"  Structure: -{structure_bonus}% materials")
    print(f"  Broker:    {broker_fee}%  |  Sales Tax: {sales_tax}%"
          f"  |  Material Cost Basis: {material_cost_pct:.0f}% of Jita")

    # --- Material cost ---
    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)
    if not materials:
        print("\n  No manufacturing materials found.")
        return

    print("  Fetching assets...")
    assets = esi.fetch_assets(p, character_id)
    asset_index = esi.build_asset_index(assets)

    print("  Fetching market prices...")
    mat_type_ids = [mat["type_id"] for mat in materials]
    all_type_ids = mat_type_ids + [product_id]
    prices = esi.get_bulk_market_data(all_type_ids, region_id)

    hdr = (f"{'Material':<30} {'Need':>10} {'Have':>10} {'Buy':>10}"
           f" {'Buy Vol m³':>12} {'Jita Sell':>14} {'Line Cost':>16}")
    print(f"\n  --- Material Cost ---\n")
    print(f"  {hdr}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}"
          f" {'-'*12} {'-'*14} {'-'*16}")

    total_owned_cost = 0.0
    total_buy_cost = 0.0
    total_buy_volume = 0.0
    cost_pct = material_cost_pct / 100.0

    for mat in materials:
        needed = mat["adjusted_quantity"]
        have = asset_index.get(mat["type_id"], 0)
        owned_qty = min(have, needed)
        buy_qty = max(0, needed - have)

        sell_price = prices[mat["type_id"]]["sell_min"]
        owned_cost = owned_qty * sell_price * cost_pct
        buy_cost = buy_qty * sell_price
        line_cost = owned_cost + buy_cost
        total_owned_cost += owned_cost
        total_buy_cost += buy_cost

        unit_vol = mat.get("volume", 0.0)
        buy_vol = unit_vol * buy_qty
        total_buy_volume += buy_vol

        print(
            f"  {mat['name']:<30} "
            f"{needed:>10,} "
            f"{have:>10,} "
            f"{buy_qty:>10,} "
            f"{buy_vol:>12,.2f} "
            f"{fmt_isk(sell_price):>14} "
            f"{fmt_isk(line_cost):>16}"
        )

    material_cost = total_owned_cost + total_buy_cost
    print(f"\n  {'Materials on hand (at ' + f'{material_cost_pct:.0f}' + '% Jita):':>74}"
          f" {fmt_isk(total_owned_cost):>16} ISK")
    print(f"  {'Materials to buy:':>74} {fmt_isk(total_buy_cost):>16} ISK")
    print(f"  {'Total material cost:':>74} {fmt_isk(material_cost):>16} ISK")
    print(f"  {'Total buy volume:':>74} {total_buy_volume:>15,.2f} m³")

    # --- Product revenue ---
    product_data = prices[product_id]
    sell_min = product_data["sell_min"]
    buy_max = product_data["buy_max"]

    broker_rate = broker_fee / 100.0
    tax_rate = sales_tax / 100.0

    # Per unit net: sell order = price * (1 - broker - tax), instant sell = price * (1 - tax)
    net_sell_unit = sell_min * (1 - broker_rate - tax_rate)
    net_buy_unit = buy_max * (1 - tax_rate)

    print(f"\n  --- Product Revenue (per unit) ---\n")
    w = 18  # column width
    print(f"  {'':30} {'Sell Order':>{w}} {'Instant Sell':>{w}}  ISK")
    print(f"  {'Market price:':<30} {fmt_isk(sell_min):>{w}} {fmt_isk(buy_max):>{w}}")
    print(f"  {f'Broker fee ({broker_fee}%):':<30}"
          f" {'-' + fmt_isk(sell_min * broker_rate):>{w}}"
          f" {'-':>{w}}")
    print(f"  {f'Sales tax ({sales_tax}%):':<30}"
          f" {'-' + fmt_isk(sell_min * tax_rate):>{w}}"
          f" {'-' + fmt_isk(buy_max * tax_rate):>{w}}")
    print(f"  {'Net per unit:':<30} {fmt_isk(net_sell_unit):>{w}} {fmt_isk(net_buy_unit):>{w}}")

    # --- Profit summary ---
    revenue_sell = net_sell_unit * total_product_qty
    revenue_buy = net_buy_unit * total_product_qty

    profit_sell = revenue_sell - material_cost
    profit_buy = revenue_buy - material_cost

    margin_sell = (profit_sell / revenue_sell * 100) if revenue_sell > 0 else 0.0
    margin_buy = (profit_buy / revenue_buy * 100) if revenue_buy > 0 else 0.0

    base_time = sde.get_activity_time(bp_id, ACTIVITY_MANUFACTURING)
    if base_time:
        total_time_hrs = (base_time * runs) / 3600.0
        isk_hr_sell = profit_sell / total_time_hrs if total_time_hrs > 0 else 0.0
        isk_hr_buy = profit_buy / total_time_hrs if total_time_hrs > 0 else 0.0
    else:
        total_time_hrs = None
        isk_hr_sell = None
        isk_hr_buy = None

    print(f"\n  --- Profit Summary ({total_product_qty}x {bp['product_name']}) ---\n")
    print(f"  {'':30} {'Sell Order':>{w}} {'Instant Sell':>{w}}")
    print(f"  {f'Revenue ({total_product_qty}x):':<30}"
          f" {fmt_isk(revenue_sell):>{w}} {fmt_isk(revenue_buy):>{w}}  ISK")
    print(f"  {'Material cost:':<30}"
          f" {'-' + fmt_isk(material_cost):>{w}} {'-' + fmt_isk(material_cost):>{w}}  ISK")
    print(f"  {'Profit:':<30}"
          f" {fmt_isk(profit_sell):>{w}} {fmt_isk(profit_buy):>{w}}  ISK")
    print(f"  {'Margin:':<30}"
          f" {margin_sell:>{w - 1}.1f}% {margin_buy:>{w - 1}.1f}%")
    if isk_hr_sell is not None:
        print(f"  {'ISK/hr:':<30}"
              f" {fmt_isk(isk_hr_sell):>{w}} {fmt_isk(isk_hr_buy):>{w}}  ISK")

    if base_time:
        print(f"\n  Base manufacturing time (per run): {fmt_time(base_time)}"
              f"  |  Total: {fmt_time(base_time * runs)}")


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
  profit <name> [me] [runs]        Profit analysis (materials vs sell price)
  summary                          Full industry dashboard

Build list:
  plan                             Action plan for the build list
                                   (resolves + classifies into ready/in-progress/
                                   blocked/buy; uses ESI inventory & jobs if authed)

Environment:
  STRUCTURE_BONUS    Structure material bonus % (default: 0)
  MARKET_REGION      Region ID for prices (default: 10000002 = The Forge/Jita)
  BROKER_FEE         Broker fee % for sell orders (default: 1.5)
  SALES_TAX          Sales tax % (default: 3.6)
  MATERIAL_COST_PCT  Cost basis for owned materials as % of Jita (default: 100)

Examples:
  python eve_inventory.py materials "Antimatter Charge M" 10 100
  python eve_inventory.py prices tritanium
  python eve_inventory.py chain "Heavy Pulse Laser II" 10 1
  python eve_inventory.py shop drake 10 5
  python eve_inventory.py profit drake 10 5
  python eve_inventory.py mecomp revelation
  python eve_inventory.py summary
  python eve_inventory.py plan
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "-h", "--help"):
        print(HELP)
        return

    command = sys.argv[1].lower()
    args = sys.argv[2:]
    structure_bonus = float(os.environ.get("STRUCTURE_BONUS", "0"))
    region_id = esi.get_market_region()
    broker_fee = float(os.environ.get("BROKER_FEE", "1.5"))
    sales_tax = float(os.environ.get("SALES_TAX", "3.6"))
    material_cost_pct = float(os.environ.get("MATERIAL_COST_PCT", "100"))

    # Auth-only command
    if command == "auth":
        cmd_auth()
        return

    # Build plan: opens its own SDE and uses ESI only if already authed.
    if command == "plan":
        cmd_plan()
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
        elif command == "profit":
            cmd_profit(p, sde, character_id, args, structure_bonus, region_id,
                       broker_fee, sales_tax, material_cost_pct)
        elif command == "summary":
            cmd_summary(p, sde, character_id)
        else:
            print(f"Unknown command: {command}")
            print(HELP)


if __name__ == "__main__":
    main()
