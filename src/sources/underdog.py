"""Underdog Fantasy NFL pick'em lines scraper via public v1 over_under_lines API."""
from __future__ import annotations

from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from ..odds_math import american_to_decimal, parse_american
from .cache_utils import RateLimitError, fetch_with_cache

UNDERDOG_URL = "https://api.underdogfantasy.com/v1/over_under_lines"

# Skip 1Q/1H/quarter/half sub-markets so we only match full-game props
_PERIOD_PREFIXES = ("period_1", "period_2", "period_3", "period_4", "1q ", "1h ", "2h ")


def _parse_matchup(abbreviated_title: str) -> str:
    parts = [p.strip() for p in (abbreviated_title or "").split("@")]
    if len(parts) == 2:
        away = normalize_team(parts[0])
        home = normalize_team(parts[1])
        if away and home:
            return f"{away} @ {home}"
    return ""


def scrape_underdog() -> List[Dict]:
    """Fetch Underdog NFL lines with a 3-minute disk cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        "underdog_lines",
        "underdogfantasy.com",
        ttl_seconds=180.0,
        fetcher=_scrape_underdog_live,
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=20.0,
    )
    return res if isinstance(res, list) else []


def _scrape_underdog_live() -> List[Dict]:
    """Fetch NFL Over/Under (Higher/Lower) lines & exact leg prices from Underdog."""
    resp = Fetcher.get(UNDERDOG_URL, timeout=20)
    if getattr(resp, "status", 200) in (403, 429):
        raise RateLimitError("underdogfantasy.com", int(resp.status))
    if resp.status != 200:
        return []
    payload = resp.json()

    nfl_games: Dict[int, str] = {}
    for g in payload.get("games", []):
        if g.get("sport_id") == "NFL" and g.get("id") is not None:
            nfl_games[int(g["id"])] = _parse_matchup(g.get("abbreviated_title", ""))

    appearances = {a["id"]: a for a in payload.get("appearances", []) if a.get("id")}
    players = {p["id"]: p for p in payload.get("players", []) if p.get("id")}

    rows: List[Dict] = []
    for ln in payload.get("over_under_lines", []):
        if ln.get("live_event"):
            continue
        ou = ln.get("over_under") or {}
        app_stat = ou.get("appearance_stat") or {}
        stat_raw = (app_stat.get("stat") or "").strip().lower()
        display_raw = (app_stat.get("display_stat") or "").strip()

        # Keep full-game markets (exclude 1Q/1H period lines except 1st TD scorer)
        if stat_raw != "period_first_touchdown_scored" and (
            stat_raw.startswith(_PERIOD_PREFIXES) or display_raw.lower().startswith(_PERIOD_PREFIXES)
        ):
            continue

        app = appearances.get(app_stat.get("appearance_id"), {})
        match_id = app.get("match_id")
        if match_id is None or int(match_id) not in nfl_games:
            continue
        matchup = nfl_games[int(match_id)]

        pl = players.get(app.get("player_id"), {})
        player_name = f"{pl.get('first_name', '')} {pl.get('last_name', '')}".strip()
        if not player_name:
            continue

        try:
            line_val = float(ln["stat_value"])
        except (KeyError, TypeError, ValueError):
            continue

        non_disc = ln.get("non_discounted_stat_value")
        orig_line = float(non_disc) if non_disc is not None else None
        is_promo = orig_line is not None and abs(orig_line - line_val) > 1e-6

        market = normalize_market(display_raw or stat_raw)

        for opt in ln.get("options", []):
            choice = (opt.get("choice") or "").lower()
            if choice == "higher":
                side = "Yes" if market in ("Anytime TD", "1st TD", "Last TD") else "Over"
            elif choice == "lower":
                if market in ("Anytime TD", "1st TD", "Last TD"):
                    side = "No"
                else:
                    side = "Under"
            else:
                continue

            am = parse_american(opt.get("american_price"))
            dec = None
            if opt.get("decimal_price") is not None:
                try:
                    dec = round(float(opt["decimal_price"]), 4)
                except (TypeError, ValueError):
                    dec = None
            if dec is None and am is not None:
                dec = round(american_to_decimal(am), 4)
            if am is None or dec is None:
                continue

            mult = None
            if opt.get("payout_multiplier") is not None:
                try:
                    mult = float(opt["payout_multiplier"])
                except (TypeError, ValueError):
                    mult = None

            rows.append({
                "source": "Underdog",
                "book": "Underdog",
                "player": player_name,
                "matchup": matchup,
                "market": market,
                "side": side,
                "line": line_val,
                "american": am,
                "decimal": dec,
                "payout_multiplier": mult,
                "odds_type": ln.get("line_type") or "balanced",
                "is_promo": is_promo,
                "promo_label": "Boost/Discount" if is_promo else "",
                "orig_line": orig_line,
            })

    return rows
