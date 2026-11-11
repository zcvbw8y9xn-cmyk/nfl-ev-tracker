"""Dataset integrity validator for the NFL +EV tracker.

Runs post-dry-run sanity checks on the cached dataset so regressions in the math or
parsers surface immediately. Read-only; exits non-zero if any check fails.

Usage:
    python3 main.py --dry-run && python3 tools/validate_dataset.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.odds_math import compute_ev_pct  # noqa: E402

DATASET = os.path.expanduser("~/.cache/nfl-ev-tracker/latest_dataset.json")


def _fail(msg: str) -> None:
    print(f"  FAIL: {msg}")
    global _FAILED
    _FAILED = True


_FAILED = False


def validate_dataset(ds: dict) -> int:
    """Run all integrity checks on an in-memory dataset dict. Returns 0 = pass, 1 = fail.
    Safe to call repeatedly (resets failure state each call). Non-throwing on data
    shape issues (treats missing keys as empty)."""
    global _FAILED
    _FAILED = False

    prop = ds.get("prop_ev_rows", [])
    game = ds.get("game_ev_rows", [])
    alc = ds.get("alc_scanner_rows", [])
    dfs = ds.get("dfs_ev_rows", [])

    print(f"rows: prop={len(prop)} game={len(game)} alc={len(alc)} dfs={len(dfs)}")

    # 1. No NaN/inf anywhere.
    for name, rows in (("prop", prop), ("game", game), ("alc", alc), ("dfs", dfs)):
        bad = sum(
            1 for r in rows for v in r.values()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v))
        )
        if bad:
            _fail(f"{name}: {bad} NaN/inf values")

    # 2. Prop EV must recompute exactly from fair_prob & decimal, and no phantom >30% EV.
    mism = signflip = 0
    for r in prop:
        fp, ev, dec = r.get("fair_prob"), r.get("true_ev_pct"), r.get("best_decimal")
        if fp is None or ev is None or dec is None:
            continue
        recalc = compute_ev_pct(fp / 100.0, dec)
        if abs(recalc - ev) > 0.5:
            mism += 1
        # Sign flip only counts if the edge is beyond the 2-decimal rounding band
        # (fair*dec within ~0.05% of 1.0 legitimately rounds EV to ~0.00).
        edge = dec * fp / 100.0
        if abs(edge - 1.0) > 5e-4 and (ev > 0) != (edge > 1.0):
            signflip += 1
    if mism:
        _fail(f"prop: {mism} EV rows don't match fair_prob*decimal")
    if signflip:
        _fail(f"prop: {signflip} EV sign-flip anomalies")
    big = [r for r in prop if (r.get("true_ev_pct") or 0) > 30]
    if big:
        _fail(f"prop: {len(big)} rows with EV>30% (likely phantom edge, e.g. Covers DIFFERENCE bug)")

    # 2b. Phantom-edge signature: a numeric EV must be backed by a genuine fair prob
    #     (or BettingPros prob). A row with true_ev_pct set but fair_prob None is exactly
    #     what the Covers "DIFFERENCE %" bug produced - no real probability behind the EV.
    phantom = [
        r for r in prop
        if r.get("true_ev_pct") is not None and r.get("fair_prob") is None
    ]
    if phantom:
        ex = phantom[0]
        _fail(
            f"prop: {len(phantom)} rows have an EV but NO fair_prob "
            f"(phantom-edge signature, e.g. Covers DIFFERENCE bug); first: "
            f"{ex.get('player')} {ex.get('pick')} ev={ex.get('true_ev_pct')}"
        )

    # 3. Fair prob within sane range.
    for r in prop:
        fp = r.get("fair_prob")
        if fp is not None and not (0 < fp < 100):
            _fail(f"prop: fair_prob out of range {fp} for {r.get('player')}")
            break

    # 4. No duplicate prop keys.
    seen = set()
    for r in prop:
        k = (r.get("player"), r.get("market"), r.get("pick"))
        if k in seen:
            _fail(f"prop: duplicate row {k}")
            break
        seen.add(k)

    # 5. Every prop matchup (if any) belongs to the slate.
    slate = {s["matchup"] for s in ds.get("slate_games", [])}
    orphans = {r.get("matchup") for r in prop if r.get("matchup")} - slate
    if orphans:
        _fail(f"prop: matchups not in slate: {orphans}")

    if _FAILED:
        print("\nRESULT: FAILED")
        return 1
    print("\nRESULT: all integrity checks passed.")
    return 0


def main() -> int:
    allow_missing = "--allow-missing" in sys.argv[1:]
    if not os.path.exists(DATASET):
        if allow_missing:
            print(f"no dataset at {DATASET}; skipping integrity check (--allow-missing).")
            print("RESULT: skipped (no dataset present).")
            return 0
        print(f"no dataset at {DATASET}; run `python3 main.py --dry-run` first")
        return 2
    ds = json.load(open(DATASET))["dataset"]
    return validate_dataset(ds)


if __name__ == "__main__":
    sys.exit(main())
