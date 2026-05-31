"""
app.py — Flask web interface for Sihcom Industry Tracker.

Reuses sde.py and esi.py for all data operations.
Deployable to Railway with gunicorn.
"""

import logging
import math
import os
import secrets
import time
import traceback
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash,
)
from preston import Preston

from sde import (
    SDE, calculate_materials, ACTIVITY_MANUFACTURING, ACTIVITY_INVENTION,
    resolve_material_chain, flatten_material_tree, get_chain_summary,
    MaterialNode,
)
import esi
from hauling import calculate_build_capacity, calculate_deficit
import build_list
import plan

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Log errors to stdout so Railway can capture them
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Chain tree cache (SDE data is static, cache permanently per params)
# ------------------------------------------------------------------
_chain_cache: dict[tuple, tuple[list, list, dict]] = {}  # key -> (tree, raw, summary)


def get_cached_chain(sde, bp_id, me, runs, structure_bonus, sub_me, resolve_reactions):
    """Cache chain resolution — SDE data never changes within a session."""
    key = (bp_id, me, runs, structure_bonus, sub_me, resolve_reactions)
    if key in _chain_cache:
        return _chain_cache[key]

    tree = resolve_material_chain(
        sde, bp_id, me, runs, structure_bonus,
        sub_me=sub_me, resolve_reactions=resolve_reactions,
    )
    raw_materials = flatten_material_tree(tree)
    summary = get_chain_summary(tree)

    _chain_cache[key] = (tree, raw_materials, summary)
    return tree, raw_materials, summary


def _compute_capacity_bundle(
    sde, p: Preston, bp_id: int, me: int, structure_bonus: float,
    character_id: int, corporation_id: int | None,
) -> dict | None:
    """Compute build-capacity bundle for the blueprint page widget.

    Resolves the chain at runs=1 to get per-run raw materials, then
    projects them against the stockpile (corp assets if available,
    else personal). Returns None if anything goes wrong or there's
    nothing useful to show — the widget renders nothing in that case.
    """
    try:
        if corporation_id:
            asset_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
            source = "corp"
        else:
            asset_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
            source = "personal"

        if not asset_index:
            return None

        # Resolve chain at runs=1 → per-run raw materials.
        # ME rounding at each level can introduce minor compounding vs
        # resolving at the user's actual runs setting, but for capacity
        # estimation the difference is rounding-level.
        _, raw_materials, _ = get_cached_chain(
            sde, bp_id, me, runs=1, structure_bonus=structure_bonus,
            sub_me=10, resolve_reactions=True,
        )
        if not raw_materials:
            return None

        capacity = calculate_build_capacity(raw_materials, asset_index)
        if capacity["capacity_runs"] is None:
            return None

        product_id = sde.find_product_for_blueprint(bp_id)
        product_name = sde.get_type_name(product_id) if product_id else "unit"
        prod_row = sde.conn.execute(
            "SELECT quantity FROM industryActivityProducts "
            "WHERE typeID = ? AND activityID = 1",
            (bp_id,),
        ).fetchone()
        qty_per_run = prod_row["quantity"] if prod_row else 1

        return {
            "capacity_runs": capacity["capacity_runs"],
            "capacity_units": capacity["capacity_runs"] * qty_per_run,
            "qty_per_run": qty_per_run,
            "product_name": product_name,
            "source": source,
            "constraints": capacity["constraints"][:3],
        }
    except Exception:
        logger.debug("Capacity computation failed", exc_info=True)
        return None


def _get_station_list(p: Preston, character_id: int) -> list[dict]:
    """Fetch manufacturing stations for the character, ranked by usage.

    Returns up to 10 stations as [{id, name}, ...]. Returns an empty list on
    any failure (the station dropdown is optional UI).
    """
    try:
        jobs = esi.fetch_industry_jobs(p, character_id, include_completed=True)
        station_ids = esi.extract_manufacturing_stations(jobs)
        stations = []
        for sid in station_ids[:10]:
            name = esi.get_cached_location_name(p, sid, "other")
            stations.append({"id": sid, "name": name})
        return stations
    except Exception:
        logger.debug("Could not fetch station list", exc_info=True)
        return []


@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"Unhandled error: {e}\n{traceback.format_exc()}")
    return f"<h1>Error</h1><pre>{e}</pre>", 500


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def get_esi_config() -> dict:
    """Build ESI config from environment variables."""
    return {
        "client_id": os.environ.get("ESI_CLIENT_ID", ""),
        "client_secret": os.environ.get("ESI_CLIENT_SECRET", ""),
        "callback_url": os.environ.get(
            "ESI_CALLBACK_URL", "http://localhost:5000/callback"
        ),
        "user_agent": os.environ.get(
            "ESI_USER_AGENT", "Sihcom Industry Tracker"
        ),
    }


# ------------------------------------------------------------------
# SDE lifecycle
# ------------------------------------------------------------------

_sde: SDE | None = None


def get_sde() -> SDE:
    """Lazy singleton SDE instance."""
    global _sde
    if _sde is None:
        ensure_sde_downloaded()
        _sde = SDE()
    return _sde


def _sde_is_valid(path: str) -> bool:
    """Quick sanity check that the SDE database is usable."""
    import sqlite3
    try:
        conn = sqlite3.connect(path)
        count = conn.execute("SELECT COUNT(*) FROM invTypes").fetchone()[0]
        conn.close()
        return count > 0
    except Exception as e:
        logger.warning(f"SDE validation failed: {e}")
        return False


def ensure_sde_downloaded():
    """Ensure an SDE is present and valid.

    On Railway the SDE is baked into the image during the build phase
    (see nixpacks.toml) — we just need to confirm it survived. We do
    NOT auto-refresh on stale because the converter requires ~2-3 GB
    of RAM and gets OOM-killed in Railway's runtime container. To
    refresh, push a code change or click "Redeploy" in Railway so the
    build phase runs again.

    A stale build is logged as a warning but doesn't block startup.
    """
    from sde import DEFAULT_SDE_PATH
    from setup_sde import build_database, is_sde_current, read_local_build
    logger.info(f"Checking for SDE at: {DEFAULT_SDE_PATH}")

    if os.path.exists(DEFAULT_SDE_PATH):
        if _sde_is_valid(DEFAULT_SDE_PATH):
            local = read_local_build()
            if local and not is_sde_current():
                logger.warning(
                    f"SDE build {local} is older than CCP's latest. "
                    "Redeploy to refresh."
                )
            else:
                logger.info(f"SDE found and valid (build {local or 'unknown'}).")
            return
        logger.warning("SDE file is corrupt — deleting and re-downloading...")
        os.remove(DEFAULT_SDE_PATH)

    logger.info("No SDE on disk — downloading CCP YAML SDE...")
    build_database()
    logger.info(f"SDE ready (build {read_local_build()}).")


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

def get_base_preston() -> Preston:
    """Unauthenticated Preston for SSO URL generation."""
    config = get_esi_config()
    return Preston(
        user_agent=config["user_agent"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        callback_url=config["callback_url"],
        scope=esi.SCOPES,
    )


def get_authed_preston_from_session() -> Preston | None:
    """Reconstruct authenticated Preston from session. Returns None if not logged in."""
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return None
    config = get_esi_config()
    try:
        return Preston(
            user_agent=config["user_agent"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            callback_url=config["callback_url"],
            scope=esi.SCOPES,
            refresh_token=refresh_token,
        )
    except Exception:
        session.clear()
        return None


def login_required(f):
    """Decorator for routes that need ESI auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("refresh_token"):
            flash("Please log in with EVE SSO first.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------
# Jinja2 filters
# ------------------------------------------------------------------

@app.template_filter("isk")
def isk_filter(value):
    if not value:
        return "-"
    return f"{value:,.2f}"


@app.template_filter("ftime")
def ftime_filter(seconds):
    if not seconds:
        return "-"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


@app.template_filter("commas")
def commas_filter(value):
    return f"{value:,}"


@app.template_filter("vol")
def vol_filter(value):
    """Format a volume in m³."""
    if not value:
        return "-"
    return f"{value:,.2f}"


# ------------------------------------------------------------------
# Routes — public
# ------------------------------------------------------------------

@app.route("/sde-info")
def sde_info():
    """Diagnostic: which SDE build is the live instance serving."""
    from setup_sde import read_local_build, get_latest_build
    sde = get_sde()
    info = {
        "local_build": read_local_build(),
        "invTypes_rows": sde.conn.execute("SELECT COUNT(*) FROM invTypes").fetchone()[0],
        "industryActivityMaterials_rows": sde.conn.execute(
            "SELECT COUNT(*) FROM industryActivityMaterials"
        ).fetchone()[0],
    }
    try:
        info["latest_ccp_build"] = get_latest_build()
        info["is_current"] = info["local_build"] == info["latest_ccp_build"]
    except Exception as e:
        info["latest_ccp_build"] = f"error: {e}"
        info["is_current"] = None
    return jsonify(info)


@app.route("/")
def index():
    """Home page is the build list, with inline name search for targets."""
    q = request.args.get("q", "").strip()
    data = build_list.load()
    results = None
    if q:
        results = get_sde().search_manufacturable(q)
    return render_template(
        "build_list.html", targets=data["targets"],
        q=q, results=results,
        character_name=session.get("character_name"),
    )


@app.route("/search")
def search():
    """Blueprint/item search (formerly the home page)."""
    q = request.args.get("q", "").strip()
    results = None
    if q:
        sde = get_sde()
        blueprints = sde.search_blueprints(q)
        items = sde.search_types(q)
        results = {"blueprints": blueprints, "items": items}
    return render_template(
        "index.html", q=q, results=results,
        character_name=session.get("character_name"),
    )


# ------------------------------------------------------------------
# Routes — build list + action plan
# ------------------------------------------------------------------

def _resolve_targets(sde, targets):
    """Turn build-list dicts into plan.Target objects with resolved input trees."""
    out = []
    for t in targets:
        bp_id = sde.find_blueprint_for_product(t["type_id"])
        if bp_id is None:
            continue  # not manufacturable
        qpr = sde.get_product_qty_per_run(bp_id) or 1
        qty = t.get("qty", 1)
        # Qty (product units) is the driver. runs is an optional advanced
        # override; when absent, derive whole runs from qty (rounding up so we
        # build at least the requested units).
        runs = t.get("runs")
        if not runs:
            runs = max(1, math.ceil(qty / qpr))
        needed = runs * qpr  # actual units produced (>= qty, whole runs only)
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


def _compute_plan():
    """Compute action-plan buckets. Shared by /plan and /api/plan.

    Returns (buckets, targets, authed, station_ctx). Without ESI auth,
    loc_index/jobs are empty and there's no build station, so everything lands
    in blocked/buy. station_ctx = {"stations": [...], "selected": id|None}.
    """
    sde = get_sde()
    data = build_list.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])
    graph = plan.merge_trees(_resolve_targets(sde, targets), buy_set)

    loc_index: dict = {}
    jobs: list = []
    build_station = None
    stations: list = []
    loc_names: dict = {}

    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        corporation_id = session.get("corporation_id")
        # Mirror the corp-or-personal source toggle used by the shopping routes.
        if corporation_id:
            loc_index = esi.get_cached_location_asset_index(
                p, corporation_id, is_corp=True,
            )
        else:
            loc_index = esi.get_cached_location_asset_index(
                p, character_id, is_corp=False,
            )
        jobs = esi.fetch_industry_jobs(p, character_id)
        stations = _get_station_list(p, character_id)
        # Saved station wins; else fall back to the most-used station.
        build_station = plan.resolve_build_station(data.get("build_station"), stations)
        session["refresh_token"] = p.refresh_token

    buckets = plan.classify(graph, loc_index, jobs, build_station, buy_set)
    volumes = sde.get_type_volumes([r["type_id"] for r in buckets["buy"]])
    buckets["buy"] = plan.enrich_buy(buckets["buy"], loc_index, build_station, volumes)
    if p:
        # Resolve the elsewhere-location ids on the buy rows to station names,
        # iterating the SAME keys attach_haul_breakdown reads (mirrors /shopping)
        # so key types match and names resolve. Baking `haul` here keeps the
        # /api/plan JSON clean (no int-keyed dicts for the JS refresh to handle).
        elsewhere_ids = {lid for r in buckets["buy"] for lid in r.get("elsewhere", {})}
        loc_names = {
            lid: esi.get_cached_location_name(p, lid, "other")
            for lid in elsewhere_ids
        }
    buckets["buy"] = plan.attach_haul_breakdown(buckets["buy"], loc_names)

    station_ctx = {"stations": stations, "selected": build_station}
    return buckets, targets, bool(p), station_ctx


@app.route("/build-list/add", methods=["POST"])
def build_list_add():
    try:
        tid = int(request.form["type_id"])
        qty = int(request.form.get("qty", 1))
        me = int(request.form.get("me", 10))
        structure_bonus = float(request.form.get("structure_bonus", 0))
    except (KeyError, ValueError) as e:
        flash(f"Invalid input: {e}")
        return redirect(url_for("index"))
    sde = get_sde()
    build_list.add_target({
        "type_id": tid, "name": sde.get_type_name(tid),
        "qty": qty,
        # runs is an optional advanced override; the form no longer surfaces it,
        # so store None and let _resolve_targets derive runs from qty.
        "runs": None,
        "me": me,
        "structure_bonus": structure_bonus,
    })
    return redirect(url_for("index"))


@app.route("/build-list/remove/<int:type_id>", methods=["POST"])
def build_list_remove(type_id):
    build_list.remove_target(type_id)
    return redirect(url_for("index"))


@app.route("/build-station", methods=["POST"])
def build_station():
    """Persist the chosen build station and redirect back to the source page.

    An empty station_id clears the saved station (falls back to most-used).
    `next` controls the redirect target (plan_view or materials).
    """
    raw = request.form.get("station_id", "").strip()
    try:
        build_list.set_build_station(int(raw) if raw else None)
    except ValueError:
        flash(f"Invalid station id: {raw}")
        return redirect(url_for("plan_view"))
    target = request.form.get("next", "plan_view")
    if target == "materials":
        return redirect(url_for("materials", view=request.form.get("view", "flat")))
    return redirect(url_for("plan_view"))


@app.route("/plan")
def plan_view():
    buckets, targets, authed, station_ctx = _compute_plan()
    return render_template(
        "plan.html", buckets=buckets, targets=targets, authed=authed,
        station_ctx=station_ctx,
        character_name=session.get("character_name"),
    )


@app.route("/api/plan")
def api_plan():
    buckets, _t, _a, _s = _compute_plan()
    return jsonify(buckets)


@app.route("/materials")
def materials():
    view = request.args.get("view", "tree")
    sde = get_sde()
    data = build_list.load()
    targets = data["targets"]
    buy_set = set(data["buy_set"])
    resolved = _resolve_targets(sde, targets)   # list[plan.Target], each with .children

    flat_rows = None
    authed = False
    if view == "flat":
        nodes = [child for t in resolved for child in t.children]
        flat = flatten_material_tree(nodes, buy_set)
        volumes = sde.get_type_volumes([r["type_id"] for r in flat])
        owned_index = {}
        p = get_authed_preston_from_session()
        # Anonymous viewing allowed; without auth owned=0 so to_buy=total.
        if p:
            authed = True
            character_id = int(session["character_id"])
            corporation_id = session.get("corporation_id")
            # Mirror the corp-or-personal source toggle used by _compute_plan.
            if corporation_id:
                owned_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
            else:
                owned_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
            session["refresh_token"] = p.refresh_token
        flat_rows = plan.attach_supply_columns(flat, owned_index, volumes)

    return render_template("materials.html", view=view, targets=resolved,
                           buy_set=buy_set, flat_rows=flat_rows, authed=authed,
                           has_targets=bool(targets),
                           character_name=session.get("character_name"))


@app.route("/materials/toggle/<int:type_id>", methods=["POST"])
def materials_toggle(type_id):
    build_list.toggle_buy(type_id)
    view = request.form.get("view", "tree")
    return redirect(url_for("materials", view=view))


@app.route("/blueprint/<int:bp_id>")
def blueprint(bp_id):
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))

    sde = get_sde()
    region_id = esi.get_market_region()

    bp_name = sde.get_type_name(bp_id)
    product_id = sde.find_product_for_blueprint(bp_id)
    product_name = sde.get_type_name(product_id) if product_id else bp_name

    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)

    # Attach market prices (volume already in materials from calculate_materials)
    grand_total = 0.0
    grand_volume = 0.0
    if materials:
        type_ids = [m["type_id"] for m in materials]
        prices = esi.get_bulk_market_data(type_ids, region_id)
        for mat in materials:
            sell_price = prices[mat["type_id"]]["sell_min"]
            mat["sell_price"] = sell_price
            mat["line_cost"] = sell_price * mat["adjusted_quantity"]
            grand_total += mat["line_cost"]
            grand_volume += mat.get("total_volume", 0.0)

    base_time = sde.get_activity_time(bp_id, ACTIVITY_MANUFACTURING)
    invention_products = sde.get_invention_products(bp_id)
    invention_materials = (
        sde.get_activity_materials(bp_id, ACTIVITY_INVENTION)
        if invention_products else []
    )

    capacity = None
    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        corporation_id = session.get("corporation_id")
        capacity = _compute_capacity_bundle(
            sde, p, bp_id, me, structure_bonus,
            character_id, corporation_id,
        )
        session["refresh_token"] = p.refresh_token

    return render_template(
        "blueprint.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        materials=materials, grand_total=grand_total, grand_volume=grand_volume,
        base_time=base_time,
        invention_products=invention_products,
        invention_materials=invention_materials,
        character_name=session.get("character_name"),
        capacity=capacity,
    )


@app.route("/market/<int:type_id>")
def market(type_id):
    sde = get_sde()
    name = sde.get_type_name(type_id)
    region_id = esi.get_market_region()
    data = esi.get_type_market_data(type_id, region_id)

    spread = data["sell_min"] - data["buy_max"]
    spread_pct = (spread / data["sell_min"] * 100) if data["sell_min"] > 0 else 0

    # Check if this item can be manufactured
    blueprint_id = sde.find_blueprint_for_product(type_id)

    return render_template(
        "market.html",
        type_id=type_id, name=name, data=data,
        spread=spread, spread_pct=spread_pct,
        blueprint_id=blueprint_id,
        character_name=session.get("character_name"),
    )


@app.route("/api/materials/<int:bp_id>")
def api_materials(bp_id):
    """JSON endpoint for live ME recalculation."""
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))

    sde = get_sde()
    region_id = esi.get_market_region()
    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)

    grand_total = 0.0
    grand_volume = 0.0
    if materials:
        type_ids = [m["type_id"] for m in materials]
        prices = esi.get_bulk_market_data(type_ids, region_id)
        for mat in materials:
            sell_price = prices[mat["type_id"]]["sell_min"]
            mat["sell_price"] = sell_price
            mat["line_cost"] = sell_price * mat["adjusted_quantity"]
            grand_total += mat["line_cost"]
            grand_volume += mat.get("total_volume", 0.0)

    capacity = None
    p = get_authed_preston_from_session()
    if p:
        character_id = int(session["character_id"])
        corporation_id = session.get("corporation_id")
        capacity = _compute_capacity_bundle(
            sde, p, bp_id, me, structure_bonus,
            character_id, corporation_id,
        )
        session["refresh_token"] = p.refresh_token

    return jsonify(
        materials=materials,
        grand_total=grand_total,
        grand_volume=grand_volume,
        capacity=capacity,
    )


# ------------------------------------------------------------------
# Routes — chain resolution
# ------------------------------------------------------------------

def _nodes_to_dict(nodes: list[MaterialNode]) -> list[dict]:
    """Convert MaterialNode tree to JSON-serializable dicts."""
    result = []
    for node in nodes:
        d = {
            "type_id": node.type_id,
            "name": node.name,
            "quantity": node.quantity_needed,
            "is_terminal": node.is_terminal,
            "activity_name": node.activity_name,
            "me_level": node.me_level,
            "depth": node.depth,
            "children": _nodes_to_dict(node.children) if node.children else [],
        }
        result.append(d)
    return result


@app.route("/chain/<int:bp_id>")
def chain(bp_id):
    """Full material chain with interactive build/buy tree."""
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    sub_me = int(request.args.get("sub_me", 10))
    resolve_reactions = request.args.get("reactions", "1") == "1"

    sde = get_sde()
    region_id = esi.get_market_region()

    bp_name = sde.get_type_name(bp_id)
    product_id = sde.find_product_for_blueprint(bp_id)
    product_name = sde.get_type_name(product_id) if product_id else bp_name

    # Resolve the full chain (cached)
    tree, raw_materials, summary = get_cached_chain(
        sde, bp_id, me, runs, structure_bonus, sub_me, resolve_reactions,
    )

    # Price everything: intermediates + raw materials
    all_type_ids = list(set(
        [m["type_id"] for m in raw_materials]
        + [i["type_id"] for i in summary["intermediates"]]
    ))
    prices = esi.get_bulk_market_data(all_type_ids, region_id) if all_type_ids else {}

    # Fetch volumes for all materials (raw + intermediates, for JS buy/build toggle)
    volumes = sde.get_type_volumes(all_type_ids) if all_type_ids else {}

    raw_total = 0.0
    raw_volume = 0.0
    for mat in raw_materials:
        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        mat["sell_price"] = sell_price
        mat["line_cost"] = sell_price * mat["quantity"]
        raw_total += mat["line_cost"]
        unit_vol = volumes.get(mat["type_id"], 0.0)
        mat["volume"] = unit_vol
        mat["total_volume"] = unit_vol * mat["quantity"]
        raw_volume += mat["total_volume"]

    # Price intermediates for buy-vs-build
    for inter in summary["intermediates"]:
        sell_price = prices.get(inter["type_id"], {}).get("sell_min", 0.0)
        inter["sell_price"] = sell_price
        inter["buy_cost"] = sell_price * inter["quantity"]

    # Tree data for template + JS
    tree_data = _nodes_to_dict(tree)

    return render_template(
        "chain.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        sub_me=sub_me, resolve_reactions=resolve_reactions,
        tree_data=tree_data,
        raw_materials=raw_materials, raw_total=raw_total, raw_volume=raw_volume,
        summary=summary, prices=prices, volumes=volumes,
        character_name=session.get("character_name"),
    )


@app.route("/chain/shopping/<int:bp_id>")
@login_required
def chain_shopping(bp_id):
    """Chain-resolved shopping list vs corp/personal assets."""
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    sub_me = int(request.args.get("sub_me", 10))
    resolve_reactions = request.args.get("reactions", "1") == "1"
    source = request.args.get("source", "corp")

    # Parse buy_set from comma-separated type_ids
    buy_param = request.args.get("buy", "")
    buy_set = None
    if buy_param:
        try:
            buy_set = set(int(x) for x in buy_param.split(",") if x.strip())
        except ValueError:
            buy_set = None

    sde = get_sde()
    region_id = esi.get_market_region()

    bp_name = sde.get_type_name(bp_id)
    product_id = sde.find_product_for_blueprint(bp_id)
    product_name = sde.get_type_name(product_id) if product_id else bp_name

    tree, _, summary = get_cached_chain(
        sde, bp_id, me, runs, structure_bonus, sub_me, resolve_reactions,
    )

    # Flatten with buy_set to get the final shopping list
    shopping_materials = flatten_material_tree(tree, buy_set=buy_set)

    # Fetch assets (cached)
    p = get_authed_preston_from_session()
    if not p:
        flash("Session expired. Please log in again.")
        return redirect(url_for("login"))

    character_id = int(session["character_id"])
    corporation_id = session.get("corporation_id")

    if source == "corp" and corporation_id:
        asset_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
    else:
        asset_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
        source = "personal"

    # Location-aware hauling (when build station is selected)
    build_station = request.args.get("location", type=int)
    deficit_data = None
    loc_names: dict[int, str] = {}

    if build_station:
        loc_index = esi.get_cached_location_asset_index(
            p, corporation_id if source == "corp" and corporation_id else character_id,
            is_corp=(source == "corp" and corporation_id is not None),
        )
        volumes_for_deficit = sde.get_type_volumes(
            [m["type_id"] for m in shopping_materials]
        ) if shopping_materials else {}
        deficit_data = calculate_deficit(
            shopping_materials, loc_index, build_station, volumes_for_deficit,
        )
        # Resolve location IDs in the "Haul (elsewhere)" column to station names
        elsewhere_ids = {lid for d in deficit_data for lid in d["elsewhere"]}
        loc_names = {
            lid: esi.get_cached_location_name(p, lid, "other")
            for lid in elsewhere_ids
        }

    station_list = _get_station_list(p, character_id)

    session["refresh_token"] = p.refresh_token

    # Calculate deficits, costs, and volumes
    total_buy_cost = 0.0
    total_buy_volume = 0.0
    type_ids = [m["type_id"] for m in shopping_materials]
    prices = esi.get_bulk_market_data(type_ids, region_id) if type_ids else {}
    volumes = sde.get_type_volumes(type_ids) if type_ids else {}

    for mat in shopping_materials:
        mat["have"] = asset_index.get(mat["type_id"], 0)
        mat["deficit"] = max(0, mat["quantity"] - mat["have"])
        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        mat["sell_price"] = sell_price
        mat["buy_cost"] = sell_price * mat["deficit"]
        total_buy_cost += mat["buy_cost"]
        unit_vol = volumes.get(mat["type_id"], 0.0)
        mat["volume"] = unit_vol
        mat["total_volume"] = unit_vol * mat["quantity"]
        mat["buy_volume"] = unit_vol * mat["deficit"]
        total_buy_volume += mat["buy_volume"]

    return render_template(
        "chain_shopping.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        sub_me=sub_me, resolve_reactions=resolve_reactions,
        materials=shopping_materials, total_buy_cost=total_buy_cost,
        total_buy_volume=total_buy_volume,
        summary=summary, buy_param=buy_param,
        character_name=session.get("character_name"),
        source=source, has_corp=corporation_id is not None,
        build_station=build_station,
        station_list=station_list,
        deficit_data=deficit_data,
        loc_names=loc_names,
    )


@app.route("/api/chain/<int:bp_id>")
def api_chain(bp_id):
    """JSON endpoint for chain data."""
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    sub_me = int(request.args.get("sub_me", 10))
    resolve_reactions = request.args.get("reactions", "1") == "1"

    sde = get_sde()
    region_id = esi.get_market_region()

    tree, raw_materials, summary = get_cached_chain(
        sde, bp_id, me, runs, structure_bonus, sub_me, resolve_reactions,
    )

    type_ids = list(set(
        [m["type_id"] for m in raw_materials]
        + [i["type_id"] for i in summary["intermediates"]]
    ))
    prices = esi.get_bulk_market_data(type_ids, region_id) if type_ids else {}

    volumes = sde.get_type_volumes(type_ids) if type_ids else {}

    raw_total = 0.0
    raw_volume = 0.0
    for mat in raw_materials:
        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        mat["sell_price"] = sell_price
        mat["line_cost"] = sell_price * mat["quantity"]
        raw_total += mat["line_cost"]
        unit_vol = volumes.get(mat["type_id"], 0.0)
        mat["volume"] = unit_vol
        mat["total_volume"] = unit_vol * mat["quantity"]
        raw_volume += mat["total_volume"]

    return jsonify(
        tree=_nodes_to_dict(tree),
        raw_materials=raw_materials,
        raw_total=raw_total,
        raw_volume=raw_volume,
        summary=summary,
        prices={str(k): v for k, v in prices.items()},
        volumes={str(k): v for k, v in volumes.items()},
    )


# ------------------------------------------------------------------
# Routes — auth
# ------------------------------------------------------------------

@app.route("/login")
def login():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    p = get_base_preston()
    return redirect(p.get_authorize_url(state=state))


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or state != session.pop("oauth_state", None):
        flash("Authentication failed. Please try again.")
        return redirect(url_for("index"))

    try:
        p = get_base_preston()
        authed = p.authenticate(code)
        info = authed.whoami()

        session["refresh_token"] = authed.refresh_token
        session["character_id"] = info.get("character_id")
        session["character_name"] = info.get("character_name", "Unknown")

        # Fetch and cache corporation ID
        corp_id = esi.get_corporation_id(authed, info["character_id"])
        if corp_id:
            session["corporation_id"] = corp_id

        # Prefetch assets in background to warm up the cache
        # This makes the first shopping list load instant
        if corp_id:
            esi.prefetch_asset_index(authed, corp_id, is_corp=True)
        esi.prefetch_asset_index(authed, info["character_id"], is_corp=False)

        flash(f"Logged in as {session['character_name']}")
    except Exception as e:
        flash(f"Authentication error: {e}")

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


@app.route("/api/stations")
@login_required
def api_stations():
    """Return the user's manufacturing stations ranked by usage."""
    p = get_authed_preston_from_session()
    if not p:
        return jsonify(error="Session expired"), 401

    character_id = int(session["character_id"])
    stations = _get_station_list(p, character_id)

    session["refresh_token"] = p.refresh_token
    return jsonify(stations=stations)


# ------------------------------------------------------------------
# Routes — authenticated
# ------------------------------------------------------------------

@app.route("/shopping/<int:bp_id>")
@login_required
def shopping(bp_id):
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    source = request.args.get("source", "corp")  # default to corp assets

    sde = get_sde()
    region_id = esi.get_market_region()

    bp_name = sde.get_type_name(bp_id)
    product_id = sde.find_product_for_blueprint(bp_id)
    product_name = sde.get_type_name(product_id) if product_id else bp_name

    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)

    p = get_authed_preston_from_session()
    if not p:
        flash("Session expired. Please log in again.")
        return redirect(url_for("login"))

    character_id = int(session["character_id"])
    corporation_id = session.get("corporation_id")

    # Fetch assets based on source toggle (corp by default, cached)
    if source == "corp" and corporation_id:
        asset_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
    else:
        asset_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
        source = "personal"  # normalize if corp was unavailable

    # Location-aware hauling (when build station is selected)
    build_station = request.args.get("location", type=int)
    deficit_data = None
    loc_names: dict[int, str] = {}

    if build_station:
        loc_index = esi.get_cached_location_asset_index(
            p, corporation_id if source == "corp" and corporation_id else character_id,
            is_corp=(source == "corp" and corporation_id is not None),
        )
        volumes = sde.get_type_volumes([m["type_id"] for m in materials]) if materials else {}
        deficit_data = calculate_deficit(
            [{"type_id": m["type_id"], "name": m["name"], "quantity": m["adjusted_quantity"]}
             for m in materials],
            loc_index, build_station, volumes,
        )
        # Resolve location IDs in the "Haul (elsewhere)" column to station names
        elsewhere_ids = {lid for d in deficit_data for lid in d["elsewhere"]}
        loc_names = {
            lid: esi.get_cached_location_name(p, lid, "other")
            for lid in elsewhere_ids
        }

    # Always fetch station list for the dropdown (if logged in)
    station_list = _get_station_list(p, character_id)

    # Update session refresh token in case Preston rotated it
    session["refresh_token"] = p.refresh_token

    total_buy_cost = 0.0
    total_buy_volume = 0.0
    if materials:
        type_ids = [m["type_id"] for m in materials]
        prices = esi.get_bulk_market_data(type_ids, region_id)
        for mat in materials:
            mat["have"] = asset_index.get(mat["type_id"], 0)
            mat["deficit"] = max(0, mat["adjusted_quantity"] - mat["have"])
            sell_price = prices[mat["type_id"]]["sell_min"]
            mat["sell_price"] = sell_price
            mat["buy_cost"] = sell_price * mat["deficit"]
            total_buy_cost += mat["buy_cost"]
            mat["buy_volume"] = mat.get("volume", 0.0) * mat["deficit"]
            total_buy_volume += mat["buy_volume"]

    return render_template(
        "shopping.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        materials=materials, total_buy_cost=total_buy_cost,
        total_buy_volume=total_buy_volume,
        character_name=session.get("character_name"),
        source=source,
        has_corp=corporation_id is not None,
        build_station=build_station,
        station_list=station_list,
        deficit_data=deficit_data,
        loc_names=loc_names,
    )


def _compute_profit(sde, bp_id, me, runs, structure_bonus, region_id,
                     broker_fee, sales_tax, material_cost_pct, asset_index):
    """Shared profit calculation logic for route and API."""
    product_id = sde.find_product_for_blueprint(bp_id)
    bp_name = sde.get_type_name(bp_id)
    product_name = sde.get_type_name(product_id) if product_id else bp_name

    # Product quantity per run (e.g. 100 for ammo, 1 for ships)
    prod_row = sde.conn.execute(
        "SELECT quantity FROM industryActivityProducts "
        "WHERE typeID = ? AND activityID = 1",
        (bp_id,),
    ).fetchone()
    qty_per_run = prod_row["quantity"] if prod_row else 1
    total_product_qty = qty_per_run * runs

    materials = calculate_materials(sde, bp_id, me, runs, structure_bonus)

    # Fetch prices for materials + product
    mat_type_ids = [m["type_id"] for m in materials]
    all_type_ids = mat_type_ids + ([product_id] if product_id else [])
    prices = esi.get_bulk_market_data(all_type_ids, region_id) if all_type_ids else {}

    cost_pct = material_cost_pct / 100.0
    total_owned_cost = 0.0
    total_buy_cost = 0.0
    total_buy_volume = 0.0

    for mat in materials:
        needed = mat["adjusted_quantity"]
        have = asset_index.get(mat["type_id"], 0)
        owned_qty = min(have, needed)
        buy_qty = max(0, needed - have)

        sell_price = prices.get(mat["type_id"], {}).get("sell_min", 0.0)
        owned_cost = owned_qty * sell_price * cost_pct
        buy_cost = buy_qty * sell_price

        mat["have"] = have
        mat["owned_qty"] = owned_qty
        mat["buy_qty"] = buy_qty
        mat["sell_price"] = sell_price
        mat["owned_cost"] = owned_cost
        mat["line_buy_cost"] = buy_cost
        mat["line_cost"] = owned_cost + buy_cost
        mat["buy_volume"] = mat.get("volume", 0.0) * buy_qty

        total_owned_cost += owned_cost
        total_buy_cost += buy_cost
        total_buy_volume += mat["buy_volume"]

    material_cost = total_owned_cost + total_buy_cost

    # Product revenue
    product_data = prices.get(product_id, {}) if product_id else {}
    sell_min = product_data.get("sell_min", 0.0)
    buy_max = product_data.get("buy_max", 0.0)

    broker_rate = broker_fee / 100.0
    tax_rate = sales_tax / 100.0

    net_sell_unit = sell_min * (1 - broker_rate - tax_rate)
    net_buy_unit = buy_max * (1 - tax_rate)

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

    return {
        "bp_name": bp_name,
        "product_name": product_name,
        "product_id": product_id,
        "qty_per_run": qty_per_run,
        "total_product_qty": total_product_qty,
        "materials": materials,
        "total_owned_cost": total_owned_cost,
        "total_buy_cost": total_buy_cost,
        "material_cost": material_cost,
        "total_buy_volume": total_buy_volume,
        "sell_min": sell_min,
        "buy_max": buy_max,
        "broker_fee": broker_fee,
        "sales_tax": sales_tax,
        "net_sell_unit": net_sell_unit,
        "net_buy_unit": net_buy_unit,
        "revenue_sell": revenue_sell,
        "revenue_buy": revenue_buy,
        "profit_sell": profit_sell,
        "profit_buy": profit_buy,
        "margin_sell": margin_sell,
        "margin_buy": margin_buy,
        "base_time": base_time,
        "total_time_hrs": total_time_hrs,
        "isk_hr_sell": isk_hr_sell,
        "isk_hr_buy": isk_hr_buy,
    }


@app.route("/profit/<int:bp_id>")
@login_required
def profit(bp_id):
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    broker_fee = float(request.args.get("broker_fee", 1.5))
    sales_tax = float(request.args.get("sales_tax", 3.6))
    material_cost_pct = float(request.args.get("material_cost_pct", 100))
    source = request.args.get("source", "corp")

    sde = get_sde()
    region_id = esi.get_market_region()

    p = get_authed_preston_from_session()
    if not p:
        flash("Session expired. Please log in again.")
        return redirect(url_for("login"))

    character_id = int(session["character_id"])
    corporation_id = session.get("corporation_id")

    if source == "corp" and corporation_id:
        asset_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
    else:
        asset_index = esi.get_cached_asset_index(p, character_id, is_corp=False)
        source = "personal"

    session["refresh_token"] = p.refresh_token

    data = _compute_profit(
        sde, bp_id, me, runs, structure_bonus, region_id,
        broker_fee, sales_tax, material_cost_pct, asset_index,
    )

    return render_template(
        "profit.html",
        bp_id=bp_id, me=me, runs=runs,
        structure_bonus=structure_bonus,
        broker_fee=broker_fee, sales_tax=sales_tax,
        material_cost_pct=material_cost_pct,
        source=source,
        has_corp=corporation_id is not None,
        character_name=session.get("character_name"),
        **data,
    )


@app.route("/api/profit/<int:bp_id>")
@login_required
def api_profit(bp_id):
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))
    broker_fee = float(request.args.get("broker_fee", 1.5))
    sales_tax = float(request.args.get("sales_tax", 3.6))
    material_cost_pct = float(request.args.get("material_cost_pct", 100))
    source = request.args.get("source", "corp")

    sde = get_sde()
    region_id = esi.get_market_region()

    p = get_authed_preston_from_session()
    if not p:
        return jsonify(error="Session expired"), 401

    character_id = int(session["character_id"])
    corporation_id = session.get("corporation_id")

    if source == "corp" and corporation_id:
        asset_index = esi.get_cached_asset_index(p, corporation_id, is_corp=True)
    else:
        asset_index = esi.get_cached_asset_index(p, character_id, is_corp=False)

    session["refresh_token"] = p.refresh_token

    data = _compute_profit(
        sde, bp_id, me, runs, structure_bonus, region_id,
        broker_fee, sales_tax, material_cost_pct, asset_index,
    )

    return jsonify(**data)


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
