"""BettingPros paginated NFL player props scraper (projections, fair odds, EV, DVP, hit rates)."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from .cache_utils import RateLimitError, fetch_with_cache, get_domain_cooldown_remaining, pace_domain


def _extract_bp_json(html_text: str) -> Dict:
    for s in re.findall(r"<script[^>]*>(.*?)</script>", html_text, re.S):
        if '"offers"' in s and len(s) > 1000:
            try:
                return json.loads(s.strip())
            except Exception:
                continue
    return {}


def _fetch_bp_page(page_num: int) -> Dict:
    if get_domain_cooldown_remaining("bettingpros.com") > 0:
        raise RateLimitError("bettingpros.com", 429)
    pace_domain("bettingpros.com", min_delay=0.55, jitter=0.45)
    url = f"https://www.bettingpros.com/nfl/props/?page={page_num}"
    p = Fetcher.get(url, timeout=15)
    if getattr(p, "status", 200) in (403, 429):
        raise RateLimitError("bettingpros.com", int(p.status))
    return _extract_bp_json(p.body.decode("utf-8", errors="ignore"))


def _scrape_bettingpros_live(max_pages: int = 25) -> List[Dict]:
    """Fetch all paginated BettingPros NFL props with projections, fair odds, DVP, and hit rates."""
    first = _fetch_bp_page(1)
    if not first:
        return []

    total_pages = min(int(first.get("pagination", {}).get("total_pages", 1)), max_pages)
    pages_data = [first]

    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_fetch_bp_page, pg) for pg in range(2, total_pages + 1)]
            for fut in as_completed(futs):
                try:
                    d = fut.result()
                    if d:
                        pages_data.append(d)
                except RateLimitError:
                    for rem in futs:
                        rem.cancel()
                    raise
                except Exception:
                    pass

    # Map market_id -> market name, event_id -> matchup
    market_map: Dict[int, str] = {}
    event_map: Dict[int, str] = {}
    for m in first.get("markets", []):
        if "id" in m:
            market_map[int(m["id"])] = normalize_market(m.get("name") or m.get("slug", ""))
    for ev in first.get("events", []):
        if "id" in ev:
            away = normalize_team(ev.get("visitor", ""))
            home = normalize_team(ev.get("home", ""))
            event_map[int(ev["id"])] = f"{away} @ {home}" if away and home else ""

    rows: List[Dict] = []
    seen = set()
    for pd in pages_data:
        for off in pd.get("offers", []):
            part = off.get("participant", {})
            pl = part.get("player", {})
            pname = part.get("name") or ""
            if not pname:
                continue
            mid = off.get("market_id")
            eid = off.get("event_id")
            mname = market_map.get(int(mid), str(mid)) if mid is not None else ""
            matchup = event_map.get(int(eid), "") if eid is not None else ""

            proj = off.get("projection") or {}
            perf = off.get("performance") or {}
            extra = off.get("extra") or {}
            opp_rank = (extra.get("opposition_rank") or {}).get("rank")
            l5 = perf.get("last_5") or {}
            l10 = perf.get("last_10") or {}
            season = perf.get("season") or {}
            injury = pl.get("injury_status") or ""

            for side_key, side_label in [("over", "Over"), ("under", "Under")]:
                sdata = off.get(side_key) or {}
                line = sdata.get("line")
                odds = sdata.get("odds")
                if line is None or odds is None:
                    continue
                if mname in ("Anytime TD", "1st TD", "Last TD") and float(line) == 0.5:
                    resolved_side = "Yes" if side_label == "Over" else "No"
                else:
                    resolved_side = side_label
                uid = (pname, mname, resolved_side, line)
                if uid in seen:
                    continue
                seen.add(uid)

                l5_total = (l5.get("over", 0) + l5.get("under", 0))
                l10_total = (l10.get("over", 0) + l10.get("under", 0))
                s_total = (season.get("over", 0) + season.get("under", 0))
                l5_rate = round((l5.get(side_key, 0) / l5_total) * 100, 1) if l5_total > 0 else None
                l10_rate = round((l10.get(side_key, 0) / l10_total) * 100, 1) if l10_total > 0 else None
                s_rate = round((season.get(side_key, 0) / s_total) * 100, 1) if s_total > 0 else None

                rows.append({
                    "source": "BettingPros",
                    "player": pname,
                    "position": pl.get("position", ""),
                    "team": normalize_team(pl.get("team", "")),
                    "injury": injury,
                    "matchup": matchup,
                    "market": mname,
                    "side": resolved_side,
                    "line": float(line),
                    "bp_best_odds": int(odds),
                    "bp_consensus_line": sdata.get("consensus_line"),
                    "bp_consensus_odds": sdata.get("consensus_odds"),
                    "bp_prob": sdata.get("probability"),
                    "bp_ev_pct": round(float(sdata["expected_value"]) * 100, 2) if sdata.get("expected_value") is not None else None,
                    "bp_rating": sdata.get("bet_rating"),
                    "bp_projection": proj.get("value"),
                    "bp_rec_side": (proj.get("recommended_side") or "").title(),
                    "bp_fair_odds": proj.get("fair_odds"),
                    "opp_dvp_rank": opp_rank,
                    "l5_hit_pct": l5_rate,
                    "l10_hit_pct": l10_rate,
                    "season_hit_pct": s_rate,
                })
    return rows


def scrape_bettingpros(max_pages: int = 25) -> List[Dict]:
    """Fetch BettingPros paginated props with a 15-minute disk cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        f"bettingpros_p{max_pages}",
        "bettingpros.com",
        ttl_seconds=900.0,
        fetcher=lambda: _scrape_bettingpros_live(max_pages=max_pages),
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=20.0,
    )
    return res if isinstance(res, list) else []
