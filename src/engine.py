"""Core +EV aggregation engine combining all scraped & API sources."""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Dict, List, Optional, Tuple

from .normalize import (
    CONSENSUS_BOOKS,
    DFS_BOOKS,
    NL_BOOKS,
    SHARP_BENCHMARK_BOOKS,
    normalize_player_name,
    player_match_key,
)
from .odds_math import (
    american_to_decimal,
    compute_ev_pct,
    devig_power,
    market_vig_pct,
    prob_to_american,
)

SHARP_SET = set(SHARP_BENCHMARK_BOOKS)
CONSENSUS_SET = set(CONSENSUS_BOOKS)
SHARP_WEIGHTS = {
    "Pinnacle": 2.0,
    "Novig": 1.6,
    "ProphetX": 1.5,
    "BookMaker.eu": 1.4,
    "LowVig.ag": 1.3,
    "BetOnline.ag": 1.2,
    "Betfair Exchange": 1.3,
    "SX Bet": 1.1,
    "Kalshi": 1.0,
    "Polymarket": 1.0,
}

# Standard deviations used to translate a sharp main line to offset lines & market-implied means
PROP_STD_DEV = {
    "Passing Yards": 42.0,
    "Pass + Rush Yards": 44.0,
    "Rush + Rec Yards": 28.0,
    "Receiving Yards": 24.0,
    "Rushing Yards": 22.0,
    "Pass Attempts": 5.5,
    "Pass Completions": 4.2,
    "Receptions": 1.75,
    "Passing TDs": 0.85,
    "Interceptions": 0.65,
    "Rush Attempts": 4.2,
    "Kicking Points": 3.8,
}

# Positional weekly Fantasy Score standard deviations
FANTASY_STD_DEV = {
    "QB": 7.4,
    "RB": 6.6,
    "WR": 6.5,
    "TE": 5.4,
    "K": 3.8,
    "DEF": 5.5,
}


def _alc_group_win_probs(mu_sigma: List[Tuple[float, float]]) -> List[float]:
    """Deterministic normal-grid quadrature for head-to-head ALC Fantasy group win probs."""
    if not mu_sigma:
        return []
    z_grid = [-4.0 + 0.2 * i for i in range(41)]
    weights = [math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi) * 0.2 for z in z_grid]
    probs: List[float] = []
    for i, (mu_i, sig_i) in enumerate(mu_sigma):
        s_i = max(float(sig_i), 1.0)
        p_win = 0.0
        for z, w in zip(z_grid, weights):
            x = mu_i + s_i * z
            prod_other = 1.0
            for j, (mu_j, sig_j) in enumerate(mu_sigma):
                if j == i:
                    continue
                prod_other *= _norm_cdf((x - mu_j) / max(float(sig_j), 1.0))
            p_win += w * prod_other
        probs.append(max(p_win, 1e-6))
    tot = sum(probs) or 1.0
    return [p / tot for p in probs]


def _sort_num(value: Optional[float], default: float) -> float:
    """Return ``value`` if it is a real number, else ``default``.

    Unlike ``value or default`` this keeps a legitimate ``0.0`` (which is falsy in
    Python) instead of replacing it with the fallback, so exact-zero EV rows sort
    where they belong instead of being pushed to the bottom.
    """
    return float(value) if value is not None else float(default)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam inverse standard normal CDF approximation for p in (0, 1)."""
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def _shift_prop_prob(ref_prob: float, ref_line: float, target_line: float, side: str, sigma: float) -> float:
    """Shift a sharp main-line fair probability to an offset ALC line via normal approximation."""
    if not sigma or sigma <= 0 or ref_prob <= 0 or ref_prob >= 1:
        return min(max(ref_prob, 0.05), 0.95)
    z0 = _norm_ppf(min(max(ref_prob, 0.05), 0.95))
    delta = (target_line - ref_line) / sigma
    z_new = (z0 + delta) if side == "Under" else (z0 - delta)
    return min(max(_norm_cdf(z_new), 0.05), 0.95)


def _tier_from_label(ev_method: str) -> str:
    """Classify the devig/fair-prob source tier from the human-readable ev_method label.

    Returns one of:
      "sharp"       - derived from sharp books (Pinnacle / sharp exchanges)
      "market"      - derived from a soft-book market consensus
      "model"       - derived from a projection model (BettingPros fair prob, etc.)
      ""            - unknown / unavailable (no genuine fair probability)
    This makes the consensus tier machine-readable for debugging & logging.
    """
    m = (ev_method or "").lower()
    if not m:
        return ""
    if m.startswith("sharp") or "pinnacle no-vig" in m or "line-shifted sharp" in m:
        return "sharp"
    if m.startswith("market consensus") or "line-shifted consensus" in m:
        return "market"
    if "bettingpros" in m or "fair prob" in m:
        return "model"
    return ""


def _weighted_consensus(book_probs: Dict[str, float]) -> Tuple[Optional[float], List[str], List[str]]:
    """Compute weighted sharp-first or broad market fair prob from `{book: devigged_prob}`."""
    if not book_probs:
        return None, [], []
    sharp_items = [(b, p) for b, p in book_probs.items() if b in SHARP_SET]
    if sharp_items:
        num = sum(p * SHARP_WEIGHTS.get(b, 1.0) for b, p in sharp_items)
        den = sum(SHARP_WEIGHTS.get(b, 1.0) for b, _ in sharp_items)
        books_sorted = sorted((b for b, _ in sharp_items), key=lambda x: -SHARP_WEIGHTS.get(x, 1.0))
        return num / den, books_sorted, ["sharp"]
    if len(book_probs) >= 2:
        vals = sorted(book_probs.values())
        avg = sum(vals) / len(vals)
        return avg, sorted(book_probs.keys()), ["market"]
    return None, [], []


def _format_pick_label(market: str, side: str, line: Optional[float]) -> str:
    if market in ("Anytime TD", "1st TD", "Last TD"):
        return f"{side}"
    if line is not None:
        if market == "Spread":
            sign = "+" if line > 0 else ""
            return f"{side} {sign}{line:g}"
        if market == "Total":
            return f"Game {side} {line:g}"
        if market == "Team Total" and " " in side:
            tm, ou = side.split(" ", 1)
            return f"{tm} Team {ou} {line:g}"
        return f"{side} {line:g}"
    return f"{side} ML" if market == "Moneyline" else side


def _match_player_key(name: str) -> Tuple[str, str]:
    return normalize_player_name(name), player_match_key(name)


def build_ev_datasets(
    espn_games: List[Dict],
    alc_data: Dict,
    pinnacle_data: Dict,
    action_data: Dict,
    bp_props: List[Dict],
    covers_data: Dict,
    sao_props: List[Dict],
    odds_api_data: Dict,
    underdog_props: Optional[List[Dict]] = None,
    prizepicks_props: Optional[List[Dict]] = None,
    sleeper_data: Optional[Dict] = None,
    propline_data: Optional[Dict] = None,
    espn_fantasy_projs: Optional[Dict[Tuple, float]] = None,
) -> Dict:
    """Merge all sources into unified Player Props, Game Lines, ALC Scanner, DFS Pick'em, and Slate datasets."""
    sleeper_data = sleeper_data or {}
    propline_data = propline_data or {}
    espn_projs = espn_fantasy_projs or {}
    sleeper_projs = sleeper_data.get("sleeper_projections", {})
    sleeper_stats = sleeper_data.get("sleeper_stats", {}) or {}
    rw_projs = sleeper_data.get("rw_projections", {})
    weather_map = sleeper_data.get("weather", {})
    for g in espn_games:
        if weather_map.get(g["matchup"]) and not g.get("weather"):
            g["weather"] = weather_map[g["matchup"]]

    game_meta = {g["matchup"]: g for g in espn_games}
    splits_map = action_data.get("splits", {})

    # ------------------------------------------------------------------
    # 1. UNIFY GAME LINES + BUILD SHARP/CONSENSUS GAME FAIR INDEX
    # Key: (matchup, market, side, line_str)
    # ------------------------------------------------------------------
    all_game_rows: List[Dict] = []
    all_game_rows.extend(alc_data.get("game_lines", []))
    all_game_rows.extend(pinnacle_data.get("game_lines", []))
    all_game_rows.extend(action_data.get("game_lines", []))
    all_game_rows.extend(covers_data.get("game_lines", []))
    all_game_rows.extend(odds_api_data.get("game_lines", []))
    all_game_rows.extend(odds_api_data.get("benchmark_game_lines", []))
    all_game_rows.extend(propline_data.get("game_lines", []))
    all_game_rows.extend(propline_data.get("benchmark_game_lines", []))

    # Collect per-book 2-way markets to compute sharp/market devigged fair probabilities
    game_two_way: Dict[Tuple, List[Dict]] = defaultdict(list)
    for row in all_game_rows:
        matchup = row.get("matchup", "")
        if game_meta and matchup not in game_meta:
            continue
        book = row.get("book", "")
        if book not in CONSENSUS_SET and book not in NL_BOOKS:
            continue
        market = row.get("market", "")
        line = row.get("line")
        line_key = round(float(line), 1) if line is not None else None
        dec = row.get("decimal")
        if not dec or float(dec) <= 1.01:
            continue
        if market == "Moneyline":
            g2k = (matchup, market, None, book)
        elif market == "Total" and line_key is not None and row.get("side") in ("Over", "Under"):
            g2k = (matchup, market, line_key, book)
        elif market == "Team Total" and line_key is not None and " " in row.get("side", ""):
            team_prefix = row["side"].split(" ", 1)[0]
            g2k = (matchup, market, (team_prefix, line_key), book)
        elif market == "Spread" and line_key is not None:
            g2k = (matchup, market, round(abs(line_key), 1), book)
        else:
            continue
        game_two_way[g2k].append({
            "side": row.get("side", ""),
            "line": line_key,
            "decimal": float(dec),
        })

    game_cons_probs: Dict[Tuple, Dict[str, float]] = defaultdict(dict)
    for (matchup, market, _, book), items in game_two_way.items():
        by_side: Dict[Tuple, float] = {}
        for it in items:
            sk = (it["side"], it["line"])
            if sk not in by_side or it["decimal"] > by_side[sk]:
                by_side[sk] = it["decimal"]
        if len(by_side) == 2:
            (s1, l1), (s2, l2) = list(by_side.keys())
            if market == "Spread" and (l1 or 0) + (l2 or 0) != 0:
                continue
            if s1 == s2:
                continue
            vig = market_vig_pct(by_side[(s1, l1)], by_side[(s2, l2)])
            if not (-1.0 <= vig <= 12.0):
                continue
            p1, p2 = devig_power(by_side[(s1, l1)], by_side[(s2, l2)])
            game_cons_probs[(matchup, market, s1, l1)][book] = p1
            game_cons_probs[(matchup, market, s2, l2)][book] = p2

    game_groups: Dict[Tuple, Dict] = {}
    for row in all_game_rows:
        matchup = row.get("matchup", "")
        if game_meta and matchup not in game_meta:
            continue
        market = row.get("market", "")
        side = row.get("side", "")
        line = row.get("line")
        line_key = round(float(line), 1) if line is not None else None
        if market == "Total" and (line_key is None or line_key < 30.0 or side not in ("Over", "Under")):
            continue
        key = (matchup, market, side, line_key)

        g = game_groups.setdefault(key, {
            "matchup": matchup,
            "kickoff_nl": game_meta.get(matchup, {}).get("kickoff_nl", ""),
            "market": market,
            "side": side,
            "line": line_key,
            "pick": _format_pick_label(market, side, line_key),
            "books": {},
            "pinnacle_fair_prob": None,
            "pinnacle_fair_american": None,
            "pinnacle_vig_pct": None,
            "alc_vig_pct": None,
            "is_alt": False,
        })

        book = row.get("book", "")
        am = row.get("american")
        dec = row.get("decimal")
        if book in NL_BOOKS and am is not None and dec is not None:
            # Keep best price if multiple feeds report same book (e.g. Action & Covers)
            existing = g["books"].get(book)
            if not existing or dec > existing["decimal"]:
                g["books"][book] = {"american": int(am), "decimal": float(dec)}

        if row.get("source") == "Pinnacle" and row.get("fair_prob") is not None:
            g["pinnacle_fair_prob"] = float(row["fair_prob"])
            g["pinnacle_fair_american"] = row.get("fair_american")
            g["pinnacle_vig_pct"] = row.get("vig_pct")
            g["is_alt"] = bool(row.get("is_alt", False))

        if row.get("source") == "ALC Stadium" and row.get("alc_vig_pct") is not None:
            g["alc_vig_pct"] = row.get("alc_vig_pct")

    game_ev_rows: List[Dict] = []
    alc_scanner_rows: List[Dict] = []

    for key, g in game_groups.items():
        if not g["books"]:
            continue
        best_book, best_info = max(g["books"].items(), key=lambda kv: kv[1]["decimal"])
        fair_prob = g["pinnacle_fair_prob"]
        fair_am = g["pinnacle_fair_american"]
        if fair_prob is None:
            cons_p, _, _ = _weighted_consensus(game_cons_probs.get(key, {}))
            if cons_p is not None:
                fair_prob = cons_p
                fair_am = prob_to_american(cons_p)

        ev_pct = compute_ev_pct(fair_prob, best_info["decimal"]) if fair_prob else None

        sp = splits_map.get(g["matchup"], {})
        tix_pct, money_pct = None, None
        if g["market"] == "Spread":
            away_team = g["matchup"].split(" @ ")[0]
            if g["side"] == away_team and sp.get("away_spread_tickets_pct") is not None:
                tix_pct = sp["away_spread_tickets_pct"]
                money_pct = sp["away_spread_money_pct"]
            elif sp.get("away_spread_tickets_pct") is not None:
                tix_pct = 100 - sp["away_spread_tickets_pct"]
                money_pct = 100 - sp["away_spread_money_pct"]
        elif g["market"] == "Moneyline":
            away_team = g["matchup"].split(" @ ")[0]
            if g["side"] == away_team and sp.get("away_ml_tickets_pct") is not None:
                tix_pct = sp["away_ml_tickets_pct"]
                money_pct = sp["away_ml_money_pct"]
            elif sp.get("away_ml_tickets_pct") is not None:
                tix_pct = 100 - sp["away_ml_tickets_pct"]
                money_pct = 100 - sp["away_ml_money_pct"]
        elif g["market"] == "Total":
            if g["side"] == "Over" and sp.get("over_tickets_pct") is not None:
                tix_pct = sp["over_tickets_pct"]
                money_pct = sp["over_money_pct"]
            elif sp.get("over_tickets_pct") is not None:
                tix_pct = 100 - sp["over_tickets_pct"]
                money_pct = 100 - sp["over_money_pct"]

        sharp_sig = ""
        if tix_pct is not None and money_pct is not None:
            diff = money_pct - tix_pct
            if diff >= 15:
                sharp_sig = f"Sharp Money (+{diff}%)"
            elif diff <= -15:
                sharp_sig = f"Public Heavy ({diff}%)"

        display_market = "Game Total" if g["market"] == "Total" else g["market"]
        row_out = {
            "kickoff_nl": g["kickoff_nl"],
            "matchup": g["matchup"],
            "market": display_market,
            "line": g["line"],
            "pick": g["pick"],
            "best_book": best_book,
            "best_american": best_info["american"],
            "best_decimal": round(best_info["decimal"], 3),
            "fair_american": fair_am,
            "fair_prob": round(fair_prob * 100, 2) if fair_prob else None,
            "ev_pct": round(ev_pct, 2) if ev_pct is not None else None,
            "books": {b: g["books"][b]["american"] if b in g["books"] else None for b in NL_BOOKS},
            "tix_pct": tix_pct,
            "money_pct": money_pct,
            "sharp_signal": sharp_sig,
            "is_alt": g["is_alt"],
        }
        if ev_pct is not None:
            game_ev_rows.append(row_out)

        # ALC Vig & Mispricing Scanner entry for every ALC line
        if "ALC Proline" in g["books"]:
            alc_info = g["books"]["ALC Proline"]
            alc_ev = compute_ev_pct(fair_prob, alc_info["decimal"]) if fair_prob else None
            alc_scanner_rows.append({
                "kickoff_nl": g["kickoff_nl"],
                "category": "Game Line",
                "matchup": g["matchup"],
                "player": "-",
                "market": display_market,
                "pick": g["pick"],
                "alc_american": alc_info["american"],
                "alc_decimal": round(alc_info["decimal"], 3),
                "alc_vig_pct": g["alc_vig_pct"],
                "pin_american": g["books"].get("Pinnacle", {}).get("american"),
                "fair_american": fair_am,
                "fair_prob": round(fair_prob * 100, 2) if fair_prob else None,
                "best_other_book": max(
                    ((b, v) for b, v in g["books"].items() if b != "ALC Proline"),
                    key=lambda kv: kv[1]["decimal"],
                    default=(None, None),
                )[0],
                "best_other_american": max(
                    ((b, v) for b, v in g["books"].items() if b != "ALC Proline"),
                    key=lambda kv: kv[1]["decimal"],
                    default=(None, {}),
                )[1].get("american"),
                "alc_ev_pct": round(alc_ev, 2) if alc_ev is not None else None,
            })

    game_ev_rows.sort(key=lambda r: (r["ev_pct"] is None, -_sort_num(r["ev_pct"], -999.0)))

    # ------------------------------------------------------------------
    # 2. UNIFY PLAYER PROPS ACROSS ALL SOURCES
    # Match primarily by (player_match_key, market, side, line)
    # ------------------------------------------------------------------
    # Build matchup-scoped & unique-global initial resolver so 'J. Goff' merges with 'Jared Goff'
    # without ever colliding 'Kyren Williams' with 'Kyle Williams' or 'Javonte' with 'Jameson'.
    init_matchup_map: Dict[Tuple[str, str], set] = defaultdict(set)
    init_global_map: Dict[str, set] = defaultdict(set)

    def _register_full_name(name: str, matchup: str = "") -> None:
        if not name:
            return
        pk = player_match_key(name)
        if "_" not in pk:
            return
        first, last = pk.split("_", 1)
        if len(first) <= 1:
            return
        ik = f"{first[0]}_{last}"
        init_global_map[ik].add(pk)
        if matchup:
            init_matchup_map[(matchup, ik)].add(pk)

    def _resolve_pkey(name: str, matchup: str = "") -> str:
        pk = player_match_key(name)
        if "_" in pk:
            first, last = pk.split("_", 1)
            if len(first) == 1:
                if matchup:
                    cands = init_matchup_map.get((matchup, pk))
                    if cands and len(cands) == 1:
                        return next(iter(cands))
                gcands = init_global_map.get(pk)
                if gcands and len(gcands) == 1:
                    return next(iter(gcands))
        return pk

    for src_list in (
        bp_props,
        sao_props,
        pinnacle_data.get("player_props", []),
        covers_data.get("player_props", []),
        action_data.get("player_props", []),
        odds_api_data.get("player_props", []),
        odds_api_data.get("benchmark_props", []),
        propline_data.get("player_props", []),
        propline_data.get("benchmark_props", []),
        propline_data.get("dfs_props", []),
        sleeper_data.get("sleeper_lines", []),
        prizepicks_props or [],
        underdog_props or [],
    ):
        for item in src_list:
            _register_full_name(item.get("player", ""), item.get("matchup", ""))

    # Index BettingPros by (pkey, market, side, line) AND keep player->(matchup, pos, injury) map
    player_meta: Dict[str, Dict] = {}
    bp_by_exact: Dict[Tuple, Dict] = {}
    bp_by_player_mkt: Dict[Tuple, List[Dict]] = defaultdict(list)

    for b in bp_props:
        pkey = _resolve_pkey(b["player"], b.get("matchup", ""))
        player_meta.setdefault(pkey, {
            "name": b["player"],
            "position": b.get("position", ""),
            "team": b.get("team", ""),
            "injury": b.get("injury", ""),
            "matchup": b.get("matchup", ""),
        })
        exact_k = (pkey, b["market"], b["side"], round(float(b["line"]), 1))
        bp_by_exact[exact_k] = b
        bp_by_player_mkt[(pkey, b["market"], b["side"])].append(b)

    # Index ScoresAndOdds
    sao_by_exact: Dict[Tuple, Dict] = {}
    sao_by_player_mkt: Dict[Tuple, Dict] = {}
    for s in sao_props:
        pkey = _resolve_pkey(s["player"], s.get("matchup", ""))
        if s.get("line") is not None:
            sao_by_exact[(pkey, s["market"], s["side"], round(float(s["line"]), 1))] = s
        sao_by_player_mkt[(pkey, s["market"], s["side"])] = s

    # Build unified prop buckets from Pinnacle, ALC, Covers, BettingPros, Action, OddsAPI
    prop_buckets: Dict[Tuple, Dict] = {}

    def _get_bucket(player: str, matchup: str, market: str, side: str, line: Optional[float]) -> Dict:
        pkey = _resolve_pkey(player, matchup)
        lkey = round(float(line), 1) if line is not None else None
        bkey = (pkey, market, side, lkey)
        meta = player_meta.get(pkey, {})
        resolved_matchup = matchup or meta.get("matchup", "")
        if bkey not in prop_buckets:
            prop_buckets[bkey] = {
                "pkey": pkey,
                "player": meta.get("name") or player,
                "position": meta.get("position", ""),
                "injury": meta.get("injury", ""),
                "matchup": resolved_matchup,
                "kickoff_nl": game_meta.get(resolved_matchup, {}).get("kickoff_nl", ""),
                "market": market,
                "side": side,
                "line": lkey,
                "pick": _format_pick_label(market, side, lkey),
                "books": {},
                "pinnacle_fair_prob": None,
                "pinnacle_fair_american": None,
                "alc_vig_pct": None,
                "covers_proj": None,
                "covers_ev_pct": None,
                "bp_proj": None,
                "bp_ev_pct": None,
                "bp_fair_odds": None,
                "bp_prob": None,
                "bp_rating": None,
                "sao_proj": None,
                "sleeper_proj": None,
                "espn_proj": None,
                "rw_proj": None,
                "opp_dvp_rank": None,
                "l5_hit_pct": None,
                "l10_hit_pct": None,
                "season_hit_pct": None,
                "sources": set(),
            }
        else:
            if not prop_buckets[bkey]["matchup"] and resolved_matchup:
                prop_buckets[bkey]["matchup"] = resolved_matchup
                prop_buckets[bkey]["kickoff_nl"] = game_meta.get(resolved_matchup, {}).get("kickoff_nl", "")
        return prop_buckets[bkey]

    # A. Pinnacle props
    for pr in pinnacle_data.get("player_props", []):
        bk = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], pr.get("line"))
        bk["books"]["Pinnacle"] = {"american": int(pr["american"]), "decimal": float(pr["decimal"])}
        bk["pinnacle_fair_prob"] = pr.get("fair_prob")
        bk["pinnacle_fair_american"] = pr.get("fair_american")
        bk["sources"].add("Pinnacle")

    # B. ALC props
    for pr in alc_data.get("player_props", []):
        bk = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], pr.get("line"))
        bk["books"]["ALC Proline"] = {"american": int(pr["american"]), "decimal": float(pr["decimal"])}
        if pr.get("alc_vig_pct") is not None:
            bk["alc_vig_pct"] = pr.get("alc_vig_pct")
        bk["sources"].add("ALC Proline")

    # C. Covers props (contains Bet365, SIA, BET99, Betway)
    for pr in covers_data.get("player_props", []):
        bk = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], pr.get("line"))
        if pr.get("covers_projection") is not None:
            bk["covers_proj"] = pr["covers_projection"]
        if pr.get("covers_ev_pct") is not None:
            bk["covers_ev_pct"] = pr["covers_ev_pct"]
        for book_name, (b_line, b_am) in pr.get("book_odds", {}).items():
            if book_name in NL_BOOKS and b_am is not None:
                # Only attach book price if line matches bucket line (or TD market)
                if b_line is None or bk["line"] is None or abs(float(b_line) - float(bk["line"])) < 0.01:
                    dec = round(american_to_decimal(b_am), 4)
                    prev = bk["books"].get(book_name)
                    if not prev or dec > prev["decimal"]:
                        bk["books"][book_name] = {"american": int(b_am), "decimal": dec}
                elif b_line is not None and bk["line"] is not None and abs(float(b_line) - float(bk["line"])) >= 0.01:
                    # Also create separate line bucket for that book's actual alt/exact line!
                    bk2 = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], b_line)
                    dec = round(american_to_decimal(b_am), 4)
                    prev2 = bk2["books"].get(book_name)
                    if not prev2 or dec > prev2["decimal"]:
                        bk2["books"][book_name] = {"american": int(b_am), "decimal": dec}
                    if pr.get("covers_projection") is not None:
                        bk2["covers_proj"] = pr["covers_projection"]
                    bk2["sources"].add("Covers")
        bk["sources"].add("Covers")

    # D. Action Network & Optional APIs
    for pr in action_data.get("player_props", []):
        bk = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], pr.get("line"))
        bk["sources"].add("Action Network")
    for pr in list(odds_api_data.get("player_props", [])) + list(propline_data.get("player_props", [])):
        bk = _get_bucket(pr["player"], pr.get("matchup", ""), pr["market"], pr["side"], pr.get("line"))
        book_name = pr.get("book", "")
        if book_name in NL_BOOKS:
            dec = float(pr["decimal"])
            prev = bk["books"].get(book_name)
            if not prev or dec > prev["decimal"]:
                bk["books"][book_name] = {"american": int(pr["american"]), "decimal": dec}
        bk["sources"].add(pr.get("source") or "The Odds API")

    # Build multi-book sharp/consensus two-way devig index across ALL prop sources (Pinnacle, Novig, ProphetX, BookMaker, DK, FD, BetMGM, etc.)
    prop_two_way: Dict[Tuple, Dict[str, float]] = defaultdict(dict)
    pin_direct_probs: Dict[Tuple, float] = {}
    bench_line_refs: Dict[Tuple, List[Dict]] = defaultdict(list)

    def _index_two_way_row(player_name: str, mkt: str, side: str, line_val: Optional[float], book_name: str, am_val: Optional[int], dec_val: Optional[float], fair_p: Optional[float] = None, matchup: str = "") -> None:
        if not player_name or not mkt or not side or not book_name:
            return
        if book_name not in CONSENSUS_SET and book_name not in NL_BOOKS:
            return
        pkey = _resolve_pkey(player_name, matchup)
        lkey = round(float(line_val), 1) if line_val is not None else None
        if dec_val and float(dec_val) > 1.01 and lkey is not None:
            cur = prop_two_way[(pkey, mkt, lkey, book_name)].get(side)
            if cur is None or float(dec_val) > cur:
                prop_two_way[(pkey, mkt, lkey, book_name)][side] = float(dec_val)
        if book_name == "Pinnacle" and fair_p is not None and lkey is not None:
            pin_direct_probs[(pkey, mkt, side, lkey)] = float(fair_p)
        if book_name in SHARP_SET or book_name in ("DraftKings", "FanDuel", "BetMGM", "Caesars", "BET99", "Betway"):
            bench_line_refs[(pkey, mkt, side)].append({
                "book": book_name,
                "line": lkey,
                "american": am_val,
                "decimal": dec_val,
                "fair_prob": fair_p,
                "is_pin": book_name == "Pinnacle",
                "weight": SHARP_WEIGHTS.get(book_name, 0.75),
            })

    for pr in pinnacle_data.get("player_props", []):
        _index_two_way_row(pr.get("player", ""), pr.get("market", ""), pr.get("side", ""), pr.get("line"), "Pinnacle", pr.get("american"), pr.get("decimal"), pr.get("fair_prob"), pr.get("matchup", ""))

    for pr in (
        list(odds_api_data.get("player_props", []))
        + list(odds_api_data.get("benchmark_props", []))
        + list(propline_data.get("player_props", []))
        + list(propline_data.get("benchmark_props", []))
    ):
        _index_two_way_row(pr.get("player", ""), pr.get("market", ""), pr.get("side", ""), pr.get("line"), pr.get("book", ""), pr.get("american"), pr.get("decimal"), matchup=pr.get("matchup", ""))

    for pr in covers_data.get("player_props", []):
        for book_name, (b_line, b_am) in pr.get("book_odds", {}).items():
            if b_am is not None:
                use_line = b_line if b_line is not None else pr.get("line")
                _index_two_way_row(pr.get("player", ""), pr.get("market", ""), pr.get("side", ""), use_line, book_name, int(b_am), round(american_to_decimal(int(b_am)), 4), matchup=pr.get("matchup", ""))

    prop_cons_probs: Dict[Tuple, Dict[str, float]] = defaultdict(dict)
    for (pkey, mkt, lkey, book_name), sides_map in prop_two_way.items():
        if "Over" in sides_map and "Under" in sides_map:
            vig = market_vig_pct(sides_map["Over"], sides_map["Under"])
            if not (-1.0 <= vig <= 10.5):
                continue
            p_over, p_under = devig_power(sides_map["Over"], sides_map["Under"])
            prop_cons_probs[(pkey, mkt, "Over", lkey)][book_name] = p_over
            prop_cons_probs[(pkey, mkt, "Under", lkey)][book_name] = p_under
        elif "Yes" in sides_map and "No" in sides_map:
            vig = market_vig_pct(sides_map["Yes"], sides_map["No"])
            if not (-1.0 <= vig <= 12.0):
                continue
            p_yes, p_no = devig_power(sides_map["Yes"], sides_map["No"])
            prop_cons_probs[(pkey, mkt, "Yes", lkey)][book_name] = p_yes
            prop_cons_probs[(pkey, mkt, "No", lkey)][book_name] = p_no

    for k, fp in pin_direct_probs.items():
        prop_cons_probs[k].setdefault("Pinnacle", fp)

    # Track the primary balanced sharp/consensus line per (pkey, mkt, side) so stale ALC lines
    # can be benchmarked cleanly via Gaussian line-shift rather than trusting soft alt books.
    _prop_main_best: Dict[Tuple, Tuple[Tuple, Tuple[float, float, int, bool]]] = {}
    for (pkey, mkt, side, lkey), bprobs in prop_cons_probs.items():
        if lkey is None or mkt in ("Anytime TD", "1st TD", "Last TD"):
            continue
        cons_p, used_books, tier = _weighted_consensus(bprobs)
        if cons_p is None or not (0.25 <= cons_p <= 0.75):
            continue
        is_sharp = bool(tier and tier[0] == "sharp")
        if not is_sharp and len(used_books) < 2:
            continue
        score = (1 if is_sharp else 0, min(len(used_books), 6), -abs(cons_p - 0.50))
        prev = _prop_main_best.get((pkey, mkt, side))
        if prev is None or score > prev[0]:
            _prop_main_best[(pkey, mkt, side)] = (score, (lkey, cons_p, len(used_books), is_sharp))
    prop_main_sharp: Dict[Tuple, Tuple[float, float, int, bool]] = {k: v[1] for k, v in _prop_main_best.items()}

    def _lookup_prop_fair(pkey: str, mkt: str, side: str, lkey: Optional[float]) -> Tuple[Optional[float], Optional[int], str]:
        if lkey is None:
            return None, None, ""
        bprobs = prop_cons_probs.get((pkey, mkt, side, lkey), {})
        if not bprobs:
            return None, None, ""
        cons_p, used_books, tier = _weighted_consensus(bprobs)
        if cons_p is None:
            return None, None, ""
        if tier and tier[0] == "sharp":
            if used_books == ["Pinnacle"]:
                label = "Pinnacle No-Vig"
            elif "Pinnacle" in used_books and len(used_books) > 1:
                label = f"Sharp Consensus ({len(used_books)}: {', '.join(used_books[:3])})"
            else:
                label = f"Sharp Exchange ({', '.join(used_books[:3])})"
        else:
            label = f"Market Consensus ({len(used_books)} books)"
        return cons_p, prob_to_american(cons_p), label

    # E. Enrich ALL buckets with BettingPros & ScoresAndOdds projections, fair odds, DVP, hit rates
    # Also include high-EV BettingPros rows even if not yet on Covers/Pinnacle (if book odds exist or for consensus)
    for bkey, bk in prop_buckets.items():
        pkey, mkt, side, lkey = bkey
        bp_match = bp_by_exact.get((pkey, mkt, side, lkey))
        if not bp_match:
            cands = bp_by_player_mkt.get((pkey, mkt, side), [])
            if cands:
                bp_match = cands[0]
        if bp_match:
            bk["bp_proj"] = bp_match.get("bp_projection")
            bk["bp_ev_pct"] = bp_match.get("bp_ev_pct") if abs((bp_match.get("line") or 0) - (lkey or 0)) < 0.01 else None
            bk["bp_fair_odds"] = bp_match.get("bp_fair_odds")
            bk["bp_prob"] = bp_match.get("bp_prob") if abs((bp_match.get("line") or 0) - (lkey or 0)) < 0.01 else None
            bk["bp_rating"] = bp_match.get("bp_rating")
            bk["opp_dvp_rank"] = bp_match.get("opp_dvp_rank")
            bk["l5_hit_pct"] = bp_match.get("l5_hit_pct")
            bk["l10_hit_pct"] = bp_match.get("l10_hit_pct")
            bk["season_hit_pct"] = bp_match.get("season_hit_pct")
            bk["sources"].add("BettingPros")

        sao_match = sao_by_exact.get((pkey, mkt, side, lkey)) or sao_by_player_mkt.get((pkey, mkt, side))
        if sao_match:
            bk["sao_proj"] = sao_match.get("sao_projection")
            bk["sources"].add("ScoresAndOdds")

        if (pkey, mkt) in sleeper_projs:
            bk["sleeper_proj"] = sleeper_projs[(pkey, mkt)]
            bk["sources"].add("Sleeper")
        if (pkey, mkt) in espn_projs:
            bk["espn_proj"] = espn_projs[(pkey, mkt)]
            bk["sources"].add("ESPN Fantasy")
        if (pkey, mkt) in rw_projs:
            bk["rw_proj"] = rw_projs[(pkey, mkt)]
            bk["sources"].add("RotoWire")

    # Index DFS Pick'em lines (Sleeper, PrizePicks, Underdog) before building prop_ev_rows & dfs_ev_rows
    all_dfs_rows: List[Dict] = []
    all_dfs_rows.extend(sleeper_data.get("sleeper_lines", []))
    pp_rows = list(prizepicks_props or [])
    ud_rows = list(underdog_props or [])
    pl_dfs = propline_data.get("dfs_props", []) or []
    oa_dfs = odds_api_data.get("dfs_props", []) or []
    has_api_pp = any(r.get("book") == "PrizePicks" for r in pl_dfs) or any(r.get("book") == "PrizePicks" for r in oa_dfs)
    if not pp_rows and not has_api_pp:
        pp_rows = [r for r in sleeper_data.get("fallback_dfs", []) if r.get("book") == "PrizePicks"]
    if not ud_rows:
        ud_rows = [r for r in sleeper_data.get("fallback_dfs", []) if r.get("book") == "Underdog"]
    all_dfs_rows.extend(pp_rows)
    all_dfs_rows.extend(ud_rows)
    all_dfs_rows.extend(oa_dfs)
    all_dfs_rows.extend(pl_dfs)

    dfs_line_lookup: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)
    for dr in all_dfs_rows:
        pkey = _resolve_pkey(dr.get("player", ""), dr.get("matchup", ""))
        mkt = dr.get("market", "")
        bk_name = dr.get("book", "")
        if pkey and mkt and bk_name in DFS_BOOKS and dr.get("line") is not None and not dr.get("is_goblin") and dr.get("odds_type") != "goblin":
            # Prefer promo/active lowest-or-first standard line for quick reference
            if bk_name not in dfs_line_lookup[(pkey, mkt)] or dr.get("is_promo"):
                dfs_line_lookup[(pkey, mkt)][bk_name] = float(dr["line"])

    # Compute final EV%, Consensus Projection, and filter to actionable NL book rows
    prop_ev_rows: List[Dict] = []
    for bkey, bk in prop_buckets.items():
        if not bk["books"]:
            continue
        # Only keep current slate games if matchup is known
        if bk["matchup"] and game_meta and bk["matchup"] not in game_meta:
            continue

        best_book, best_info = max(bk["books"].items(), key=lambda kv: kv[1]["decimal"])

        # Determine Fair Probability & EV Method
        fair_prob = bk["pinnacle_fair_prob"]
        fair_am = bk["pinnacle_fair_american"]
        ev_method = ""
        true_ev = None

        cons_fair_p, cons_fair_am, cons_label = _lookup_prop_fair(bk["pkey"], bk["market"], bk["side"], bk["line"])
        if cons_fair_p is not None and (fair_prob is None or "Sharp Consensus" in cons_label):
            fair_prob = cons_fair_p
            fair_am = cons_fair_am
            ev_method = cons_label
        elif fair_prob is not None:
            ev_method = "Pinnacle No-Vig"

        if fair_prob is not None:
            true_ev = compute_ev_pct(fair_prob, best_info["decimal"])
            if not ev_method:
                ev_method = "Pinnacle No-Vig"
        elif bk["bp_prob"] is not None:
            fair_prob = float(bk["bp_prob"])
            fair_am = prob_to_american(fair_prob)
            true_ev = compute_ev_pct(fair_prob, best_info["decimal"])
            ev_method = "BettingPros Fair Prob"
        # NOTE (bug fix): We deliberately do NOT derive a fair prob / EV from a solitary
        # Covers projection. A projection-vs-line gap is not a market-implied probability;
        # converting it through a normal CDF produced fake 26%-65% "edges" across the
        # whole slate. Covers also does not publish a real EV% (only a "DIFFERENCE %").
        # Rows with no genuine fair prob (sharp/consensus/BettingPros) stay true_ev=None
        # and are excluded from EV ranking rather than surfaced as phantom +EV.

        # Average model EV. NOTE: covers_ev_pct is deliberately excluded — it was a
        # misread "DIFFERENCE %", not a real EV (see bug fix). Only BettingPros EV is a
        # trustworthy model-EV input here.
        model_evs = [x for x in [bk["bp_ev_pct"]] if x is not None]
        model_ev_avg = round(sum(model_evs) / len(model_evs), 2) if model_evs else None

        # Consensus projection (Covers, BettingPros, SAO, Sleeper, ESPN Fantasy, RotoWire)
        projs = [
            x for x in [
                bk["covers_proj"], bk["bp_proj"], bk["sao_proj"],
                bk["sleeper_proj"], bk["espn_proj"], bk["rw_proj"],
            ]
            if x is not None
        ]
        cons_proj = round(sum(projs) / len(projs), 2) if projs else None

        # If no EV metric at all, skip unless ALC is present (for ALC scanner)
        if true_ev is not None or model_ev_avg is not None:
            sort_ev = true_ev if true_ev is not None else _sort_num(model_ev_avg, -999.0)
            dfs_ref = dfs_line_lookup.get((bk["pkey"], bk["market"]), {})
            prop_ev_rows.append({
                "kickoff_nl": bk["kickoff_nl"],
                "matchup": bk["matchup"],
                "player": bk["player"],
                "pos_inj": f"{bk['position']} {bk['injury']}".strip(),
                "market": bk["market"],
                "pick": bk["pick"],
                "best_book": best_book,
                "best_american": best_info["american"],
                "best_decimal": round(best_info["decimal"], 3),
                "fair_american": fair_am,
                "fair_prob": round(fair_prob * 100, 2) if fair_prob else None,
                "true_ev_pct": round(true_ev, 2) if true_ev is not None else None,
                "model_ev_pct": model_ev_avg,
                "ev_method": ev_method,
                "fair_prob_tier": _tier_from_label(ev_method),
                "books": {b: bk["books"][b]["american"] if b in bk["books"] else None for b in NL_BOOKS},
                "dfs_lines": {b: dfs_ref.get(b) for b in DFS_BOOKS},
                "cons_proj": cons_proj,
                "covers_proj": bk["covers_proj"],
                "bp_proj": bk["bp_proj"],
                "sao_proj": bk["sao_proj"],
                "sleeper_proj": bk["sleeper_proj"],
                "espn_proj": bk["espn_proj"],
                "rw_proj": bk["rw_proj"],
                "opp_dvp_rank": bk["opp_dvp_rank"],
                "l5_hit_pct": bk["l5_hit_pct"],
                "l10_hit_pct": bk["l10_hit_pct"],
                "season_hit_pct": bk["season_hit_pct"],
                "sources_count": len(bk["sources"]),
                "_sort_ev": sort_ev,
            })

        # Add ALC Player Props to ALC Vig & Mispricing Scanner
        if "ALC Proline" in bk["books"]:
            alc_info = bk["books"]["ALC Proline"]
            alc_lkey = round(float(bk["line"]), 1) if bk["line"] is not None else None
            exact_bprobs = prop_cons_probs.get((bk["pkey"], bk["market"], bk["side"], alc_lkey), {}) if alc_lkey is not None else {}
            has_sharp_exact = any(b in SHARP_SET for b in exact_bprobs)
            main_ref = prop_main_sharp.get((bk["pkey"], bk["market"], bk["side"]))

            alc_fair_prob, alc_fair_am = fair_prob, fair_am
            if (
                not has_sharp_exact
                and main_ref is not None
                and alc_lkey is not None
                and abs(alc_lkey - main_ref[0]) >= 0.5
                and bk["market"] in PROP_STD_DEV
            ):
                ref_line, ref_prob, _ref_n, ref_sharp = main_ref
                if ref_sharp or alc_fair_prob is None:
                    alc_fair_prob = _shift_prop_prob(
                        ref_prob, ref_line, alc_lkey, bk["side"], PROP_STD_DEV[bk["market"]]
                    )
                    alc_fair_am = prob_to_american(alc_fair_prob)

            alc_ev = compute_ev_pct(alc_fair_prob, alc_info["decimal"]) if alc_fair_prob else None
            other_books = [(b, v) for b, v in bk["books"].items() if b != "ALC Proline"]
            best_other = max(other_books, key=lambda kv: kv[1]["decimal"], default=(None, {}))
            # Determine sharp/market consensus main line even when ALC sits on a stale line
            bp_cands = bp_by_player_mkt.get((bk["pkey"], bk["market"], bk["side"]), [])
            mkt_cons_line = None
            mkt_cons_odds = bp_cands[0].get("bp_consensus_odds") or bp_cands[0].get("bp_best_odds") if bp_cands else None
            if bk["market"] not in ("Anytime TD", "1st TD", "Last TD"):
                if main_ref is not None:
                    mkt_cons_line = main_ref[0]
                elif bp_cands:
                    mkt_cons_line = bp_cands[0].get("bp_consensus_line") or bp_cands[0].get("line")
                else:
                    ref_lines = sorted(
                        float(r["line"])
                        for r in bench_line_refs.get((bk["pkey"], bk["market"], bk["side"]), [])
                        if r.get("line") is not None
                    )
                    if ref_lines:
                        mkt_cons_line = ref_lines[len(ref_lines) // 2]
            line_diff = None
            if mkt_cons_line is not None and bk["line"] is not None and bk["market"] not in ("Anytime TD", "1st TD", "Last TD"):
                line_diff = round(float(bk["line"]) - float(mkt_cons_line), 1)
            alc_scanner_rows.append({
                "kickoff_nl": bk["kickoff_nl"],
                "category": "Player Prop",
                "matchup": bk["matchup"],
                "player": bk["player"],
                "market": bk["market"],
                "pick": bk["pick"],
                "alc_american": alc_info["american"],
                "alc_decimal": round(alc_info["decimal"], 3),
                "alc_vig_pct": bk["alc_vig_pct"],
                "pin_american": bk["books"].get("Pinnacle", {}).get("american"),
                "fair_american": alc_fair_am,
                "fair_prob": round(alc_fair_prob * 100, 2) if alc_fair_prob else None,
                "best_other_book": best_other[0],
                "best_other_american": best_other[1].get("american"),
                "mkt_cons_line": mkt_cons_line,
                "mkt_cons_odds": mkt_cons_odds,
                "line_diff": line_diff,
                "alc_ev_pct": round(alc_ev, 2) if alc_ev is not None else None,
            })

    prop_ev_rows.sort(key=lambda r: -r["_sort_ev"])
    # Sort ALC scanner: positive EV first, then favorable stale lines, then negative EV, then unbenchmarked
    def _alc_sort_key(r: Dict):
        ev = r.get("alc_ev_pct")
        if ev is not None and ev > 0:
            return (0, -ev)
        if r.get("line_diff") is not None and r["line_diff"] != 0:
            fav = r["line_diff"] if "Under" in r["pick"] else -r["line_diff"]
            if fav > 0:
                return (1, -fav)
        if ev is not None:
            return (2, -ev)
        if r.get("line_diff") is not None and r["line_diff"] != 0:
            fav = r["line_diff"] if "Under" in r["pick"] else -r["line_diff"]
            return (3, -fav)
        return (4, 0)
    alc_scanner_rows.sort(key=_alc_sort_key)

    # ------------------------------------------------------------------
    # 3. DFS PICK'EM +EV & LINE DISCREPANCIES (Sleeper, PrizePicks, Underdog)
    # ------------------------------------------------------------------
    def _compute_implied_fantasy(pkey: str, mode: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], int, str]:
        """Compute fair fantasy projection across scoring modes ('ppr', 'half_ppr', 'alc_0ppr').

        Returns (fair_fp, mkt_implied_fp, sleeper_fp, rw_fp_adj, mkt_hits, label).
        """
        st = sleeper_stats.get(pkey) or {}
        pos = (st.get("position") or (player_meta.get(pkey, {}) or {}).get("position") or "").upper()
        rec_w = 1.0 if mode == "ppr" else (0.5 if mode == "half_ppr" else 0.0)
        py_w = 0.05 if mode == "alc_0ppr" else 0.04
        int_w = 2.0 if mode == "alc_0ppr" else 1.0

        def _exp_component(mkt: str, sigma: float, st_key: str) -> Tuple[float, bool]:
            ref = prop_main_sharp.get((pkey, mkt, "Over"))
            if ref is not None:
                ref_line, ref_over_p, _n, _is_sharp = ref
                # Mean implied by balanced O/U line + sharp win probability
                mu = max(0.0, float(ref_line) - sigma * _norm_ppf(1.0 - float(ref_over_p)))
                return mu, True
            bp_cands = bp_by_player_mkt.get((pkey, mkt, "Over"), [])
            if bp_cands:
                val = bp_cands[0].get("bp_projection") or bp_cands[0].get("bp_consensus_line") or bp_cands[0].get("line")
                if val is not None:
                    return max(0.0, float(val)), True
            fallback = st.get(st_key)
            if fallback is not None:
                return max(0.0, float(fallback)), False
            return max(0.0, float(rw_projs.get((pkey, mkt)) or sleeper_projs.get((pkey, mkt)) or 0.0)), False

        mkt_hits = 0
        if pos == "K":
            k_pts, k_hit = _exp_component("Kicking Points", 3.8, "pts_ppr")
            mkt_hits = 1 if k_hit else 0
            mkt_fp = k_pts if k_hit else None
            rec_est = 0.0
            py_est = 0.0
            int_est = 0.0
        else:
            pass_yd, h_py = _exp_component("Passing Yards", 42.0, "pass_yd")
            pass_td, h_ptd = _exp_component("Passing TDs", 0.85, "pass_td")
            rush_yd, h_ry = _exp_component("Rushing Yards", 22.0, "rush_yd")
            rec_yd, h_cy = _exp_component("Receiving Yards", 24.0, "rec_yd")
            rec_n, h_rec = _exp_component("Receptions", 1.75, "rec")

            # Interceptions (Poisson conversion when quoted at 0.5)
            int_ref = prop_main_sharp.get((pkey, "Interceptions", "Over"))
            if int_ref is not None:
                if abs(float(int_ref[0]) - 0.5) < 0.05:
                    pass_int = min(-math.log(max(1.0 - min(float(int_ref[1]), 0.85), 0.15)), 1.85)
                else:
                    pass_int = max(0.0, float(int_ref[0]) - 0.65 * _norm_ppf(1.0 - float(int_ref[1])))
                h_int = True
            else:
                pass_int = max(0.0, float(st.get("pass_int") or 0.0))
                h_int = False

            # Anytime TD -> Poisson expected Rush + Rec TDs
            atd_p, _, _ = _lookup_prop_fair(pkey, "Anytime TD", "Yes", 0.5)
            if atd_p is None:
                bp_td = bp_by_player_mkt.get((pkey, "Anytime TD", "Yes"), [])
                if bp_td and bp_td[0].get("bp_prob") is not None:
                    atd_p = float(bp_td[0]["bp_prob"])
            if atd_p is not None:
                rush_rec_td = min(-math.log(max(1.0 - min(float(atd_p), 0.88), 0.12)), 1.75)
                h_atd = True
            else:
                rush_rec_td = max(0.0, float(st.get("rush_td") or 0.0) + float(st.get("rec_td") or 0.0))
                h_atd = False

            mkt_hits = sum([h_py, h_ptd, h_int, h_ry, h_cy, h_rec, h_atd])
            fum_lost = max(0.0, float(st.get("fum_lost") or 0.0))
            two_pt = max(0.0, float(st.get("two_pt") or 0.0))
            rec_est = rec_n
            py_est = pass_yd
            int_est = pass_int

            if mkt_hits >= 1:
                mkt_fp = (
                    pass_yd * py_w + pass_td * 4.0 - pass_int * int_w
                    + rush_yd * 0.1 + rec_yd * 0.1 + rec_n * rec_w
                    + rush_rec_td * 6.0 - fum_lost * 2.0 + two_pt * 2.0
                )
            else:
                mkt_fp = None

        if mode == "ppr":
            sl_fp = sleeper_projs.get((pkey, "Fantasy Points"))
            espn_fp = espn_projs.get((pkey, "Fantasy Points"))
        elif mode == "half_ppr":
            sl_fp = sleeper_projs.get((pkey, "Fantasy Points (Half PPR)"))
            espn_fp = espn_projs.get((pkey, "Fantasy Points (Half PPR)"))
        elif mode == "standard":
            sl_fp = sleeper_projs.get((pkey, "Fantasy Points (Standard)"))
            espn_fp = espn_projs.get((pkey, "Fantasy Points (Standard)"))
        else:
            sl_fp = sleeper_projs.get((pkey, "Fantasy Points (ALC)"))
            espn_fp = espn_projs.get((pkey, "Fantasy Points (ALC)"))

        rw_raw = rw_projs.get((pkey, "Fantasy Points"))
        rw_fp_adj: Optional[float] = None
        if rw_raw is not None:
            if mode == "ppr":
                rw_fp_adj = round(float(rw_raw), 2)
            elif mode == "half_ppr":
                rw_fp_adj = round(max(0.0, float(rw_raw) - 0.5 * rec_est), 2)
            elif mode == "standard":
                rw_fp_adj = round(max(0.0, float(rw_raw) - 1.0 * rec_est), 2)
            else:
                rw_fp_adj = round(max(0.0, float(rw_raw) - 1.0 * rec_est + 0.01 * py_est - 1.0 * int_est), 2)

        model_pairs = [("Sleeper", sl_fp), ("ESPN", espn_fp), ("RotoWire", rw_fp_adj)]
        active_models = [(name, float(val)) for name, val in model_pairs if val is not None]
        model_avg = (sum(v for _, v in active_models) / len(active_models)) if active_models else None

        if mkt_fp is not None and mkt_hits >= 3:
            fair_fp = (0.78 * mkt_fp + 0.22 * model_avg) if model_avg is not None else mkt_fp
            label = f"Market-Implied ({mkt_hits} sharp props)"
        elif mkt_fp is not None and mkt_hits >= 1:
            fair_fp = (0.55 * mkt_fp + 0.45 * model_avg) if model_avg is not None else mkt_fp
            label = f"Market + Model ({mkt_hits} prop{'s' if mkt_hits > 1 else ''})"
        elif model_avg is not None:
            fair_fp = model_avg
            if len(active_models) > 1:
                label = " + ".join(n for n, _ in active_models) + " Model"
            else:
                label = f"{active_models[0][0]} Projection"
        else:
            return None, None, None, None, None, 0, ""

        return (
            round(fair_fp, 2),
            round(mkt_fp, 2) if mkt_fp is not None else None,
            round(float(sl_fp), 2) if sl_fp is not None else None,
            round(float(espn_fp), 2) if espn_fp is not None else None,
            rw_fp_adj,
            mkt_hits,
            label,
        )

    def _resolve_fantasy_mode(mkt: str, book: str, lkey: Optional[float], pkey: str) -> Optional[Tuple[str, str]]:
        """Decipher whether a Fantasy Points line uses Standard (0 PPR), Half PPR, ALC 0-PPR, or Full PPR."""
        if not mkt.startswith("Fantasy Points"):
            return None
        if "(Standard)" in mkt:
            return "standard", "Standard (0 PPR)"
        if "(Half PPR)" in mkt:
            return "half_ppr", "Half PPR (0.5)"
        if "(ALC)" in mkt or "ALC" in book:
            return "alc_0ppr", "ALC (0 PPR)"
        if "(PPR)" in mkt or book in ("Sleeper", "PrizePicks", "Betr"):
            return "ppr", "Full PPR (1.0)"
        if book == "Underdog":
            return "half_ppr", "Half PPR (0.5)"
        # Unknown sportsbook/feed posting generic 'Fantasy Points': compare line vs Standard & Full-PPR projections
        if lkey is not None:
            rec_est = float((sleeper_stats.get(pkey) or {}).get("rec") or sleeper_projs.get((pkey, "Receptions")) or 0.0)
            ppr_ref = sleeper_projs.get((pkey, "Fantasy Points"))
            std_ref = sleeper_projs.get((pkey, "Fantasy Points (Standard)"))
            if rec_est >= 2.0 and ppr_ref is not None and std_ref is not None:
                if abs(float(lkey) - float(std_ref)) + 0.4 < abs(float(lkey) - float(ppr_ref)):
                    return "standard", "Standard (0 PPR)"
        return "ppr", "Full PPR (1.0)"

    pin_by_exact: Dict[Tuple, Dict] = {}
    pin_by_player_mkt: Dict[Tuple, List[Dict]] = defaultdict(list)
    for pr in pinnacle_data.get("player_props", []):
        pkey = _resolve_pkey(pr.get("player", ""), pr.get("matchup", ""))
        mkt = pr.get("market", "")
        side = pr.get("side", "")
        ln = pr.get("line")
        lkey = round(float(ln), 1) if ln is not None else None
        pin_by_exact[(pkey, mkt, side, lkey)] = pr
        pin_by_player_mkt[(pkey, mkt, side)].append(pr)

    # Index projection lookup from prop_buckets / covers / bp / sao / sleeper / rw
    covers_proj_map: Dict[Tuple, float] = {}
    for pr in covers_data.get("player_props", []):
        if pr.get("covers_projection") is not None:
            pkey = _resolve_pkey(pr.get("player", ""), pr.get("matchup", ""))
            covers_proj_map[(pkey, pr.get("market", ""))] = float(pr["covers_projection"])

    dfs_dedup: Dict[Tuple, Dict] = {}
    for dr in all_dfs_rows:
        book = dr.get("book", "")
        if book not in DFS_BOOKS:
            continue
        pkey = _resolve_pkey(dr.get("player", ""), dr.get("matchup", ""))
        mkt = dr.get("market", "")
        side = dr.get("side", "")
        ln = dr.get("line")
        exact_line = round(float(ln), 2) if ln is not None else None
        lkey = round(float(ln), 1) if ln is not None else None
        dkey = (book, pkey, mkt, side, exact_line if exact_line is not None else lkey)

        meta = player_meta.get(pkey, {})
        resolved_matchup = dr.get("matchup") or meta.get("matchup", "")
        if resolved_matchup and game_meta and resolved_matchup not in game_meta:
            continue

        # Avoid duplicate overwrite unless promo
        if dkey in dfs_dedup and not dr.get("is_promo"):
            continue

        pin_exact = pin_by_exact.get((pkey, mkt, side, lkey))
        pin_cands = pin_by_player_mkt.get((pkey, mkt, side), [])
        pin_ref = pin_exact or (pin_cands[0] if pin_cands else None)
        pin_line = pin_ref.get("line") if pin_ref else None
        pin_am = pin_ref.get("american") if pin_ref else None

        # Also check PropLine/OddsAPI Pinnacle & sharp exchange lines (Novig, ProphetX, BookMaker, DK, FD)
        bench_cands = bench_line_refs.get((pkey, mkt, side), [])
        if pin_line is None and bench_cands:
            pin_api = [c for c in bench_cands if c.get("is_pin") and c.get("line") is not None]
            if pin_api:
                exact_api = next((c for c in pin_api if c["line"] == lkey), pin_api[0])
                pin_line = exact_api["line"]
                pin_am = exact_api.get("american")

        bp_exact = bp_by_exact.get((pkey, mkt, side, lkey)) if lkey is not None else None
        bp_cands = bp_by_player_mkt.get((pkey, mkt, side), [])
        bp_ref = bp_exact or (bp_cands[0] if bp_cands else None)
        mkt_cons_line = (bp_ref.get("bp_consensus_line") or bp_ref.get("line")) if bp_ref else None
        if mkt_cons_line is None and bench_cands:
            sharp_lines = [c["line"] for c in bench_cands if c.get("line") is not None]
            if sharp_lines:
                sharp_lines_sorted = sorted(sharp_lines)
                mkt_cons_line = sharp_lines_sorted[len(sharp_lines_sorted) // 2]

        # Determine Fair Prob & DFS EV % (exact line match first, then sharp line-shift / Fantasy Points model)
        fair_prob = None
        fair_am = None
        ev_method = ""
        dfs_ev_pct = None
        fp_mkt_line = None
        fp_fair_proj = None
        fp_sl_proj = None
        fp_espn_proj = None
        fp_rw_proj = None
        scoring_rule = ""
        fp_resolved = _resolve_fantasy_mode(mkt, book, exact_line, pkey) if exact_line is not None else None

        if fp_resolved is not None and exact_line is not None:
            mode, scoring_rule = fp_resolved
            fp_fair, fp_mkt, fp_sl, fp_espn, fp_rw, _fp_hits, fp_label = _compute_implied_fantasy(pkey, mode)
            if fp_fair is not None:
                pos = (sleeper_stats.get(pkey, {}).get("position") or meta.get("position") or "").upper()
                fp_sigma = FANTASY_STD_DEV.get(pos, 6.7)
                z_fp = (float(fp_fair) - float(exact_line)) / fp_sigma
                raw_p = _norm_cdf(z_fp) if side == "Over" else _norm_cdf(-z_fp)
                fair_prob = min(max(raw_p, 0.05), 0.95)
                fair_am = prob_to_american(fair_prob)
                ev_method = fp_label
                fp_fair_proj = fp_fair
                fp_mkt_line = fp_mkt if fp_mkt is not None else fp_fair
                fp_sl_proj = fp_sl
                fp_espn_proj = fp_espn
                fp_rw_proj = fp_rw
                if mkt_cons_line is None:
                    mkt_cons_line = fp_mkt_line
        else:
            cons_fair_p, cons_fair_am, cons_label = _lookup_prop_fair(pkey, mkt, side, lkey)
            if cons_fair_p is not None and (exact_line is None or lkey is None or abs(float(exact_line) - float(lkey)) < 1e-6 or mkt not in PROP_STD_DEV):
                fair_prob = cons_fair_p
                fair_am = cons_fair_am
                ev_method = cons_label
            elif cons_fair_p is not None and exact_line is not None and mkt in PROP_STD_DEV:
                fair_prob = _shift_prop_prob(cons_fair_p, float(lkey), float(exact_line), side, PROP_STD_DEV[mkt])
                fair_am = prob_to_american(fair_prob)
                ev_method = cons_label
            elif pin_exact and pin_exact.get("fair_prob") is not None:
                pin_fp = float(pin_exact["fair_prob"])
                if exact_line is not None and lkey is not None and abs(float(exact_line) - float(lkey)) >= 1e-6 and mkt in PROP_STD_DEV:
                    fair_prob = _shift_prop_prob(pin_fp, float(lkey), float(exact_line), side, PROP_STD_DEV[mkt])
                    fair_am = prob_to_american(fair_prob)
                else:
                    fair_prob = pin_fp
                    fair_am = pin_exact.get("fair_american")
                ev_method = "Pinnacle No-Vig"
            elif bp_exact and bp_exact.get("bp_prob") is not None:
                fair_prob = float(bp_exact["bp_prob"])
                fair_am = prob_to_american(fair_prob)
                ev_method = "BettingPros Fair Prob"
            elif exact_line is not None and mkt in PROP_STD_DEV:
                main_ref = prop_main_sharp.get((pkey, mkt, side))
                if main_ref is not None and abs(float(exact_line) - main_ref[0]) >= 0.5 and (main_ref[3] or main_ref[2] >= 2):
                    fair_prob = _shift_prop_prob(main_ref[1], main_ref[0], float(exact_line), side, PROP_STD_DEV[mkt])
                    fair_am = prob_to_american(fair_prob)
                    ev_method = f"Line-Shifted {'Sharp' if main_ref[3] else 'Consensus'}"

        # PrizePicks pays on the full entry, not on an individual leg. Never invent
        # a conventional single-leg price/EV, including for legacy cached rows.
        no_single_leg_odds = bool(dr.get("no_single_leg_odds") or book == "PrizePicks")
        leg_dec = float(dr["decimal"]) if dr.get("decimal") is not None and not no_single_leg_odds else None
        if fair_prob is not None and leg_dec is not None:
            dfs_ev_pct = round(compute_ev_pct(fair_prob, leg_dec), 2)
        if no_single_leg_odds:
            ev_method = "Entry payout table; single-leg EV unavailable"

        # Compute line cushion vs Pinnacle (preferred), BettingPros consensus, or Fantasy projection
        bench_line = pin_line if pin_line is not None else mkt_cons_line
        line_diff = None
        cushion = None
        if fp_resolved is not None and fp_fair_proj is not None and exact_line is not None:
            line_diff = round(float(exact_line) - float(fp_fair_proj), 2)
            cushion = round(float(fp_fair_proj) - float(exact_line), 2) if side == "Over" else round(float(exact_line) - float(fp_fair_proj), 2)
        elif bench_line is not None and exact_line is not None and mkt not in ("Anytime TD", "1st TD", "Last TD"):
            line_diff = round(float(exact_line) - float(bench_line), 2)
            # cushion = advantage to the bettor, positive = favourable.
            # Over wins when actual > line, so a LOWER offered line (vs the sharp line)
            # is easier  -> favorable when line_diff < 0 -> cushion = -line_diff.
            # Under wins when actual < line, so a HIGHER offered line is easier
            # -> favorable when line_diff > 0 -> cushion = +line_diff.
            cushion = line_diff if side == "Under" else -line_diff

        # Gather projections & hit rates
        if fp_resolved is not None and fp_fair_proj is not None:
            c_proj = None
            b_proj = None
            sl_proj = fp_sl_proj
            espn_p = fp_espn_proj
            rw_p = fp_rw_proj
            cons_proj = fp_fair_proj
        else:
            c_proj = covers_proj_map.get((pkey, mkt))
            b_proj = bp_ref.get("bp_projection") if bp_ref else None
            s_proj = (sao_by_player_mkt.get((pkey, mkt, side)) or {}).get("sao_projection")
            sl_proj = sleeper_projs.get((pkey, mkt))
            espn_p = espn_projs.get((pkey, mkt))
            rw_p = rw_projs.get((pkey, mkt))
            projs = [x for x in [c_proj, b_proj, s_proj, sl_proj, espn_p, rw_p] if x is not None]
            cons_proj = round(sum(projs) / len(projs), 2) if projs else None

        dfs_dedup[dkey] = {
            "kickoff_nl": game_meta.get(resolved_matchup, {}).get("kickoff_nl", ""),
            "matchup": resolved_matchup,
            "book": book,
            "player": meta.get("name") or dr.get("player", ""),
            "pos_inj": f"{meta.get('position', '')} {meta.get('injury', '')}".strip(),
            "market": mkt,
            "scoring_rule": scoring_rule,
            "pick": _format_pick_label(mkt, side, exact_line),
            "side": side,
            "line": exact_line,
            "leg_american": int(dr["american"]) if dr.get("american") is not None and not no_single_leg_odds else None,
            "leg_decimal": round(leg_dec, 3) if leg_dec is not None else None,
            "odds_type": dr.get("odds_type") or ("goblin" if dr.get("is_goblin") else "standard"),
            "is_goblin": bool(dr.get("is_goblin") or dr.get("odds_type") == "goblin"),
            "adjusted_payout": bool(dr.get("adjusted_payout")),
            "no_single_leg_odds": no_single_leg_odds,
            "is_promo": bool(dr.get("is_promo")) and not bool(dr.get("is_goblin") or dr.get("odds_type") == "goblin"),
            "promo_label": "" if dr.get("is_goblin") or dr.get("odds_type") == "goblin" else dr.get("promo_label", ""),
            "orig_line": dr.get("orig_line"),
            "pin_line": pin_line,
            "pin_american": pin_am,
            "fair_american": fair_am,
            "fair_prob": round(fair_prob * 100, 2) if fair_prob else None,
            "dfs_ev_pct": dfs_ev_pct,
            "ev_method": ev_method,
            "fair_prob_tier": _tier_from_label(ev_method),
            "mkt_cons_line": mkt_cons_line,
            "line_diff": line_diff,
            "cushion": cushion,
            "cons_proj": cons_proj,
            "sleeper_proj": sl_proj,
            "espn_proj": espn_p,
            "rw_proj": rw_p,
            "bp_proj": b_proj,
            "covers_proj": c_proj,
            "l5_hit_pct": bp_ref.get("l5_hit_pct") if bp_ref else None,
            "l10_hit_pct": bp_ref.get("l10_hit_pct") if bp_ref else None,
            "season_hit_pct": bp_ref.get("season_hit_pct") if bp_ref else None,
        }

    all_dfs_output = list(dfs_dedup.values())
    dfs_ev_rows = [r for r in all_dfs_output if not r.get("is_goblin")]
    goblin_rows = [r for r in all_dfs_output if r.get("is_goblin")]

    def _dfs_sort_key(r: Dict):
        if r.get("is_promo"):
            return (0, -(r.get("cushion") or 0), -(r.get("dfs_ev_pct") or 0))
        ev = r.get("dfs_ev_pct")
        cush = r.get("cushion")
        if ev is not None and ev > 0:
            return (1, -ev, -(cush or 0))
        if cush is not None and cush > 0:
            return (2, -cush, -_sort_num(ev, -999.0))
        if ev is not None:
            return (3, -ev, 0)
        return (4, -_sort_num(cush, -999.0), 0)

    dfs_ev_rows.sort(key=_dfs_sort_key)
    goblin_rows.sort(
        key=lambda r: (
            -(r.get("fair_prob") if r.get("fair_prob") is not None else -1),
            -(r.get("cushion") if r.get("cushion") is not None else -999),
            -(r.get("l5_hit_pct") if r.get("l5_hit_pct") is not None else -1),
        )
    )

    # ------------------------------------------------------------------
    # 4. ALC PRO•LINE FANTASY GROUPS (0-PPR Football Model + Live Groups)
    # ------------------------------------------------------------------
    alc_fantasy_groups: List[Dict] = []
    for grp in alc_data.get("fantasy_groups", []) or []:
        is_nfl = bool(grp.get("is_nfl"))
        enriched_entries: List[Dict] = []
        mu_sigma_list: List[Tuple[float, float]] = []
        all_have_proj = is_nfl

        for ent in grp.get("entries", []):
            e_copy = dict(ent)
            if is_nfl:
                pkey = _resolve_pkey(ent.get("player", ""), "")
                fp_fair, fp_mkt, fp_sl, fp_espn, fp_rw, fp_hits, fp_label = _compute_implied_fantasy(pkey, "alc_0ppr")
                st = sleeper_stats.get(pkey) or {}
                meta = player_meta.get(pkey) or {}
                pos = (st.get("position") or meta.get("position") or "").upper()
                sig = FANTASY_STD_DEV.get(pos, 6.7)
                e_copy["matchup"] = meta.get("matchup", "")
                e_copy["pos"] = pos
                e_copy["alc_proj_pts"] = fp_fair
                e_copy["mkt_implied_pts"] = fp_mkt
                e_copy["sleeper_proj_pts"] = fp_sl
                e_copy["espn_proj_pts"] = fp_espn
                e_copy["rw_proj_pts"] = fp_rw
                e_copy["model_label"] = fp_label
                e_copy["mkt_hits"] = fp_hits
                if fp_fair is not None:
                    mu_sigma_list.append((float(fp_fair), sig))
                else:
                    all_have_proj = False
            else:
                e_copy["alc_proj_pts"] = None
                e_copy["model_win_prob"] = None
                e_copy["ev_pct"] = None
            enriched_entries.append(e_copy)

        if is_nfl and all_have_proj and len(mu_sigma_list) == len(enriched_entries) and len(enriched_entries) >= 2:
            win_probs = _alc_group_win_probs(mu_sigma_list)
            for e_copy, wp in zip(enriched_entries, win_probs):
                e_copy["model_win_prob"] = round(wp * 100.0, 2)
                e_copy["ev_pct"] = round(compute_ev_pct(wp, float(e_copy["decimal"])), 2)
        else:
            for e_copy in enriched_entries:
                e_copy.setdefault("model_win_prob", None)
                e_copy.setdefault("ev_pct", None)

        best_ev = max((e["ev_pct"] for e in enriched_entries if e.get("ev_pct") is not None), default=None)
        alc_fantasy_groups.append({
            "sport": grp.get("sport", ""),
            "sport_raw": grp.get("sport_raw", ""),
            "is_nfl": is_nfl,
            "group_id": grp.get("group_id", ""),
            "group_title": grp.get("group_title", ""),
            "kickoff": grp.get("kickoff", ""),
            "vig_pct": grp.get("vig_pct"),
            "best_ev_pct": best_ev,
            "entries": enriched_entries,
        })

    alc_fantasy_groups.sort(
        key=lambda g: (0 if g["is_nfl"] else 1, -_sort_num(g["best_ev_pct"], -999.0), g["vig_pct"] if g["vig_pct"] is not None else 999)
    )

    return {
        "slate_games": espn_games,
        "splits": splits_map,
        "prop_ev_rows": prop_ev_rows,
        "game_ev_rows": game_ev_rows,
        "alc_scanner_rows": alc_scanner_rows,
        "dfs_ev_rows": dfs_ev_rows,
        "goblin_rows": goblin_rows,
        "alc_fantasy_groups": alc_fantasy_groups,
    }
