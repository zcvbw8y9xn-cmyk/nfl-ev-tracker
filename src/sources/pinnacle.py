"""Pinnacle Guest API scraper for sharp NFL game lines & player props (and NL-playable)."""
from __future__ import annotations

import re
from typing import Dict, List
from scrapling.fetchers import Fetcher

from ..normalize import normalize_market, normalize_team
from ..odds_math import american_to_decimal, devig_power, market_vig_pct, prob_to_american
from .cache_utils import RateLimitError, fetch_with_cache, pace_domain


def _extract_player_and_market(desc: str) -> tuple[str, str]:
    """Parse Pinnacle special description like 'Germie Bernard Total Receiving Yards'."""
    d = (desc or "").strip()
    # Common Pinnacle patterns: '<Player> Total <Stat>'
    m = re.match(r"^(.+?)\s+(Total\s+.+)$", d, re.I)
    if m:
        return m.group(1).strip(), normalize_market(m.group(2).strip())
    return d, normalize_market(d)


def scrape_pinnacle() -> Dict[str, List[Dict]]:
    """Fetch Pinnacle Guest API lines with a 2-minute cache & 403/429 cooldown fallback."""
    res = fetch_with_cache(
        "pinnacle_nfl",
        "pinnacle.com",
        ttl_seconds=120.0,
        fetcher=_scrape_pinnacle_live,
        is_valid=lambda d: bool(d and (d.get("game_lines") or d.get("player_props"))),
        cooldown_minutes=15.0,
    )
    return res if isinstance(res, dict) else {"game_lines": [], "player_props": []}


def _scrape_pinnacle_live() -> Dict[str, List[Dict]]:
    """Fetch NFL Week matchups and straight markets from Pinnacle Guest API."""
    m_resp = Fetcher.get(
        "https://guest.api.arcadia.pinnacle.com/0.1/leagues/889/matchups",
        timeout=20,
    )
    if getattr(m_resp, "status", 200) in (403, 429):
        raise RateLimitError("pinnacle.com", int(m_resp.status))
    matchups = m_resp.json()
    pace_domain("pinnacle.com", min_delay=0.4, jitter=0.25)
    mk_resp = Fetcher.get(
        "https://guest.api.arcadia.pinnacle.com/0.1/leagues/889/markets/straight",
        timeout=20,
    )
    if getattr(mk_resp, "status", 200) in (403, 429):
        raise RateLimitError("pinnacle.com", int(mk_resp.status))
    markets = mk_resp.json()

    by_id = {m["id"]: m for m in matchups}
    mk_by_mid: Dict[int, List[Dict]] = {}
    for mk in markets:
        mk_by_mid.setdefault(mk["matchupId"], []).append(mk)

    game_lines: List[Dict] = []
    player_props: List[Dict] = []

    for m in matchups:
        # 1. Full game lines (period == 0)
        if m.get("type") == "matchup" and not m.get("parentId"):
            parts = m.get("participants", [])
            away_p = next((p for p in parts if p.get("alignment") == "away"), {})
            home_p = next((p for p in parts if p.get("alignment") == "home"), {})
            away = normalize_team(away_p.get("name", ""))
            home = normalize_team(home_p.get("name", ""))
            if not away or not home:
                continue
            matchup = f"{away} @ {home}"

            for mk in mk_by_mid.get(m["id"], []):
                if mk.get("period") != 0:
                    continue
                mtype = mk.get("type")
                prices = mk.get("prices", [])
                if mtype == "moneyline" and len(prices) == 2:
                    p_away = next((x for x in prices if x.get("designation") == "away"), None)
                    p_home = next((x for x in prices if x.get("designation") == "home"), None)
                    if p_away and p_home:
                        d_a = american_to_decimal(p_away["price"])
                        d_h = american_to_decimal(p_home["price"])
                        fp_a, fp_h = devig_power(d_a, d_h)
                        vig = round(market_vig_pct(d_a, d_h), 2)
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Moneyline", "side": away,
                            "line": None, "american": p_away["price"], "decimal": round(d_a, 4),
                            "fair_prob": fp_a, "fair_american": prob_to_american(fp_a), "vig_pct": vig,
                        })
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Moneyline", "side": home,
                            "line": None, "american": p_home["price"], "decimal": round(d_h, 4),
                            "fair_prob": fp_h, "fair_american": prob_to_american(fp_h), "vig_pct": vig,
                        })
                elif mtype == "spread" and len(prices) == 2:
                    p_away = next((x for x in prices if x.get("designation") == "away"), None)
                    p_home = next((x for x in prices if x.get("designation") == "home"), None)
                    if p_away and p_home:
                        d_a = american_to_decimal(p_away["price"])
                        d_h = american_to_decimal(p_home["price"])
                        fp_a, fp_h = devig_power(d_a, d_h)
                        vig = round(market_vig_pct(d_a, d_h), 2)
                        is_alt = bool(mk.get("isAlternate"))
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Spread", "side": away,
                            "line": p_away.get("points"), "american": p_away["price"], "decimal": round(d_a, 4),
                            "fair_prob": fp_a, "fair_american": prob_to_american(fp_a), "vig_pct": vig, "is_alt": is_alt,
                        })
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Spread", "side": home,
                            "line": p_home.get("points"), "american": p_home["price"], "decimal": round(d_h, 4),
                            "fair_prob": fp_h, "fair_american": prob_to_american(fp_h), "vig_pct": vig, "is_alt": is_alt,
                        })
                elif mtype == "total" and len(prices) == 2:
                    p_o = next((x for x in prices if x.get("designation") == "over"), None)
                    p_u = next((x for x in prices if x.get("designation") == "under"), None)
                    if p_o and p_u:
                        d_o = american_to_decimal(p_o["price"])
                        d_u = american_to_decimal(p_u["price"])
                        fp_o, fp_u = devig_power(d_o, d_u)
                        vig = round(market_vig_pct(d_o, d_u), 2)
                        is_alt = bool(mk.get("isAlternate"))
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Total", "side": "Over",
                            "line": p_o.get("points"), "american": p_o["price"], "decimal": round(d_o, 4),
                            "fair_prob": fp_o, "fair_american": prob_to_american(fp_o), "vig_pct": vig, "is_alt": is_alt,
                        })
                        game_lines.append({
                            "source": "Pinnacle", "book": "Pinnacle", "matchup": matchup,
                            "away": away, "home": home, "market": "Total", "side": "Under",
                            "line": p_u.get("points"), "american": p_u["price"], "decimal": round(d_u, 4),
                            "fair_prob": fp_u, "fair_american": prob_to_american(fp_u), "vig_pct": vig, "is_alt": is_alt,
                        })

        # 2. Player props
        sp = m.get("special")
        if sp and sp.get("category") == "Player Props":
            parent = by_id.get(m.get("parentId"), {})
            pparts = parent.get("participants", [])
            away_p = next((p for p in pparts if p.get("alignment") == "away"), {})
            home_p = next((p for p in pparts if p.get("alignment") == "home"), {})
            away = normalize_team(away_p.get("name", ""))
            home = normalize_team(home_p.get("name", ""))
            matchup = f"{away} @ {home}" if away and home else ""
            player_name, market_name = _extract_player_and_market(sp.get("description", ""))

            pparts_map = {pt.get("id"): (pt.get("name") or "").lower() for pt in m.get("participants", [])}

            for mk in mk_by_mid.get(m["id"], []):
                prices = mk.get("prices", [])
                if len(prices) == 2:
                    # Pinnacle sends Over/Under for total-style props, but Yes/No for
                    # scorer-style props (Anytime/First/Last TD when offered). Match either
                    # designation (or participant name) so scorer markets are not dropped.
                    def _side_of(price, labels):
                        des = (price.get("designation") or "").lower()
                        if des in labels:
                            return True
                        pname = pparts_map.get(price.get("participantId"))  # noqa: B023 - used immediately
                        return bool(pname) and pname in labels

                    p_o = next(
                        (x for x in prices if _side_of(x, ("over", "yes"))),
                        None,
                    )
                    p_u = next(
                        (x for x in prices if _side_of(x, ("under", "no"))),
                        None,
                    )
                    if p_o and p_u and p_o.get("price") and p_u.get("price"):
                        d_o = american_to_decimal(p_o["price"])
                        d_u = american_to_decimal(p_u["price"])
                        fp_o, fp_u = devig_power(d_o, d_u)
                        vig = round(market_vig_pct(d_o, d_u), 2)
                        # Normalise scorer markets to the canonical Yes/No sides on a 0.5
                        # line that every downstream lookup (engine consensus index,
                        # _lookup_prop_fair, DFS/ALC buckets) expects for TD props.
                        is_td = market_name in ("Anytime TD", "1st TD", "Last TD")
                        side_o = "Yes" if is_td else "Over"
                        side_u = "No" if is_td else "Under"
                        line_o = 0.5 if is_td else p_o.get("points")
                        line_u = 0.5 if is_td else p_u.get("points")
                        player_props.append({
                            "source": "Pinnacle",
                            "book": "Pinnacle",
                            "player": player_name,
                            "matchup": matchup,
                            "market": market_name,
                            "side": side_o,
                            "line": line_o,
                            "american": p_o["price"],
                            "decimal": round(d_o, 4),
                            "fair_prob": fp_o,
                            "fair_american": prob_to_american(fp_o),
                            "vig_pct": vig,
                        })
                        player_props.append({
                            "source": "Pinnacle",
                            "book": "Pinnacle",
                            "player": player_name,
                            "matchup": matchup,
                            "market": market_name,
                            "side": side_u,
                            "line": line_u,
                            "american": p_u["price"],
                            "decimal": round(d_u, 4),
                            "fair_prob": fp_u,
                            "fair_american": prob_to_american(fp_u),
                            "vig_pct": vig,
                        })

    return {"game_lines": game_lines, "player_props": player_props}
