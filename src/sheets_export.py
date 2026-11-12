"""Google Sheets creator, formatter, and exporter using the `gsheets` CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from .normalize import NL_BOOKS, NL_TZ


def _run_gsheets(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["gsheets"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
    return proc


def _val(v: Any) -> Any:
    return "" if v is None else v


def export_to_google_sheets(
    dataset: Dict,
    source_counts: Dict[str, int],
    optional_status: Dict[str, Any],
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, str]:
    """Create (or update) the NFL Week +EV Tracker Google Sheet and apply formatting."""
    now_nl = datetime.now(NL_TZ).strftime("%Y-%m-%d %I:%M %p NST")
    slate_games = dataset.get("slate_games", [])
    week_num = slate_games[0].get("week") if slate_games else ""
    title = f"NFL Week {week_num} +EV & NL Props Tracker" if week_num else "NFL +EV & NL Props Tracker"

    # 1. Create spreadsheet if no spreadsheet_id provided
    tab_names = [
        "Dashboard & Config",
        "+EV Player Props",
        "DFS Pick'em +EV",
        "+EV Game Lines",
        "ALC Vig Scanner",
        "Slate & Splits",
        "Bet Log & CLV",
    ]
    sheet_ids: Dict[str, int] = {}

    def _fetch_sheet_map(sid: str) -> Dict[str, int]:
        raw = json.loads(_run_gsheets(["readonly", "list-sheets", sid, "--json"]).stdout)
        items = raw if isinstance(raw, list) else raw.get("sheets", [])
        out: Dict[str, int] = {}
        for sh in items:
            props = sh.get("properties", sh)
            t = props.get("title", "")
            val = props.get("id") if props.get("id") is not None else props.get("sheetId")
            if t and val is not None:
                out[t] = int(val)
        return out

    if not spreadsheet_id:
        res = _run_gsheets(["mutate", "create", "--title", title])
        out_txt = res.stdout.strip()
        try:
            created = json.loads(out_txt)
            spreadsheet_id = created.get("spreadsheetId") or created.get("id")
        except json.JSONDecodeError:
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", out_txt) or re.search(r"\b([a-zA-Z0-9_-]{25,})\b", out_txt)
            spreadsheet_id = m.group(1) if m else None
        if not spreadsheet_id:
            raise RuntimeError(f"Could not parse spreadsheetId from: {out_txt}")

        sheet_ids = _fetch_sheet_map(spreadsheet_id)
        if sheet_ids and tab_names[0] not in sheet_ids:
            first_id = next(iter(sheet_ids.values()))
            _run_gsheets(["mutate", "rename-sheet", spreadsheet_id, "--sheet-id", str(first_id), "--title", tab_names[0]])
        for tname in tab_names[1:]:
            if tname not in sheet_ids:
                _run_gsheets(["mutate", "add-sheet", spreadsheet_id, "--title", tname])
        sheet_ids = _fetch_sheet_map(spreadsheet_id)
    else:
        sheet_ids = _fetch_sheet_map(spreadsheet_id)
        if "Sheet1" in sheet_ids and tab_names[0] not in sheet_ids:
            _run_gsheets(["mutate", "rename-sheet", spreadsheet_id, "--sheet-id", str(sheet_ids["Sheet1"]), "--title", tab_names[0]])
            sheet_ids = _fetch_sheet_map(spreadsheet_id)
        for tname in tab_names:
            if tname not in sheet_ids:
                _run_gsheets(["mutate", "add-sheet", spreadsheet_id, "--title", tname])
        sheet_ids = _fetch_sheet_map(spreadsheet_id)

    # ------------------------------------------------------------------
    # Build 2D rows for each tab
    # ------------------------------------------------------------------

    # TAB 2: +EV Player Props
    prop_headers = [
        "Rank", "Kickoff (NST)", "Matchup", "Player", "Pos / Inj", "Market", "Pick",
        "Best NL Book", "Best NL Odds", "Best NL Dec", "Fair Odds (No-Vig)", "Fair Prob %",
        "True EV %", "Model EV %", "EV Method", "Kelly Stake ($)",
        "ALC Proline", "Pinnacle", "bet365", "Sports Interaction", "BET99", "Betway",
        "Sleeper Line", "PrizePicks Line", "Underdog Line",
        "Consensus Proj", "Covers Proj", "BettingPros Proj", "SAO Proj", "Sleeper Proj", "RotoWire Proj",
        "Opp DVP Rank", "L5 Hit %", "L10 Hit %", "Season Hit %", "Sources",
    ]
    prop_rows_data = [prop_headers]
    for idx, r in enumerate(dataset.get("prop_ev_rows", [])[:400], start=1):
        rownum = idx + 1
        # Formula: Full Kelly = EV / (Dec - 1); Stake = Bankroll * KellyMult * FullKelly
        kelly_formula = (
            f"=IF(AND(ISNUMBER(J{rownum}),ISNUMBER(M{rownum}),J{rownum}>1,M{rownum}>0,'Dashboard & Config'!$C$5>0),"
            f"ROUND(((M{rownum}/100)/(J{rownum}-1))*'Dashboard & Config'!$C$5*MIN(1,MAX(0,'Dashboard & Config'!$C$6)),2),0)"
        )
        dfs_ref = r.get("dfs_lines") or {}
        prop_rows_data.append([
            idx,
            r["kickoff_nl"],
            r["matchup"],
            r["player"],
            r["pos_inj"],
            r["market"],
            r["pick"],
            r["best_book"],
            r["best_american"],
            r["best_decimal"],
            _val(r["fair_american"]),
            _val(r["fair_prob"]),
            _val(r["true_ev_pct"]),
            _val(r["model_ev_pct"]),
            r["ev_method"],
            kelly_formula,
            _val(r["books"].get("ALC Proline")),
            _val(r["books"].get("Pinnacle")),
            _val(r["books"].get("bet365")),
            _val(r["books"].get("Sports Interaction")),
            _val(r["books"].get("BET99")),
            _val(r["books"].get("Betway")),
            _val(dfs_ref.get("Sleeper")),
            _val(dfs_ref.get("PrizePicks")),
            _val(dfs_ref.get("Underdog")),
            _val(r["cons_proj"]),
            _val(r["covers_proj"]),
            _val(r["bp_proj"]),
            _val(r["sao_proj"]),
            _val(r.get("sleeper_proj")),
            _val(r.get("rw_proj")),
            _val(r["opp_dvp_rank"]),
            _val(r["l5_hit_pct"]),
            _val(r["l10_hit_pct"]),
            _val(r["season_hit_pct"]),
            r["sources_count"],
        ])

    # TAB 2B: DFS Pick'em +EV (Sleeper, PrizePicks, Underdog)
    dfs_headers = [
        "Rank", "Kickoff (NST)", "Matchup", "Platform", "Player", "Pos / Inj",
        "Market", "Pick", "Promo / Taco", "Orig Line",
        "Leg Odds (Am)", "Leg Dec", "Pin Line", "Pin Odds", "Fair Odds", "Fair Prob %",
        "DFS EV %", "EV Method", "Mkt Cons Line", "Cushion vs Mkt",
        "Consensus Proj", "Sleeper Proj", "RotoWire Proj", "BP Proj", "Covers Proj",
        "L5 Hit %", "L10 Hit %", "Season Hit %",
    ]
    dfs_rows_data = [dfs_headers]
    for idx, r in enumerate(dataset.get("dfs_ev_rows", [])[:400], start=1):
        dfs_rows_data.append([
            idx,
            r["kickoff_nl"],
            r["matchup"],
            r["book"],
            r["player"],
            r["pos_inj"],
            r["market"],
            r["pick"],
            r.get("promo_label", ""),
            _val(r.get("orig_line")),
            r["leg_american"],
            r["leg_decimal"],
            _val(r.get("pin_line")),
            _val(r.get("pin_american")),
            _val(r.get("fair_american")),
            _val(r.get("fair_prob")),
            _val(r.get("dfs_ev_pct")),
            r.get("ev_method", ""),
            _val(r.get("mkt_cons_line")),
            _val(r.get("cushion")),
            _val(r.get("cons_proj")),
            _val(r.get("sleeper_proj")),
            _val(r.get("rw_proj")),
            _val(r.get("bp_proj")),
            _val(r.get("covers_proj")),
            _val(r.get("l5_hit_pct")),
            _val(r.get("l10_hit_pct")),
            _val(r.get("season_hit_pct")),
        ])

    # TAB 3: +EV Game Lines
    game_headers = [
        "Kickoff (NST)", "Matchup", "Market", "Pick",
        "Best NL Book", "Best NL Odds", "Best NL Dec",
        "Pinnacle Fair Odds", "Pinnacle Fair Prob %", "EV % vs Pinnacle", "Kelly Stake ($)",
        "ALC Proline", "Pinnacle", "bet365", "Sports Interaction", "BET99", "Betway",
        "Public Tix %", "Public Money %", "Sharp Signal", "Alt Line?",
    ]
    game_rows_data = [game_headers]
    for idx, r in enumerate(dataset.get("game_ev_rows", [])[:300], start=1):
        rownum = idx + 1
        kelly_formula = (
            f"=IF(AND(ISNUMBER(G{rownum}),ISNUMBER(J{rownum}),G{rownum}>1,J{rownum}>0,'Dashboard & Config'!$C$5>0),"
            f"ROUND(((J{rownum}/100)/(G{rownum}-1))*'Dashboard & Config'!$C$5*MIN(1,MAX(0,'Dashboard & Config'!$C$6)),2),0)"
        )
        game_rows_data.append([
            r["kickoff_nl"],
            r["matchup"],
            r["market"],
            r["pick"],
            r["best_book"],
            r["best_american"],
            r["best_decimal"],
            _val(r["fair_american"]),
            _val(r["fair_prob"]),
            _val(r["ev_pct"]),
            kelly_formula,
            _val(r["books"].get("ALC Proline")),
            _val(r["books"].get("Pinnacle")),
            _val(r["books"].get("bet365")),
            _val(r["books"].get("Sports Interaction")),
            _val(r["books"].get("BET99")),
            _val(r["books"].get("Betway")),
            _val(r["tix_pct"]),
            _val(r["money_pct"]),
            r["sharp_signal"],
            "Alt" if r["is_alt"] else "Main",
        ])

    # TAB 4: ALC Vig Scanner
    alc_headers = [
        "Kickoff (NST)", "Category", "Matchup", "Player", "Market", "Pick",
        "ALC Odds (Am)", "ALC Dec", "ALC Vig %", "Pinnacle Odds", "Fair Odds (No-Vig)",
        "Fair Prob %", "Best Other NL Book", "Best Other Odds",
        "Mkt Cons Line", "Line Diff (ALC-Mkt)", "ALC EV %", "Mispricing Flag",
    ]
    alc_rows_data = [alc_headers]
    for idx, r in enumerate(dataset.get("alc_scanner_rows", [])[:400], start=1):
        rownum = idx + 1
        flag_formula = (
            f'=IF(ISNUMBER(Q{rownum}),'
            f'IF(Q{rownum}>=3,"🔥 STRONG +EV ON ALC",'
            f'IF(Q{rownum}>0,"✅ +EV / Stale Line",'
            f'IF(Q{rownum}>=-3,"⚖️ Near Fair","🚫 High Vig -EV"))),'
            f'IF(AND(ISNUMBER(P{rownum}),P{rownum}<>0),'
            f'IF(OR(AND(ISNUMBER(SEARCH("Under",F{rownum})),P{rownum}>0),AND(ISNUMBER(SEARCH("Over",F{rownum})),P{rownum}<0)),'
            f'"🔥 STALE LINE VS MKT","⚠️ Worse Line vs Mkt"),"No Benchmark"))'
        )
        alc_rows_data.append([
            r["kickoff_nl"],
            r["category"],
            r["matchup"],
            r["player"],
            r["market"],
            r["pick"],
            r["alc_american"],
            r["alc_decimal"],
            _val(r["alc_vig_pct"]),
            _val(r["pin_american"]),
            _val(r["fair_american"]),
            _val(r["fair_prob"]),
            _val(r["best_other_book"]),
            _val(r["best_other_american"]),
            _val(r.get("mkt_cons_line")),
            _val(r.get("line_diff")),
            _val(r["alc_ev_pct"]),
            flag_formula,
        ])

    # TAB 5: Slate & Splits
    slate_headers = [
        "Week", "Kickoff (NST)", "Matchup", "Venue", "Indoor?", "Weather",
        "Away Spread Tix %", "Away Spread Money %", "Over Total Tix %", "Over Total Money %",
        "Away ML Tix %", "Away ML Money %",
    ]
    slate_rows_data = [slate_headers]
    splits = dataset.get("splits", {})
    for g in slate_games:
        sp = splits.get(g["matchup"], {})
        slate_rows_data.append([
            _val(g.get("week")),
            g.get("kickoff_nl", ""),
            g.get("matchup", ""),
            g.get("venue", ""),
            "Dome" if g.get("indoor") else "Outdoor",
            g.get("weather", ""),
            _val(sp.get("away_spread_tickets_pct")),
            _val(sp.get("away_spread_money_pct")),
            _val(sp.get("over_tickets_pct")),
            _val(sp.get("over_money_pct")),
            _val(sp.get("away_ml_tickets_pct")),
            _val(sp.get("away_ml_money_pct")),
        ])

    # TAB 6: Bet Log & CLV
    betlog_headers = [
        "Date Logged", "Matchup", "Book (NL)", "Market", "Pick",
        "Odds (Am)", "Decimal", "Closing Odds (Am)", "Stake ($)", "Result",
        "To Win ($)", "Net P/L ($)", "CLV %", "Notes",
    ]
    betlog_rows_data = [betlog_headers]
    for rnum in range(2, 52):
        betlog_rows_data.append([
            "", "", "", "", "", "",
            f'=IF(OR(ISBLANK(F{rnum}),F{rnum}=0),"",IF(F{rnum}>0,1+F{rnum}/100,1+100/ABS(F{rnum})))',
            "", "", "",
            f'=IF(OR(ISBLANK(G{rnum}),ISBLANK(I{rnum})),"",ROUND(I{rnum}*(G{rnum}-1),2))',
            f'=IF(J{rnum}="Win",K{rnum},IF(J{rnum}="Loss",-I{rnum},IF(J{rnum}="Push",0,"")))',
            f'=IF(OR(ISBLANK(G{rnum}),ISBLANK(H{rnum}),H{rnum}=0),"",ROUND((G{rnum}/(IF(H{rnum}>0,1+H{rnum}/100,1+100/ABS(H{rnum})))-1)*100,2))',
            "",
        ])

    # TAB 1: Dashboard & Config
    odds_api_stat = "ACTIVE" if optional_status.get("the_odds_api") else "READY (Add THE_ODDS_API_KEY in .env)"
    propline_stat = "ACTIVE" if optional_status.get("propline") else "READY (Add PROPLINE_API_KEY in .env)"
    bdl_stat = "ACTIVE" if optional_status.get("balldontlie") else "READY (Add BALLDONTLIE_API_KEY in .env)"
    dash_rows_data = [
        ["NFL +EV & NEWFOUNDLAND SPORTSBOOK TRACKER", "", "", "", "Last Updated (NST):", now_nl],
        [f"NFL Week {week_num} Slate | Newfoundland Books Only: ALC Proline, Pinnacle, bet365, Sports Interaction, BET99, Betway", "", "", "", "Total Slate Games:", len(slate_games)],
        ["", "", "", "", "", ""],
        ["BANKROLL & SIZING CONFIG (Editable)", "", "Value", "", "LIVE WORKBOOK KPIs", "Value"],
        ["", "Starting Bankroll ($)", 1000, "", "Positive EV Player Props (>0%)", "=COUNTIF('+EV Player Props'!M2:M500,\">0\")"],
        ["", "Kelly Multiplier (0.25 = Quarter)", 0.25, "", "Best Player Prop EV %", "=IFERROR(MAX('+EV Player Props'!M2:M500),0)"],
        ["", "Min EV % Threshold", 2.0, "", "Positive EV Game Lines (>0%)", "=COUNTIF('+EV Game Lines'!J2:J400,\">0\")"],
        ["", "", "", "", "Best Game Line EV %", "=IFERROR(MAX('+EV Game Lines'!J2:J400),0)"],
        ["", "", "", "", "DFS Pick'em +EV / Promos", "=COUNTIF('DFS Pick\\'em +EV'!Q2:Q500,\">0\")+COUNTA('DFS Pick\\'em +EV'!I2:I500)-COUNTBLANK('DFS Pick\\'em +EV'!I2:I500)"],
        ["", "", "", "", "ALC +EV / Stale Lines", "=COUNTIF('ALC Vig Scanner'!Q2:Q500,\">0\")+COUNTIF('ALC Vig Scanner'!R2:R500,\"*STALE LINE*\")"],
        ["", "", "", "", "Avg ALC Vig % (Scanned)", "=IFERROR(ROUND(AVERAGE('ALC Vig Scanner'!I2:I500),2),0)"],
        ["", "", "", "", "Bet Log Total Staked ($)", "=SUM('Bet Log & CLV'!I2:I100)"],
        ["", "", "", "", "Bet Log Net P/L ($)", "=SUM('Bet Log & CLV'!L2:L100)"],
        ["", "", "", "", "", ""],
        ["DATA SOURCES & SCRAPERS", "Method", "Records Pulled", "Status", "Notes / Coverage", ""],
        ["ALC Proline & Stadium Bets", "Scrapling (NL Local IP)", source_counts.get("alc_total", 0), "ACTIVE", "Game lines, alt spreads, totals, Passing/Rushing/Rec/TD props", ""],
        ["Pinnacle Guest API", "Arcadia JSON API", source_counts.get("pinnacle_total", 0), "ACTIVE", "Sharp No-Vig Fair Odds benchmark + NL-playable", ""],
        ["Covers.com (NL Localized)", "Scrapling (14 matchups)", source_counts.get("covers_total", 0), "ACTIVE", "bet365, Sports Interaction, BET99, Betway odds + EV model", ""],
        ["BettingPros", "Scrapling (Paginated)", source_counts.get("bp_total", 0), "ACTIVE", "Projections, fair odds, EV, Opp DVP rank, L5/L10/Season hit %", ""],
        ["Action Network", "Public API + SSR", source_counts.get("action_total", 0), "ACTIVE", "bet365 CA, SIA, BET99, Betway CA + Ticket/Money splits", ""],
        ["ScoresAndOdds", "Scrapling", source_counts.get("sao_total", 0), "ACTIVE", "Independent player prop projections & market consensus", ""],
        ["Sleeper (Picks + Projections)", "Sleeper API + RotoWire", source_counts.get("sleeper_total", 0), "ACTIVE", "Sleeper Picks odds/lines + Sleeper weekly projections + weather", ""],
        ["PrizePicks", "Partner API", source_counts.get("prizepicks_total", 0), "ACTIVE", "Standard Pick'em lines + live Taco / flash-sale promos", ""],
        ["Underdog Fantasy", "Public v1 JSON API", source_counts.get("underdog_total", 0), "ACTIVE", "Higher/Lower lines, exact leg prices & multipliers", ""],
        ["ESPN NFL Scoreboard", "Public JSON API", source_counts.get("espn_games", 0), "ACTIVE", "Canonical Week schedule, NST kickoffs, Dome/Outdoor venue", ""],
        ["The Odds API v4", "REST API (Optional Key)", source_counts.get("odds_api_total", 0), odds_api_stat, "Set THE_ODDS_API_KEY in .env to expand prop/book coverage", ""],
        ["PropLine API v1", "REST API (Optional Key)", source_counts.get("propline_total", 0), propline_stat, "Set PROPLINE_API_KEY in .env for cross-book & Pick'em props", ""],
        ["Balldontlie NFL Props API", "REST API (Optional Key)", source_counts.get("bdl_total", 0), bdl_stat, "Set BALLDONTLIE_API_KEY in .env to add milestone/O-U props", ""],
    ]

    # ------------------------------------------------------------------
    # Write all tabs in a single batch JSON operation (clearing first if updating)
    # ------------------------------------------------------------------
    batch_ops = []
    tab_payloads = [
        ("Dashboard & Config", dash_rows_data),
        ("+EV Player Props", prop_rows_data),
        ("DFS Pick'em +EV", dfs_rows_data),
        ("+EV Game Lines", game_rows_data),
        ("ALC Vig Scanner", alc_rows_data),
        ("Slate & Splits", slate_rows_data),
        ("Bet Log & CLV", betlog_rows_data),
    ]
    for tname, rows in tab_payloads:
        batch_ops.append({"op": "clear", "range": f"'{tname}'!A1:AL500"})
        batch_ops.append({"op": "write", "range": f"'{tname}'!A1", "data": rows})

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(batch_ops, tf)
        batch_path = tf.name

    try:
        _run_gsheets(["mutate", "batch", spreadsheet_id, "-f", batch_path])
    finally:
        os.unlink(batch_path)

    # ------------------------------------------------------------------
    # Format all tabs cleanly (headers, freeze, widths, conditional format, validation)
    # ------------------------------------------------------------------
    format_cmds = [
        # Dashboard header & subheaders
        ["mutate", "format", spreadsheet_id, "--sheet-id", str(sheet_ids["Dashboard & Config"]),
         "--start-row", "0", "--end-row", "2", "--start-col", "0", "--end-col", "6",
         "--bold", "--bg-color", "#1B365D", "--fg-color", "#FFFFFF"],
        ["mutate", "format", spreadsheet_id, "--sheet-id", str(sheet_ids["Dashboard & Config"]),
         "--start-row", "3", "--end-row", "4", "--start-col", "0", "--end-col", "6",
         "--bold", "--bg-color", "#D9E1F2", "--fg-color", "#1B365D"],
        ["mutate", "format", spreadsheet_id, "--sheet-id", str(sheet_ids["Dashboard & Config"]),
         "--start-row", "14", "--end-row", "15", "--start-col", "0", "--end-col", "5",
         "--bold", "--bg-color", "#1B365D", "--fg-color", "#FFFFFF"],
        # Editable config cells highlight
        ["mutate", "format", spreadsheet_id, "--sheet-id", str(sheet_ids["Dashboard & Config"]),
         "--start-row", "4", "--end-row", "7", "--start-col", "2", "--end-col", "3",
         "--bold", "--bg-color", "#FFF2CC", "--fg-color", "#000000"],
        ["mutate", "autosize", spreadsheet_id, "--sheet-id", str(sheet_ids["Dashboard & Config"]),
         "--start-col", "0", "--end-col", "6"],
    ]

    for tname, col_count in [
        ("+EV Player Props", len(prop_headers)),
        ("DFS Pick'em +EV", len(dfs_headers)),
        ("+EV Game Lines", len(game_headers)),
        ("ALC Vig Scanner", len(alc_headers)),
        ("Slate & Splits", len(slate_headers)),
        ("Bet Log & CLV", len(betlog_headers)),
    ]:
        sid = str(sheet_ids[tname])
        format_cmds.extend([
            ["mutate", "format", spreadsheet_id, "--sheet-id", sid,
             "--start-row", "0", "--end-row", "1", "--start-col", "0", "--end-col", str(col_count),
             "--bold", "--bg-color", "#1B365D", "--fg-color", "#FFFFFF", "--align", "CENTER", "--wrap"],
            ["mutate", "freeze", spreadsheet_id, "--sheet-id", sid, "--rows", "1", "--cols", "1"],
            ["mutate", "autosize", spreadsheet_id, "--sheet-id", sid, "--start-col", "0", "--end-col", str(col_count)],
        ])

    # Conditional formatting for EV% columns
    format_cmds.extend([
        # +EV Player Props True EV % (col M = index 12)
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["+EV Player Props"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "12", "--end-col", "13",
         "--condition", "NUMBER_GREATER", "--value", "3", "--color", "#B7E1CD"],
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["+EV Player Props"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "12", "--end-col", "13",
         "--condition", "NUMBER_BETWEEN", "--value", "0,3", "--color", "#E6F4EA"],
        # DFS Pick'em +EV DFS EV % (col Q = index 16) & Cushion (col T = index 19)
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["DFS Pick'em +EV"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "16", "--end-col", "17",
         "--condition", "NUMBER_GREATER", "--value", "3", "--color", "#B7E1CD"],
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["DFS Pick'em +EV"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "16", "--end-col", "17",
         "--condition", "NUMBER_BETWEEN", "--value", "0,3", "--color", "#E6F4EA"],
        # +EV Game Lines EV % (col J = index 9)
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["+EV Game Lines"]),
         "--start-row", "1", "--end-row", "350", "--start-col", "9", "--end-col", "10",
         "--condition", "NUMBER_GREATER", "--value", "2", "--color", "#B7E1CD"],
        # ALC Vig Scanner ALC EV % (col Q = index 16)
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["ALC Vig Scanner"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "16", "--end-col", "17",
         "--condition", "NUMBER_GREATER", "--value", "0", "--color", "#B7E1CD"],
        ["mutate", "conditional-format", spreadsheet_id, "--sheet-id", str(sheet_ids["ALC Vig Scanner"]),
         "--start-row", "1", "--end-row", "450", "--start-col", "16", "--end-col", "17",
         "--condition", "NUMBER_LESS", "--value", "-5", "--color", "#F4CCCC"],
        # Bet Log dropdowns
        ["mutate", "data-validation", spreadsheet_id, "'Bet Log & CLV'!C2:C100",
         "--values", ",".join(NL_BOOKS)],
        ["mutate", "data-validation", spreadsheet_id, "'Bet Log & CLV'!J2:J100",
         "--values", "Pending,Win,Loss,Push,Cashout"],
    ])

    for cmd in format_cmds:
        _run_gsheets(cmd, check=False)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return {"spreadsheet_id": spreadsheet_id, "url": url, "title": title}
