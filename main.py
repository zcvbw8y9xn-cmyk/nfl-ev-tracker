"""CLI entrypoint: scrape all NFL sources, compute +EV & ALC mispricings, export to Google Sheets."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


from src.pipeline import load_local_env, run_pipeline
from src.sheets_export import export_to_google_sheets


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(description="NFL +EV & Newfoundland Sportsbook Tracker")
    parser.add_argument("--spreadsheet-id", default=os.getenv("SPREADSHEET_ID", ""), help="Existing Google Sheet ID to update")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and print summary without writing to Google Sheets")
    args = parser.parse_args()

    result = run_pipeline(log_fn=print)
    dataset = result["dataset"]
    source_counts = result["source_counts"]
    optional_status = result["optional_status"]

    print("\n=== AGGREGATION SUMMARY ===")
    print(f"  +EV Player Props rows : {len(dataset['prop_ev_rows'])}")
    print(f"  DFS Pick'em +EV rows  : {len(dataset['dfs_ev_rows'])}")
    print(f"  +EV Game Lines rows   : {len(dataset['game_ev_rows'])}")
    print(f"  ALC Scanner rows      : {len(dataset['alc_scanner_rows'])}")

    print("\nTop 5 +EV Player Props:")
    for r in dataset["prop_ev_rows"][:5]:
        ba = f"{r['best_american']:+d}" if r.get("best_american") is not None else "N/A"
        print(f"  - {r['player']} | {r['market']} {r['pick']} | Best: {r['best_book']} ({ba}) | True EV: {r['true_ev_pct']}% | Method: {r['ev_method']}")

    print("\nTop 5 DFS Pick'em Plays (Sleeper / PrizePicks / Underdog):")
    for r in dataset["dfs_ev_rows"][:5]:
        tag = f" [{r['promo_label']}]" if r.get("promo_label") else ""
        ev_str = f"{r['dfs_ev_pct']}%" if r.get("dfs_ev_pct") is not None else "N/A"
        cush_str = f"{r['cushion']:+.1f}" if r.get("cushion") is not None else "0.0"
        leg_am = f"{r['leg_american']:+d}" if r.get("leg_american") is not None else "N/A"
        print(f"  - [{r['book']}]{tag} {r['player']} | {r['market']} {r['pick']} | Odds: {leg_am} | EV: {ev_str} | Cushion: {cush_str}")

    print("\nTop 5 ALC Mispricings / Lowest Vig:")
    for r in dataset["alc_scanner_rows"][:5]:
        alc_am = f"{r['alc_american']:+d}" if r.get("alc_american") is not None else "N/A"
        print(f"  - {r['matchup']} {r['player']} | {r['market']} {r['pick']} | ALC: {alc_am} vs Fair: {r['fair_american']} | ALC EV: {r['alc_ev_pct']}%")

    # Automatic dataset integrity check (non-fatal). Catches regressions such as the
    # Covers "DIFFERENCE %" phantom-EV bug or an orphaned market/team that silently
    # drops data. Runs on the just-built dataset (in-process), so it works even on a
    # dry-run. Set NFL_EV_SKIP_VALIDATE=1 to skip.
    if not os.getenv("NFL_EV_SKIP_VALIDATE"):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
            import validate_dataset as _vd  # noqa: WPS433
            print("\n=== DATASET INTEGRITY CHECK ===")
            _rc = _vd.validate_dataset(dataset)
            if _rc != 0:
                print("WARNING: dataset integrity check FAILED - see messages above before trusting output.")
        except Exception as _exc:  # noqa: BLE001
            print(f"WARNING: dataset integrity check could not run: {type(_exc).__name__}: {_exc}")

    if args.dry_run:
        return

    print("\nExporting to Google Sheets...")
    res = export_to_google_sheets(
        dataset=dataset,
        source_counts=source_counts,
        optional_status=optional_status,
        spreadsheet_id=args.spreadsheet_id.strip() or None,
    )
    print("SUCCESS! Google Sheet created/updated:")
    print(f"  Title: {res['title']}")
    print(f"  ID:    {res['spreadsheet_id']}")
    print(f"  URL:   {res['url']}")


if __name__ == "__main__":
    main()
