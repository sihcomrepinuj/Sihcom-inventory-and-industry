"""
app.py — Flask web interface for Sihcom Industry Tracker.

Reuses sde.py and esi.py for all data operations.
Deployable to Railway with gunicorn.
"""

import logging
import os
import secrets
import traceback
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash,
)
from preston import Preston

from sde import (
    SDE, calculate_materials, ACTIVITY_MANUFACTURING, ACTIVITY_INVENTION,
)
import esi

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Log errors to stdout so Railway can capture them
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    """Download the SDE if it doesn't exist or is corrupt."""
    from sde import DEFAULT_SDE_PATH
    logger.info(f"Checking for SDE at: {DEFAULT_SDE_PATH}")

    if os.path.exists(DEFAULT_SDE_PATH):
        if _sde_is_valid(DEFAULT_SDE_PATH):
            logger.info("SDE found and valid.")
            return
        else:
            logger.warning("SDE file is corrupt — deleting and re-downloading...")
            os.remove(DEFAULT_SDE_PATH)

    logger.info("Downloading SDE (~2 MB from Fuzzwork CSVs)...")
    from setup_sde import build_database
    build_database()
    logger.info("SDE ready.")


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


# ------------------------------------------------------------------
# Routes — public
# ------------------------------------------------------------------

@app.route("/")
def index():
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

    # Attach market prices
    grand_total = 0.0
    if materials:
        type_ids = [m["type_id"] for m in materials]
        prices = esi.get_bulk_market_data(type_ids, region_id)
        for mat in materials:
            sell_price = prices[mat["type_id"]]["sell_min"]
            mat["sell_price"] = sell_price
            mat["line_cost"] = sell_price * mat["adjusted_quantity"]
            grand_total += mat["line_cost"]

    base_time = sde.get_activity_time(bp_id, ACTIVITY_MANUFACTURING)
    invention_products = sde.get_invention_products(bp_id)
    invention_materials = (
        sde.get_activity_materials(bp_id, ACTIVITY_INVENTION)
        if invention_products else []
    )

    return render_template(
        "blueprint.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        materials=materials, grand_total=grand_total,
        base_time=base_time,
        invention_products=invention_products,
        invention_materials=invention_materials,
        character_name=session.get("character_name"),
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
    if materials:
        type_ids = [m["type_id"] for m in materials]
        prices = esi.get_bulk_market_data(type_ids, region_id)
        for mat in materials:
            sell_price = prices[mat["type_id"]]["sell_min"]
            mat["sell_price"] = sell_price
            mat["line_cost"] = sell_price * mat["adjusted_quantity"]
            grand_total += mat["line_cost"]

    return jsonify(materials=materials, grand_total=grand_total)


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

        flash(f"Logged in as {session['character_name']}")
    except Exception as e:
        flash(f"Authentication error: {e}")

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Routes — authenticated
# ------------------------------------------------------------------

@app.route("/shopping/<int:bp_id>")
@login_required
def shopping(bp_id):
    me = int(request.args.get("me", 10))
    runs = int(request.args.get("runs", 1))
    structure_bonus = float(request.args.get("structure_bonus", 0))

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
    assets = esi.fetch_assets(p, character_id)
    asset_index = esi.build_asset_index(assets)

    # Update session refresh token in case Preston rotated it
    session["refresh_token"] = p.refresh_token

    total_buy_cost = 0.0
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

    return render_template(
        "shopping.html",
        bp_id=bp_id, bp_name=bp_name, product_name=product_name,
        me=me, runs=runs, structure_bonus=structure_bonus,
        materials=materials, total_buy_cost=total_buy_cost,
        character_name=session.get("character_name"),
    )


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
