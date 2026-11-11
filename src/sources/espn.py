"""Canonical current-week NFL slate and ESPN Fantasy weekly projections."""
from __future__ import annotations

import json
import urllib.request
from typing import Dict, List, Optional
from scrapling.fetchers import Fetcher

from ..normalize import normalize_team, player_match_key, to_nl_time_str
from .cache_utils import RateLimitError, fetch_with_cache, pace_domain

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_FANTASY_PROJS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3?scoringPeriodId={week}&view=kona_player_info"
)

_ESPN_STAT_TO_MARKET = {
    "0": "Pass Attempts",
    "1": "Pass Completions",
    "3": "Passing Yards",
    "4": "Passing TDs",
    "20": "Interceptions",
    "23": "Rush Attempts",
    "24": "Rushing Yards",
    "42": "Receiving Yards",
    "53": "Receptions",
    "83": "FG Made",
}


def fetch_espn_slate() -> List[Dict]:
    """Fetch this week's NFL games with a 5-minute disk cache & cooldown fallback."""
    res = fetch_with_cache(
        "espn_slate",
        "espn.com",
        ttl_seconds=300.0,
        fetcher=_fetch_espn_slate_live,
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=15.0,
    )
    return res if isinstance(res, list) else []


def _fetch_espn_slate_live() -> List[Dict]:
    """Fetch this week's NFL games with canonical matchup keys and NST kickoff times."""
    resp = Fetcher.get(ESPN_SCOREBOARD_URL, timeout=15)
    if getattr(resp, "status", 200) in (403, 429):
        raise RateLimitError("espn.com", int(resp.status))
    data = resp.json()
    season_year = data.get("season", {}).get("year")
    week_num = data.get("week", {}).get("number")
    games: List[Dict] = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away_abbr = normalize_team(away.get("team", {}).get("abbreviation", ""))
        home_abbr = normalize_team(home.get("team", {}).get("abbreviation", ""))
        if not away_abbr or not home_abbr:
            continue
        status = comp.get("status", {}).get("type", {}).get("state", "pre")
        venue = comp.get("venue", {}).get("fullName", "")
        indoor = comp.get("venue", {}).get("indoor", False)
        espn_w = ev.get("weather") or comp.get("weather") or {}
        espn_weather = ""
        if isinstance(espn_w, dict) and (espn_w.get("displayValue") or espn_w.get("temperature") is not None):
            parts = []
            if espn_w.get("temperature") is not None:
                parts.append(f"{espn_w['temperature']}F")
            if espn_w.get("displayValue"):
                parts.append(str(espn_w["displayValue"]))
            espn_weather = ", ".join(parts)
        iso_date = ev.get("date", "")
        games.append({
            "season": season_year,
            "week": week_num,
            "matchup": f"{away_abbr} @ {home_abbr}",
            "away": away_abbr,
            "home": home_abbr,
            "kickoff_iso": iso_date,
            "kickoff_nl": to_nl_time_str(iso_date) if iso_date else "",
            "status": status,
            "venue": venue,
            "indoor": indoor,
            "espn_weather": espn_weather,
        })
    return games


def fetch_espn_fantasy_projections(
    season: Optional[int] = None,
    week: Optional[int] = None,
) -> Dict[tuple, float]:
    """Fetch public ESPN Fantasy weekly projections with a 45-minute disk cache."""
    if season is None or week is None:
        games = fetch_espn_slate()
        if games:
            season = season or games[0].get("season")
            week = week or games[0].get("week")
    if not season or not week:
        return {}
    s_int, w_int = int(season), int(week)
    items = fetch_with_cache(
        f"espn_fantasy_v2_{s_int}_w{w_int}",
        "fantasy.espn.com",
        ttl_seconds=2700.0,
        fetcher=lambda: [
            [pk, m, v] for (pk, m), v in _fetch_espn_fantasy_projections_live(s_int, w_int).items()
        ],
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=20.0,
    )
    if not isinstance(items, list):
        return {}
    return {(row[0], row[1]): float(row[2]) for row in items if len(row) == 3}


def _fetch_espn_fantasy_projections_live(
    season: int,
    week: int,
) -> Dict[tuple, float]:
    """Fetch public ESPN Fantasy weekly player stat & fantasy-point projections."""
    try:
        pace_domain("fantasy.espn.com", min_delay=0.4, jitter=0.3)
        url = ESPN_FANTASY_PROJS_URL.format(season=season, week=week)
        filter_hdr = json.dumps({
            "players": {
                "limit": 500,
                "filterSlotIds": {"value": [0, 2, 4, 6, 17]},
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            }
        })
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
                "x-fantasy-filter": filter_hdr,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status in (403, 429):
                raise RateLimitError("fantasy.espn.com", int(resp.status))
            data = json.loads(resp.read().decode("utf-8"))
    except RateLimitError:
        raise
    except Exception:
        return {}

    proj_map: Dict[tuple, float] = {}
    for entry in data.get("players", []) or []:
        pl = entry.get("player") or {}
        name = (pl.get("fullName") or "").strip()
        if not name:
            continue
        pkey = player_match_key(name)
        if not pkey:
            continue

        # Strictly select the current season/week projected stat block (statSourceId == 1)
        proj_stat = next(
            (
                st for st in (pl.get("stats") or [])
                if st.get("seasonId") == season
                and st.get("statSourceId") == 1
                and st.get("scoringPeriodId") == week
            ),
            None,
        )
        if not proj_stat:
            continue

        raw_stats = proj_stat.get("stats") or {}
        if not raw_stats and not proj_stat.get("appliedTotal"):
            continue

        for stat_key, market in _ESPN_STAT_TO_MARKET.items():
            val = raw_stats.get(stat_key)
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0 or stat_key in ("3", "24", "42", "53"):
                        proj_map[(pkey, market)] = round(fval, 2)
                except (TypeError, ValueError):
                    pass

        pass_yd = float(raw_stats.get("3") or 0.0)
        pass_td = float(raw_stats.get("4") or 0.0)
        pass_int = float(raw_stats.get("20") or 0.0)
        rush_yd = float(raw_stats.get("24") or 0.0)
        rush_td = float(raw_stats.get("25") or 0.0)
        rec_yd = float(raw_stats.get("42") or 0.0)
        rec_td = float(raw_stats.get("43") or 0.0)
        rec_n = float(raw_stats.get("53") or 0.0)
        fum_lost = float(raw_stats.get("72") or 0.0)
        fgm = float(raw_stats.get("83") or 0.0)
        xpm = float(raw_stats.get("86") or 0.0)
        # Two-point conversions (ESPN stat ids 205=pass, 206=rush, 207=rec). Sleeper and
        # the engine both include these at 2 pts each; ESPN previously dropped them,
        # which skewed ALC/Half/Standard projections slightly whenever a 2-pt was projected.
        two_pt = (
            float(raw_stats.get("205") or 0.0)
            + float(raw_stats.get("206") or 0.0)
            + float(raw_stats.get("207") or 0.0)
        )
        applied_total = float(proj_stat.get("appliedTotal") or 0.0)

        if rush_td > 0 or rec_td > 0:
            proj_map[(pkey, "Anytime TD")] = round(rush_td + rec_td, 2)
        if pass_yd > 0 and rush_yd > 0:
            proj_map[(pkey, "Pass + Rush Yards")] = round(pass_yd + rush_yd, 2)
        if rush_yd > 0 or rec_yd > 0:
            proj_map[(pkey, "Rush + Rec Yards")] = round(rush_yd + rec_yd, 2)
        if fgm > 0 or xpm > 0:
            kick_pts = applied_total if applied_total > 0 else (3.0 * fgm + xpm)
            proj_map[(pkey, "Kicking Points")] = round(kick_pts, 2)

        # Fantasy point scoring variants matching Sleeper / DFS Pick'em / ALC rules
        if applied_total > 0:
            # ESPN league defaults PPR = appliedTotal (use explicit adjustments when stat breakdown exists)
            ppr_pts = applied_total
            # Sleeper/PrizePicks use -1 INT while ESPN uses -2 INT; compute Half PPR & ALC directly from stats
            half_pts = (
                0.04 * pass_yd + 4.0 * pass_td - 1.5 * pass_int
                + 0.1 * rush_yd + 6.0 * rush_td
                + 0.1 * rec_yd + 6.0 * rec_td + 0.5 * rec_n
                - 2.0 * fum_lost + 2.0 * two_pt + (ppr_pts if (fgm > 0 or xpm > 0) and pass_yd == 0 and rush_yd == 0 and rec_yd == 0 else 0.0)
            )
            if ppr_pts > 0 and (pass_yd > 0 or rush_yd > 0 or rec_yd > 0):
                half_pts = max(0.0, ppr_pts - 0.5 * rec_n)
            alc_pts = (
                0.05 * pass_yd + 4.0 * pass_td - 2.0 * pass_int
                + 0.1 * rush_yd + 6.0 * rush_td
                + 0.1 * rec_yd + 6.0 * rec_td
                - 2.0 * fum_lost + 2.0 * two_pt + (ppr_pts if (fgm > 0 or xpm > 0) and pass_yd == 0 and rush_yd == 0 and rec_yd == 0 else 0.0)
            )
            std_pts = max(0.0, ppr_pts - 1.0 * rec_n) if (pass_yd > 0 or rush_yd > 0 or rec_yd > 0) else ppr_pts
            proj_map[(pkey, "Fantasy Points")] = round(ppr_pts, 2)
            proj_map[(pkey, "Fantasy Points (Half PPR)")] = round(half_pts, 2)
            proj_map[(pkey, "Fantasy Points (Standard)")] = round(std_pts, 2)
            if alc_pts > 0:
                proj_map[(pkey, "Fantasy Points (ALC)")] = round(alc_pts, 2)

    return proj_map
