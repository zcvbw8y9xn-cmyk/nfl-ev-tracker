"""Sleeper NFL projections + Sleeper Picks lines (and RotoWire weather/projections)."""
from __future__ import annotations

from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team, player_match_key
from ..odds_math import american_to_decimal
from .cache_utils import RateLimitError, fetch_with_cache, pace_domain

SLEEPER_STATE_URL = "https://api.sleeper.app/v1/state/nfl"
ROTOWIRE_PICKS_URL = "https://www.rotowire.com/picks/api/lines.php?sport=NFL"

# Map Sleeper projection stat keys -> canonical markets
_SLEEPER_STAT_TO_MARKET = {
    "pass_yd": "Passing Yards",
    "pass_td": "Passing TDs",
    "pass_cmp": "Pass Completions",
    "pass_att": "Pass Attempts",
    "pass_int": "Interceptions",
    "rush_yd": "Rushing Yards",
    "rush_att": "Rush Attempts",
    "rec_yd": "Receiving Yards",
    "rec": "Receptions",
}


def _fetch_sleeper_projections() -> tuple[Dict[tuple, float], Dict[str, Dict]]:
    """Fetch Sleeper weekly projections with a 30-minute disk cache & cooldown fallback."""
    cached = fetch_with_cache(
        "sleeper_weekly_projs_v2",
        "sleeper.com",
        ttl_seconds=1800.0,
        fetcher=_fetch_sleeper_projections_live,
        is_valid=lambda d: bool(isinstance(d, dict) and d.get("projs")),
        cooldown_minutes=20.0,
    )
    if not isinstance(cached, dict):
        return {}, {}
    proj_map = {(row[0], row[1]): float(row[2]) for row in cached.get("projs", []) if len(row) == 3}
    stats_map = cached.get("stats") or {}
    return proj_map, stats_map


def _fetch_sleeper_projections_live() -> Dict:
    """Fetch official Sleeper weekly player stat projections & fantasy point projections."""
    try:
        state_resp = Fetcher.get(SLEEPER_STATE_URL, timeout=10)
        if getattr(state_resp, "status", 200) in (403, 429):
            raise RateLimitError("sleeper.com", int(state_resp.status))
        if state_resp.status != 200:
            return {"projs": [], "stats": {}}
        st = state_resp.json()
        season = st.get("season", "2026")
        week = st.get("display_week") or st.get("week") or 1
        season_type = st.get("season_type", "regular")

        pace_domain("sleeper.com", min_delay=0.35, jitter=0.25)
        url = f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type={season_type}"
        resp = Fetcher.get(url, timeout=20)
        if getattr(resp, "status", 200) in (403, 429):
            raise RateLimitError("sleeper.com", int(resp.status))
        if resp.status != 200:
            return {"projs": [], "stats": {}}
        data = resp.json()
    except RateLimitError:
        raise
    except Exception:
        return {"projs": [], "stats": {}}

    proj_map: Dict[tuple, float] = {}
    stats_map: Dict[str, Dict] = {}
    for item in data:
        pl = item.get("player") or {}
        name = f"{pl.get('first_name', '')} {pl.get('last_name', '')}".strip()
        if not name:
            continue
        pkey = player_match_key(name)
        stats = item.get("stats") or {}

        for stat_key, market in _SLEEPER_STAT_TO_MARKET.items():
            val = stats.get(stat_key)
            if val is not None:
                try:
                    proj_map[(pkey, market)] = round(float(val), 2)
                except (TypeError, ValueError):
                    pass

        # Anytime TD projection = rush_td + rec_td
        rtd = stats.get("rush_td")
        ctd = stats.get("rec_td")
        if rtd is not None or ctd is not None:
            proj_map[(pkey, "Anytime TD")] = round(float(rtd or 0) + float(ctd or 0), 2)

        # Combo yards
        pyd = stats.get("pass_yd")
        ryd = stats.get("rush_yd")
        cyd = stats.get("rec_yd")
        if pyd is not None and ryd is not None:
            proj_map[(pkey, "Pass + Rush Yards")] = round(float(pyd) + float(ryd), 2)
        if ryd is not None and cyd is not None:
            proj_map[(pkey, "Rush + Rec Yards")] = round(float(ryd) + float(cyd), 2)

        # Fantasy point projections across scoring rules
        pts_ppr = stats.get("pts_ppr")
        pts_half = stats.get("pts_half_ppr")
        pts_std = stats.get("pts_std")
        if pts_ppr is not None:
            try:
                proj_map[(pkey, "Fantasy Points")] = round(float(pts_ppr), 2)
            except (TypeError, ValueError):
                pass
        if pts_half is not None:
            try:
                proj_map[(pkey, "Fantasy Points (Half PPR)")] = round(float(pts_half), 2)
            except (TypeError, ValueError):
                pass
        if pts_std is not None:
            try:
                proj_map[(pkey, "Fantasy Points (Standard)")] = round(float(pts_std), 2)
            except (TypeError, ValueError):
                pass

        # Compute exact ALC Fantasy (0 PPR, 0.05/pass yd, 4 pass TD, -2 INT, 6 rush/rec TD, 0.1 rush/rec yd, -2 fum_lost)
        pass_yd = float(stats.get("pass_yd") or 0)
        pass_td = float(stats.get("pass_td") or 0)
        pass_int = float(stats.get("pass_int") or 0)
        rush_yd = float(stats.get("rush_yd") or 0)
        rush_td = float(stats.get("rush_td") or 0)
        rec_yd = float(stats.get("rec_yd") or 0)
        rec_td = float(stats.get("rec_td") or 0)
        rec_n = float(stats.get("rec") or 0)
        fum_lost = float(stats.get("fum_lost") or 0)
        two_pt = float(stats.get("pass_2pt") or 0) + float(stats.get("rush_2pt") or 0) + float(stats.get("rec_2pt") or 0)
        alc_pts = (
            0.05 * pass_yd + 4.0 * pass_td - 2.0 * pass_int
            + 0.1 * rush_yd + 6.0 * rush_td
            + 0.1 * rec_yd + 6.0 * rec_td
            - 2.0 * fum_lost + 2.0 * two_pt
        )
        if alc_pts > 0 or pts_std is not None:
            proj_map[(pkey, "Fantasy Points (ALC)")] = round(alc_pts, 2)

        stats_map[pkey] = {
            "position": (pl.get("position") or "").upper(),
            "team": normalize_team(pl.get("team") or ""),
            "pass_yd": pass_yd,
            "pass_td": pass_td,
            "pass_int": pass_int,
            "rush_yd": rush_yd,
            "rush_td": rush_td,
            "rec_yd": rec_yd,
            "rec": rec_n,
            "rec_td": rec_td,
            "fum_lost": fum_lost,
            "two_pt": two_pt,
            "pts_ppr": round(float(pts_ppr), 2) if pts_ppr is not None else None,
            "pts_half_ppr": round(float(pts_half), 2) if pts_half is not None else None,
            "pts_std": round(float(pts_std), 2) if pts_std is not None else None,
            "pts_alc": round(alc_pts, 2),
        }

    return {
        "projs": [[pk, m, v] for (pk, m), v in proj_map.items()],
        "stats": stats_map,
    }


def _fetch_rotowire_picks_and_context() -> Dict:
    """Fetch Sleeper Picks lines + RotoWire context with a 5-minute disk cache."""
    cached = fetch_with_cache(
        "rotowire_picks_ctx",
        "rotowire.com",
        ttl_seconds=300.0,
        fetcher=_fetch_rotowire_picks_and_context_live,
        is_valid=lambda d: bool(isinstance(d, dict) and (d.get("sleeper_lines") or d.get("rw_projs"))),
        cooldown_minutes=15.0,
    )
    if not isinstance(cached, dict):
        return {"sleeper_lines": [], "rw_projections": {}, "weather": {}, "fallback_dfs": []}
    return {
        "sleeper_lines": cached.get("sleeper_lines", []),
        "rw_projections": {(row[0], row[1]): float(row[2]) for row in cached.get("rw_projs", []) if len(row) == 3},
        "weather": cached.get("weather", {}),
        "fallback_dfs": cached.get("fallback_dfs", []),
    }


def _fetch_rotowire_picks_and_context_live() -> Dict:
    """Fetch Sleeper Picks lines, RotoWire projections, and game weather from RotoWire Picks API."""
    try:
        resp = Fetcher.get(ROTOWIRE_PICKS_URL, timeout=20)
        if getattr(resp, "status", 200) in (403, 429):
            raise RateLimitError("rotowire.com", int(resp.status))
        if resp.status != 200:
            return {"sleeper_lines": [], "rw_projs": [], "weather": {}, "fallback_dfs": []}
        data = resp.json()
    except RateLimitError:
        raise
    except Exception:
        return {"sleeper_lines": [], "rw_projs": [], "weather": {}, "fallback_dfs": []}

    # Keep only NFL full-game markets (exclude Season / Quarter / non-NFL)
    nfl_markets: Dict[int, str] = {}
    for m in data.get("markets", []):
        if m.get("sport") == "NFL" and m.get("category") == "Game" and m.get("marketID") is not None:
            nfl_markets[int(m["marketID"])] = normalize_market(m.get("marketName", ""))

    events: Dict[int, str] = {}
    weather_map: Dict[str, str] = {}
    for ev in data.get("events", []):
        eid = ev.get("eventID")
        away = normalize_team(ev.get("awayTeam", ""))
        home = normalize_team(ev.get("homeTeam", ""))
        if eid is not None and away and home:
            matchup = f"{away} @ {home}"
            events[int(eid)] = matchup
            w = ev.get("weather") or {}
            if w.get("isDome"):
                weather_map[matchup] = "Dome"
            elif w.get("atStart"):
                st = w["atStart"]
                desc = st.get("desc", "")
                temp = st.get("temp")
                wind = st.get("wind")
                precip = st.get("precip")
                parts = []
                if desc:
                    parts.append(str(desc).title())
                if temp is not None:
                    parts.append(f"{temp}F")
                if wind is not None:
                    parts.append(f"Wind {wind}mph")
                if precip:
                    parts.append(f"{precip}% precip")
                if parts:
                    weather_map[matchup] = ", ".join(parts)

    entities: Dict[int, Dict] = {}
    for ent in data.get("entities", []):
        if ent.get("sport") == "NFL" and ent.get("entityID") is not None:
            entities[int(ent["entityID"])] = ent

    sleeper_lines: List[Dict] = []
    fallback_dfs: List[Dict] = []
    rw_projections: Dict[tuple, float] = {}

    for pr in data.get("props", []):
        mid = pr.get("marketID")
        if mid is None or int(mid) not in nfl_markets:
            continue
        market = nfl_markets[int(mid)]
        eids = pr.get("entities") or []
        if len(eids) != 1 or int(eids[0]) not in entities:
            continue
        ent = entities[int(eids[0])]
        player_name = (ent.get("name") or "").strip()
        if not player_name:
            continue

        matchup = events.get(int(ent["eventID"]), "") if ent.get("eventID") is not None else ""
        proj = pr.get("projection")
        if proj is not None:
            try:
                rw_projections[(player_match_key(player_name), market)] = round(float(proj), 2)
            except (TypeError, ValueError):
                pass

        for ln in pr.get("lines", []):
            bk_raw = (ln.get("book") or "").lower()
            if bk_raw not in ("sleeper", "prizepicks", "underdog"):
                continue
            book = {"sleeper": "Sleeper", "prizepicks": "PrizePicks", "underdog": "Underdog"}[bk_raw]
            line_val = ln.get("line")
            if line_val is None:
                continue
            line_val = float(line_val)

            for side_key, side_label in [("over", "Over"), ("under", "Under")]:
                am = ln.get(side_key)
                if am is None:
                    continue
                try:
                    am_int = int(am)
                    dec = round(american_to_decimal(am_int), 4)
                except (TypeError, ValueError):
                    continue

                resolved_side = (
                    "Yes" if (market == "Anytime TD" and side_label == "Over" and line_val == 0.5)
                    else "No" if (market == "Anytime TD" and side_label == "Under" and line_val == 0.5)
                    else side_label
                )
                row = {
                    "source": "Sleeper" if book == "Sleeper" else f"RotoWire ({book})",
                    "book": book,
                    "player": player_name,
                    "matchup": matchup,
                    "market": market,
                    "side": resolved_side,
                    "line": line_val,
                    "american": am_int,
                    "decimal": dec,
                    "payout_multiplier": None,
                    "odds_type": "standard",
                    "is_promo": False,
                    "promo_label": "",
                    "orig_line": None,
                }
                if book == "Sleeper":
                    sleeper_lines.append(row)
                else:
                    fallback_dfs.append(row)

    return {
        "sleeper_lines": sleeper_lines,
        "rw_projs": [[pk, m, v] for (pk, m), v in rw_projections.items()],
        "weather": weather_map,
        "fallback_dfs": fallback_dfs,
    }


def scrape_sleeper() -> Dict:
    """Fetch Sleeper NFL projections + Sleeper Picks lines + RotoWire context."""
    sleeper_projs, sleeper_stats = _fetch_sleeper_projections()
    rw = _fetch_rotowire_picks_and_context()
    return {
        "sleeper_lines": rw["sleeper_lines"],
        "sleeper_projections": sleeper_projs,
        "sleeper_stats": sleeper_stats,
        "rw_projections": rw["rw_projections"],
        "weather": rw["weather"],
        "fallback_dfs": rw["fallback_dfs"],
    }
