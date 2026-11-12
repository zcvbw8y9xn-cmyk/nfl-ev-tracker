"""Odds conversion, devigging, EV, vig, and Kelly sizing utilities."""
from __future__ import annotations

import math
from typing import Optional, Tuple


def american_to_decimal(odds: float) -> float:
    """Convert American odds (e.g., -110, +150) to decimal odds (e.g., 1.909, 2.50)."""
    if odds == 0:
        raise ValueError("American odds cannot be 0")
    if odds > 0:
        return 1.0 + (odds / 100.0)
    return 1.0 + (100.0 / abs(odds))


def decimal_to_american(dec: float) -> int:
    """Convert decimal odds (>1.0) to integer American odds."""
    if dec <= 1.0:
        return -10000
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100.0))
    return int(round(-100.0 / (dec - 1.0)))


def decimal_to_implied_prob(dec: float) -> float:
    """Implied probability from decimal odds."""
    if dec <= 1.0:
        return 1.0
    return 1.0 / dec


def american_to_implied_prob(odds: float) -> float:
    """Implied probability from American odds."""
    return decimal_to_implied_prob(american_to_decimal(odds))


def prob_to_american(prob: float) -> Optional[int]:
    """Convert fair probability (0 < p < 1) to fair American odds."""
    if prob <= 0.0 or prob >= 1.0:
        return None
    dec = 1.0 / prob
    return decimal_to_american(dec)


def prob_to_decimal(prob: float) -> Optional[float]:
    """Convert fair probability (0 < p < 1) to fair decimal odds."""
    if prob <= 0.0 or prob >= 1.0:
        return None
    return 1.0 / prob


def parse_american(val: str | float | int | None) -> Optional[int]:
    """Parse an American odds value: '+120', '-115', 120, -115, 'EVEN'.

    Never interprets a bare decimal-looking number (1.92, 2.5, 1.5) as decimal:
    without a sign, a value in (1.0, 100) is invalid for American odds and returns
    None rather than guessing.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        if v == 0 or v != v:
            return None
        if abs(v) >= 100.0:
            return int(round(v))
        return None

    s = str(val).strip().upper().replace("\u2212", "-").replace("\u2013", "-")
    if not s or s in {"-", "\u2014", "N/A", "NA", "NONE", "OFF"}:
        return None
    if s in {"EVEN", "EV", "PK", "EVS"}:
        return 100
    s = s.replace(",", "").replace("$", "")
    try:
        if s.startswith("+"):
            num = float(s[1:])
            return int(round(num)) if num > 0 else None
        if s.startswith("-"):
            num = float(s[1:])
            if num <= 0:
                return None
            return -int(round(num))
        num = float(s)
        if num >= 100.0:
            return int(round(num))
        return None
    except ValueError:
        return None


def parse_decimal(val: str | float | int | None) -> Optional[float]:
    """Parse a decimal-odds value: '1.92', 1.92, '2.50', '1.5'.

    Decimal odds are always > 1.0. A signed or >=100 value ('-115', '+120', 120)
    is not decimal odds, so it returns None instead of misinterpreting it.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        if v != v or v <= 1.0:  # NaN or not a valid decimal price
            return None
        return v
    s = str(val).strip().upper().replace("\u2212", "-").replace("\u2013", "-")
    if not s or s in {"-", "\u2014", "N/A", "NA", "NONE", "OFF"}:
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        num = float(s)
    except ValueError:
        return None
    return num if num > 1.0 else None


def parse_odds(val: str | float | int | None, fmt: str = "auto") -> Optional[int]:
    """Parse an odds value into American odds for a known source format.

    ``fmt`` controls how bare numbers are interpreted:
      * 'american'  - the value is American odds (signed ints, 'EVEN');
      * 'decimal'   - the value is decimal odds (e.g. 1.92, 2.5, 1.5);
      * 'auto'      - infer: strings with an explicit sign or abs>=100 are American,
        bare numbers in (1.01, 100) are treated as decimal (best-effort legacy mode).
    """
    if fmt == "american":
        return parse_american(val)
    if fmt == "decimal":
        dec = parse_decimal(val)
        return decimal_to_american(dec) if dec is not None else None
    # auto (legacy heuristic)
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return int(val) if val != 0 else None
    if isinstance(val, float):
        v = float(val)
        if v == 0 or v != v:
            return None
        if abs(v) >= 100.0:
            return int(round(v))
        if v > 1.0:
            return decimal_to_american(v)
        return None
    s = str(val).strip().upper().replace("\u2212", "-").replace("\u2013", "-")
    if not s or s in {"-", "\u2014", "N/A", "NA", "NONE", "OFF"}:
        return None
    if s in {"EVEN", "EV", "PK", "EVS"}:
        return 100
    s = s.replace(",", "").replace("$", "")
    try:
        if s.startswith("+"):
            num = float(s[1:])
            return int(round(num)) if num > 0 else None
        if s.startswith("-"):
            num = float(s[1:])
            if num <= 0:
                return None
            return -int(round(num)) if num >= 100 or "." not in s else None
        num = float(s)
        if 1.01 <= num < 100.0:
            return decimal_to_american(num)
        if num >= 100.0:
            return int(round(num))
    except ValueError:
        return None
    return None


# Backwards-compatible alias for any code that still imports the old name.
def parse_american_or_decimal(val: str | float | int | None) -> Optional[int]:
    """Deprecated alias for :func:`parse_odds` (auto-infer). Use an explicit format instead."""
    return parse_odds(val, fmt="auto")


def market_vig_pct(dec_a: float, dec_b: float) -> float:
    """Compute bookmaker overround / vig percentage for a 2-way market."""
    if dec_a <= 1.0 or dec_b <= 1.0:
        return 0.0
    overround = (1.0 / dec_a) + (1.0 / dec_b) - 1.0
    return overround * 100.0


def devig_power(dec_a: float, dec_b: float) -> Tuple[float, float]:
    """Compute no-vig fair probabilities using the power method (k where p1^k + p2^k = 1).
    Falls back to multiplicative devigging if solver cannot bracket.
    """
    p1 = 1.0 / dec_a
    p2 = 1.0 / dec_b
    total = p1 + p2
    if total <= 0:
        return 0.5, 0.5
    if abs(total - 1.0) < 1e-6:
        return p1, p2
    # A combined implied probability <= 1.0 means a genuine no-vig/arbitrage market.
    # The power solver has no k in its bracket that reproduces this (it would push both
    # prices *up* to sum to 1), and silently degrades toward 0.5/0.5. Normalising
    # proportionally is the correct no-vig treatment for a sub-100% book.
    if total <= 1.0:
        return p1 / total, p2 / total

    # Bisection for k in [0.05, 20.0] such that p1^k + p2^k = 1
    lo, hi = 0.05, 20.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        val = (p1 ** mid) + (p2 ** mid)
        if val > 1.0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    f1 = p1 ** k
    f2 = p2 ** k
    s = f1 + f2
    if s <= 0:
        return p1 / total, p2 / total
    return f1 / s, f2 / s


def devig_multiplicative(dec_a: float, dec_b: float) -> Tuple[float, float]:
    """Compute no-vig fair probabilities using multiplicative normalization."""
    p1 = 1.0 / dec_a
    p2 = 1.0 / dec_b
    total = p1 + p2
    if total <= 0:
        return 0.5, 0.5
    return p1 / total, p2 / total


def compute_ev_pct(fair_prob: float, offered_dec: float) -> float:
    """Expected Value %: (fair_prob * offered_dec - 1) * 100."""
    if fair_prob <= 0 or fair_prob >= 1 or offered_dec <= 1.0:
        return 0.0
    return ((fair_prob * offered_dec) - 1.0) * 100.0


def kelly_fraction(fair_prob: float, offered_dec: float, multiplier: float = 0.25) -> float:
    """Fractional Kelly stake percentage of bankroll (default Quarter Kelly = 0.25).
    Returns percentage (e.g., 1.85 means 1.85% of bankroll). The multiplier is
    constrained to [0, 1], so this helper never recommends more than full Kelly.
    """
    try:
        fair_prob = float(fair_prob)
        offered_dec = float(offered_dec)
        multiplier = float(multiplier)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(x) for x in (fair_prob, offered_dec, multiplier)):
        return 0.0
    if fair_prob <= 0 or fair_prob >= 1 or offered_dec <= 1.0:
        return 0.0
    multiplier = min(max(multiplier, 0.0), 1.0)
    b = offered_dec - 1.0
    q = 1.0 - fair_prob
    full_kelly = ((b * fair_prob) - q) / b
    if full_kelly <= 0 or multiplier == 0:
        return 0.0
    return full_kelly * multiplier * 100.0
