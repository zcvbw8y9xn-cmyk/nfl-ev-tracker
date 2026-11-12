"""Covers.com scraper for NL-localized sportsbooks (bet365, SIA, BET99, Betway) + EV projections."""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set, Tuple
from bs4 import BeautifulSoup
from scrapling.fetchers import Fetcher

from ..normalize import normalize_book_name, normalize_market, normalize_team
from ..odds_math import american_to_decimal, parse_american
from .cache_utils import RateLimitError, fetch_with_cache, get_domain_cooldown_remaining, pace_domain

COVERS_TARGET_BOOKS = {"bet365", "Sports Interaction", "BET99", "Betway"}


def _parse_book_odds_cell(text: str, market_type: str) -> List[Tuple[Optional[float], Optional[int]]]:
    """Parse Covers odds table cells like '+120 2.20 6/5 -140 1.71 5/7' or 'o 41.5 -103 1.97 ... u 41.5 -117 ...'."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if market_type == "Moneyline":
        # Two American odds numbers
        ams = re.findall(r"(?<![\d./])([+-]\d{3,4}|EVEN)(?!\d)", t, re.I)
        res = []
        for a in ams[:2]:
            res.append((None, parse_american(a)))
        return res
    if market_type == "Spread":
        # Matches e.g. "+2.5 -110" or "-2.5 +100"
        pairs = re.findall(r"([+-]?\d+(?:\.5|\.0)?)\s+([+-]\d{3,4}|EVEN)", t, re.I)
        return [(float(ln), parse_american(am)) for ln, am in pairs[:2]]
    if market_type == "Total":
        pairs = re.findall(r"[ou]\s*(\d+(?:\.\d+)?)\s+([+-]\d{3,4}|EVEN)", t, re.I)
        return [(float(ln), parse_american(am)) for ln, am in pairs[:2]]
    return []


def _parse_compare_odds_columns(container) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Extract {book: (line, american_odds)} from a Covers compare-odds-table."""
    out: Dict[str, Tuple[Optional[float], Optional[int]]] = {}
    for col in container.select(".compare-odds-column"):
        img = col.select_one("img.sportsbook-logo")
        if not img:
            continue
        book = normalize_book_name(img.get("alt", ""))
        if book not in COVERS_TARGET_BOOKS:
            continue
        for a in col.select("a"):
            txt = " ".join(a.get_text(" ", strip=True).split())
            if not txt or txt == "-":
                continue
            # Formats: "o35.5 -115", "u244.5 -115", "+550", "-113"
            m = re.match(r"^[ou]\s*(\d+(?:\.\d+)?)\s*([+-]\d{3,4}|EVEN)$", txt, re.I)
            if m:
                out[book] = (float(m.group(1)), parse_american(m.group(2)))
                break
            m2 = re.match(r"^([+-]\d{3,4}|EVEN)$", txt, re.I)
            if m2:
                out[book] = (None, parse_american(m2.group(1)))
                break
    return out


def scrape_covers_odds_and_matchups(slate_matchups: Set[str]) -> Tuple[List[Dict], List[str]]:
    """Scrape Covers /nfl/odds for game lines across NL books and return matching matchup IDs."""
    if get_domain_cooldown_remaining("covers.com") > 0:
        raise RateLimitError("covers.com", 429)
    pace_domain("covers.com", min_delay=0.5, jitter=0.45)
    p = Fetcher.get("https://www.covers.com/sport/football/nfl/odds", timeout=20)
    if getattr(p, "status", 200) in (403, 429):
        raise RateLimitError("covers.com", int(p.status))
    soup = BeautifulSoup(p.body.decode("utf-8", errors="ignore"), "html.parser")

    game_lines: List[Dict] = []
    matchup_ids: List[str] = []
    seen_mids: Set[str] = set()

    for pid, mtype in [("tab-moneyline", "Moneyline"), ("tab-spread", "Spread"), ("tab-total", "Total")]:
        pan = soup.select_one(f"#{pid}")
        if not pan:
            continue
        headers = []
        for th in pan.select("thead th, thead td"):
            img = th.select_one("img")
            headers.append(normalize_book_name(img.get("alt") if img else th.get_text(strip=True)))

        for tr in pan.select("tbody tr"):
            tds = tr.select("td")
            if len(tds) < 2:
                continue
            info_txt = " ".join(tds[0].get_text(" ", strip=True).split())
            if not info_txt:
                continue

            # Extract matchup teams from links or tokens
            team_links = [a.get_text(strip=True) for a in tds[0].select("a") if a.get_text(strip=True) not in {"Projections", "Line Movement", "Picks", "Matchup"}]
            away, home = "", ""
            if len(team_links) >= 2:
                away, home = normalize_team(team_links[0]), normalize_team(team_links[1])
            else:
                # Fallback regex for two 2-3 letter team codes
                codes = re.findall(r"\b([A-Z]{2,3})\b", info_txt)
                codes = [normalize_team(c) for c in codes if c not in {"ET", "NOV", "DEC", "OCT"}]
                if len(codes) >= 2:
                    away, home = codes[0], codes[1]

            if not away or not home:
                continue
            matchup = f"{away} @ {home}"
            if slate_matchups and matchup not in slate_matchups:
                continue

            # Collect matchup ID for current-week slate
            for a in tds[0].select('a[href*="/sport/football/nfl/matchup/"]'):
                mm = re.search(r"/matchup/(\d+)", a.get("href", ""))
                if mm and mm.group(1) not in seen_mids:
                    seen_mids.add(mm.group(1))
                    matchup_ids.append(mm.group(1))

            for col_idx in range(1, min(len(tds), len(headers))):
                book = headers[col_idx]
                if book not in COVERS_TARGET_BOOKS:
                    continue
                parsed = _parse_book_odds_cell(tds[col_idx].get_text(" ", strip=True), mtype)
                if len(parsed) < 2:
                    continue
                sides = [away, home] if mtype in ("Moneyline", "Spread") else ["Over", "Under"]
                for side_name, (line_val, am_odds) in zip(sides, parsed):
                    if am_odds is None:
                        continue
                    game_lines.append({
                        "source": "Covers",
                        "book": book,
                        "matchup": matchup,
                        "away": away,
                        "home": home,
                        "market": mtype,
                        "side": side_name,
                        "line": line_val,
                        "american": am_odds,
                        "decimal": round(american_to_decimal(am_odds), 4),
                    })

    return game_lines, matchup_ids


def _scrape_covers_player_props_page(url: str) -> List[Dict]:
    """Scrape either /nfl/player-props or a matchup /picks page for Covers EV props & book odds."""
    if get_domain_cooldown_remaining("covers.com") > 0:
        raise RateLimitError("covers.com", 429)
    pace_domain("covers.com", min_delay=0.5, jitter=0.45)
    p = Fetcher.get(url, timeout=20)
    if getattr(p, "status", 200) in (403, 429):
        raise RateLimitError("covers.com", int(p.status))
    soup = BeautifulSoup(p.body.decode("utf-8", errors="ignore"), "html.parser")
    results: List[Dict] = []

    # 1. Try #props table rows on /nfl/player-props or matchup page (has exact EV% + projection)
    for row in soup.select("[data-odds='content']"):
        # Walk up to card container
        card = row.find_parent("div", class_=re.compile(r"pick-cards-expert-component|prop-row|player-prop", re.I))
        if not card:
            card = row.parent.parent if row.parent else None
        if not card:
            continue

        card_txt = " ".join(card.get_text(" ", strip=True).split())
        book_odds = _parse_compare_odds_columns(row)

        # Check if this is a player prop card (matchup /picks page style or league page style)
        # Matchup /picks page style: "Passing Yards Michael Penix Jr. o216.5 Passing Yards (-115) Projection 237.69 (Over) ... EV Model Rating"
        m_pick = re.search(
            r"^(Passing Yards|Rushing Yards|Receiving Yards|Receptions Made|Receptions|Passing Touchdowns|Passing Attempts|Passing Completions|Rushing Attempts|Interceptions Thrown|Anytime Touchdown)\s+(.+?)\s+([ou]\d+(?:\.\d+)?)\s+.+?\(([+-]\d+|EVEN)\)\s+Projection\s+([\d.]+)\s+\((Over|Under)\)",
            card_txt,
            re.I,
        )
        if m_pick:
            mkt = normalize_market(m_pick.group(1))
            player = m_pick.group(2).strip()
            line_num = float(m_pick.group(3)[1:])
            proj = float(m_pick.group(5))
            side = m_pick.group(6).title()
            # Try to get matchup from page title or header
            title_el = soup.select_one("h1")
            title_txt = title_el.get_text(" ", strip=True) if title_el else ""
            matchup = ""
            # e.g. "Kansas City Chiefs vs Atlanta Falcons"
            if " vs " in title_txt.lower() or " at " in title_txt.lower():
                parts = re.split(r"\s+(?:vs\.?|at|@)\s+", title_txt.split("Picks")[0], flags=re.I)
                if len(parts) >= 2:
                    a1, h1 = normalize_team(parts[0]), normalize_team(parts[1])
                    if a1 and h1:
                        matchup = f"{a1} @ {h1}"

            results.append({
                "source": "Covers",
                "player": player,
                "matchup": matchup,
                "market": mkt,
                "side": side,
                "line": line_num,
                "covers_projection": proj,
                "covers_ev_pct": None,
                "book_odds": book_odds,
            })
            continue

        # Anytime TD on matchup card: "Anytime Touchdown Player Name Score a Touchdown (Yes: +150)"
        m_td = re.search(r"^Anytime Touchdown\s+(.+?)\s+Score a Touchdown\s+\(Yes:\s*([+-]\d+)\)", card_txt, re.I)
        if m_td:
            results.append({
                "source": "Covers",
                "player": m_td.group(1).strip(),
                "matchup": "",
                "market": "Anytime TD",
                "side": "Yes",
                "line": 0.5,
                "covers_projection": None,
                "covers_ev_pct": None,
                "book_odds": book_odds,
            })

    # 2. Also parse the structured EV table on /nfl/player-props and matchup #props table!
    # Select only leaf prop containers (containing exactly 1 [data-odds='content'] element)
    for odds_box in soup.select("[data-odds='content']"):
        tr = odds_box.find_parent("tr") or odds_box.find_parent("div", class_=re.compile(r"prop-card-row|matchup-prop-row|player-prop", re.I)) or odds_box.parent.parent
        if not tr or len(tr.select("[data-odds='content']")) > 1:
            continue
        txt = " ".join(tr.get_text(" ", strip=True).split())
        if "EV" not in txt or "PROJECTION" not in txt:
            continue
        book_odds = _parse_compare_odds_columns(odds_box)
        m_proj = re.search(r"([\d.]+)\s*(OVER|UNDER|TDs)\s*PROJECTION", txt, re.I)
        # IMPORTANT: Covers labels its row metric as "... DIFFERENCE <X>% EV Rating: S".
        # The percentage is the PROJECTION-minus-LINE "DIFFERENCE", NOT a bankroll EV%.
        # Treating it as EV fabricates ~26% edges on essentially every prop. Covers does
        # not expose a clean numeric EV here (only a projection + star/letter rating),
        # so we intentionally do NOT capture an EV number; downstream derives fair value
        # from the projection vs the offered line instead.
        player_el = tr.select_one('a[href*="/players/"]')
        matchup_el = tr.select_one('a[href*="/matchup/"]')
        if not player_el:
            continue
        # Prefer the canonical full name from the player-slug href. Covers' display text
        # is often initial-style ("A. St. Brown", "J. Goff") which cannot be merged with
        # full names from other sources; the href slug contains the full name.
        player = player_el.get_text(" ", strip=True)
        href = player_el.get("href", "") or ""
        m_slug = re.search(r"/players/\d+/([a-z0-9. +'-]+)$", href, re.I)
        if m_slug:
            slug = re.sub(r"[^A-Za-z .'-]", " ", m_slug.group(1)).strip()
            slug = slug.replace("-", " ").replace("'", "")
            slug = re.sub(r"\s+", " ", slug).strip().title()
            if len(slug.split()) >= 2:
                player = slug
        matchup = ""
        if matchup_el:
            mt = matchup_el.get_text(" ", strip=True)
            if "@" in mt:
                p1, p2 = mt.split("@", 1)
                matchup = f"{normalize_team(p1)} @ {normalize_team(p2)}"
        if not matchup:
            # The main /player-props row text begins with the matchup, e.g.
            # "RECEIVING YARDS NE @ DET A. St. Brown (WR) o77.5 ...".
            m_mu = re.search(r"\b([A-Z]{2,4})\s+@\s+([A-Z]{2,4})\b", txt, re.I)
            if m_mu:
                a1, h1 = normalize_team(m_mu.group(1)), normalize_team(m_mu.group(2))
                if a1 and h1 and a1 != h1:
                    matchup = f"{a1} @ {h1}"

        ev_pct = None  # Covers only publishes a "DIFFERENCE %", not a real EV%.
        proj = float(m_proj.group(1)) if m_proj else None
        txt_up = txt.upper()
        has_specific_td = any(w in txt_up for w in ("PASSING", "RUSHING", "RECEIVING", "1ST", "FIRST", "LAST", "2+", "3+"))
        is_td = (
            ("ANYTIME TOUCHDOWN" in txt_up or "SCORE A TOUCHDOWN" in txt_up or (bool(m_proj and m_proj.group(2).upper() == "TDS")))
            and not has_specific_td
        )
        if is_td:
            side = "Yes"
            mkt = "Anytime TD"
            line_val = 0.5
        else:
            side = m_proj.group(2).title() if (m_proj and m_proj.group(2).upper() in ("OVER", "UNDER")) else "Over"
            m_line = re.search(r"\b([ou])(\d+(?:\.\d+)?)\s+([A-Za-z0-9+\s]+?)\s+[\d.]+\s+(?:OVER|UNDER|TDS)", txt, re.I)
            mkt = normalize_market(m_line.group(3)) if m_line else ""
            line_val = float(m_line.group(2)) if m_line else None

        if player and mkt:
            results.append({
                "source": "Covers",
                "player": player,
                "matchup": matchup,
                "market": mkt,
                "side": side,
                "line": line_val,
                "covers_projection": proj,
                "covers_ev_pct": ev_pct,
                "book_odds": book_odds,
            })

    return results


def _scrape_covers_live(slate_matchups: Set[str]) -> Dict[str, List[Dict]]:
    """Scrape Covers game lines + main player props + all current-week matchup prop pages."""
    game_lines, matchup_ids = scrape_covers_odds_and_matchups(slate_matchups)

    urls = ["https://www.covers.com/sport/football/nfl/player-props"] + [
        f"https://www.covers.com/sport/football/nfl/matchup/{mid}/picks" for mid in matchup_ids
    ]

    all_props: List[Dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_scrape_covers_player_props_page, u) for u in urls]
        for fut in as_completed(futs):
            try:
                all_props.extend(fut.result())
            except RateLimitError:
                for rem in futs:
                    rem.cancel()
                raise
            except Exception:
                pass

    return {"game_lines": game_lines, "player_props": all_props, "matchup_ids": matchup_ids}


def scrape_covers(slate_matchups: Set[str]) -> Dict[str, List[Dict]]:
    """Scrape Covers with a 10-minute disk cache & 403/429 cooldown fallback."""
    slate_key = hashlib.sha256("|".join(sorted(slate_matchups)).encode("utf-8")).hexdigest()[:12]
    res = fetch_with_cache(
        f"covers_{slate_key}",
        "covers.com",
        ttl_seconds=600.0,
        fetcher=lambda: _scrape_covers_live(slate_matchups),
        is_valid=lambda d: bool(d and (d.get("game_lines") or d.get("player_props"))),
        cooldown_minutes=20.0,
    )
    return res if isinstance(res, dict) else {"game_lines": [], "player_props": [], "matchup_ids": []}
