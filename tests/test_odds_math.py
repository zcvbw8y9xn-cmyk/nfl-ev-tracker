"""Regression tests for odds math, normalization, and engine sorting/EV.

Run with:  python3 -m unittest discover -s tests -v
(or:       python3 tests/test_odds_math.py)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.odds_math import (  # noqa: E402
    american_to_decimal,
    american_to_implied_prob,
    compute_ev_pct,
    decimal_to_american,
    devig_power,
    kelly_fraction,
    market_vig_pct,
    parse_american,
    parse_decimal,
    parse_odds,
)
from src.normalize import normalize_team, normalize_market, player_match_key  # noqa: E402
from src.engine import build_ev_datasets, _shift_prop_prob  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


class TestOddsConversions(unittest.TestCase):
    def test_american_decimal_roundtrip(self):
        for am in (-1000, -500, -110, -105, -101, 100, 150, 500, 10000):
            self.assertEqual(decimal_to_american(american_to_decimal(am)), am, am)

    def test_implied_prob(self):
        self.assertTrue(approx(american_to_implied_prob(-110), 110 / 210))
        self.assertTrue(approx(american_to_implied_prob(150), 100 / 250))


class TestParseExplicit(unittest.TestCase):
    def test_parse_american(self):
        self.assertEqual(parse_american("+120"), 120)
        self.assertEqual(parse_american("-115"), -115)
        self.assertEqual(parse_american(120), 120)
        self.assertEqual(parse_american(-115), -115)
        self.assertEqual(parse_american("EVEN"), 100)
        # A bare decimal-looking number is NOT American odds -> None (no guessing).
        self.assertIsNone(parse_american("1.92"))
        self.assertIsNone(parse_american(1.5))

    def test_parse_decimal(self):
        self.assertTrue(approx(parse_decimal("1.92"), 1.92))
        self.assertEqual(decimal_to_american(parse_decimal("1.92")), -109)
        self.assertTrue(approx(parse_decimal(2.5), 2.5))
        # Signed values are never decimal; a bare int 120 under fmt=decimal means decimal
        # odds 120.0 (huge underdog price), which is valid.
        self.assertIsNone(parse_decimal("-115"))
        self.assertTrue(approx(parse_decimal(120), 120.0))
        self.assertIsNone(parse_decimal(0.5))

    def test_parse_odds_format(self):
        self.assertEqual(parse_odds("1.92", fmt="decimal"), -109)
        self.assertEqual(parse_odds("-115", fmt="american"), -115)
        # explicit american refuses to treat a decimal string as american
        self.assertIsNone(parse_odds("1.92", fmt="american"))
        self.assertEqual(parse_odds("1.92", fmt="auto"), -109)


class TestDevig(unittest.TestCase):
    def test_power_sums_to_one_and_keeps_order(self):
        for a, b in [(-110, -110), (-115, -105), (-150, 130), (120, -140), (-500, 400)]:
            p1, p2 = devig_power(american_to_decimal(a), american_to_decimal(b))
            self.assertTrue(approx(p1 + p2, 1.0, 1e-6), (a, b, p1, p2))
            self.assertTrue(0 < p1 < 1 and 0 < p2 < 1)

    def test_symmetric_market(self):
        p1, p2 = devig_power(american_to_decimal(-110), american_to_decimal(-110))
        self.assertTrue(approx(p1, 0.5) and approx(p2, 0.5))

    def test_arbitrage_market_normalises_not_half_half(self):
        # +130 / +130 implies < 100% combined -> a genuine arb. Power method used to
        # return 0.5/0.5; must now normalise proportionally (and favour neither).
        da, db = american_to_decimal(130), american_to_decimal(130)
        self.assertLess(market_vig_pct(da, db), 0)
        p1, p2 = devig_power(da, db)
        self.assertTrue(approx(p1 + p2, 1.0, 1e-9))
        self.assertTrue(approx(p1, 0.5) and approx(p2, 0.5))

    def test_arb_lopsided_not_half_half(self):
        # Different prices but still sub-100% combined must keep the favourite favoured.
        da, db = american_to_decimal(120), american_to_decimal(135)
        if (1 / da) + (1 / db) <= 1.0:
            p1, p2 = devig_power(da, db)
            self.assertTrue(approx(p1 + p2, 1.0, 1e-9))
            self.assertGreater(p1, p2)  # the more likely side stays more likely


class TestEVAndKelly(unittest.TestCase):
    def test_ev_zero_at_fair(self):
        self.assertTrue(approx(compute_ev_pct(110 / 210, american_to_decimal(-110)), 0.0, 1e-9))

    def test_ev_positive_example(self):
        self.assertTrue(approx(compute_ev_pct(0.55, 2.20), 21.0))

    def test_kelly_quarter(self):
        self.assertTrue(approx(kelly_fraction(0.55, 2.20), 0.175 * 25))

    def test_kelly_multiplier_is_bounded_to_zero_and_full_kelly(self):
        full = kelly_fraction(0.55, 2.20, 1.0)
        self.assertTrue(approx(kelly_fraction(0.55, 2.20, 2.0), full))
        self.assertEqual(kelly_fraction(0.55, 2.20, -0.5), 0.0)

    def test_kelly_non_finite_inputs_are_zero(self):
        self.assertEqual(kelly_fraction(float("nan"), 2.20), 0.0)
        self.assertEqual(kelly_fraction(0.55, float("inf")), 0.0)
        self.assertEqual(kelly_fraction(0.55, 2.20, float("nan")), 0.0)

    def test_kelly_no_edge_is_zero(self):
        self.assertEqual(kelly_fraction(0.4, american_to_decimal(-110)), 0.0)


class TestNormalizeTeam(unittest.TestCase):
    def test_exact_and_nicknames(self):
        cases = {
            "Arizona Cardinals": "ARI",
            "LA Chargers": "LAC",
            "Los Angeles Chargers": "LAC",
            "Los Angeles Rams": "LA",
            "New York Giants": "NYG",
            "New York Jets": "NYJ",
            "SAN FRANCISCO 49ERS": "SF",
            "Niners": "SF",
            "Tampa Bay Buccaneers": "TB",
            "Kansas City Chiefs": "KC",
            "Jaguars": "JAX",
        }
        for raw, exp in cases.items():
            self.assertEqual(normalize_team(raw), exp, raw)

    def test_no_cross_contamination(self):
        # Relocated franchises and city duplicates should map to a single NFL code
        # (never to an unrelated team via a stray substring).
        self.assertEqual(normalize_team("St. Louis Rams"), "LA")
        self.assertEqual(normalize_team("San Diego Chargers"), "LAC")
        self.assertEqual(normalize_team("Oakland Raiders"), "LV")
        # San Francisco Giants (MLB) is out of scope but should resolve to SF, not NYG.
        self.assertEqual(normalize_team("San Francisco Giants"), "SF")
        # Unknown team passes through upper-cased, not mapped to garbage.
        self.assertEqual(normalize_team("Toronto Argonauts"), "TORONTO ARGONAUTS")


class TestPlayerKey(unittest.TestCase):
    def test_merge_equivalent_names(self):
        self.assertEqual(player_match_key("Amon-Ra St. Brown"), player_match_key("AmonRa St Brown"))
        self.assertEqual(player_match_key("Josh Allen"), player_match_key("Joshua Allen"))
        self.assertEqual(player_match_key("Kenneth Walker III"), player_match_key("Ken Walker"))
        self.assertEqual(player_match_key("Michael Pittman Jr."), player_match_key("Michael Pittman"))


class TestLineShift(unittest.TestCase):
    def test_direction_and_complement(self):
        self.assertGreater(_shift_prop_prob(0.5, 0.0, -24.0, "Over", 24.0), 0.5)
        self.assertLess(_shift_prop_prob(0.5, 0.0, 24.0, "Over", 24.0), 0.5)
        for d in (-10, -5, 5, 10):
            o = _shift_prop_prob(0.55, 250.0, 250.0 + d, "Over", 24.0)
            u = _shift_prop_prob(0.45, 250.0, 250.0 + d, "Under", 24.0)
            self.assertTrue(approx(o + u, 1.0, 1e-6), (d, o, u))

    def test_sigma_zero_guard(self):
        # Must not raise ZeroDivisionError.
        self.assertEqual(_shift_prop_prob(0.5, 100, 110, "Over", 0.0), 0.5)


class TestEngineSort(unittest.TestCase):
    def _run(self, rows):
        espn = [{"matchup": "A @ B", "kickoff_nl": "", "home": "B", "away": "A"}]
        pin = {"game_lines": rows, "player_props": []}
        ds = build_ev_datasets(
            espn_games=espn,
            alc_data={"game_lines": [], "player_props": [], "fantasy_groups": []},
            pinnacle_data=pin,
            action_data={"game_lines": [], "splits": {}, "player_props": []},
            bp_props=[],
            covers_data={"game_lines": [], "player_props": []},
            sao_props=[],
            odds_api_data={
                "game_lines": [], "player_props": [],
                "benchmark_props": [], "benchmark_game_lines": [],
            },
        )
        return ds["game_ev_rows"]

    @staticmethod
    def _ml(side, am, fp):
        dec = 1 + am / 100 if am > 0 else 1 + 100 / abs(am)
        return {
            "source": "Pinnacle", "book": "Pinnacle", "matchup": "A @ B",
            "away": "A", "home": "B", "market": "Moneyline", "side": side,
            "line": None, "american": am, "decimal": dec,
            "fair_prob": fp, "fair_american": None, "vig_pct": 1.0,
        }

    def test_exact_zero_ev_sorts_above_negative(self):
        # A at +150 with fair 44% -> EV very positive; B at -170 fair 56% -> negative.
        # Construct an exact 0.0 EV row and a negative row and assert order.
        rows = [self._ml("A", 150, 0.40), self._ml("B", -170, 0.60)]
        out = self._run(rows)
        evs = [r["ev_pct"] for r in out]
        # 0.0 EV row must not be pushed below the negative one.
        self.assertEqual(evs, sorted(evs, reverse=True), evs)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPrizePicksGoblinHandling(unittest.TestCase):
    def test_scraper_includes_goblins_without_conventional_prices(self):
        from unittest.mock import patch
        import src.sources.prizepicks as P

        def projection(kind, line):
            return {
                "attributes": {"odds_type": kind, "line_score": line, "allowed_wager_types": "under_or_over"},
                "relationships": {
                    "duration": {"data": {"id": "full"}},
                    "new_player": {"data": {"id": "p1"}},
                    "stat_type": {"data": {"id": "yards"}},
                    "game": {"data": {"id": "g1"}},
                },
            }

        payload = {
            "data": [projection("standard", 25.5), projection("goblin", 23.5), projection("demon", 30.5)],
            "included": [
                {"type": "duration", "id": "full", "attributes": {"name": "Full"}},
                {"type": "new_player", "id": "p1", "attributes": {"display_name": "Test Player"}},
                {"type": "stat_type", "id": "yards", "attributes": {"name": "Rushing Yards"}},
                {"type": "game", "id": "g1", "attributes": {"metadata": {"game_info": {"teams": {}}}}},
            ],
        }

        class Response:
            status = 200
            def json(self):
                return payload

        with patch.object(P.Fetcher, "get", return_value=Response()):
            rows = P._scrape_prizepicks_live()
        self.assertEqual({r["odds_type"] for r in rows}, {"standard", "goblin"})
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["no_single_leg_odds"] for r in rows))
        self.assertTrue(all(r["american"] is None and r["decimal"] is None for r in rows))
        goblins = [r for r in rows if r["is_goblin"]]
        self.assertEqual(len(goblins), 2)
        self.assertTrue(all(r["adjusted_payout"] and not r["is_promo"] for r in goblins))

    @staticmethod
    def _row(line, odds_type="standard", legacy_odds=True):
        return {
            "source": "PrizePicks", "book": "PrizePicks", "player": "Test Player",
            "matchup": "", "market": "Rushing Yards", "side": "Over", "line": line,
            "american": -119 if legacy_odds else None,
            "decimal": 1.8433 if legacy_odds else None,
            "odds_type": odds_type, "is_goblin": odds_type == "goblin",
            "adjusted_payout": odds_type == "goblin",
            "no_single_leg_odds": True,
        }

    def test_prizepicks_standard_and_goblin_never_get_single_leg_odds_or_ev(self):
        ds = build_ev_datasets(
            espn_games=[], alc_data={}, pinnacle_data={}, action_data={}, bp_props=[],
            covers_data={}, sao_props=[], odds_api_data={},
            prizepicks_props=[self._row(25.5), self._row(23.5, "goblin", False)],
        )
        self.assertEqual(len(ds["dfs_ev_rows"]), 1)
        standard = ds["dfs_ev_rows"][0]
        self.assertIsNone(standard["leg_american"])
        self.assertIsNone(standard["leg_decimal"])
        self.assertIsNone(standard["dfs_ev_pct"])
        self.assertTrue(standard["no_single_leg_odds"])
        self.assertEqual(standard["ev_method"], "Entry payout table; single-leg EV unavailable")

        self.assertEqual(len(ds["goblin_rows"]), 1)
        goblin = ds["goblin_rows"][0]
        self.assertTrue(goblin["is_goblin"])
        self.assertTrue(goblin["adjusted_payout"])
        self.assertFalse(goblin["is_promo"])
        self.assertIsNone(goblin["leg_american"])
        self.assertIsNone(goblin["leg_decimal"])
        self.assertIsNone(goblin["dfs_ev_pct"])


class TestPinnacleTDNormalization(unittest.TestCase):
    """Suggestion 2: if Pinnacle ever publishes a Yes/No TD scorer market, the parser
    must emit canonical Yes/No sides on a 0.5 line so it joins the consensus index.
    (The live Guest API currently sends no TD scorer props; this guards the code path.)"""

    def test_yes_no_td_market_normalized(self):
        import json
        import src.sources.pinnacle as P

        matchups = [
            {"id": 1, "type": "matchup", "participants": [
                {"alignment": "away", "name": "Kansas City"},
                {"alignment": "home", "name": "Buffalo"}]},
            {"id": 10, "parentId": 1,
             "special": {"category": "Player Props", "description": "Test Player Anytime Touchdown"},
             "participants": [{"id": 100, "name": "Yes"}, {"id": 101, "name": "No"}]},
        ]
        markets = [
            {"matchupId": 10, "type": "total", "period": 0, "prices": [
                {"designation": "yes", "participantId": 100, "price": 125},
                {"designation": "no", "participantId": 101, "price": -150}]},
        ]

        class _Resp:
            def __init__(self, d):
                self._d = d
                self.status = 200

            def json(self):
                return self._d

            def read(self):
                return json.dumps(self._d).encode()

        orig = P.Fetcher.get

        def _fake(url, **kw):
            return _Resp(matchups if "matchups" in url else markets)

        P.Fetcher.get = _fake
        try:
            res = P._scrape_pinnacle_live()
        finally:
            P.Fetcher.get = orig

        pp = res["player_props"]
        self.assertEqual({r["side"] for r in pp}, {"Yes", "No"})
        self.assertEqual({r["line"] for r in pp}, {0.5})
        self.assertTrue(approx(sum(r["fair_prob"] for r in pp), 1.0, 1e-6))


class TestCoversEvNotMisparsed(unittest.TestCase):
    """Regression: Covers labels its row metric '... DIFFERENCE <X>% EV Rating: S'.
    That X is the projection-minus-line difference, NOT a real EV%. The old regex
    captured it as covers_ev_pct, fabricating uniform ~26% 'edges'. The parser must
    now leave covers_ev_pct as None while still capturing the projection."""

    def test_difference_percentage_not_captured_as_ev(self):
        import src.sources.covers as C

        html = """
        <table><tr>
          <td><a href="/players/joe-burrow-123">J. Burrow</a></td>
          <td><a href="/matchup/pit-cin-1">PIT @ CIN</a></td>
          <td data-odds='content'>o0.5 Rushing Yards</td>
          <td>3.55 OVER PROJECTION +3.1 DIFFERENCE 27.11% EV Rating: S
              Best odds Betinia o0.5 -105 o0.5 -110</td>
        </tr></table>
        """
        class _Resp:
            def __init__(self, b):
                self.body = b
                self.status = 200

        orig_get = C.Fetcher.get
        orig_pace = C.pace_domain
        orig_cool = C.get_domain_cooldown_remaining
        C.Fetcher.get = lambda url, **kw: _Resp(html.encode("utf-8"))
        C.pace_domain = lambda *a, **k: None
        C.get_domain_cooldown_remaining = lambda d: 0
        try:
            rows = C._scrape_covers_player_props_page("https://www.covers.com/sport/football/nfl/player-props")
        finally:
            C.Fetcher.get = orig_get
            C.pace_domain = orig_pace
            C.get_domain_cooldown_remaining = orig_cool

        self.assertTrue(rows, "expected at least one parsed row")
        # find the rushing-yards row we injected
        r = next((x for x in rows if x["covers_projection"] == 3.55), None)
        self.assertIsNotNone(r, f"expected the 3.55 projection row, got {rows[:2]}")
        self.assertIsNone(r["covers_ev_pct"], "DIFFERENCE % must not be stored as EV")


class TestMarketAndTeamNormalization(unittest.TestCase):
    """Regression guards for two silent-drop bugs:
    BUG 12: abbreviated markets ('Rec Yds', 'Rush Att', 'Recs') must normalize.
    BUG 13: ALC collapsed team names ('LOS ANGELESC'/'LOS ANGELESR'/'SANFRANCISCO')."""

    def test_abbreviated_markets(self):
        for raw, want in [
            ("Rec Yds", "Receiving Yards"),
            ("Rec Yards", "Receiving Yards"),
            ("Receiving Yds", "Receiving Yards"),
            ("Rush Att", "Rush Attempts"),
            ("Rushing Att", "Rush Attempts"),
            ("Recs", "Receptions"),
            ("Rec", "Receptions"),
        ]:
            self.assertEqual(normalize_market(raw), want, f"{raw!r} should normalize to {want!r}")

    def test_alc_collapsed_team_names(self):
        from src.normalize import normalize_team
        self.assertEqual(normalize_team("LOS ANGELESC"), "LAC")
        self.assertEqual(normalize_team("LOS ANGELESR"), "LA")
        self.assertEqual(normalize_team("SANFRANCISCO"), "SF")
        self.assertEqual(normalize_team("SANFRANCISCO49ERS"), "SF")


class TestCoversFullNameFromSlug(unittest.TestCase):
    """BUG 14: Covers must recover full player names (from href slug) and matchup from
    row text so initial-style names don't orphan props from their real buckets."""

    def test_initial_name_and_matchup_recovered(self):
        import src.sources.covers as C

        html = """
        <table><tr>
          <td><a href="/sport/football/nfl/players/28779/jared-goff">J. Goff</a></td>
          <td data-odds='content'>o0.5 Rushing Yards</td>
          <td>RUSHING YARDS NE @ DET J. Goff (QB) o0.5 Rushing Yards 3.99 OVER PROJECTION
              +3.5 DIFFERENCE 5.0% EV Rating: S Best odds bet365 o0.5 +120</td>
        </tr></table>
        """

        class _Resp:
            def __init__(self, b):
                self.body = b
                self.status = 200

        og, op, oc = C.Fetcher.get, C.pace_domain, C.get_domain_cooldown_remaining
        C.Fetcher.get = lambda url, **kw: _Resp(html.encode())
        C.pace_domain = lambda *a, **k: None
        C.get_domain_cooldown_remaining = lambda d: 0
        try:
            rows = C._scrape_covers_player_props_page("https://www.covers.com/x")
        finally:
            C.Fetcher.get, C.pace_domain, C.get_domain_cooldown_remaining = og, op, oc

        self.assertTrue(rows)
        r = rows[0]
        self.assertEqual(r["player"], "Jared Goff")
        self.assertEqual(r["matchup"], "NE @ DET")


class TestFairProbTier(unittest.TestCase):
    """_tier_from_label classifies the devig/fair-prob source for machine-readable logging."""

    def test_tier_classification(self):
        from src.engine import _tier_from_label
        cases = {
            "Pinnacle No-Vig": "sharp",
            "Sharp Consensus (2: Pinnacle, Novig)": "sharp",
            "Sharp Exchange (Novig, Kalshi)": "sharp",
            "Market Consensus (4 books)": "market",
            "Line-Shifted Sharp": "sharp",
            "Line-Shifted Consensus": "market",
            "BettingPros Fair Prob": "model",
            "Entry payout table; single-leg EV unavailable": "",
            "": "",
        }
        for label, expected in cases.items():
            self.assertEqual(_tier_from_label(label), expected, label)


class TestBetLogTemplateClean(unittest.TestCase):
    """BUG 16: Bet Log must not ship a stray hardcoded 'Pending' in only row 2."""

    def test_no_stray_pending_in_template(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "sheets_export.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('"Pending" if rnum == 2 else ""', src,
                         "stray row-2-only 'Pending' default should be removed")


class TestValidatorIgnoresTierString(unittest.TestCase):
    """fair_prob_tier is a string field; validator must not treat it as a phantom EV."""

    def test_validator_passes_row_with_tier_and_no_ev_issue(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import validate_dataset as vd
        ds = {
            "prop_ev_rows": [{
                "player": "X", "market": "Y", "pick": "Over",
                "matchup": "A @ B",
                "fair_prob": 55.0, "best_decimal": 2.00,
                "true_ev_pct": round((0.55 * 2.0 - 1) * 100, 2),  # exactly 10.00
                "fair_prob_tier": "sharp",                     # string field
            }],
            "game_ev_rows": [], "alc_scanner_rows": [], "dfs_ev_rows": [],
            "slate_games": [{"matchup": "A @ B"}],
        }
        # Should pass: EV recomputes and fair_prob is present; tier string is irrelevant.
        self.assertEqual(vd.validate_dataset(ds), 0)


class TestValidatorLongshotVsPhantom(unittest.TestCase):
    """The >30% EV check must accept legit longshot mispricing but still catch phantoms."""

    def _rc(self, row):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import validate_dataset as vd
        row.setdefault("matchup", "A @ B")
        ds = {"prop_ev_rows": [row], "game_ev_rows": [], "alc_scanner_rows": [],
              "dfs_ev_rows": [], "slate_games": [{"matchup": "A @ B"}]}
        return vd.validate_dataset(ds)

    def test_legit_longshot_passes(self):
        # +1750 soft price vs sharp fair 7.89% (fair +1167) => genuine ~46% EV.
        rc = self._rc({"player": "Luepke", "market": "Anytime TD", "pick": "Yes",
                       "fair_prob": 7.89, "best_decimal": 18.5, "true_ev_pct": 46.04,
                       "fair_prob_tier": "sharp"})
        self.assertEqual(rc, 0)

    def test_phantom_high_ev_no_fair_prob_fails(self):
        rc = self._rc({"player": "P", "market": "Rec Yds", "pick": "Over",
                       "fair_prob": None, "best_decimal": 1.9, "true_ev_pct": 40.0,
                       "fair_prob_tier": ""})
        self.assertEqual(rc, 1)

    def test_high_ev_non_longshot_fails(self):
        # Sharp tier but short odds (dec 2.0) is not a longshot -> suspicious.
        rc = self._rc({"player": "P", "market": "Rec Yds", "pick": "Over",
                       "fair_prob": 52.0, "best_decimal": 2.0, "true_ev_pct": 40.0,
                       "fair_prob_tier": "sharp"})
        self.assertEqual(rc, 1)

    def test_longshot_with_wrong_math_still_fails(self):
        rc = self._rc({"player": "Luepke", "market": "Anytime TD", "pick": "Yes",
                       "fair_prob": 7.89, "best_decimal": 18.5, "true_ev_pct": 99.0,
                       "fair_prob_tier": "sharp"})
        self.assertEqual(rc, 1)
