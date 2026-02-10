"""
esi.py — ESI API wrapper using Preston for authenticated and public calls.

Handles:
  - SSO authentication (with local callback server)
  - Token persistence and refresh
  - Character assets, blueprints, and industry jobs
  - Location name resolution
  - Market price data (public, no auth required)
"""

import json
import os
import sys
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from preston import Preston

# ESI scopes needed for industry tracking
SCOPES = " ".join([
    "esi-assets.read_assets.v1",
    "esi-characters.read_blueprints.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-corporations.read_blueprints.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-industry.read_character_mining.v1",
    "esi-industry.read_corporation_mining.v1",
])

CONFIG_FILE = "config.json"
TOKEN_FILE = "tokens.json"


# ------------------------------------------------------------------
# Config / tokens
# ------------------------------------------------------------------

def load_config() -> dict:
    """Load or create config.json with ESI credentials."""
    if not os.path.exists(CONFIG_FILE):
        template = {
            "client_id": "YOUR_CLIENT_ID_HERE",
            "client_secret": "YOUR_CLIENT_SECRET_HERE",
            "callback_url": "http://localhost:8888/callback",
            "user_agent": "Sihcom Industry Tracker (contact@example.com)",
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(template, f, indent=2)
        print(f"Created {CONFIG_FILE} — fill in your ESI credentials and re-run.")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_tokens(refresh_token: str):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"refresh_token": refresh_token}, f)


def load_refresh_token() -> str | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f).get("refresh_token")


# ------------------------------------------------------------------
# SSO Authentication
# ------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the SSO callback code on localhost."""
    auth_code = None

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        _CallbackHandler.auth_code = query.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>Authentication successful!</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, *args):
        pass  # suppress HTTP logging


def authenticate(config: dict) -> Preston:
    """Run the full SSO browser flow and return an authed Preston."""
    p = Preston(
        user_agent=config["user_agent"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        callback_url=config["callback_url"],
        scope=SCOPES,
    )

    print("\n=== EVE SSO Authentication ===")
    print("Open this URL in your browser:\n")
    print(p.get_authorize_url())
    print("\nWaiting for callback on localhost:8888 ...")

    server = HTTPServer(("localhost", 8888), _CallbackHandler)
    server.handle_request()
    server.server_close()

    code = _CallbackHandler.auth_code
    if not code:
        print("ERROR: No auth code received.")
        sys.exit(1)

    authed = p.authenticate(code)
    save_tokens(authed.refresh_token)
    print("Authentication complete!")
    return authed


def get_authed_preston(config: dict | None = None) -> Preston:
    """Get an authenticated Preston, refreshing from saved token if possible."""
    config = config or load_config()
    token = load_refresh_token()

    if token:
        try:
            p = Preston(
                user_agent=config["user_agent"],
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                callback_url=config["callback_url"],
                scope=SCOPES,
                refresh_token=token,
            )
            p.whoami()
            print("Authenticated via saved refresh token.")
            return p
        except Exception as e:
            print(f"Saved token invalid ({e}), re-authenticating...")

    return authenticate(config)


# ------------------------------------------------------------------
# Character info
# ------------------------------------------------------------------

def get_character_id(p: Preston) -> int:
    return p.whoami()["character_id"]


def get_character_name(p: Preston) -> str:
    return p.whoami().get("name", "Unknown")


# ------------------------------------------------------------------
# Assets
# ------------------------------------------------------------------

def fetch_assets(p: Preston, character_id: int) -> list[dict]:
    """Fetch all character assets (paginated)."""
    all_assets = []
    page = 1
    while True:
        try:
            result = p.get_op(
                "get_characters_character_id_assets",
                character_id=character_id,
                page=page,
            )
            if not result:
                break
            all_assets.extend(result)
            page += 1
        except Exception:
            break
    return all_assets


def build_asset_index(assets: list[dict]) -> dict[int, int]:
    """Build {type_id: total_quantity} from asset list."""
    index: dict[int, int] = defaultdict(int)
    for a in assets:
        index[a["type_id"]] += a.get("quantity", 1)
    return dict(index)


# ------------------------------------------------------------------
# Blueprints
# ------------------------------------------------------------------

def fetch_blueprints(p: Preston, character_id: int) -> list[dict]:
    """Fetch all character blueprints (paginated)."""
    all_bps = []
    page = 1
    while True:
        try:
            result = p.get_op(
                "get_characters_character_id_blueprints",
                character_id=character_id,
                page=page,
            )
            if not result:
                break
            all_bps.extend(result)
            page += 1
        except Exception:
            break
    return all_bps


# ------------------------------------------------------------------
# Industry Jobs
# ------------------------------------------------------------------

def fetch_industry_jobs(
    p: Preston, character_id: int, include_completed: bool = False
) -> list[dict]:
    """Fetch character industry jobs."""
    kwargs: dict = {"character_id": character_id}
    if include_completed:
        kwargs["include_completed"] = True
    try:
        result = p.get_op(
            "get_characters_character_id_industry_jobs", **kwargs
        )
        return result if result else []
    except Exception as e:
        print(f"Error fetching industry jobs: {e}")
        return []


# ------------------------------------------------------------------
# Location resolution
# ------------------------------------------------------------------

def resolve_location_name(
    p: Preston, location_id: int, location_type: str
) -> str:
    """Resolve a location ID to a human-readable name."""
    try:
        if location_type == "station":
            r = p.get_op(
                "get_universe_stations_station_id", station_id=location_id
            )
            return r.get("name", str(location_id))
        elif location_type == "solar_system":
            r = p.get_op(
                "get_universe_systems_system_id", system_id=location_id
            )
            return r.get("name", str(location_id))
        elif location_type == "other":
            try:
                r = p.get_op(
                    "get_universe_structures_structure_id",
                    structure_id=location_id,
                )
                return r.get("name", str(location_id))
            except Exception:
                return f"Structure {location_id}"
    except Exception:
        pass
    return str(location_id)


# ------------------------------------------------------------------
# Public (unauthenticated) ESI access
# ------------------------------------------------------------------

DEFAULT_REGION = 10000002  # The Forge (Jita)

_public_preston: Preston | None = None


def get_public_preston(config: dict | None = None) -> Preston:
    """Get an unauthenticated Preston for public ESI endpoints (markets, etc.)."""
    global _public_preston
    if _public_preston is not None:
        return _public_preston

    if config is None:
        try:
            config = load_config()
        except SystemExit:
            config = {}

    user_agent = config.get("user_agent", "Sihcom Industry Tracker")
    _public_preston = Preston(user_agent=user_agent)
    return _public_preston


def get_market_region() -> int:
    """Get the market region ID from MARKET_REGION env var, default to The Forge."""
    return int(os.environ.get("MARKET_REGION", str(DEFAULT_REGION)))


# ------------------------------------------------------------------
# Market data
# ------------------------------------------------------------------

_price_cache: dict[tuple[int, int], dict] = {}


def fetch_market_orders(
    type_id: int,
    region_id: int = DEFAULT_REGION,
) -> list[dict]:
    """Fetch all market orders for a type in a region (paginated, public endpoint)."""
    p = get_public_preston()
    all_orders = []
    page = 1
    while True:
        try:
            result = p.get_op(
                "get_markets_region_id_orders",
                region_id=region_id,
                order_type="all",
                type_id=type_id,
                page=page,
            )
            if not result:
                break
            all_orders.extend(result)
            page += 1
        except Exception:
            break
    return all_orders


def get_type_market_data(
    type_id: int,
    region_id: int = DEFAULT_REGION,
) -> dict:
    """
    Get aggregated market data for a type in a region.

    Returns dict with keys:
        sell_min, sell_volume, sell_orders,
        buy_max, buy_volume, buy_orders
    """
    key = (region_id, type_id)
    if key in _price_cache:
        return _price_cache[key]

    orders = fetch_market_orders(type_id, region_id)

    sell_orders = [o for o in orders if not o.get("is_buy_order", False)]
    buy_orders = [o for o in orders if o.get("is_buy_order", False)]

    result = {
        "sell_min": min((o["price"] for o in sell_orders), default=0.0),
        "sell_volume": sum(o.get("volume_remain", 0) for o in sell_orders),
        "sell_orders": len(sell_orders),
        "buy_max": max((o["price"] for o in buy_orders), default=0.0),
        "buy_volume": sum(o.get("volume_remain", 0) for o in buy_orders),
        "buy_orders": len(buy_orders),
    }

    _price_cache[key] = result
    return result


def get_bulk_market_data(
    type_ids: list[int],
    region_id: int = DEFAULT_REGION,
) -> dict[int, dict]:
    """Get market data for multiple types. Uses session cache to avoid re-fetching."""
    results = {}
    for tid in type_ids:
        results[tid] = get_type_market_data(tid, region_id)
    return results
