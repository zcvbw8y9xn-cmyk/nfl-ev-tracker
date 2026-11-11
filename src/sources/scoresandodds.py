"""ScoresAndOdds NFL player props scraper (extra independent projections + best lines)."""
from __future__ import annotations

import re
from typing import Dict, List
from bs4 import BeautifulSoup
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from ..odds_math import parse_american
from .cache_utils import RateLimitError, fetch_with_cache


def scrape_scoresandodds() -> List[Dict]:
    """Scrape ScoresAndOdds /nfl/props with a 10-minute disk cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        "scoresandodds_props",
        "scoresandodds.com",
        ttl_seconds=600.0,
        fetcher=_scrape_scoresandodds_live,
        is_valid=lambda r: isinstance(r, list) and len(r) > 0,
        cooldown_minutes=20.0,
    )
    return res if isinstance(res, list) else []


def _scrape_scoresandodds_live() -> List[Dict]:
    """Scrape ScoresAndOdds /nfl/props for independent player projections and market lines."""
    p = Fetcher.get("https://www.scoresandodds.com/nfl/props", timeout=15)
    if getattr(p, "status", 200) in (403, 429):
        raise RateLimitError("scoresandodds.com", int(p.status))
    soup = BeautifulSoup(p.body.decode("utf-8", errors="ignore"), "html.parser")
    rows: List[Dict] = []

    for li in soup.select("[data-name][data-diff]"):
        name_el = li.select_one(".props-name a")
        player = name_el.get_text(" ", strip=True) if name_el else li.get("data-name", "").title()
        if not player:
            continue

        clip = li.select_one("[data-content]")
        mkt_raw = ""
        if clip:
            m = re.search(r'data-market="([^"]+)"', clip.get("data-content", ""))
            if m:
                mkt_raw = m.group(1)
        if not mkt_raw:
            sec = li.find_parent(attrs={"data-market": True})
            mkt_raw = sec.get("data-market", "") if sec else ""
        market = normalize_market(mkt_raw or "Passing Yards")

        # Projection from data attribute or projection cell
        proj_val = None
        for attr in ("data-proj", "data-projection", "data-value"):
            if li.get(attr):
                try:
                    proj_val = float(li.get(attr))
                    break
                except ValueError:
                    pass
        if proj_val is None:
            proj_el = li.select_one(".table-list-proj, [class*='proj']")
            if proj_el:
                mnum = re.search(r"\d+(?:\.\d+)?", proj_el.get_text(" ", strip=True))
                if mnum:
                    proj_val = float(mnum.group(0))

        # Best odds Over / Under inside .best-odds
        for box in li.select(".game-odds"):
            ml_el = box.select_one(".data-moneyline")
            od_el = box.select_one(".data-odds")
            if not ml_el or not od_el:
                continue
            ml_txt = ml_el.get_text(strip=True).lower()
            od_txt = od_el.get_text(strip=True)
            am = parse_american(od_txt)
            # Parse the line from a moneyline cell like 'o41.5' / 'u41.5'. Guard against
            # malformed cells ('o', 'o41.5x', 'under 41.5') so one bad row cannot abort
            # the whole ScoresAndOdds scrape.
            m_line = re.match(r"^([ou])\s*(\d+(?:\.\d+)?)", ml_txt)
            if not m_line:
                continue
            side = "Over" if m_line.group(1) == "o" else "Under"
            try:
                line = float(m_line.group(2))
            except ValueError:
                continue
            rows.append({
                "source": "ScoresAndOdds",
                "player": player,
                "market": market,
                "side": side,
                "line": line,
                "sao_odds": am,
                "sao_projection": proj_val,
            })
    return rows
