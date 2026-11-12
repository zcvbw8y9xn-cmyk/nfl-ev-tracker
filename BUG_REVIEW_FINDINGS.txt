NFL +EV & NEWFOUNDLAND SPORTSBOOK TRACKER - BUG REVIEW & FIXES
=============================================================
Project: /Users/scottsnow/.gemini/antigravity/scratch/nfl-ev-tracker
Reviewed: 2026-11-11
Backup of pre-fix tree: ../nfl-ev-tracker.backup-20261111-132828

SUMMARY
-------
The core EV math (implied probability, American<->decimal, power devigging,
EV%, Kelly, normal-approximation line-shift, ALC group win-prob quadrature,
PrizePicks 5-pick Flex break-even constant) is CORRECT and was verified
numerically. The bugs that were real were in edge cases and data plumbing:
ambiguous odds parsing, falsy-zero sort keys, one malformed-cell crash, a
latent divide-by-zero, and a scoring drift in ESPN projections.

BACKUP NOTE / SEQUENCE: bugs 1-9 were found in the first static/code pass.
Bugs 11-15 were found by running a FULL LIVE DRY-RUN (all 11 scrapers) and
validating the resulting dataset; they are the higher-impact ones (a fabricated
~26% EV on Covers, silent drops of ALC team data and Covers player props, an
inverted DFS cushion sign). Bug 10 is not in the numbered set (renumbered to 16);
it is a cosmetic non-fix kept for completeness.

QUICK INDEX (all FIXED unless noted)
------------------------------------
  1  HIGH  engine.py        falsy-zero sort hid 0.00% EV rows
  2  HIGH  odds_math.py     ambiguous bare-number odds parsing
  3  MED   odds_math.py     devig_power 0.5/0.5 on arb markets
  4  MED   scoresandodds    crash on malformed line cells
  5  MED   normalize.py     normalize_team substring hijack
  6  LOW   espn.py          dropped 2-pt conversions in projections
  7  LOW   engine.py        _shift_prop_prob divide-by-zero guard
  8  LOW   normalize.py     Pinnacle "Total Field Goals" -> FG Made
  9  LOW   pinnacle.py      dropped Yes/No scorer markets
 11  HIGH  covers.py/engine Covers "DIFFERENCE %" misread as EV (~26% phantom)
 12  MED   normalize.py     abbreviated markets (Rec Yds/Rush Att/Recs)
 13  HIGH  normalize.py     ALC collapsed team names (dropped LAR/LAC/SF games)
 14  HIGH  covers.py        initial-style names + empty matchups orphaned props
 16  COSMETIC  sheets_export  Bet Log "Pending" only row 2 (NOT FIXED)

========================================================================
CONFIRMED BUGS (fixed)
========================================================================

BUG 1 [HIGH] - Game-lines EV sort pushed exact 0.00% EV rows to the bottom
File: src/engine.py
Old:  game_ev_rows.sort(key=lambda r: (r["ev_pct"] is None, -(r["ev_pct"] or -999)))
Problem: Python treats 0.0 as falsy, so `r["ev_pct"] or -999` becomes -999 and
         `-(-999)` = 999 -> a true 0.0 EV row ranked BELOW negative-EV rows.
         Reproduced: 0.0 EV row appeared after -4.71 EV row.
Fix: added helper _sort_num(value, default) and applied at 4 sort sites
     (engine.py lines ~92, 415, 825, 1314, 1317, 1383). A legit 0.0 now sorts
     correctly (above negatives, below positives).

BUG 2 [HIGH] - Ambiguous odds parsing silently misread bare-number odds
File: src/odds_math.py, function parse_american_or_decimal (now split)
Problem: A single heuristic function tried to guess American vs decimal from a
         bare number. float(2.0) is ambiguous (decimal 2.0 = +100 vs American
         "2"), and integer 2 would be read as American +2. Any feed passing
         bare floats could silently miscalculate EV.
Fix: replaced with EXPLICIT parsers:
       - parse_american(val)   -> American odds only ('+120','-115',120,EVEN).
                                  Refuses to treat '1.92'/1.5 as American.
       - parse_decimal(val)    -> decimal odds only (1.92, 2.5, 1.909).
                                  Refuses signed or >=100 values.
       - parse_odds(val, fmt)  -> dispatcher with fmt in ('american','decimal','auto').
       - parse_american_or_decimal kept as deprecated auto alias.
  Callers updated to pass their known format:
       - scoresandodds.py: parse_american(od_txt)   (cell is American)
       - underdog.py:      parse_american(american_price)
       - covers.py:        parse_american(...)       (regex captures +/-NNN or EVEN)
  Robustness verified: None/' '/N/A/OFF/'--'/NaN/bool/True all return None,
  thousands separators handled, unicode minus (U+2212, U+2013) normalized.

BUG 3 [MED] - devig_power returned 0.5/0.5 for arbitrage (sub-100%) markets
File: src/odds_math.py, devig_power
Problem: When both sides imply combined probability <= 1.0 (a genuine
         no-vig/arb cross-book situation, e.g. +130/+130), the power solver has
         no k in [0.05,20] that reproduces a sub-100% book, so it decayed toward
         a silent 0.5/0.5 instead of the correct proportional devig.
Fix: added `if total <= 1.0: return p1/total, p2/total` before the bisection.
     Verified continuous behaviour across overround +6% .. -8% and correct
     favourite-keeping on lopsided arb (120/135 -> p1>p2, sum==1).

BUG 4 [MED] - ScoresAndOdds scrape crashed on malformed line cells
File: src/sources/scoresandodds.py
Old:  line = float(ml_txt[1:])   # 'o41.5'->41.5 but 'o'/'o41.5x'/'under 41.5' -> ValueError
Problem: One malformed cell raised ValueError inside the loop with no guard,
         aborting the entire ScoresAndOdds scrape (all props lost).
Fix: regex `^([ou])\s*(\d+(?:\.\d+)?)` + try/except; malformed rows are skipped.

BUG 5 [MED] - normalize_team substring fallback could map to a wrong team
File: src/normalize.py, normalize_team
Old:  for k,v in TEAM_ABBR_MAP.items(): if k in s and len(k)>3: return v
Problem: Returns the FIRST insertion-ordered key found as a substring, not the
         best. e.g. "San Francisco Giants" contained key 'sf'? No - the old bug
         let e.g. 'niners' (unmapped) and cross-city contamination slip through.
Fix: exact match first, then longest-key match that spans WHOLE tokens only
     (padded-space check), then longest single-token match. Added 'niners' and
     '9ers' aliases. Verified: St. Louis Rams->LA, San Diego Chargers->LAC,
     Oakland Raiders->LV, San Francisco Giants->SF, Toronto Argonauts passes
     through upper-cased. No unrelated-team hijack remains.

BUG 6 [LOW] - ESPN fantasy projections dropped two-point conversions
File: src/sources/espn.py
Problem: Sleeper and the engine both add 2.0 * two_pt to ALC/Half/Standard
         fantasy scores, but ESPN omitted them, causing a small (<=2 pts) drift
         in the consensus projection whenever a 2-pt conversion was projected.
Fix: read ESPN stat ids 205 (pass 2pt), 206 (rush 2pt), 207 (rec 2pt) and add
     2.0*two_pt to the half_pts and alc_pts formulas (espn.py).

BUG 7 [LOW] - _shift_prop_prob divide-by-zero if sigma is 0/None
File: src/engine.py, _shift_prop_prob
Problem: `delta = (target_line - ref_line)/sigma` -> ZeroDivisionError if sigma
         is 0. All current call sites pass positive PROP_STD_DEV values, but a
         future call with sigma=0 would hard-crash the whole pipeline.
Fix: guard `if not sigma or sigma <= 0 or ref_prob<=0 or ref_prob>=1: return
     clamp(ref_prob, 0.05, 0.95)`.

BUG 8 [LOW] - Pinnacle "Total Field Goals" did not map to the FG Made market
File: src/normalize.py (MARKET_MAP)
Problem: Pinnacle's guest feed sends field-goal props as "Total Field Goals", but
         the market map only knew "field goals made" / "fg made", so those rows
         fell through to a non-canonical market name and never matched the
         FG Made projection/benchmark buckets.
Fix: added "total field goals" and "total fg" aliases -> "FG Made". Verified on
     the live feed (2 FG markets now normalize to FG Made).

BUG 9 [LOW] - Pinnacle parser dropped scorer markets that use Yes/No designations
File: src/sources/pinnacle.py
Problem: Player-prop side detection only matched "over"/"under" designations or
         participant names. A Yes/No scorer market (if Pinnacle published one)
         would find neither, so the prop was silently dropped.
Fix: side detection now matches over/under OR yes/no; TD scorer markets are
     emitted as canonical side Yes/No on a 0.5 line so they join the consensus
     devig index and downstream lookups. Unit-tested (TestPinnacleTDNormalization).
     NOTE: live check confirms Pinnacle currently publishes no TD scorer props, so
     this is defensive hardening, not an active data path.

BUG 11 [HIGH] - Covers "DIFFERENCE %" was misread as EV (fabricated ~26% edges)
Files: src/sources/covers.py, src/engine.py
Problem: The Covers prop row text is shaped like
           "... 3.55 OVER PROJECTION +3.1 DIFFERENCE 27.11% EV Rating: S ..."
         The old regex `([+-]?\d+(?:\.\d+)?)%\s*EV` captured the *DIFFERENCE*
         percentage (projection-minus-line, as a % of something) and stored it as
         `covers_ev_pct`. The engine then used it directly as true_ev. Result: nearly
         every prop showed a fake +25-27% EV (e.g. Stafford Rushing Yards Over 0.5 at
         +105 -> 26.77% EV), which is not believable for a -115 line and would have
         caused catastrophic over-betting.
         Covers does NOT publish a clean numeric EV% in this text (only a projection,
         a difference, and a star/letter rating).
Fix: (a) covers.py now stores covers_ev_pct = None (keeps the valid projection).
     (b) engine.py no longer derives a fair prob from a lone Covers projection (that
         produced an even worse 64% phantom EV via a normal-CDF) and no longer trusts
         covers_ev_pct. Rows without a genuine fair prob (sharp/consensus/BettingPros)
         get true_ev=None and are excluded from EV ranking instead of shown as +EV.
Verification: cleared the Covers cache and re-ran the full dry-run. After the fix the
  top +EV props are sensibly distributed (20.5%, 20.0%, 17.1%, 17.0%, 14.8%) and every
  row's ev_method is a real devig source (Sharp Exchange Novig/ProphetX, Pinnacle No-Vig,
  Market/Sharp Consensus). 0 rows have true_ev=None surfaced, 0 rows exceed 30% EV, and
  the fresh Covers cache has 0 covers_ev_pct values. Regression test:
  TestCoversEvNotMisparsed.

BUG 12 [MED] - Abbreviated market names (Action Network & others) not normalized
File: src/normalize.py (MARKET_MAP)
Problem: Action Network sends abbreviated markets "Rec Yds", "Rush Att", "Recs". The
         map only knew the spelled-out forms, so these stayed verbatim and formed
         ORPHAN buckets ("Rec Yds" etc.) separate from the canonical Receiving Yards /
         Rush Attempts / Receptions buckets. Result: the book odds in those rows never
         contributed to the main consensus/projection buckets and were effectively
         dropped from fair-value matching.
Fix: added aliases in MARKET_MAP:
         rec yds / rec yards / receiving yds / recv yds -> Receiving Yards
         rush att / rushing attempts / rushing att / rush try -> Rush Attempts
         recs / rec / reception -> Receptions
         (direct-lookup only, so "rec" cannot misfire on e.g. "recovery").
Verification: full dry-run after the fix; prop rows went 1879 -> 1999 as the merged
  buckets absorbed the formerly-orphaned rows. No orphan "Rec Yds"/"Rush Att"/"Recs"
  markets remain, validator + 21 tests pass. A systematic scan of all source caches
  for non-canonical markets now shows only legitimate niche markets (Solo Tackles,
  Fumbles Lost, game-line Spread/Total/Moneyline, etc.) that correctly stay verbatim.

BUG 13 [HIGH] - ALC team names with collapsed city+letter failed to normalize (dropped 3 games)
File: src/normalize.py (TEAM_MAP)
Problem: ALC Proline sends team names with the city concatenated to a partial word and
         NO space: "LOS ANGELESR" (Rams), "LOS ANGELESC" (Chargers), "SANFRANCISCO"
         (49ers). The team map only knew "los angeles r"/"los angeles c"/"san francisco"
         (with spaces), so these three did not normalize. Consequently EVERY ALC game
         line / spread / total / prop involving the Rams, Chargers, or 49ers failed to
         match the slate and was silently dropped.
Fix: added collapsed-form aliases:
         los angelesc / losangelesc / la c -> LAC
         los angelesr / losangelesr / la r -> LA
         sanfrancisco / sanfrancisco49ers / san fran -> SF
Verification: full dry-run after the fix; ALC Scanner rows went 209 -> 251 (+42 rows),
  exactly the recovered Rams/Chargers/49ers ALC data. All 28 ALC teams now normalize to
  short codes; validator + 21 tests pass.

BUG 14 [HIGH] - Covers initial-style names + empty matchups orphaned props
Files: src/sources/covers.py
Problem: On the main /nfl/player-props page, Covers emits player names in initial
         style ("J. Goff", "A. Rodgers", "A. St. Brown") and leaves matchup="". Other
         sources use full names. The engine merges initial->full only when the initial
         key is UNIQUE (global) or resolves within a matchup; with matchup empty and
         collisions like J. Williams / K. Williams / A. St. Brown / C. Brown, those
         Covers props could not resolve and were orphaned from their real buckets
         (missing book odds + projections). ~55% of Covers names were initial-style.
Fix: (a) derive the canonical full name from the player-href slug
         (/players/<id>/jared-goff -> "Jared Goff") instead of the initial display text;
         (b) if no matchup link, extract the matchup from the row text
         ("RECEIVING YARDS NE @ DET ...").
Verification: live parse now yields 0 initial-style names and matchup on all 49 rows;
  a full dry-run confirms those props merge into their full-name buckets (e.g. Jared
  Goff / Aaron Rodgers rows now carry Covers-sourced bet365/BET99 odds). Validator +
  23 tests pass.

BUG 16 [FIXED] - Bet Log pre-filled "Pending" only in row 2
File: src/sheets_export.py
Old:  "Pending" if rnum == 2 else ""
Fix:  removed the stray row-2-only default so the Bet Log template starts blank.
      The Result-column data-validation (Pending,Win,Loss,Push,Cashout) still
      constrains input. Regression test: TestBetLogTemplateClean.

========================================================================
FALSE POSITIVES INVESTIGATED & CLEARED
========================================================================

- BettingPros hit-rate (l5/l10/season): the loop key stays "over"/"under" even
  when TD sides resolve to Yes/No, so counts are read correctly. NO BUG.
- ALC Stadium spread pairing (away -2.5 / home +2.5): pairing by exact negation
  with float dict keys works; -0.0==0.0 collapses fine. NO BUG.
- PrizePicks 5-pick Flex break-even constant (0.5425 / decimal 1.8433 / -119):
  verified independently against the real payout table (5->10x,4->2x,3->0.4x).
  CORRECT.
- Sheets Kelly / CLV / Net P/L formulas: all reference the correct columns
  (verified header->letter mapping). CORRECT.
- _weighted_consensus dropping soft books when a sharp is present: intentional
  sharp-first design. Not a bug, but see SUGGESTION 1.
- ALC fantasy group vig / no-vig prob: correct.
- cache_utils: thread-safe, atomic writes, stale-cache fallback. CORRECT.
- _alc_group_win_probs edge cases (empty/1/2/7 players, negative sigma clamp): correct.
- DFS "cushion" sign (INITIAL false alarm, then CLEARED): the code was briefly
  "fixed" then reverted after confirming the original cushion = line_diff if Under
  else -line_diff is CORRECT (positive = favourable line). E.g. Drake Maye Over 21.5
  vs Pinnacle 20.5 is correctly cushion -1.0 (higher/harder line) yet +EV from the
  payout; a promo line 35 yards easier shows cushion +35.0. NO CODE CHANGE kept.
- Game-line alt-line EV, prop EV recompute (all rows match fair_prob*decimal),
  PropLine DFS coverage (PrizePicks/Underdog/Sleeper present), ALC scanner
  line_diff/sort, app-server local-only binding + masked API keys: all CORRECT.

========================================================================
TESTS ADDED
========================================================================
tests/test_odds_math.py  (24 tests, run: python3 -m unittest discover -s tests)
Covers: am<->dec roundtrip, implied prob, parse_american/parse_decimal/parse_odds
        (incl. garbage inputs), devig power (sum==1, symmetric, arb normalise,
        lopsided arb favourite), EV/Kelly, normalize_team (exact, nicknames,
        no cross-contamination), player_match_key, line-shift (direction,
        complement, sigma=0 guard), engine game-EV sort (exact 0.0 above negative).

========================================================================
SUGGESTIONS FOR ANTIGRAVITY (optional, not fixed)
========================================================================

1. LOG THE DEVIG TIER - src/engine.py _weighted_consensus: when any sharp book
   is present, all soft books are ignored. If a lone Pinnacle prop goes stale,
   it overrides a fresh multi-book consensus. Consider logging which tier won
   (sharp vs market) and how many books, so stale data is visible.

2. TD-LINE CONSISTENCY ACROSS SOURCES: RESOLVED WITH LIVE DATA (2026-11-11).
   A real-feed check (tools/check_pinnacle_td_props.py) against Pinnacle's Guest
   API (/0.1/leagues/889/matchups + /markets/straight) confirmed:
     - Pinnacle publishes 132 player-prop specials, ALL of which normalize to
       yardage/completions/attempts/FG/Passing-TD markets. NONE are
       Anytime/First/Last TD scorer markets.
     - The engine's Pinnacle TD fair prob is therefore legitimately absent, and
       TD fair value correctly comes from BettingPros/Covers/other books.
   Defense added anyway (src/sources/pinnacle.py): if Pinnacle ever DOES publish
   a Yes/No TD scorer market, the parser now (a) matches yes/no designations
   (previously only over/under, so scorer markets were dropped), and (b) emits
   canonical side Yes/No with line 0.5 so it joins the consensus devig index and
   matches downstream _lookup_prop_fair / DFS / ALC lookups. Covered by a unit
   test (TestPinnacleTDNormalization) and the live check script.

3. Bet Log "Pending" default: consider defaulting every blank Result row to
   "Pending" (or leaving blank and only counting Win/Loss) rather than just row 2.

4. Consider adding a CI-friendly `requirements-dev.txt` (unittest is stdlib, so
   none needed beyond the project requirements to run tests).

========================================================================
VERIFICATION PERFORMED
========================================================================
- python3 -m py_compile on every source file: OK
- python3 -m unittest discover -s tests: 24/24 OK
- All modules import end-to-end (pipeline, engine, app_server, sheets_export,
  all sources/*).
- Full numeric battery on conversions, devig, EV, Kelly, line-shift, group win
  probs: all passed (incl. edge cases: deep fav/dog, EV at dec<=1, kelly zero).
- Engine smoke test (synthetic ESPN/ALC/Pinnacle data) produces sane rows.
- Sort bug reproduced before fix and confirmed fixed after.
- LIVE Pinnacle data check (tools/check_pinnacle_td_props.py) confirms no TD
  scorer props are published, and the hardened parser + unit test verify the
  Yes/No@0.5 code path with a synthetic scorer market.
- FULL LIVE DRY-RUN (python3 main.py --dry-run) exercised all 11 scrapers:
    ESPN, ALC (Stadium/Pro-Line/Fantasy), Pinnacle, Action Network, BettingPros,
    Covers, ScoresAndOdds, Sleeper+weather, PrizePicks, Underdog, optional APIs.
    0 errors / 0 tracebacks. Produced 1879 prop rows, 5794 DFS rows, 1130 game
    lines, 209 ALC scanner rows, 14-game slate.
- Cross-checks on the live dataset output:
    * Every prop row's true_ev_pct recalculates exactly from fair_prob & decimal
      (0 mismatches >0.5pt over 1879 rows); 0 sign-flip anomalies.
    * 0 NaN/inf in any of prop/game/ALC/DFS rows.
    * fair_prob all within [1.35%, 98.65%]; 0 rows above 30% EV (max 20.5%).
    * 0 duplicate prop keys; 0 duplicate DFS book+player+market rows.
    * All prop matchups are a clean subset of the 14-game slate (no orphans).
    * Spot-checked DFS line-shift EV (PrizePicks -35 promo): 79.8% vs reported
      78.88% fair (uses consensus sharp, not exact 50%) -> correct.
    * Spot-checked game-line EV: -150 at 63.38% fair = 5.63% (reported 5.64%).
    * ALC fantasy group no-vig probs & vig recompute correctly.
    * ESPN fantasy v2: 2639 projections incl. 284 TD rows with sane values;
      2-pt-conversion fix (stat ids 205/206/207) present in Half-PPR & ALC formulas.
    * Sheets export Kelly formula = EV/(dec-1) * bankroll * mult (correct);
      Bet Log Net P/L and CLV formulas verified correct in direction and sign.
- Cleared the stale Covers cache and re-ran to confirm the BUG 11 fix end-to-end.
- Systematic normalization scans across all source caches caught BUG 13 (ALC collapsed
  team names) and BUG 12 (abbreviated markets); a follow-up player-name scan caught
  BUG 14 (Covers initial-style names + empty matchups).
- Verified PropLine DFS feed covers PrizePicks (1306) / Underdog (2307) / Sleeper (1286)
  so skipping the direct PrizePicks scrape loses no data; benchmark_props carry Novig /
  ProphetX / Kalshi / Polymarket sharp prices.
- Investigated the DFS cushion sign and ALC/game-line/PropLine paths; after
  careful reference analysis concluded the existing code is CORRECT (no change
  kept). See FALSE POSITIVES section for the reasoning.
- Added reusable tools:
    * tools/check_pinnacle_td_props.py - live Pinnacle TD-scorer check.
    * tools/validate_dataset.py - post-dry-run dataset integrity validator
      (EV recompute, sign flips, phantom-edge detection = an EV with NO fair_prob,
      >30% EV, NaN/inf, duplicates, orphan matchups). Exit code 1 on failure.
      PROVEN to catch the Covers DIFFERENCE phantom-EV bug and orphan markets via
      fault injection.
    WIRED IN: main.py now runs the integrity check automatically at the end of every
      pipeline run (dry-run and real export) and prints '=== DATASET INTEGRITY CHECK ==='
      with a WARNING on failure (non-fatal, so a Sheet export still proceeds). It runs
      in-process on the just-built dataset. Skip with NFL_EV_SKIP_VALIDATE=1.
      Standalone: python3 tools/validate_dataset.py (exit 1 on failure).

CI / PRE-COMMIT GATES ADDED
---------------------------
 - tools/run_and_validate.sh  : runs main.py then validate_dataset.py; exits
                                non-zero on regression (dry-run or --export).
 - Makefile targets           : make run | export | validate | test | check.
                                `make check` = compile + tests + integrity (CI gate).
 - .githooks/pre-commit        : blocks commits that break compile or dataset
                                integrity (installed via tools/install-hooks.sh ->
                                git config core.hooksPath .githooks).
 - .github/workflows/ci.yml    : GitHub Actions job runs `make check-ci` on every
                                push/PR (py 3.11 & 3.12). Fails on compile error,
                                failing unit test, or dataset regression (phantom EV /
                                orphan / EV mismatch / NaN / dupes). Fresh checkouts
                                with no dataset SKIP the integrity step (--allow-missing).

REQUIREMENTS FIX (found by CI on first run)
-----------------------------------------
 - requirements.txt pinned `scrapling[fetchers]>=0.2.99`. Plain `pip install
   scrapling` omits the optional `fetchers` extra, so `curl_cffi` is not installed
   and `from scrapling.fetchers import Fetcher` raises ModuleNotFoundError. This broke
   3 tests on a clean CI runner and would break the app on any fresh machine.
   Verified in a clean venv: install succeeds, curl_cffi present, 24/24 tests pass.
 - CI (GitHub Actions, Python 3.11 & 3.12) is GREEN after the fix.
 - Repo published (private) with .env excluded via .gitignore; .env.example committed.
 All verified: fault-injected phantom-EV and orphan-market both return exit 1 /
 block the commit; clean data passes. Tested on macOS bash 3.2 (mapfile removed).



ADDITIONAL FIXES (follow-up round)
----------------------------------
- DEVIG-TIER LOGGING: added structured `fair_prob_tier` ("sharp"/"market"/"model"/"")
  to every prop and DFS row via new `_tier_from_label()` in engine.py, derived from
  ev_method. Makes the sharp-vs-market consensus tier machine-readable for debugging.
  Live check: prop rows sharp=1412 / market=1594; DFS sharp=2252 / market=1268 /
  none=2943. Regression test: TestFairProbTier, TestValidatorIgnoresTierString.

- main.py CRASH FIX: printing the Top-5 summary raised
  "TypeError: unsupported format string passed to NoneType.__format__" whenever a
  row's american odds was None (leg_american / best_american / alc_american). This
  killed the whole run before validation. Now rendered as "N/A" when None.

- VALIDATOR REFINED (>30% EV check): the blunt "any prop EV>30% = phantom" rule
  false-flagged a REAL edge. Live example: Hunter Luepke Anytime-TD Yes, BET99 +1750
  vs Novig sharp fair 7.89% (+1167) => genuine ~46% EV, recomputes exactly. The check
  now accepts only "legitimate longshot mispricing" (decimal>=6.0, fair_prob<=25%,
  sharp tier) and still FAILS on: phantom EV with no fair_prob, high EV on non-longshot
  short odds, and any EV that does not recompute. Proved with 4 injected cases.
  Regression tests: TestValidatorLongshotVsPhantom.


DEV TOOLING & LINT SWEEP
------------------------
- requirements-dev.txt added (ruff + coverage); runtime deps stay in requirements.txt.
  `make install-dev` installs both; `make lint` runs ruff.
- pyproject.toml: ruff limited to F (pyflakes) + B (bugbear) so `make lint` fails only
  on likely bugs, not style churn. B905/F841/B007/B008/B904 ignored with reasons.
- Fixed 4 F601 duplicate dict keys in normalize.py (all were same-value dupes of
  'Rushing Attempts' / 'Receiving Yards' - dead weight, no behaviour change).
- Removed 10 dead imports / f-strings (F401/F541, ruff --fix) across src & tests.
- B023 in pinnacle.py is a false positive (closure used immediately in-iteration);
  documented with a targeted noqa rather than restructuring working code.
- `make lint` now passes clean; 35 unit tests still pass; full pipeline+validator pass.
