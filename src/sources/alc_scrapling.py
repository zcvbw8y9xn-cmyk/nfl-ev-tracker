"""Scrapling scraper for Atlantic Lottery Corporation (ALC) Proline & Stadium Bets.

Scrapes:
  - https://www.alc.ca/portal?action=GoGamesList&coupon=STADIUM-PLY (single-game ML, spreads + alts, totals)
  - https://www.alc.ca/portal?action=GoGamesList&coupon=PRO-LINE (game lines + Passing/Rushing/Receiving/Receptions/TD player props)
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from ..odds_math import decimal_to_american, market_vig_pct
from .cache_utils import RateLimitError, fetch_with_cache, pace_domain


def _parse_alc_line_item(text: str) -> Optional[Tuple[str, Optional[float], float]]:
    """Parse strings like 'V +2.5 (1.92)', 'O 41.0 (1.92)', 'V 2.14', 'O 215.5 (1.85)'."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or t.lower() == "none":
        return None
    # Pattern with line: e.g. "V +2.5 (1.92)" or "O 215.5 (1.85)"
    m = re.match(r"^([VHOU])\s*([+-]?\d+(?:\.\d+)?)\s*\((\d+\.\d+)\)$", t, re.I)
    if m:
        side = m.group(1).upper()
        line = float(m.group(2))
        dec = float(m.group(3))
        return side, line, dec
    # Moneyline pattern: e.g. "V 2.14" or "H 1.71"
    m2 = re.match(r"^([VH])\s*(\d+\.\d+)$", t, re.I)
    if m2:
        return m2.group(1).upper(), None, float(m2.group(2))
    return None


def _parse_teams_from_header(text: str) -> Tuple[str, str]:
    """Extract away and home abbreviations from 'Washington At New York G'."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    parts = re.split(r"\s+At\s+", t, flags=re.I)
    if len(parts) == 2:
        return normalize_team(parts[0]), normalize_team(parts[1])
    return "", ""


def scrape_alc() -> Dict[str, List[Dict]]:
    """Scrape ALC Stadium Bets and Proline with a 2-minute cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        "alc_proline",
        "alc.ca",
        ttl_seconds=120.0,
        fetcher=_scrape_alc_live,
        is_valid=lambda d: bool(d and (d.get("game_lines") or d.get("player_props") or d.get("fantasy_groups"))),
        cooldown_minutes=15.0,
        min_delay=0.5,
        jitter=0.35,
    )
    return res if isinstance(res, dict) else {"game_lines": [], "player_props": [], "fantasy_groups": []}


def _scrape_alc_live() -> Dict[str, List[Dict]]:
    """Scrape ALC Stadium Bets and Proline NFL game lines and player props via Scrapling."""
    game_lines: List[Dict] = []
    player_props: List[Dict] = []

    # 1. Scrape STADIUM-PLY for single-game lines (ML, Spreads + Alts, Totals)
    p_stad = Fetcher.get(
        "https://www.alc.ca/portal?action=GoGamesList&coupon=STADIUM-PLY",
        timeout=20,
    )
    if getattr(p_stad, "status", 200) in (403, 429):
        raise RateLimitError("alc.ca", int(p_stad.status))
    soup_stad = BeautifulSoup(p_stad.body.decode("utf-8", errors="ignore"), "html.parser")
    for panel in soup_stad.select("div.panel.panel-default"):
        title_el = panel.select_one("span.title")
        if not title_el or title_el.get_text(strip=True) != "FOOTBALL - NFL":
            continue
        for row in panel.select("section.proline-game-row"):
            teams_el = row.select_one("header .event-teams")
            if not teams_el:
                continue
            away, home = _parse_teams_from_header(teams_el.get_text(" ", strip=True))
            if not away or not home:
                continue
            matchup = f"{away} @ {home}"

            # Moneyline
            ml_items = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".moneyline-column .row-item")]
            ml_items = [x for x in ml_items if x is not None]
            v_ml = next((x for x in ml_items if x[0] == "V"), None)
            h_ml = next((x for x in ml_items if x[0] == "H"), None)
            if v_ml and h_ml:
                vig = market_vig_pct(v_ml[2], h_ml[2])
                game_lines.append({
                    "source": "ALC Stadium",
                    "book": "ALC Proline",
                    "matchup": matchup,
                    "away": away,
                    "home": home,
                    "market": "Moneyline",
                    "side": away,
                    "line": None,
                    "decimal": v_ml[2],
                    "american": decimal_to_american(v_ml[2]),
                    "opp_decimal": h_ml[2],
                    "opp_american": decimal_to_american(h_ml[2]),
                    "alc_vig_pct": round(vig, 2),
                })
                game_lines.append({
                    "source": "ALC Stadium",
                    "book": "ALC Proline",
                    "matchup": matchup,
                    "away": away,
                    "home": home,
                    "market": "Moneyline",
                    "side": home,
                    "line": None,
                    "decimal": h_ml[2],
                    "american": decimal_to_american(h_ml[2]),
                    "opp_decimal": v_ml[2],
                    "opp_american": decimal_to_american(v_ml[2]),
                    "alc_vig_pct": round(vig, 2),
                })

            # Spreads (all variants in dropdown)
            spread_opts = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".spread-column a.option")]
            spread_opts = [x for x in spread_opts if x is not None and x[1] is not None]
            # Fallback if no dropdown options
            if not spread_opts:
                spread_opts = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".spread-column .row-item")]
                spread_opts = [x for x in spread_opts if x is not None and x[1] is not None]

            v_spreads = {x[1]: x[2] for x in spread_opts if x[0] == "V"}
            h_spreads = {x[1]: x[2] for x in spread_opts if x[0] == "H"}
            for v_line, v_dec in v_spreads.items():
                h_line = round(-v_line, 1)
                h_dec = h_spreads.get(h_line)
                vig = round(market_vig_pct(v_dec, h_dec), 2) if h_dec else None
                game_lines.append({
                    "source": "ALC Stadium",
                    "book": "ALC Proline",
                    "matchup": matchup,
                    "away": away,
                    "home": home,
                    "market": "Spread",
                    "side": away,
                    "line": v_line,
                    "decimal": v_dec,
                    "american": decimal_to_american(v_dec),
                    "opp_decimal": h_dec,
                    "opp_american": decimal_to_american(h_dec) if h_dec else None,
                    "alc_vig_pct": vig,
                })
                if h_dec:
                    game_lines.append({
                        "source": "ALC Stadium",
                        "book": "ALC Proline",
                        "matchup": matchup,
                        "away": away,
                        "home": home,
                        "market": "Spread",
                        "side": home,
                        "line": h_line,
                        "decimal": h_dec,
                        "american": decimal_to_american(h_dec),
                        "opp_decimal": v_dec,
                        "opp_american": decimal_to_american(v_dec),
                        "alc_vig_pct": vig,
                    })

            # Totals (dropdown or direct)
            tot_opts = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".total-column a.option")]
            tot_opts = [x for x in tot_opts if x is not None and x[1] is not None]
            if not tot_opts:
                tot_opts = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".total-column .row-item")]
                tot_opts = [x for x in tot_opts if x is not None and x[1] is not None]
            o_tots = {x[1]: x[2] for x in tot_opts if x[0] == "O"}
            u_tots = {x[1]: x[2] for x in tot_opts if x[0] == "U"}
            for line, o_dec in o_tots.items():
                u_dec = u_tots.get(line)
                vig = round(market_vig_pct(o_dec, u_dec), 2) if u_dec else None
                game_lines.append({
                    "source": "ALC Stadium",
                    "book": "ALC Proline",
                    "matchup": matchup,
                    "away": away,
                    "home": home,
                    "market": "Total",
                    "side": "Over",
                    "line": line,
                    "decimal": o_dec,
                    "american": decimal_to_american(o_dec),
                    "opp_decimal": u_dec,
                    "opp_american": decimal_to_american(u_dec) if u_dec else None,
                    "alc_vig_pct": vig,
                })
                if u_dec:
                    game_lines.append({
                        "source": "ALC Stadium",
                        "book": "ALC Proline",
                        "matchup": matchup,
                        "away": away,
                        "home": home,
                        "market": "Total",
                        "side": "Under",
                        "line": line,
                        "decimal": u_dec,
                        "american": decimal_to_american(u_dec),
                        "opp_decimal": o_dec,
                        "opp_american": decimal_to_american(o_dec),
                        "alc_vig_pct": vig,
                    })

    # 2. Scrape PRO-LINE for NFL Player Props (Passing, Rushing, Receptions, Receiving, Anytime TD, 1st TD)
    pace_domain("alc.ca", min_delay=0.55, jitter=0.35)
    p_pro = Fetcher.get(
        "https://www.alc.ca/portal?action=GoGamesList&coupon=PRO-LINE",
        timeout=20,
    )
    if getattr(p_pro, "status", 200) in (403, 429):
        raise RateLimitError("alc.ca", int(p_pro.status))
    soup_pro = BeautifulSoup(p_pro.body.decode("utf-8", errors="ignore"), "html.parser")
    prop_panels = {
        "FTB-TTL PASSING YARDS",
        "FTB-TTL RUSHING YARDS",
        "FTB - TTL RECEPTIONS",
        "FTB-TTL RECEIVING YARDS",
        "FTB - TOUCHDOWN SCORERS",
        "FTB - 1ST TOUCHDOWN SCORER",
    }
    for panel in soup_pro.select("div.panel.panel-default"):
        title_el = panel.select_one("span.title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if title not in prop_panels:
            continue
        market_name = normalize_market(title)

        # Two-way O/U props
        for row in panel.select("section.proline-game-row"):
            spans = row.select(".game-title-column span")
            if not spans:
                continue
            player_name = spans[0].get_text(strip=True)
            tot_items = [_parse_alc_line_item(x.get_text(strip=True)) for x in row.select(".total-column .row-item")]
            tot_items = [x for x in tot_items if x is not None and x[1] is not None]
            o_item = next((x for x in tot_items if x[0] == "O"), None)
            u_item = next((x for x in tot_items if x[0] == "U"), None)
            if o_item and u_item:
                vig = round(market_vig_pct(o_item[2], u_item[2]), 2)
                player_props.append({
                    "source": "ALC Proline",
                    "book": "ALC Proline",
                    "player": player_name,
                    "matchup": "",
                    "market": market_name,
                    "side": "Over",
                    "line": o_item[1],
                    "decimal": o_item[2],
                    "american": decimal_to_american(o_item[2]),
                    "opp_decimal": u_item[2],
                    "opp_american": decimal_to_american(u_item[2]),
                    "alc_vig_pct": vig,
                })
                player_props.append({
                    "source": "ALC Proline",
                    "book": "ALC Proline",
                    "player": player_name,
                    "matchup": "",
                    "market": market_name,
                    "side": "Under",
                    "line": u_item[1],
                    "decimal": u_item[2],
                    "american": decimal_to_american(u_item[2]),
                    "opp_decimal": o_item[2],
                    "opp_american": decimal_to_american(o_item[2]),
                    "alc_vig_pct": vig,
                })

        # Outright TD Scorers
        for outright in panel.select("section.outright-game-row"):
            for sub in outright.select("div.panel.panel-default"):
                sub_title = sub.select_one("span.title")
                sub_txt = sub_title.get_text(" ", strip=True) if sub_title else ""
                matchup = ""
                if " - " in sub_txt:
                    away, home = _parse_teams_from_header(sub_txt.split(" - ")[-1])
                    if away and home:
                        matchup = f"{away} @ {home}"
                for item in sub.select(".outright-game-row-item"):
                    name_el = item.select_one(".name")
                    odds_el = item.select_one(".odds")
                    if not name_el or not odds_el:
                        continue
                    try:
                        dec = float(odds_el.get_text(strip=True))
                    except ValueError:
                        continue
                    player_props.append({
                        "source": "ALC Proline",
                        "book": "ALC Proline",
                        "player": name_el.get_text(strip=True),
                        "matchup": matchup,
                        "market": market_name,
                        "side": "Yes",
                        "line": 0.5,
                        "decimal": dec,
                        "american": decimal_to_american(dec),
                        "opp_decimal": None,
                        "opp_american": None,
                        "alc_vig_pct": None,
                    })

    fantasy_groups = _scrape_alc_fantasy()
    return {"game_lines": game_lines, "player_props": player_props, "fantasy_groups": fantasy_groups}


def _scrape_alc_fantasy() -> List[Dict]:
    """Scrape ALC Proline Fantasy fixed-odds head-to-head groups from GoGamesList?coupon=FANTASY."""
    groups: List[Dict] = []
    try:
        pace_domain("alc.ca", min_delay=0.55, jitter=0.35)
        resp = Fetcher.get(
            "https://www.alc.ca/portal?action=GoGamesList&coupon=FANTASY",
            timeout=20,
        )
        if getattr(resp, "status", 200) in (403, 429):
            raise RateLimitError("alc.ca", int(resp.status))
        if resp.status != 200:
            return []
        html = resp.body.decode("utf-8", errors="ignore")
    except RateLimitError:
        raise
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen_groups = set()
    # Top-level panels carry sport headings ('FOOTBALL - ...', 'HOCKEY - SKATERS', 'GOLFERS')
    for sport_panel in soup.select("div.panel.panel-default"):
        # Skip nested group panels here by checking direct panel-heading
        heading = sport_panel.find("div", class_="panel-heading", recursive=False)
        if not heading:
            continue
        title_el = heading.select_one("span.title")
        sport_raw = title_el.get_text(" ", strip=True) if title_el else ""
        if not sport_raw or sport_raw.lower().startswith("group "):
            continue

        sport_upper = sport_raw.upper()
        is_nfl = ("FOOTBALL" in sport_upper or "NFL" in sport_upper) and "SOCCER" not in sport_upper
        sport_label = "NFL Fantasy" if is_nfl else sport_raw.title()

        for sub_panel in sport_panel.select("section.outright-game-row div.panel.panel-default"):
            sub_head = sub_panel.find("div", class_="panel-heading", recursive=False)
            sub_title_el = sub_head.select_one("span.title") if sub_head else None
            group_title = sub_title_el.get_text(" ", strip=True) if sub_title_el else ""
            if not group_title:
                continue

            m_gid = re.search(r"Group\s+(\d+)", group_title, re.I)
            group_id = m_gid.group(1) if m_gid else group_title
            kickoff = group_title.split(" - ", 1)[1].strip() if " - " in group_title else ""
            dedup_key = (sport_raw, group_title)
            if dedup_key in seen_groups:
                continue
            seen_groups.add(dedup_key)

            entries: List[Dict] = []
            for item in sub_panel.select(".outright-game-row-item"):
                slot_el = item.select_one(".id")
                name_el = item.select_one(".name")
                odds_el = item.select_one(".odds")
                if not name_el or not odds_el:
                    continue
                try:
                    dec = float(odds_el.get_text(strip=True))
                except ValueError:
                    continue
                if dec <= 1.0:
                    continue
                entries.append({
                    "slot": slot_el.get_text(strip=True) if slot_el else "",
                    "player": re.sub(r"\s+", " ", name_el.get_text(" ", strip=True)),
                    "decimal": round(dec, 2),
                    "american": decimal_to_american(dec),
                    "implied_prob": round(100.0 / dec, 2),
                })

            if len(entries) < 2:
                continue
            sum_imp = sum(1.0 / e["decimal"] for e in entries)
            vig_pct = round((sum_imp - 1.0) * 100.0, 2)
            for e in entries:
                e["no_vig_prob"] = round((1.0 / e["decimal"]) / sum_imp * 100.0, 2)

            groups.append({
                "sport": sport_label,
                "sport_raw": sport_raw,
                "is_nfl": is_nfl,
                "group_id": group_id,
                "group_title": group_title,
                "kickoff": kickoff,
                "vig_pct": vig_pct,
                "entries": entries,
            })

    return groups
