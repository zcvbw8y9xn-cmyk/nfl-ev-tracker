"""Action Network API & SSR scraper for NL-available books + public/money splits."""
from __future__ import annotations

import json
import re
from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_book_name, normalize_market, normalize_team
from ..odds_math import american_to_decimal
from .cache_utils import RateLimitError, fetch_with_cache, pace_domain

# Action Network bookIds:
# 3=Pinnacle, 12=Sports Interaction, 1071=bet365 Canada, 1074=betway Canada, 2672=Bet99, 15=Consensus, 30=Open
BOOK_ID_MAP = {
    "3": "Pinnacle",
    "12": "Sports Interaction",
    "1071": "bet365",
    "1074": "Betway",
    "2672": "BET99",
}


def scrape_action_network() -> Dict[str, List[Dict]]:
    """Fetch Action Network odds & splits with a 2-minute cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        "action_network_nfl",
        "actionnetwork.com",
        ttl_seconds=120.0,
        fetcher=_scrape_action_network_live,
        is_valid=lambda d: bool(d and (d.get("game_lines") or d.get("player_props"))),
        cooldown_minutes=15.0,
    )
    return res if isinstance(res, dict) else {"game_lines": [], "splits": {}, "player_props": []}


def _scrape_action_network_live() -> Dict[str, List[Dict]]:
    """Fetch NFL game odds across Canadian/NL books + public bet/money splits + featured props."""
    game_lines: List[Dict] = []
    splits: Dict[str, Dict] = {}
    player_props: List[Dict] = []

    url = "https://api.actionnetwork.com/web/v2/scoreboard/nfl?bookIds=3,12,1071,1074,2672,15,30"
    api_resp = Fetcher.get(url, timeout=20)
    if getattr(api_resp, "status", 200) in (403, 429):
        raise RateLimitError("actionnetwork.com", int(api_resp.status))
    data = api_resp.json()

    for g in data.get("games", []):
        teams = g.get("teams", [])
        away_t = next((t for t in teams if t.get("id") == g.get("away_team_id")), {})
        home_t = next((t for t in teams if t.get("id") == g.get("home_team_id")), {})
        away = normalize_team(away_t.get("abbr", ""))
        home = normalize_team(home_t.get("abbr", ""))
        if not away or not home:
            continue
        matchup = f"{away} @ {home}"

        # Public vs money splits from book 15 (consensus) or game.bet_info
        mkts = g.get("markets", {})
        consensus = mkts.get("15", {}).get("event", {})
        spread_c = consensus.get("spread", [])
        total_c = consensus.get("total", [])
        ml_c = consensus.get("moneyline", [])
        away_spread_pct = next((x.get("bet_info", {}).get("tickets", {}).get("percent") for x in spread_c if x.get("side") == "away"), None)
        away_spread_money = next((x.get("bet_info", {}).get("money", {}).get("percent") for x in spread_c if x.get("side") == "away"), None)
        over_pct = next((x.get("bet_info", {}).get("tickets", {}).get("percent") for x in total_c if x.get("side") == "over"), None)
        over_money = next((x.get("bet_info", {}).get("money", {}).get("percent") for x in total_c if x.get("side") == "over"), None)
        away_ml_pct = next((x.get("bet_info", {}).get("tickets", {}).get("percent") for x in ml_c if x.get("side") == "away"), None)
        away_ml_money = next((x.get("bet_info", {}).get("money", {}).get("percent") for x in ml_c if x.get("side") == "away"), None)

        splits[matchup] = {
            "away_spread_tickets_pct": away_spread_pct,
            "away_spread_money_pct": away_spread_money,
            "over_tickets_pct": over_pct,
            "over_money_pct": over_money,
            "away_ml_tickets_pct": away_ml_pct,
            "away_ml_money_pct": away_ml_money,
        }

        for bid, bname in BOOK_ID_MAP.items():
            ev_mkts = mkts.get(bid, {}).get("event", {})
            for mtype, market_label in [("moneyline", "Moneyline"), ("spread", "Spread"), ("total", "Total")]:
                for item in ev_mkts.get(mtype, []):
                    odds = item.get("odds")
                    if not odds:
                        continue
                    side_raw = item.get("side", "")
                    if mtype in ("moneyline", "spread"):
                        side = away if side_raw == "away" else home
                    else:
                        side = "Over" if side_raw == "over" else "Under"
                    line = item.get("value") if mtype in ("spread", "total") else None
                    game_lines.append({
                        "source": "Action Network",
                        "book": normalize_book_name(bname),
                        "matchup": matchup,
                        "away": away,
                        "home": home,
                        "market": market_label,
                        "side": side,
                        "line": line,
                        "american": int(odds),
                        "decimal": round(american_to_decimal(int(odds)), 4),
                    })

    # Also grab public props from SSR page if available
    try:
        pace_domain("actionnetwork.com", min_delay=0.5, jitter=0.35)
        p = Fetcher.get("https://www.actionnetwork.com/nfl/props", timeout=15)
        if getattr(p, "status", 200) in (403, 429):
            raise RateLimitError("actionnetwork.com", int(p.status))
        txt = p.body.decode("utf-8", errors="ignore")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt)
        if m:
            nd = json.loads(m.group(1))
            pr = nd.get("props", {}).get("pageProps", {}).get("projectionsResponse", {})
            players_map = {pl["id"]: pl for pl in pr.get("players", []) if "id" in pl}
            games_map = {gm["id"]: gm for gm in pr.get("games", []) if "id" in gm}
            for pp in pr.get("playerProps", []):
                pl = players_map.get(pp.get("player_id"), {})
                pname = pl.get("full_name") or pp.get("player_abbr", "")
                market_name = normalize_market(pp.get("custom_pick_type_display_name") or pp.get("custom_pick_type", ""))
                for ln in pp.get("lines", []):
                    odds = ln.get("odds")
                    val = ln.get("value")
                    side = (ln.get("side") or "").title()
                    if not pname or odds is None:
                        continue
                    player_props.append({
                        "source": "Action Network",
                        "book": "Consensus/Action",
                        "player": pname,
                        "matchup": "",
                        "market": market_name,
                        "side": side,
                        "line": val,
                        "american": int(odds),
                        "decimal": round(american_to_decimal(int(odds)), 4),
                    })
    except Exception:
        pass

    return {"game_lines": game_lines, "player_props": player_props, "splits": splits}
