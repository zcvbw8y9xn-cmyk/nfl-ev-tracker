"""Shared scraping, aggregation, and disk cache pipeline for CLI and Mac desktop app."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from src.engine import build_ev_datasets
from src.sources.action_network import scrape_action_network
from src.sources.alc_scrapling import scrape_alc
from src.sources.bettingpros import scrape_bettingpros
from src.sources.covers import scrape_covers
from src.sources.espn import fetch_espn_fantasy_projections, fetch_espn_slate
from src.sources.optional_apis import fetch_balldontlie_props, fetch_propline_api, fetch_the_odds_api
from src.sources.pinnacle import scrape_pinnacle
from src.sources.prizepicks import scrape_prizepicks
from src.sources.scoresandodds import scrape_scoresandodds
from src.sources.sleeper import scrape_sleeper
from src.sources.underdog import scrape_underdog
from src.sources.weather import enrich_games_with_weather

NL_TZ = ZoneInfo("America/St_Johns")
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path.home() / ".cache" / "nfl-ev-tracker"
CACHE_PATH = CACHE_DIR / "latest_dataset.json"
ACCOUNTS_PATH = CACHE_DIR / "accounts.json"

ProgressCallback = Callable[[int, int, str, str], None]
LogCallback = Callable[[str], None]


def load_local_env() -> None:
    """Load key=value variables from .env in project root if not already set."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT_DIR / ".env")
        return
    except ImportError:
        pass
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


MANAGED_ENV_KEYS = (
    "THE_ODDS_API_KEY",
    "PROPLINE_API_KEY",
    "BALLDONTLIE_API_KEY",
    "SPREADSHEET_ID",
)


def _mask_secret(val: str) -> str:
    v = val.strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:4]}••••{v[-4:]}"


def get_api_key_status() -> Dict[str, Any]:
    """Return non-secret configured status and masked preview for managed API keys."""
    load_local_env()
    out: Dict[str, Any] = {}
    for k in MANAGED_ENV_KEYS:
        val = os.getenv(k, "").strip()
        out[k] = {
            "configured": bool(val),
            "masked": _mask_secret(val) if k != "SPREADSHEET_ID" else val,
        }
    return out


def save_api_keys(updates: Dict[str, str]) -> Dict[str, Any]:
    """Persist non-empty or explicitly cleared managed keys to `.env` and `os.environ`."""
    load_local_env()
    env_path = ROOT_DIR / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    cleaned_updates = {k: str(v).strip() for k, v in updates.items() if k in MANAGED_ENV_KEYS and v is not None}

    seen = set()
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        k, _ = line.split("=", 1)
        k_clean = k.strip()
        if k_clean in cleaned_updates:
            new_lines.append(f"{k_clean}={cleaned_updates[k_clean]}")
            seen.add(k_clean)
        else:
            new_lines.append(line)

    for k, v in cleaned_updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")

    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    for k, v in cleaned_updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return get_api_key_status()


DEFAULT_ACCOUNTS: List[Dict[str, Any]] = [
    {"id": "prizepicks", "platform": "PrizePicks", "kind": "dfs", "attached": True, "handle": "", "supports_public_promos": True, "promos": []},
    {"id": "underdog", "platform": "Underdog", "kind": "dfs", "attached": True, "handle": "", "supports_public_promos": True, "promos": []},
    {"id": "sleeper", "platform": "Sleeper", "kind": "dfs", "attached": True, "handle": "", "supports_public_promos": True, "sleeper_user": None, "promos": []},
    {"id": "bet365", "platform": "bet365", "kind": "book", "attached": True, "handle": "", "supports_public_promos": False, "promos": []},
    {"id": "bet99", "platform": "BET99", "kind": "book", "attached": True, "handle": "", "supports_public_promos": False, "promos": []},
    {"id": "sia", "platform": "Sports Interaction", "kind": "book", "attached": True, "handle": "", "supports_public_promos": False, "promos": []},
    {"id": "alc", "platform": "ALC Proline", "kind": "book", "attached": True, "handle": "", "supports_public_promos": False, "promos": []},
    {"id": "betway", "platform": "Betway", "kind": "book", "attached": False, "handle": "", "supports_public_promos": False, "promos": []},
    {"id": "pinnacle", "platform": "Pinnacle", "kind": "book", "attached": False, "handle": "", "supports_public_promos": False, "promos": []},
]


def load_accounts() -> List[Dict[str, Any]]:
    """Load user-attached sportsbook/DFS account profiles and custom account promos."""
    if not ACCOUNTS_PATH.exists():
        return [dict(item, promos=list(item.get("promos", []))) for item in DEFAULT_ACCOUNTS]
    try:
        raw = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            by_id = {str(x.get("id")): x for x in raw if isinstance(x, dict) and x.get("id")}
            merged = []
            for d in DEFAULT_ACCOUNTS:
                cur = by_id.pop(d["id"], None)
                merged.append(cur if cur else dict(d, promos=[]))
            merged.extend(by_id.values())
            return merged
    except Exception:
        pass
    return [dict(item, promos=list(item.get("promos", []))) for item in DEFAULT_ACCOUNTS]


def save_accounts(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Atomically persist user-attached accounts & account promos (never stores passwords)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleaned: List[Dict[str, Any]] = []
    for item in accounts:
        if not isinstance(item, dict) or not item.get("platform"):
            continue
        promos_clean = []
        for p in item.get("promos") or []:
            if not isinstance(p, dict):
                continue
            promos_clean.append({
                "id": str(p.get("id") or ""),
                "title": str(p.get("title") or "Promo Boost").strip(),
                "type": str(p.get("type") or "profit_boost"),
                "boost_pct": float(p.get("boost_pct") or 0),
                "max_stake": float(p.get("max_stake") or 25),
                "scope": str(p.get("scope") or "all"),
                "active": bool(p.get("active", True)),
            })
        cleaned.append({
            "id": str(item.get("id") or item["platform"].lower().replace(" ", "_")),
            "platform": str(item["platform"]),
            "kind": str(item.get("kind") or "book"),
            "attached": bool(item.get("attached")),
            "handle": str(item.get("handle") or "").strip(),
            "supports_public_promos": bool(item.get("supports_public_promos")),
            "sleeper_user": item.get("sleeper_user"),
            "promos": promos_clean,
        })
    tmp = ACCOUNTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNTS_PATH)
    return cleaned


def verify_sleeper_username(username: str) -> Dict[str, Any]:
    """Verify a public Sleeper username via api.sleeper.app/v1/user/<username>."""
    clean = username.strip().lstrip("@")
    if not clean:
        raise ValueError("Enter a Sleeper username.")
    url = f"https://api.sleeper.app/v1/user/{urllib.parse.quote(clean)}"
    req = urllib.request.Request(url, headers={"User-Agent": "NFLEVTracker/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict) or not data.get("user_id"):
        raise ValueError(f"Sleeper user '@{clean}' not found.")
    avatar = data.get("avatar")
    return {
        "user_id": data["user_id"],
        "username": data.get("username") or clean,
        "display_name": data.get("display_name") or data.get("username") or clean,
        "avatar_url": f"https://sleepercdn.com/avatars/thumbs/{avatar}" if avatar else None,
    }


def load_cached_payload() -> Optional[Dict[str, Any]]:
    """Return last saved pipeline payload from disk, or None if unavailable."""
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("dataset"), dict):
            return data
    except Exception:
        return None
    return None


def save_cached_payload(payload: Dict[str, Any]) -> None:
    """Atomically write pipeline payload to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CACHE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(CACHE_PATH)


def run_pipeline(
    progress_cb: Optional[ProgressCallback] = None,
    log_fn: Optional[LogCallback] = None,
) -> Dict[str, Any]:
    """Execute all 11 scrapers/APIs, aggregate EV datasets, cache to disk, and return payload."""
    load_local_env()
    total_steps = 11

    def _emit(step: int, title: str, detail: str = "") -> None:
        if log_fn and not detail:
            log_fn(f"{step}/{total_steps} {title}")
        elif log_fn and detail:
            log_fn(f"    -> {detail}")
        if progress_cb:
            progress_cb(step, total_steps, title, detail)

    _emit(1, "Fetching ESPN canonical Week schedule...")
    espn_games = fetch_espn_slate()
    slate_set = {g["matchup"] for g in espn_games}
    espn_season = espn_games[0].get("season") if espn_games else None
    espn_week = espn_games[0].get("week") if espn_games else None
    espn_fantasy_projs = fetch_espn_fantasy_projections(espn_season, espn_week)
    _emit(
        1,
        "Fetching ESPN canonical Week schedule...",
        f"{len(espn_games)} NFL games this week, {len(espn_fantasy_projs)} ESPN Fantasy projection entries.",
    )

    _emit(2, "Scraping ALC Proline & Stadium Bets via Scrapling...")
    alc_data = scrape_alc()
    _emit(
        2,
        "Scraping ALC Proline & Stadium Bets via Scrapling...",
        f"{len(alc_data['game_lines'])} game lines, {len(alc_data['player_props'])} player props, {len(alc_data.get('fantasy_groups', []))} fantasy groups.",
    )

    _emit(3, "Fetching Pinnacle Guest API (sharp benchmark + NL-playable)...")
    pinnacle_data = scrape_pinnacle()
    _emit(
        3,
        "Fetching Pinnacle Guest API (sharp benchmark + NL-playable)...",
        f"{len(pinnacle_data['game_lines'])} game lines, {len(pinnacle_data['player_props'])} player props.",
    )

    _emit(4, "Fetching Action Network (bet365 CA, SIA, BET99, Betway CA + public/money splits)...")
    action_data = scrape_action_network()
    _emit(
        4,
        "Fetching Action Network (bet365 CA, SIA, BET99, Betway CA + public/money splits)...",
        f"{len(action_data['game_lines'])} game lines, {len(action_data['splits'])} game splits.",
    )

    _emit(5, "Scraping BettingPros paginated props (projections, fair odds, DVP, hit rates)...")
    bp_props = scrape_bettingpros()
    _emit(5, "Scraping BettingPros paginated props (projections, fair odds, DVP, hit rates)...", f"{len(bp_props)} prop sides.")

    _emit(6, "Scraping Covers.com (NL-localized books across all slate matchups)...")
    covers_data = scrape_covers(slate_set)
    _emit(
        6,
        "Scraping Covers.com (NL-localized books across all slate matchups)...",
        f"{len(covers_data['game_lines'])} game lines, {len(covers_data['player_props'])} player props.",
    )

    _emit(7, "Scraping ScoresAndOdds player prop projections...")
    sao_props = scrape_scoresandodds()
    _emit(7, "Scraping ScoresAndOdds player prop projections...", f"{len(sao_props)} prop sides.")

    _emit(8, "Fetching Sleeper Picks + live stadium field weather (Open-Meteo, NOAA NWS, RotoWire, ESPN)...")
    sleeper_data = scrape_sleeper()
    enrich_games_with_weather(espn_games, sleeper_data.get("weather", {}))
    weather_fields = sum(1 for g in espn_games if g.get("weather_sources"))
    _emit(
        8,
        "Fetching Sleeper Picks + live stadium field weather (Open-Meteo, NOAA NWS, RotoWire, ESPN)...",
        f"{len(sleeper_data['sleeper_lines'])} Sleeper Picks sides, {len(sleeper_data['sleeper_projections'])} Sleeper projections, {weather_fields} field forecasts.",
    )

    _emit(9, "Fetching PrizePicks NFL lines & Taco promos...")
    if os.getenv("PROPLINE_API_KEY", "").strip():
        prizepicks_props = []
        _emit(9, "Fetching PrizePicks NFL lines & Taco promos...", "Skipped direct PrizePicks scrape (using PropLine API feed).")
    else:
        prizepicks_props = scrape_prizepicks()
        _emit(9, "Fetching PrizePicks NFL lines & Taco promos...", f"{len(prizepicks_props)} PrizePicks prop sides.")

    _emit(10, "Fetching Underdog Fantasy NFL Higher/Lower lines...")
    underdog_props = scrape_underdog()
    _emit(10, "Fetching Underdog Fantasy NFL Higher/Lower lines...", f"{len(underdog_props)} Underdog prop sides.")

    _emit(11, "Checking optional APIs (The Odds API / PropLine / Balldontlie)...")
    odds_api_data = fetch_the_odds_api(slate_matchups=slate_set)
    propline_data = fetch_propline_api(slate_matchups=slate_set)
    bdl_data = fetch_balldontlie_props()
    parts = []
    if odds_api_data.get("enabled"):
        o_tag = "24h cache" if odds_api_data.get("from_cache") else "live"
        if odds_api_data.get("props_skipped_for_quota"):
            o_tag += ", lines-only to save quota"
        parts.append(
            f"The Odds API ({o_tag}): {len(odds_api_data['game_lines'])} lines, "
            f"{len(odds_api_data['player_props'])} props (rem: {odds_api_data.get('remaining')})"
        )
    if propline_data.get("enabled"):
        p_tag = "1h cache" if propline_data.get("from_cache") else "live"
        b_cnt = len(propline_data.get("benchmark_props", [])) + len(propline_data.get("benchmark_game_lines", []))
        parts.append(
            f"PropLine ({p_tag}): {len(propline_data['game_lines'])} NL lines, "
            f"{len(propline_data['player_props'])} NL props, {b_cnt} sharp/mkt benchmarks, "
            f"{len(propline_data.get('dfs_props', []))} DFS (rem: {propline_data.get('remaining')})"
        )
    if bdl_data.get("enabled"):
        parts.append(f"Balldontlie: {len(bdl_data.get('player_props', []))} props")
    msg = " | ".join(parts) if parts else "The Odds API / PropLine / Balldontlie not configured in .env (using free/scrapable sources)."
    _emit(11, "Checking optional APIs (The Odds API / PropLine / Balldontlie)...", msg)

    dataset = build_ev_datasets(
        espn_games=espn_games,
        alc_data=alc_data,
        pinnacle_data=pinnacle_data,
        action_data=action_data,
        bp_props=bp_props,
        covers_data=covers_data,
        sao_props=sao_props,
        odds_api_data=odds_api_data,
        underdog_props=underdog_props,
        prizepicks_props=prizepicks_props,
        sleeper_data=sleeper_data,
        propline_data=propline_data,
        espn_fantasy_projs=espn_fantasy_projs,
    )

    source_counts = {
        "alc_total": len(alc_data["game_lines"]) + len(alc_data["player_props"]) + len(alc_data.get("fantasy_groups", [])),
        "pinnacle_total": len(pinnacle_data["game_lines"]) + len(pinnacle_data["player_props"]),
        "covers_total": len(covers_data["game_lines"]) + len(covers_data["player_props"]),
        "bp_total": len(bp_props),
        "action_total": len(action_data["game_lines"]) + len(action_data["player_props"]),
        "sao_total": len(sao_props),
        "sleeper_total": len(sleeper_data.get("sleeper_lines", [])) + len(sleeper_data.get("sleeper_projections", {})),
        "espn_fantasy_total": len(espn_fantasy_projs),
        "prizepicks_total": len(prizepicks_props),
        "underdog_total": len(underdog_props),
        "espn_games": len(espn_games),
        "odds_api_total": len(odds_api_data.get("game_lines", [])) + len(odds_api_data.get("player_props", [])),
        "propline_total": len(propline_data.get("game_lines", [])) + len(propline_data.get("player_props", [])) + len(propline_data.get("dfs_props", [])),
        "bdl_total": len(bdl_data.get("player_props", [])),
    }
    optional_status = {
        "the_odds_api": bool(odds_api_data.get("enabled")),
        "propline": bool(propline_data.get("enabled")),
        "balldontlie": bool(bdl_data.get("enabled")),
    }

    now_nl = datetime.now(NL_TZ)
    payload = {
        "fetched_at": now_nl.isoformat(),
        "fetched_at_display": now_nl.strftime("%a %b %d, %I:%M %p NST"),
        "dataset": dataset,
        "source_counts": source_counts,
        "optional_status": optional_status,
    }
    save_cached_payload(payload)
    return payload
