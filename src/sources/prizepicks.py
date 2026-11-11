"""PrizePicks NFL pick'em lines scraper via public partner-api projections feed."""
from __future__ import annotations

from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from ..odds_math import decimal_to_american
from .cache_utils import RateLimitError, fetch_with_cache

PRIZEPICKS_URL = "https://partner-api.prizepicks.com/projections?league_id=9&per_page=250"

# Standard 5/6-Pick Flex optimal break-even probability (~54.25% => ~-118.6 / 1.8433 decimal)
PP_STANDARD_PROB = 0.5425
PP_STANDARD_DEC = round(1.0 / PP_STANDARD_PROB, 4)
PP_STANDARD_AM = decimal_to_american(PP_STANDARD_DEC)


def scrape_prizepicks(include_alt_types: bool = False) -> List[Dict]:
    """Fetch PrizePicks NFL lines with a 10-minute cache & 30-minute 403/429 cooldown fallback."""
    res = fetch_with_cache(
        f"prizepicks_alt{int(include_alt_types)}",
        "prizepicks.com",
        ttl_seconds=600.0,
        fetcher=lambda: _scrape_prizepicks_live(include_alt_types=include_alt_types),
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=30.0,
    )
    return res if isinstance(res, list) else []


def _scrape_prizepicks_live(include_alt_types: bool = False) -> List[Dict]:
    """Fetch PrizePicks NFL lines (standard + Taco/flash-sale promos) from partner API."""
    resp = Fetcher.get(PRIZEPICKS_URL, timeout=20)
    if getattr(resp, "status", 200) in (403, 429):
        raise RateLimitError("prizepicks.com", int(resp.status))
    if resp.status != 200:
        return []
    payload = resp.json()

    inc_map: Dict[tuple, Dict] = {}
    for item in payload.get("included", []):
        itype = item.get("type")
        iid = item.get("id")
        if itype and iid:
            inc_map[(itype, iid)] = item.get("attributes") or {}

    rows: List[Dict] = []
    for item in payload.get("data", []):
        attrs = item.get("attributes") or {}
        if attrs.get("is_live") or attrs.get("in_game"):
            continue
        if attrs.get("event_type") == "combo":
            continue

        odds_type = (attrs.get("odds_type") or "standard").lower()
        if not include_alt_types and odds_type != "standard":
            continue

        rels = item.get("relationships") or {}
        dur_id = (rels.get("duration") or {}).get("data", {}).get("id")
        dur_name = (inc_map.get(("duration", dur_id), {}).get("name") or "Full").lower()
        if dur_name not in ("full", "game", ""):
            continue

        player_id = (rels.get("new_player") or {}).get("data", {}).get("id")
        player_attrs = inc_map.get(("new_player", player_id), {})
        if player_attrs.get("combo"):
            continue
        player_name = (player_attrs.get("display_name") or player_attrs.get("name") or "").strip()
        if not player_name:
            continue

        stat_id = (rels.get("stat_type") or {}).get("data", {}).get("id")
        stat_name = inc_map.get(("stat_type", stat_id), {}).get("name") or ""
        st_lower = stat_name.lower()
        if any(w in st_lower for w in (
            "per carry", "first 5", "first 10", "consecutive", "in first",
            "quarter", "half", "1q", "1h", "2h", "combo", "on first",
        )):
            continue
        market = normalize_market(stat_name)

        game_id = (rels.get("game") or {}).get("data", {}).get("id")
        game_attrs = inc_map.get(("game", game_id), {})
        teams_info = ((game_attrs.get("metadata") or {}).get("game_info") or {}).get("teams") or {}
        away = normalize_team((teams_info.get("away") or {}).get("abbreviation", ""))
        home = normalize_team((teams_info.get("home") or {}).get("abbreviation", ""))
        matchup = f"{away} @ {home}" if away and home else ""

        base_line = attrs.get("line_score")
        if base_line is None:
            continue
        base_line = float(base_line)

        flash = attrs.get("flash_sale_line_score")
        is_promo = bool(attrs.get("is_promo")) and flash is not None
        active_line = float(flash) if is_promo else base_line
        promo_label = (attrs.get("discount_name") or "promo").upper() if is_promo else ""

        wager_types = (attrs.get("allowed_wager_types") or "under_or_over").lower()
        sides: List[str] = []
        if market in ("Anytime TD", "1st TD", "Last TD"):
            sides = ["Yes"]
        else:
            if "over" in wager_types:
                sides.append("Over")
            if "under" in wager_types and not is_promo:
                sides.append("Under")

        for side in sides:
            rows.append({
                "source": "PrizePicks",
                "book": "PrizePicks",
                "player": player_name,
                "matchup": matchup,
                "market": market,
                "side": side,
                "line": active_line,
                "american": PP_STANDARD_AM,
                "decimal": PP_STANDARD_DEC,
                "payout_multiplier": 1.0,
                "odds_type": odds_type,
                "is_promo": is_promo,
                "promo_label": f"{promo_label} (-{attrs.get('discount_percentage', '')}%)" if is_promo else "",
                "orig_line": base_line if is_promo else None,
            })

    return rows
