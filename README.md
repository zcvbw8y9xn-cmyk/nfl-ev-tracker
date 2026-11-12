# NFL +EV & Newfoundland Sportsbook Tracker

Automated multi-source NFL +EV scraper and Google Sheets exporter built for **Newfoundland** bettors.

## Newfoundland Sportsbooks Tracked
- **ALC Proline / Stadium Bets** (scraped live via `scrapling`)
- **Pinnacle** (sharp no-vig benchmark + playable in NL)
- **bet365**
- **Sports Interaction (SIA)**
- **BET99**
- **Betway**

## DFS Pick'em Platforms Tracked
- **Sleeper** (Sleeper Picks lines/odds + official weekly NFL player projections)
- **PrizePicks** (Standard Pick'em lines, live Taco / flash-sale promos, and Goblin alt lines; no fabricated single-leg odds/EV)
- **Underdog Fantasy** (Higher/Lower lines + exact leg prices & multipliers)

## Data Sources
1. **ALC Proline & Stadium Bets** (`alc.ca` via `scrapling`) — single-game ML, alt spreads, totals, and player props (Passing/Rushing/Receiving Yards, Receptions, Anytime TD, 1st TD) + ALC Vig % scanner.
2. **Pinnacle Guest API** (`guest.api.arcadia.pinnacle.com`) — real-time sharp lines and power-devigged fair odds.
3. **Covers.com** — NL-localized book odds across bet365, SIA, BET99, Betway + Covers EV projections.
4. **BettingPros** — paginated props with projections, fair odds, defense-vs-position (DVP) rank, and L5/L10/Season hit rates.
5. **Action Network** — Canadian book lines + public ticket % vs money % splits.
6. **ScoresAndOdds** — independent player prop projections.
7. **Sleeper + RotoWire Picks** — Sleeper weekly stat projections (`api.sleeper.com`), live Sleeper Picks odds, RotoWire projections, and game weather.
8. **PrizePicks** (`partner-api.prizepicks.com`) — standard Pick'em lines & Taco discounts.
9. **Underdog Fantasy** (`api.underdogfantasy.com/v1/over_under_lines`) — live Higher/Lower lines & prices.
10. **ESPN NFL Scoreboard** — canonical schedule, NST kickoff times, and dome/outdoor venue info.
11. **Optional APIs** — drop `THE_ODDS_API_KEY` or `BALLDONTLIE_API_KEY` into `.env` to maximize coverage automatically on refresh.

## Usage

```bash
# First run (creates a new Google Sheet)
python3 main.py

# Refresh an existing Google Sheet in place
python3 main.py --spreadsheet-id YOUR_SPREADSHEET_ID

# Dry run (print top +EV plays without touching Google Sheets)
python3 main.py --dry-run
```

## Quality gates (regression protection)

Every pipeline run automatically validates the resulting dataset (phantom EV,
orphan markets/teams, EV math mismatches, NaN/inf, duplicate rows) and prints
`=== DATASET INTEGRITY CHECK ===`. To gate a run or a commit explicitly:

```bash
# Run + validate in one step (fails non-zero on any regression)
tools/run_and_validate.sh            # dry-run + validate
tools/run_and_validate.sh --export   # full run (writes Sheet) + validate

# Just validate the current cached dataset
python3 tools/validate_dataset.py      # exit 1 on failure

# Make targets
make validate   # integrity check only
make test       # unit tests
make check      # compile + tests + integrity (CI gate)
make check-ci   # same, but skips integrity when no cached dataset exists
```

### CI (GitHub Actions)

`.github/workflows/ci.yml` runs `make check-ci` on every push and pull request
(Python 3.11 & 3.12). The build fails if any source doesn't compile, any unit
test fails, or a present dataset shows phantom EV / an orphan market / an EV math
mismatch / NaN / duplicate rows. On a fresh checkout with no cached scrape
dataset the integrity step is skipped (not failed).

`main` is protected: both status checks (`…(3.11)` and `…(3.12)`) must pass, and
the branch must be up to date, before a pull request can merge. Direct pushes,
force-pushes and branch deletion are blocked for everyone, admins included.

### pre-commit hook (if you use git)

A tracked hook runs the dataset integrity check before every commit:

```bash
git init            # if not already a repo
tools/install-hooks.sh   # activates .githooks/pre-commit
```

It blocks any commit that would reintroduce phantom EV or an orphan market/team.
Bypass only when absolutely sure with `git commit --no-verify`.

## Contributing / PR workflow

`main` is protected — all changes go through a pull request with green CI.

```bash
# 1. Branch off main
git checkout main && git pull
git checkout -b fix/short-description

# 2. Make changes, then run the gate locally before committing
make check            # compile + unit tests + dataset integrity

# 3. Commit and push the branch (pre-commit hook also runs here)
git commit -am "fix: what you changed"
git push -u origin fix/short-description

# 4. Open a PR and merge once CI is green
gh pr create --fill
gh pr checks --watch
gh pr merge --squash --delete-branch
```

A red CI check (compile error, failing test, phantom EV, orphan market, EV math
mismatch, NaN, duplicate rows) blocks the merge until fixed. Keep branches small
and focused so the gate stays meaningful.

## macOS Desktop App

Build or rebuild `/Applications/NFL EV Tracker.app` (native Cocoa + `WKWebView` wrapper backed by `src/app_server.py` and cached in `~/.cache/nfl-ev-tracker/latest_dataset.json`):

```bash
./macos/build_app.sh
open -a "/Applications/NFL EV Tracker.app"
```
