"""Optional API connectors: The Odds API v4, PropLine v1, and Balldontlie.

These activate automatically whenever keys are set in `.env` or via the Mac app UI.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..normalize import CONSENSUS_BOOKS, DFS_BOOKS, NL_BOOKS, normalize_book_name, normalize_market, normalize_team
from ..odds_math import american_to_decimal

CACHE_DIR = Path.home() / ".cache" / "nfl-ev-tracker"


def _load_api_cache(cache_name: str, ttl_hours: float, min_version: int = 1) -> Optional[Dict]:
    """Return cached API payload if it exists, matches schema version, and is younger than `ttl_hours`."""
    path = CACHE_DIR / f"api_cache_{cache_name}.json"
    if not path.exists() or ttl_hours <= 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("schema_version", 1)) < min_version:
            return None
        fetched_ts = float(data.get("fetched_ts", 0))
        if fetched_ts <= 0:
            return None
        age_hours = (time.time() - fetched_ts) / 3600.0
        if age_hours < ttl_hours:
            result = dict(data.get("payload") or {})
            result["from_cache"] = True
            result["cache_age_hours"] = round(age_hours, 2)
            result["cached_at"] = data.get("cached_at")
            return result
    except Exception:
        return None
    return None


def _save_api_cache(cache_name: str, payload: Dict, schema_version: int = 1) -> str:
    """Persist API payload with timestamp and schema version to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"api_cache_{cache_name}.json"
    cached_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    wrapper = {
        "schema_version": schema_version,
        "fetched_ts": time.time(),
        "cached_at": cached_at,
        "payload": payload,
    }
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    return cached_at


def _build_event_params(api_key: str, regions: str, markets: str) -> str:
    params = {
        "apiKey": api_key,
        "markets": markets,
        "oddsFormat": "american",
    }
    if regions:
        params["regions"] = regions
    return urllib.parse.urlencode(params)


def _fetch_single_event_props(
    ev: Dict,
    source_name: str,
    api_key: str,
    base_event_url: str,
    regions: str,
    prop_markets: str,
) -> Dict:
    """Fetch and parse NL player props, sharp/consensus benchmark props, and DFS props for one event."""
    eid = ev.get("id")
    away = normalize_team(ev.get("away_team", ""))
    home = normalize_team(ev.get("home_team", ""))
    if not eid or not away or not home:
        return {"player_props": [], "benchmark_props": [], "dfs_props": [], "remaining": None}

    matchup = f"{away} @ {home}"
    pparams = _build_event_params(api_key, regions, prop_markets)
    purl = base_event_url.format(event_id=eid) + f"?{pparams}"
    remaining = None
    try:
        with urllib.request.urlopen(purl, timeout=15) as pr:
            remaining = (
                pr.headers.get("x-requests-remaining")
                or pr.headers.get("x-ratelimit-remaining")
            )
            pdata = json.loads(pr.read().decode("utf-8"))
    except Exception:
        return {"player_props": [], "benchmark_props": [], "dfs_props": [], "remaining": None}

    player_props: List[Dict] = []
    benchmark_props: List[Dict] = []
    dfs_props: List[Dict] = []
    for bm in pdata.get("bookmakers", []):
        book = normalize_book_name(bm.get("title", bm.get("key", "")))
        is_nl = book in NL_BOOKS
        is_dfs = book in DFS_BOOKS
        is_cons = book in CONSENSUS_BOOKS
        if not is_nl and not is_dfs and not is_cons:
            continue
        for mk in bm.get("markets", []):
            mname = normalize_market(mk.get("key", ""))
            mk_desc = str(mk.get("description") or "")
            for out in mk.get("outcomes", []):
                player = out.get("description") or out.get("name", "")
                if not player:
                    continue
                dfs_type = (out.get("dfs_odds_type") or "").strip().lower()
                is_dfs_promo = False
                if is_dfs and dfs_type:
                    if dfs_type == "promo":
                        is_dfs_promo = True
                    elif dfs_type != "standard":
                        continue

                raw_side = out.get("name", "")
                raw_low = raw_side.strip().lower()
                if mname in ("Anytime TD", "1st TD", "Last TD"):
                    if raw_low in ("no", "under"):
                        side = "No"
                    elif raw_low in ("yes", "over") or raw_low == (player or "").strip().lower():
                        side = "Yes"
                    else:
                        continue
                elif raw_low == "under" or raw_low.startswith("under "):
                    side = "Under"
                elif raw_low == "over" or raw_low.startswith("over "):
                    side = "Over"
                else:
                    continue

                if is_dfs_promo and side == "Under":
                    continue

                line = out.get("point")
                if line is None and mname in ("Anytime TD", "1st TD", "Last TD"):
                    line = 0.5
                if line is None:
                    continue

                price = out.get("price")
                if book == "PrizePicks":
                    price_int = -119
                    dec = 1.8433
                else:
                    price_int = -110 if price is None else int(round(float(price)))
                    dec = round(american_to_decimal(price_int), 4)
                row = {
                    "source": source_name,
                    "book": book,
                    "player": player,
                    "matchup": matchup,
                    "market": mname,
                    "side": side,
                    "line": line,
                    "american": price_int,
                    "decimal": dec,
                }
                if is_dfs_promo:
                    orig_line = None
                    m_from = re.search(r"from\s+([\d.]+)", mk_desc, re.I)
                    if m_from:
                        try:
                            orig_line = float(m_from.group(1))
                        except ValueError:
                            orig_line = None
                    promo_label = "PROMO"
                    if orig_line and float(orig_line) > float(line):
                        disc_pct = int(round((float(orig_line) - float(line)) / float(orig_line) * 100))
                        promo_label = f"PROMO (-{disc_pct}%)"
                    row["is_promo"] = True
                    row["promo_label"] = promo_label
                    row["orig_line"] = orig_line
                if is_dfs:
                    dfs_props.append(row)
                elif is_nl:
                    player_props.append(row)
                elif is_cons:
                    benchmark_props.append(row)

    return {
        "player_props": player_props,
        "benchmark_props": benchmark_props,
        "dfs_props": dfs_props,
        "remaining": remaining,
    }


def _parse_odds_api_events(
    events: List[Dict],
    source_name: str,
    api_key: str,
    base_event_url: str,
    regions: str,
    prop_markets: str,
    max_events: int = 16,
    include_props: bool = True,
    slate_matchups: Optional[Set[str]] = None,
) -> Dict:
    """Shared parser for The Odds API v4 and Odds-API-compatible PropLine v1 payloads."""
    game_lines: List[Dict] = []
    benchmark_game_lines: List[Dict] = []
    player_props: List[Dict] = []
    benchmark_props: List[Dict] = []
    dfs_props: List[Dict] = []
    remaining: Optional[str] = None
    slate_events: List[Dict] = []

    for ev in events:
        away = normalize_team(ev.get("away_team", ""))
        home = normalize_team(ev.get("home_team", ""))
        if not away or not home:
            continue
        matchup = f"{away} @ {home}"
        if slate_matchups and matchup in slate_matchups:
            slate_events.append(ev)
        elif not slate_matchups:
            slate_events.append(ev)

        for bm in ev.get("bookmakers", []):
            book = normalize_book_name(bm.get("title", bm.get("key", "")))
            is_nl = book in NL_BOOKS
            is_cons = book in CONSENSUS_BOOKS
            if not is_nl and not is_cons:
                continue
            for mk in bm.get("markets", []):
                mkey = mk.get("key", "")
                mk_period = str(mk.get("period") or "").strip().lower()
                mk_desc = str(mk.get("description") or "").strip()
                desc_l = mk_desc.lower()
                if mk_period and mk_period not in ("full", "ft", "game", "full_game", "match"):
                    continue
                if any(tok in desc_l for tok in ("1st half", "2nd half", "1st quarter", "2nd quarter", "3rd quarter", "4th quarter", "first half", "second half", "1h ", "2h ", "1q ", "2q ", "3q ", "4q ")):
                    continue

                mk_team = str(mk.get("team") or "").strip()
                is_team_total = mkey == "totals" and (bool(mk_team) or "team total" in desc_l)
                team_abbr = ""
                if is_team_total:
                    team_abbr = normalize_team(mk_team) if mk_team else ""
                    if not team_abbr and " - " in mk_desc:
                        team_abbr = normalize_team(mk_desc.split(" - ", 1)[-1].strip())
                    if not team_abbr:
                        continue
                    mlabel = "Team Total"
                else:
                    mlabel = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(mkey, mkey)

                for out in mk.get("outcomes", []):
                    price = out.get("price")
                    if price is None:
                        continue
                    pt_val = out.get("point")
                    if mlabel == "Total" and pt_val is not None:
                        try:
                            if float(pt_val) < 30.0:
                                continue
                        except (TypeError, ValueError):
                            pass
                    price_int = int(round(float(price)))
                    raw_side = out.get("name", "")
                    if mkey in ("h2h", "spreads"):
                        side = normalize_team(raw_side)
                    elif is_team_total:
                        side = f"{team_abbr} {raw_side}".strip()
                    else:
                        side = raw_side
                    g_row = {
                        "source": source_name,
                        "book": book,
                        "matchup": matchup,
                        "away": away,
                        "home": home,
                        "market": mlabel,
                        "side": side,
                        "line": out.get("point"),
                        "american": price_int,
                        "decimal": round(american_to_decimal(price_int), 4),
                    }
                    if is_nl:
                        game_lines.append(g_row)
                    elif is_cons:
                        benchmark_game_lines.append(g_row)

    target_events = (slate_events or events)[:max_events]
    if include_props and target_events:
        with ThreadPoolExecutor(max_workers=min(3, len(target_events))) as pool:
            futures = [
                pool.submit(
                    _fetch_single_event_props,
                    ev,
                    source_name,
                    api_key,
                    base_event_url,
                    regions,
                    prop_markets,
                )
                for ev in target_events
            ]
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception:
                    continue
                player_props.extend(res["player_props"])
                benchmark_props.extend(res["benchmark_props"])
                dfs_props.extend(res["dfs_props"])
                if res.get("remaining") is not None:
                    remaining = res["remaining"]

    return {
        "game_lines": game_lines,
        "benchmark_game_lines": benchmark_game_lines,
        "player_props": player_props,
        "benchmark_props": benchmark_props,
        "dfs_props": dfs_props,
        "remaining": remaining,
    }


def _fetch_odds_api_compatible(
    env_var: str,
    source_name: str,
    base_url: str,
    sport_key: str,
    regions: str,
    prop_markets: str,
    cache_ttl_hours: float = 0.0,
    include_props: bool = True,
    force: bool = False,
    slate_matchups: Optional[Set[str]] = None,
    cache_version: int = 1,
) -> Dict:
    """Fetch game lines, sharp benchmark odds, and event player props from an Odds-API-compatible endpoint."""
    api_key = os.getenv(env_var, "").strip()
    if not api_key:
        return {
            "enabled": False,
            "game_lines": [],
            "benchmark_game_lines": [],
            "player_props": [],
            "benchmark_props": [],
            "dfs_props": [],
            "remaining": None,
        }

    if not force and cache_ttl_hours > 0:
        cached = _load_api_cache(env_var, cache_ttl_hours, min_version=cache_version)
        if cached is not None:
            return cached

    query_params = _build_event_params(api_key, regions, "h2h,spreads,totals")
    url = f"{base_url}/sports/{sport_key}/odds?{query_params}"
    remaining = None
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            remaining = resp.headers.get("x-requests-remaining") or resp.headers.get("x-ratelimit-remaining")
            events = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {
            "enabled": True,
            "error": str(exc),
            "game_lines": [],
            "benchmark_game_lines": [],
            "player_props": [],
            "benchmark_props": [],
            "dfs_props": [],
            "remaining": remaining,
        }

    if not isinstance(events, list):
        return {
            "enabled": True,
            "game_lines": [],
            "benchmark_game_lines": [],
            "player_props": [],
            "benchmark_props": [],
            "dfs_props": [],
            "remaining": remaining,
        }

    parsed = _parse_odds_api_events(
        events=events,
        source_name=source_name,
        api_key=api_key,
        base_event_url=f"{base_url}/sports/{sport_key}/events/{{event_id}}/odds",
        regions=regions,
        prop_markets=prop_markets,
        include_props=include_props,
        slate_matchups=slate_matchups,
    )
    payload = {
        "enabled": True,
        "game_lines": parsed["game_lines"],
        "benchmark_game_lines": parsed["benchmark_game_lines"],
        "player_props": parsed["player_props"],
        "benchmark_props": parsed["benchmark_props"],
        "dfs_props": parsed["dfs_props"],
        "remaining": parsed["remaining"] or remaining,
        "from_cache": False,
        "props_skipped_for_quota": not include_props,
    }
    if cache_ttl_hours > 0 and (payload["game_lines"] or payload["player_props"] or payload["dfs_props"] or payload["benchmark_props"]):
        payload["cached_at"] = _save_api_cache(env_var, payload, schema_version=cache_version)
    return payload


def fetch_the_odds_api(force: bool = False, slate_matchups: Optional[Set[str]] = None) -> Dict:
    """Fetch NFL game lines (plus sharp/consensus benchmark odds) from The Odds API v4, cached 24h."""
    has_propline = bool(os.getenv("PROPLINE_API_KEY", "").strip())
    return _fetch_odds_api_compatible(
        env_var="THE_ODDS_API_KEY",
        source_name="The Odds API",
        base_url="https://api.the-odds-api.com/v4",
        sport_key="americanfootball_nfl",
        regions="us,eu",
        prop_markets="player_pass_yds,player_pass_tds,player_rush_yds,player_reception_yds,player_receptions,player_anytime_td",
        cache_ttl_hours=24.0,
        include_props=not has_propline,
        force=force,
        slate_matchups=slate_matchups,
        cache_version=1,
    )


def fetch_propline_api(force: bool = False, slate_matchups: Optional[Set[str]] = None) -> Dict:
    """Fetch ALL 19 NFL prop markets, 80+ sharp/consensus books, and Pick'em props from PropLine v1, cached 1h."""
    return _fetch_odds_api_compatible(
        env_var="PROPLINE_API_KEY",
        source_name="PropLine",
        base_url="https://api.prop-line.com/v1",
        sport_key="football_nfl",
        regions="",
        prop_markets=(
            "player_pass_yds,player_pass_tds,player_pass_completions,player_pass_attempts,"
            "player_pass_interceptions,player_rush_yds,player_rush_attempts,player_rush_longest,"
            "player_reception_yds,player_receptions,player_reception_longest,"
            "player_rush_reception_yds,player_anytime_td,player_1st_td,player_last_td,"
            "player_kicking_points,player_sacks,player_tackles_assists,player_assists"
        ),
        cache_ttl_hours=1.0,
        include_props=True,
        force=force,
        slate_matchups=slate_matchups,
        cache_version=6,
    )


def fetch_balldontlie_props() -> Dict:
    """Fetch NFL player props from Balldontlie API if BALLDONTLIE_API_KEY is set."""
    api_key = os.getenv("BALLDONTLIE_API_KEY", "").strip()
    if not api_key:
        return {"enabled": False, "player_props": []}
    req = urllib.request.Request(
        "https://api.balldontlie.io/nfl/v1/odds/player_props",
        headers={"Authorization": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return {"enabled": True, "player_props": payload.get("data", [])}
    except Exception as exc:
        return {"enabled": True, "error": str(exc), "player_props": []}
