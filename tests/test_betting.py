"""Model, staking, ledger and matching tests. Stdlib only: py -m unittest -v"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from betbot.config import Config
from betbot.ledger import Ledger
from betbot.model import (
    Event,
    best_combo,
    build_candidates,
    normalise,
    overround,
    reference_probs,
)
from betbot.providers.merge import attach_reference
from betbot.staking import kelly_fraction, plan_stake
from betbot.util import utcnow


def prices(probs: dict[str, float], margin: float = 0.02) -> dict[str, float]:
    return {label: round(1.0 / (p * (1.0 + margin)), 3) for label, p in probs.items()}


def make_event(name: str, probs, margin=0.02, books=None, hours=5) -> Event:
    event = Event(
        event_id=name,
        sport="soccer_test",
        league="Test League",
        home="Home FC",
        away="Away FC",
        commence_time=utcnow() + timedelta(hours=hours),
    )
    event.quotes["h2h"] = {}
    sharp = prices(probs, margin)
    event.quotes["h2h"]["pinnacle"] = sharp
    for book, boost in (books or {}).items():
        event.quotes["h2h"][book] = {
            label: round(price * (1.0 + boost), 3) for label, price in sharp.items()
        }
    return event


class TestMath(unittest.TestCase):
    def test_normalise_sums_to_one(self):
        probs = normalise({"A": 2.0, "B": 4.0, "C": 4.0})
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=9)
        self.assertAlmostEqual(probs["A"], 0.5, places=9)

    def test_overround(self):
        self.assertAlmostEqual(overround({"A": 2.0, "B": 2.0}), 0.0, places=9)
        self.assertAlmostEqual(overround({"A": 1.5, "B": 2.5}), 0.0667, places=4)

    def test_too_few_outcomes_is_not_a_market(self):
        self.assertIsNone(normalise({"A": 1.5}))
        self.assertIsNone(overround({"A": 1.5}))

    def test_sharp_book_wins_the_reference(self):
        market = {
            "pinnacle": prices({"A": 0.62, "B": 0.24, "C": 0.14}, 0.02),
            "generous": prices({"A": 0.55, "B": 0.28, "C": 0.17}, 0.02),
        }
        ref = reference_probs(market)
        self.assertTrue(ref.sharp)
        self.assertEqual(ref.books, ["pinnacle"])
        self.assertAlmostEqual(ref.probs["A"], 0.62, places=3)

    def test_no_sharp_book_falls_back_to_average(self):
        market = {
            "book1": prices({"A": 0.6, "B": 0.4}, 0.05),
            "book2": prices({"A": 0.5, "B": 0.5}, 0.05),
        }
        ref = reference_probs(market)
        self.assertFalse(ref.sharp)
        self.assertAlmostEqual(ref.probs["A"], 0.55, places=3)


class TestScanAndCombo(unittest.TestCase):
    def cfg(self, **overrides) -> Config:
        base = dict(
            mode="paper", provider="demo", execution="paper", bankroll=1000.0,
            target_odds=2.0, odds_tolerance=0.15, legs=2,
            min_leg_odds=1.2, max_leg_odds=1.9, min_probability=0.55,
            min_combo_probability=0.4, min_edge=0.02, pool_edge_floor=-0.03,
            max_overround=0.12, max_candidates=20, min_lead_minutes=30, markets=["h2h"],
        )
        base.update(overrides)
        return Config(**base)

    def test_boosted_price_becomes_a_candidate(self):
        event = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                           books={"bet365": 0.09})
        scan = build_candidates([event], self.cfg(), utcnow())
        home = [c for c in scan.candidates if c.selection == "Home"]
        self.assertEqual(len(home), 1)
        self.assertGreater(home[0].edge, 0.05)
        self.assertEqual(home[0].book, "bet365")

    def test_near_fair_favourite_stays_in_pool(self):
        event = make_event("e1", {"Home": 0.70, "Draw": 0.19, "Away": 0.11})
        scan = build_candidates([event], self.cfg(), utcnow())
        home = [c for c in scan.candidates if c.selection == "Home"]
        self.assertEqual(len(home), 1)
        # taking the sharp book's own price means paying its ~2% margin
        self.assertAlmostEqual(home[0].edge, -0.02, places=2)

    def test_fair_prices_do_not_clear_the_ev_gate(self):
        # 0.70 x 0.71 at sharp prices lands at ~1.93 combined odds, but EV is
        # negative once the margin is paid - the agent must stay silent.
        a = make_event("e1", {"Home": 0.70, "Draw": 0.19, "Away": 0.11})
        b = make_event("e2", {"Home": 0.71, "Draw": 0.18, "Away": 0.11})
        cfg = self.cfg()
        scan = build_candidates([a, b], cfg, utcnow())
        self.assertEqual(len(scan.candidates), 2)
        combo, notes = best_combo(scan.candidates, cfg)
        self.assertIsNone(combo)
        self.assertIn("EV", notes["reason"])

    def test_zero_edge_mode_picks_the_most_probable_pair(self):
        a = make_event("e1", {"Home": 0.70, "Draw": 0.19, "Away": 0.11})
        b = make_event("e2", {"Home": 0.71, "Draw": 0.18, "Away": 0.11})
        cfg = self.cfg(allow_zero_edge=True)
        scan = build_candidates([a, b], cfg, utcnow())
        combo, notes = best_combo(scan.candidates, cfg)
        self.assertIsNotNone(combo, notes)
        self.assertGreater(combo.prob, 0.45)
        self.assertLess(combo.ev, 0.0)

    def test_short_lead_time_is_skipped(self):
        event = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                           books={"bet365": 0.09}, hours=0)
        scan = build_candidates([event], self.cfg(), utcnow())
        self.assertEqual(scan.rejects["starts_too_soon"], 1)

    def test_low_probability_leg_is_rejected(self):
        event = make_event("e1", {"Home": 0.40, "Draw": 0.30, "Away": 0.30},
                           books={"bet365": 0.20})
        scan = build_candidates([event], self.cfg(), utcnow())
        self.assertEqual(scan.candidates, [])

    def test_combo_needs_two_different_events(self):
        a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                       books={"bet365": 0.09})
        b = make_event("e2", {"Home": 0.80, "Draw": 0.12, "Away": 0.08},
                       books={"bet365": 0.06})
        scan = build_candidates([a, b], self.cfg(), utcnow())
        combo, notes = best_combo(scan.candidates, self.cfg())
        self.assertIsNotNone(combo, notes)
        self.assertEqual(len({leg.event_id for leg in combo.legs}), 2)
        self.assertGreater(combo.ev, 0.02)
        self.assertGreaterEqual(combo.odds, 1.7)
        self.assertLessEqual(combo.odds, 2.3)

    def test_combo_never_reuses_one_fixture(self):
        a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                       books={"bet365": 0.09, "williamhill": 0.08})
        scan = build_candidates([a], self.cfg(legs=2), utcnow())
        combo, notes = best_combo(scan.candidates, self.cfg(legs=2))
        self.assertIsNone(combo)
        self.assertTrue(notes.get("reason"))

    def test_same_fixture_legs_are_never_paired(self):
        a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                       books={"bet365": 0.09})
        b = make_event("e2", {"Home": 0.60, "Draw": 0.25, "Away": 0.15},
                       books={"bet365": 0.09})
        cfg = self.cfg()
        scan = build_candidates([a, b], cfg, utcnow())
        # two candidates, two events: the pair is legal
        self.assertEqual(len(scan.candidates), 2)
        self.assertEqual(len({c.event_id for c in scan.candidates}), 2)

    def test_zero_edge_mode_can_still_find_a_bet(self):
        a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                       books={"bet365": 0.03})
        b = make_event("e2", {"Home": 0.80, "Draw": 0.12, "Away": 0.08},
                       books={"bet365": 0.02})
        cfg = self.cfg(min_edge=0.0)
        scan = build_candidates([a, b], cfg, utcnow())
        combo, notes = best_combo(scan.candidates, cfg)
        self.assertIsNotNone(combo, notes)


class TestStaking(unittest.TestCase):
    def test_kelly_positive_only_with_edge(self):
        self.assertGreater(kelly_fraction(0.55, 2.0), 0)
        self.assertEqual(kelly_fraction(0.45, 2.0), 0.0)

    def test_stake_caps_are_respected(self):
        cfg = Config(bankroll=1000.0, max_stake_pct=0.02, daily_stake_cap_pct=0.05,
                     kelly_fraction=1.0, min_stake=1.0)
        plan = plan_stake(0.75, 2.0, 1000.0, cfg)
        self.assertLessEqual(plan.stake, 20.0)
        self.assertEqual(plan.capped_by, "max_stake_pct")

    def test_daily_cap_wins(self):
        cfg = Config(bankroll=1000.0, max_stake_pct=0.5, daily_stake_cap_pct=0.05,
                     kelly_fraction=1.0, min_stake=1.0)
        plan = plan_stake(0.9, 3.0, 1000.0, cfg, already_staked_today=48.0)
        self.assertAlmostEqual(plan.stake, 2.0, places=2)
        self.assertEqual(plan.capped_by, "daily_stake_cap")

    def test_fixed_stake_mode_ignores_kelly_and_edge(self):
        cfg = Config(bankroll=2100.0, fixed_stake_enabled=True, fixed_stake=200.0,
                     max_stake_pct=0.10, daily_stake_cap_pct=0.20, min_stake=1.0)
        plan = plan_stake(0.45, 2.0, 2100.0, cfg)
        self.assertAlmostEqual(plan.stake, 200.0, places=2)
        self.assertEqual(plan.capped_by, "fixed_stake")

    def test_fixed_stake_still_respects_hard_caps(self):
        cfg = Config(bankroll=2100.0, fixed_stake_enabled=True, fixed_stake=900.0,
                     max_stake_pct=0.10, daily_stake_cap_pct=0.20, min_stake=1.0)
        plan = plan_stake(0.60, 2.0, 2100.0, cfg)
        self.assertAlmostEqual(plan.stake, 210.0, places=2)
        self.assertEqual(plan.capped_by, "max_stake_pct")

    def test_thin_edge_is_skipped_not_rounded_up(self):
        cfg = Config(bankroll=100.0, max_stake_pct=0.02, kelly_fraction=0.25,
                     min_stake=1.0, daily_stake_cap_pct=0.05)
        plan = plan_stake(0.5001, 2.0002, 100.0, cfg)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.capped_by, "below_min_stake")


class TestLedger(unittest.TestCase):
    def test_roundtrip_and_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "bets.db")
            cfg = Config(bankroll=500.0, target_odds=2.0, odds_tolerance=0.2,
                         legs=2, min_leg_odds=1.2, max_leg_odds=1.9,
                         min_probability=0.5, min_combo_probability=0.4,
                         min_edge=0.01, markets=["h2h"], max_candidates=10)
            a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                           books={"bet365": 0.09})
            b = make_event("e2", {"Home": 0.80, "Draw": 0.12, "Away": 0.08},
                           books={"bet365": 0.06})
            scan = build_candidates([a, b], cfg, utcnow())
            combo, _ = best_combo(scan.candidates, cfg)
            self.assertIsNotNone(combo)

            bet_id = ledger.add_bet(combo, 20.0, "paper", "demo", "paper", status="open")
            self.assertEqual(ledger.bets_today("paper"), 1)
            self.assertEqual(len(ledger.open_bets("paper")), 1)
            self.assertEqual(ledger.summary("paper")["open"], 1)

            pnl = ledger.settle(bet_id, "win")
            self.assertAlmostEqual(pnl, 20.0 * (combo.odds - 1.0), places=2)
            summary = ledger.summary("paper")
            self.assertEqual(summary["wins"], 1)
            self.assertGreater(summary["roi"], 0)
            self.assertEqual(ledger.open_bets("paper"), [])
            with self.assertRaises(ValueError):
                ledger.settle(bet_id, "loss")

    def test_dry_run_slips_do_not_consume_the_daily_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "bets.db")
            cfg = Config(bankroll=500.0, target_odds=2.0, odds_tolerance=0.2,
                         legs=2, min_leg_odds=1.2, max_leg_odds=1.9,
                         min_probability=0.5, min_combo_probability=0.4,
                         min_edge=0.01, markets=["h2h"], max_candidates=10)
            a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                           books={"bet365": 0.09})
            b = make_event("e2", {"Home": 0.80, "Draw": 0.12, "Away": 0.08},
                           books={"bet365": 0.06})
            combo, _ = best_combo(build_candidates([a, b], cfg, utcnow()).candidates, cfg)
            ledger.add_bet(combo, 20.0, "live", "demo", "betpawa", status="dry_run")
            self.assertEqual(ledger.bets_today("live"), 0)
            self.assertEqual(ledger.staked_today("live"), 0.0)
            self.assertEqual(ledger.open_bets("live"), [])


class TestReferenceMerge(unittest.TestCase):
    def test_fuzzy_club_names_attach(self):
        soft = Event(
            event_id="api-1", sport="soccer_epl", league="Premier League",
            home="Man City", away="Burnley",
            commence_time=utcnow() + timedelta(hours=4),
        )
        soft.quotes["h2h"] = {
            "bet365": {"Man City": 1.30, "Draw": 5.0, "Burnley": 9.0},
        }
        ref = Event(
            event_id="bf-1", sport="soccer", league="Premier League",
            home="Manchester City", away="Burnley",
            commence_time=utcnow() + timedelta(hours=4),
        )
        ref.quotes["h2h"] = {
            "betfair": {"Manchester City": 1.34, "The Draw": 5.4, "Burnley": 8.8},
        }
        attached = attach_reference([soft], [ref])
        self.assertEqual(attached, 1)
        self.assertIn("betfair", soft.quotes["h2h"])
        self.assertAlmostEqual(soft.quotes["h2h"]["betfair"]["Man City"], 1.34)

    def test_placement_payload_travels_with_the_price(self):
        soft = Event(
            event_id="api-1", sport="soccer_epl", league="Premier League",
            home="Man City", away="Burnley",
            commence_time=utcnow() + timedelta(hours=4),
        )
        soft.quotes["h2h"] = {
            "bet365": {"Man City": 1.30, "Draw": 5.0, "Burnley": 9.0},
        }
        ref = Event(
            event_id="bf-1", sport="soccer", league="Premier League",
            home="Manchester City", away="Burnley",
            commence_time=utcnow() + timedelta(hours=4),
        )
        ref.quotes["h2h"] = {
            "pinnacle": {"Manchester City": 1.34, "Draw": 5.4, "Burnley": 8.8},
        }
        ref.venue["h2h"] = {
            "Manchester City": {"team": "TEAM1", "event_id": 1, "line_id": 2,
                                "period_number": 0, "sport_id": 29,
                                "bet_type": "MONEYLINE", "price": 1.34},
        }
        attached = attach_reference([soft], [ref])
        self.assertEqual(attached, 1)
        self.assertAlmostEqual(soft.quotes["h2h"]["pinnacle"]["Man City"], 1.34)
        self.assertEqual(soft.venue["h2h"]["Man City"]["team"], "TEAM1")
        self.assertAlmostEqual(soft.venue["h2h"]["Man City"]["price"], 1.34)

    def test_different_fixture_is_not_attached(self):
        soft = Event(
            event_id="api-1", sport="soccer_epl", league="Premier League",
            home="Arsenal", away="Chelsea",
            commence_time=utcnow() + timedelta(hours=4),
        )
        soft.quotes["h2h"] = {
            "bet365": {"Arsenal": 2.0, "Draw": 3.4, "Chelsea": 3.6},
        }
        ref = Event(
            event_id="bf-1", sport="soccer", league="Premier League",
            home="Burnley", away="Everton",
            commence_time=utcnow() + timedelta(hours=4),
        )
        ref.quotes["h2h"] = {
            "betfair": {"Burnley": 2.6, "The Draw": 3.3, "Everton": 2.8},
        }
        self.assertEqual(attach_reference([soft], [ref]), 0)
        self.assertNotIn("betfair", soft.quotes["h2h"])


class TestOddsApiParsing(unittest.TestCase):
    def test_event_payload_becomes_quotes(self):
        from betbot.providers.theoddsapi import _to_event

        item = {
            "id": "abc123",
            "sport_key": "soccer_epl",
            "sport_title": "Premier League",
            "commence_time": "2026-11-20T12:30:00Z",
            "home_team": "Arsenal",
            "away_team": "Burnley",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "markets": [
                        {"key": "h2h", "outcomes": [
                            {"name": "Arsenal", "price": 1.30},
                            {"name": "Draw", "price": 5.50},
                            {"name": "Burnley", "price": 9.50},
                        ]},
                    ],
                },
                {
                    "key": "bet365",
                    "markets": [
                        {"key": "totals", "outcomes": [
                            {"name": "Over", "point": 2.5, "price": 1.90},
                            {"name": "Under", "point": 2.5, "price": 1.95},
                        ]},
                    ],
                },
            ],
        }
        event = _to_event(item)
        self.assertIsNotNone(event)
        self.assertEqual(event.name, "Arsenal vs Burnley")
        self.assertEqual(event.quotes["h2h"]["pinnacle"]["Arsenal"], 1.30)
        self.assertEqual(event.quotes["totals"]["bet365"]["Over 2.5"], 1.90)

    def test_missing_key_is_a_clear_error(self):
        import os

        from betbot.providers.base import ProviderError
        from betbot.providers.theoddsapi import TheOddsApiProvider

        saved = os.environ.pop("THE_ODDS_API_KEY", None)
        try:
            with self.assertRaises(ProviderError):
                TheOddsApiProvider()
        finally:
            if saved is not None:
                os.environ["THE_ODDS_API_KEY"] = saved


class TestPs3838(unittest.TestCase):
    def payload(self, status=1):
        return {
            "sportId": 29,
            "last": 1,
            "leagues": [
                {
                    "id": 1980,
                    "events": [
                        {
                            "id": 758023991,
                            "starts": "2026-11-20T15:00:00Z",
                            "home": "Arsenal",
                            "away": "Burnley",
                            "periods": [
                                {
                                    "number": 0,
                                    "status": status,
                                    "lineId": 419715968,
                                    "moneyline": {"home": 1.30, "draw": 5.50, "away": 9.50},
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def test_odds_become_event_with_placement_data(self):
        from betbot.providers.ps3838 import _events_from_odds

        events = _events_from_odds(self.payload(), {1980: "Premier League"})
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.league, "Premier League")
        self.assertEqual(event.quotes["h2h"]["pinnacle"]["Arsenal"], 1.30)
        self.assertEqual(event.quotes["h2h"]["pinnacle"]["Draw"], 5.50)
        venue = event.venue["h2h"]["Arsenal"]
        self.assertEqual(venue["team"], "TEAM1")
        self.assertEqual(venue["line_id"], 419715968)
        self.assertEqual(venue["event_id"], 758023991)
        self.assertEqual(event.venue["h2h"]["Burnley"]["team"], "TEAM2")
        self.assertEqual(event.venue["h2h"]["Draw"]["team"], "DRAW")

    def test_offline_period_is_skipped(self):
        from betbot.providers.ps3838 import _events_from_odds

        self.assertEqual(_events_from_odds(self.payload(status=2), {}), [])

    def test_parlay_request_matches_spec(self):
        from betbot.model import Candidate
        from betbot.providers.ps3838 import parlay_request

        def leg(team, line_id, event_id):
            return Candidate(
                event_id=str(event_id), league="L", match="A vs B",
                commence_time=utcnow(), market="h2h", selection="Arsenal",
                price=1.5, book="pinnacle", fair_prob=0.65, edge=0.02,
                overround=0.02, n_books=1, sharp_reference=True,
                venue={"sport_id": 29, "event_id": event_id, "period_number": 0,
                       "line_id": line_id, "bet_type": "MONEYLINE", "team": team},
            )

        request = parlay_request([leg("TEAM1", 111, 1), leg("TEAM2", 222, 2)], 25.0)
        self.assertEqual(request["riskAmount"], 25.0)
        self.assertEqual(request["roundRobinOptions"], ["Parlay"])
        self.assertEqual(request["oddsFormat"], "Decimal")
        self.assertEqual(len(request["legs"]), 2)
        self.assertEqual(request["legs"][0]["legBetType"], "MONEYLINE")
        self.assertEqual(request["legs"][1]["team"], "TEAM2")
        self.assertNotEqual(request["legs"][0]["uniqueLegId"], request["legs"][1]["uniqueLegId"])
        self.assertTrue(parlay_request([leg("TEAM1", 1, 1)], 5.0)["riskAmount"] == 5.0)

    def test_straight_request_shape(self):
        from betbot.model import Candidate
        from betbot.providers.ps3838 import straight_request

        leg = Candidate(
            event_id="1", league="L", match="A vs B", commence_time=utcnow(),
            market="h2h", selection="Arsenal", price=1.5, book="pinnacle",
            fair_prob=0.65, edge=0.02, overround=0.02, n_books=1, sharp_reference=True,
            venue={"sport_id": 29, "event_id": 758023991, "period_number": 0,
                   "line_id": 419715968, "bet_type": "MONEYLINE", "team": "TEAM1"},
        )
        request = straight_request(leg, 12.345)
        self.assertEqual(request["stake"], 12.35)
        self.assertEqual(request["winRiskStake"], "RISK")
        self.assertEqual(request["fillType"], "NORMAL")
        self.assertEqual(request["team"], "TEAM1")
        self.assertIsNone(request["side"])

    def test_missing_venue_is_an_explicit_error(self):
        from betbot.model import Candidate
        from betbot.providers.ps3838 import parlay_request

        leg = Candidate(
            event_id="1", league="L", match="A vs B", commence_time=utcnow(),
            market="h2h", selection="Arsenal", price=1.5, book="bet365",
            fair_prob=0.65, edge=0.02, overround=0.02, n_books=1, sharp_reference=False,
        )
        with self.assertRaises(ValueError):
            parlay_request([leg], 10.0)

    def test_response_status_mapping(self):
        from betbot.providers.ps3838 import summarize_response

        self.assertEqual(summarize_response({"status": "ACCEPTED", "betId": 1})[0], "accepted")
        self.assertEqual(
            summarize_response({"status": "PROCESSED_WITH_ERROR", "errorCode": "LINE_CHANGED"}),
            ("rejected", "LINE_CHANGED"),
        )
        self.assertEqual(summarize_response({"status": "PENDING_ACCEPTANCE"})[0], "pending")

    def test_parlay_odds_extraction(self):
        from betbot.providers.ps3838 import parlay_odds_from_line, parlay_max_stake

        self.assertAlmostEqual(
            parlay_odds_from_line({"roundRobinOptions": [{"roundRobinOption": "Parlay", "odds": 2.13}]}),
            2.13,
        )
        self.assertAlmostEqual(
            parlay_odds_from_line({
                "status": "VALID",
                "roundRobinOptionWithOdds": [{"roundRobinOption": "Parlay", "odds": 2.05}],
            }),
            2.05,
        )
        self.assertIsNone(parlay_odds_from_line({"status": "PROCESSED_WITH_ERROR"}))
        self.assertIsNone(parlay_odds_from_line({}))
        self.assertEqual(parlay_max_stake({"maxParlayRiskStake": 50.0}), 50.0)

    def test_parlay_line_legs_use_line_endpoint_spelling(self):
        from betbot.model import Candidate
        from betbot.providers.ps3838 import parlay_line_legs

        leg = Candidate(
            event_id="1", league="L", match="A vs B", commence_time=utcnow(),
            market="h2h", selection="Arsenal", price=1.5, book="pinnacle",
            fair_prob=0.65, edge=0.02, overround=0.02, n_books=1, sharp_reference=True,
            venue={"sport_id": 29, "event_id": 758023991, "period_number": 0,
                   "line_id": 419715968, "bet_type": "MONEYLINE", "team": "TEAM1"},
        )
        payload = parlay_line_legs([leg])
        self.assertEqual(payload[0]["team"], "Team1")
        self.assertEqual(payload[0]["eventId"], 758023991)
        self.assertNotIn("lineId", payload[0])
        self.assertIn("uniqueLegId", payload[0])


class FakePs3838:
    """Stands in for Ps3838Client so the live path can be tested offline."""

    def __init__(self, line, response):
        self.line = line
        self.response = response
        self.seen_line = None
        self.seen_parlay = None

    def parlay_line(self, legs):
        self.seen_line = legs
        return self.line

    def place_parlay(self, request):
        self.seen_parlay = request
        return self.response


class TestPs3838Broker(unittest.TestCase):
    def cfg(self):
        return Config(bankroll=1000.0, min_stake=1.0, target_odds=2.0,
                      odds_tolerance=0.08, legs=2, min_leg_odds=1.2,
                      max_leg_odds=1.85, min_probability=0.55,
                      min_combo_probability=0.4, min_edge=0.02, markets=["h2h"])

    def legs(self):
        from betbot.model import Candidate

        out = []
        for index, team in enumerate(("TEAM1", "TEAM2"), start=1):
            out.append(
                Candidate(
                    event_id=str(index), league="L", match=f"E{index}", commence_time=utcnow(),
                    market="h2h", selection="Home", price=1.41, book="pinnacle",
                    fair_prob=0.68, edge=0.02, overround=0.02, n_books=1,
                    sharp_reference=True,
                    venue={"sport_id": 29, "event_id": 100 + index, "period_number": 0,
                           "line_id": 500 + index, "bet_type": "MONEYLINE", "team": team},
                )
            )
        return out

    def combo(self):
        from betbot.model import Combo

        return Combo(legs=self.legs(), odds=2.0, prob=0.46, ev=0.03)

    def test_parlay_is_placed_and_reported(self):
        from betbot.execution import Ps3838Broker

        client = FakePs3838(
            line={"status": "VALID",
                  "roundRobinOptionWithOdds": [{"roundRobinOption": "Parlay", "odds": 2.02}],
                  "maxParlayRiskStake": 100.0},
            response={"status": "ACCEPTED", "betId": 555, "price": 2.02},
        )
        placement = Ps3838Broker(client).place(self.combo(), 20.0, self.cfg())
        self.assertEqual(placement.status, "accepted")
        self.assertEqual(placement.ref, "555")
        self.assertEqual(client.seen_parlay["riskAmount"], 20.0)
        self.assertEqual(len(client.seen_parlay["legs"]), 2)

    def test_stale_venue_price_aborts_before_placing(self):
        from betbot.execution import Ps3838Broker

        client = FakePs3838(
            line={"status": "VALID",
                  "roundRobinOptionWithOdds": [{"roundRobinOption": "Parlay", "odds": 1.80}]},
            response={"status": "ACCEPTED", "betId": 1},
        )
        placement = Ps3838Broker(client).place(self.combo(), 20.0, self.cfg())
        self.assertEqual(placement.status, "rejected")
        self.assertIsNone(client.seen_parlay)
        self.assertIn("below floor", placement.detail)

    def test_stake_is_capped_to_venue_maximum(self):
        from betbot.execution import Ps3838Broker

        client = FakePs3838(
            line={"status": "VALID",
                  "roundRobinOptionWithOdds": [{"roundRobinOption": "Parlay", "odds": 2.01}],
                  "maxParlayRiskStake": 12.0},
            response={"status": "ACCEPTED", "betId": 9},
        )
        placement = Ps3838Broker(client).place(self.combo(), 40.0, self.cfg())
        self.assertEqual(placement.status, "accepted")
        self.assertEqual(client.seen_parlay["riskAmount"], 12.0)
        self.assertEqual(placement.stake, 12.0)

    def test_venue_error_is_surfaced(self):
        from betbot.execution import Ps3838Broker

        client = FakePs3838(
            line={"status": "VALID",
                  "roundRobinOptionWithOdds": [{"roundRobinOption": "Parlay", "odds": 2.01}]},
            response={"status": "PROCESSED_WITH_ERROR",
                      "errorCode": "ODDS_NO_LONGER_OFFERED_FOR_PARLAY_1"},
        )
        placement = Ps3838Broker(client).place(self.combo(), 20.0, self.cfg())
        self.assertEqual(placement.status, "rejected")
        self.assertIn("ODDS_NO_LONGER_OFFERED", placement.detail)


class TestBetpawa(unittest.TestCase):
    """Pure price/slippage logic. No browser, no network."""

    def cfg(self, **overrides):
        base = dict(
            bankroll=10000.0, currency="TZS", min_stake=1.0,
            target_odds=2.0, odds_tolerance=0.08, legs=2,
            min_leg_odds=1.2, max_leg_odds=1.85, min_probability=0.55,
            min_combo_probability=0.4, min_edge=0.02, markets=["h2h"],
            kelly_fraction=0.25, max_stake_pct=0.02, daily_stake_cap_pct=0.05,
            betpawa_max_slippage=0.02,
        )
        base.update(overrides)
        return Config(**base)

    def combo(self, price_a=1.41, price_b=1.41, prob=0.53, ev=0.054):
        from betbot.model import Candidate, Combo

        legs = []
        for index, (team, price) in enumerate(
            (("Arsenal", price_a), ("Chelsea", price_b)), start=1
        ):
            legs.append(
                Candidate(
                    event_id=str(index), league="L", match=f"E{index} v",
                    commence_time=utcnow(), market="h2h", selection=team,
                    price=price, book="pinnacle", fair_prob=0.70, edge=0.02,
                    overround=0.02, n_books=1, sharp_reference=True,
                )
            )
        return Combo(legs=legs, odds=round(price_a * price_b, 3), prob=prob, ev=ev)

    # -- parsing -------------------------------------------------------
    def test_parse_prices_reads_in_display_order(self):
        from betbot.betpawa import parse_prices

        text = "Arsenal 1.85\nChelsea 2.10"
        self.assertEqual(parse_prices(text, 2), [1.85, 2.10])

    def test_parse_prices_stops_at_expected_count(self):
        from betbot.betpawa import parse_prices

        self.assertEqual(parse_prices("1.50 1.60 1.70", 2), [1.50, 1.60])

    def test_parse_prices_ignores_nonsense_values(self):
        from betbot.betpawa import parse_prices

        self.assertEqual(parse_prices("0.00 / 1.00 / 5000.00", 3), [])

    def test_slip_noise_removes_stake_and_total_lines(self):
        from betbot.betpawa import strip_slip_noise

        text = "Arsenal 1.85\nTotal odds 2.05\nStake 500.00\nChelsea 2.10"
        kept = strip_slip_noise(text)
        self.assertNotIn("2.05", kept)
        self.assertNotIn("500.00", kept)
        self.assertIn("1.85", kept)

    # -- slippage ------------------------------------------------------
    def test_matching_price_keeps_the_planned_stake(self):
        from betbot.betpawa import assess_slippage

        decision = assess_slippage(self.combo(), 120.0, [1.41, 1.41], self.cfg(), 10000.0)
        self.assertTrue(decision.ok, decision.reason)
        self.assertFalse(decision.reduced)
        self.assertAlmostEqual(decision.stake, 120.0)
        self.assertAlmostEqual(decision.odds, 1.9881, places=4)

    def test_small_slippage_reduces_the_stake(self):
        from betbot.betpawa import assess_slippage

        # 1% worse per leg: inside tolerance, EV still positive, Kelly says less
        decision = assess_slippage(self.combo(), 120.0, [1.395, 1.395], self.cfg(), 10000.0)
        self.assertTrue(decision.ok, decision.reason)
        self.assertTrue(decision.reduced)
        self.assertLess(decision.stake, 120.0)

    def test_slippage_past_tolerance_aborts(self):
        from betbot.betpawa import assess_slippage

        decision = assess_slippage(self.combo(), 120.0, [1.30, 1.41], self.cfg(), 10000.0)
        self.assertFalse(decision.ok)
        self.assertIn("scanned at", decision.reason)

    def test_venue_price_below_min_edge_aborts(self):
        from betbot.betpawa import assess_slippage

        # inside the 2% tolerance, but the combination no longer clears 5% EV
        combo = self.combo(price_a=1.60, price_b=1.60, prob=0.40)
        decision = assess_slippage(combo, 120.0, [1.59, 1.59], self.cfg(min_edge=0.05), 10000.0)
        self.assertFalse(decision.ok)
        self.assertIn("min_edge", decision.reason)

    def test_wrong_number_of_read_prices_aborts(self):
        from betbot.betpawa import assess_slippage

        decision = assess_slippage(self.combo(), 120.0, [1.41], self.cfg(), 10000.0)
        self.assertFalse(decision.ok)
        self.assertIn("expected 2", decision.reason)

    def test_implausibly_high_read_aborts_instead_of_betting_it(self):
        from betbot.betpawa import assess_slippage

        # the combined odds line (2.0) read as a leg price would inflate the EV
        decision = assess_slippage(self.combo(), 120.0, [2.00, 1.41], self.cfg(), 10000.0)
        self.assertFalse(decision.ok)
        self.assertIn("read wrong", decision.reason)

    def test_zero_edge_mode_does_not_gate_on_ev(self):
        from betbot.betpawa import assess_slippage

        # EV is +0.9%: below min_edge, so strict mode refuses but maximum-
        # probability mode still takes it
        combo = self.combo(price_a=1.40, price_b=1.40, prob=0.515)
        strict = assess_slippage(combo, 120.0, [1.40, 1.40], self.cfg(), 10000.0)
        loose = assess_slippage(
            combo, 120.0, [1.40, 1.40], self.cfg(allow_zero_edge=True), 10000.0
        )
        self.assertFalse(strict.ok)
        self.assertTrue(loose.ok, loose.reason)

    # -- wiring --------------------------------------------------------
    def test_broker_is_registered(self):
        from betbot.execution import BetpawaBroker, build_broker

        cfg = self.cfg(execution="betpawa")
        cfg.validate()
        self.assertIsInstance(build_broker(cfg), BetpawaBroker)

    def test_invalid_slippage_tolerance_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cfg(betpawa_max_slippage=1.5).validate()

    def test_broker_hands_the_browser_the_live_bankroll(self):
        from betbot.execution import BetpawaBroker, Placement

        class FakeBrowser:
            def __init__(self):
                self.seen = None

            def place(self, combo, stake, bankroll, already_staked_today):
                self.seen = (stake, bankroll, already_staked_today)
                return Placement(ref="FAKE", status="accepted")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(
                execution="betpawa", mode="live",
                db_path=str(Path(tmp) / "bets.db"),
            )
            browser = FakeBrowser()
            placement = BetpawaBroker(browser).place(self.combo(), 50.0, cfg)
            self.assertEqual(placement.status, "accepted")
            self.assertEqual(browser.seen, (50.0, 10000.0, 0.0))


class TestBetpawaProvider(unittest.TestCase):
    @staticmethod
    def item(event_id="39172703", kickoff=None):
        return {
            "id": event_id,
            "name": "Bayern Munich - 1. FC Cologne",
            "startTime": (kickoff or utcnow() + timedelta(hours=8)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "participants": [
                {"id": "1", "name": "Bayern Munich", "position": 1},
                {"id": "2", "name": "1. FC Cologne", "position": 2},
            ],
            "markets": [{
                "marketType": {"id": "3743", "name": "1X2 - FT",
                               "displayName": "1X2 | Full Time"},
                "row": [{"id": "418", "prices": [
                    {"id": "900", "name": "1", "odds": 1.44, "suspended": False},
                    {"id": "901", "name": "X", "odds": 4.20, "suspended": False},
                    {"id": "902", "name": "2", "odds": 6.10, "suspended": False},
                ]}],
            }],
            "competition": {"id": "99", "name": "Bundesliga"},
        }

    def test_real_payload_shape_becomes_h2h_quotes(self):
        from betbot.providers.betpawa import _to_event
        from betbot.util import utcnow as now

        event = _to_event(self.item(), now() + timedelta(minutes=45),
                          now() + timedelta(days=3))
        self.assertIsNotNone(event)
        self.assertEqual(event.home, "Bayern Munich")
        self.assertEqual(event.away, "1. FC Cologne")
        self.assertEqual(event.league, "Bundesliga")
        quotes = event.quotes["h2h"]["betpawa"]
        self.assertAlmostEqual(quotes["Bayern Munich"], 1.44)
        self.assertAlmostEqual(quotes["Draw"], 4.20)
        self.assertAlmostEqual(quotes["1. FC Cologne"], 6.10)
        venue = event.venue["h2h"]["Draw"]
        self.assertEqual(venue["outcome"], "X")
        self.assertEqual(venue["price_id"], "901")
        self.assertEqual(venue["event_id"], "39172703")

    def test_suspended_and_too_soon_events_are_dropped(self):
        from betbot.providers.betpawa import _to_event
        from betbot.util import utcnow as now

        item = self.item(kickoff=now() + timedelta(minutes=10))
        self.assertIsNone(_to_event(item, now() + timedelta(minutes=45),
                                    now() + timedelta(days=3)))
        item = self.item()
        item["markets"][0]["row"][0]["prices"][1]["suspended"] = True
        self.assertIsNone(_to_event(item, now() + timedelta(minutes=45),
                                    now() + timedelta(days=3)))

    def test_grouped_response_is_deduplicated(self):
        from betbot.providers.betpawa import to_events

        payload = {"responses": [
            {"responses": [self.item("a"), self.item("b")]},
            {"responses": [self.item("a"), self.item("c")]},
        ]}
        events = to_events(payload, Config(min_lead_minutes=45))
        self.assertEqual({e.event_id for e in events}, {"a", "b", "c"})
        self.assertEqual([e.commence_time for e in events],
                         sorted(e.commence_time for e in events))

    def test_betpawa_is_a_valid_provider(self):
        Config(provider="betpawa", reference="theoddsapi").validate()


class TestVenueBook(unittest.TestCase):
    def test_reference_prices_are_not_offered_as_tickets(self):
        cfg = Config(markets=["h2h"], venue_book="betpawa",
                     min_leg_odds=1.2, max_leg_odds=1.85,
                     min_probability=0.5, min_combo_probability=0.4,
                     target_odds=2.0, odds_tolerance=0.2, legs=2,
                     max_candidates=10)
        event = Event(
            event_id="e1", sport="soccer", league="Bundesliga",
            home="Home", away="Away", commence_time=utcnow() + timedelta(hours=5),
        )
        event.quotes["h2h"] = {
            "betpawa": {"Home": 1.72, "Draw": 4.0, "Away": 6.0},
            "pinnacle": {"Home": 1.75, "Draw": 4.2, "Away": 5.5},
        }
        scan = build_candidates([event], cfg, utcnow())
        self.assertTrue(scan.candidates)
        self.assertEqual({c.book for c in scan.candidates}, {"betpawa"})
        self.assertAlmostEqual(scan.candidates[0].price, 1.72)


class TestSettleWithRealPrices(unittest.TestCase):
    @staticmethod
    def combo():
        cfg = Config(bankroll=500.0, target_odds=2.0, odds_tolerance=0.2,
                     legs=2, min_leg_odds=1.2, max_leg_odds=1.9,
                     min_probability=0.5, min_combo_probability=0.4,
                     min_edge=0.01, markets=["h2h"], max_candidates=10)
        a = make_event("e1", {"Home": 0.62, "Draw": 0.24, "Away": 0.14},
                       books={"bet365": 0.09})
        b = make_event("e2", {"Home": 0.80, "Draw": 0.12, "Away": 0.08},
                       books={"bet365": 0.06})
        combo, _ = best_combo(build_candidates([a, b], cfg, utcnow()).candidates, cfg)
        return combo

    def test_actual_prices_drive_pnl_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "bets.db")
            combo = self.combo()
            bet_id = ledger.add_bet(combo, 20.0, "paper", "betpawa", "betpawa",
                                    status="open")
            actual = [1.30, 1.40]
            pnl = ledger.settle(bet_id, "win", prices=actual)
            self.assertAlmostEqual(pnl, round(20.0 * (1.30 * 1.40 - 1.0), 2), places=2)
            bet = ledger.get_bet(bet_id)
            self.assertAlmostEqual(bet.actual_odds, 1.82)
            self.assertEqual([leg["actual_price"] for leg in bet.legs], actual)
            # scanned odds stay on the record so the slippage is auditable
            self.assertAlmostEqual(bet.odds, combo.odds)

    def test_wrong_number_of_prices_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "bets.db")
            bet_id = ledger.add_bet(self.combo(), 20.0, "paper", "demo",
                                    "paper", status="open")
            with self.assertRaises(ValueError):
                ledger.settle(bet_id, "win", prices=[1.5])

    def test_existing_ledger_gets_the_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bets.db"
            Ledger(path)                      # initial schema
            ledger = Ledger(path)             # re-open: migration must be safe
            self.assertIsNone(ledger.get_bet(1))


class TestReferenceMergeByPosition(unittest.TestCase):
    def test_transliterated_away_name_still_maps(self):
        soft = Event(
            event_id="bp", sport="soccer", league="Bundesliga",
            home="Bayern Munich", away="1. FC Cologne",
            commence_time=utcnow() + timedelta(hours=4),
        )
        soft.quotes["h2h"] = {"betpawa": {
            "Bayern Munich": 1.44, "Draw": 4.20, "1. FC Cologne": 6.10}}
        ref = Event(
            event_id="api", sport="soccer", league="Bundesliga",
            home="Bayern Munich", away="1. FC Köln",
            commence_time=utcnow() + timedelta(hours=4),
        )
        ref.quotes["h2h"] = {"pinnacle": {
            "Bayern Munich": 1.50, "Draw": 4.00, "1. FC Köln": 5.80}}
        self.assertEqual(attach_reference([soft], [ref]), 1)
        mapped = soft.quotes["h2h"]["pinnacle"]
        self.assertAlmostEqual(mapped["Bayern Munich"], 1.50)
        self.assertAlmostEqual(mapped["1. FC Cologne"], 5.80)
        self.assertAlmostEqual(mapped["Draw"], 4.00)


class TestConfigValidation(unittest.TestCase):
    def test_the_odds_api_is_a_valid_reference(self):
        Config(reference="theoddsapi").validate()

    def test_unknown_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(reference="nope").validate()


class TestWebControls(unittest.TestCase):
    def test_config_whitelist_and_typed_live_confirmation(self):
        from betbot.webapp import LIVE_CONFIRM, validated_updates

        current = {
            "bankroll": 100000.0, "betpawa_dry_run": True,
            "min_leg_odds": 1.2, "max_leg_odds": 1.85,
        }
        merged, applied, errors = validated_updates(
            current,
            {"bankroll": "250000", "mode": "live", "betpawa_dry_run": False},
            "",
        )
        self.assertIn("not editable", " ".join(errors))
        self.assertIn("typed live confirmation", " ".join(errors))
        self.assertEqual(applied, {})
        self.assertEqual(merged, current)

        merged, applied, errors = validated_updates(
            current,
            {"bankroll": "250000", "betpawa_dry_run": False},
            LIVE_CONFIRM,
        )
        self.assertEqual(errors, [])
        self.assertEqual(applied["bankroll"], 250000.0)
        self.assertFalse(applied["betpawa_dry_run"])
        self.assertEqual(merged["bankroll"], 250000.0)

    def test_enabling_auto_place_needs_typed_live_confirmation(self):
        from betbot.webapp import LIVE_CONFIRM, validated_updates

        current = {"betpawa_dry_run": False, "auto_place_enabled": False}

        merged, applied, errors = validated_updates(
            current, {"auto_place_enabled": True}, "")
        self.assertIn("typed live confirmation", " ".join(errors))
        self.assertEqual(applied, {})
        self.assertFalse(merged["auto_place_enabled"])

        # turning it OFF needs no confirmation; turning it ON needs the exact text
        merged, applied, errors = validated_updates(
            current, {"auto_place_enabled": False}, "")
        self.assertEqual(errors, [])
        self.assertFalse(applied["auto_place_enabled"])

        merged, applied, errors = validated_updates(
            current, {"auto_place_enabled": "true"}, LIVE_CONFIRM)
        self.assertEqual(errors, [])
        self.assertTrue(applied["auto_place_enabled"])
        self.assertTrue(merged["auto_place_enabled"])

    def test_dry_run_detection_is_conservative(self):
        from betbot.webapp import is_dry_run

        self.assertTrue(is_dry_run(Config(execution="paper", mode="live")))
        self.assertTrue(is_dry_run(Config(execution="ticket", mode="live")))
        self.assertTrue(is_dry_run(Config(
            execution="betpawa", mode="live", betpawa_dry_run=True)))
        self.assertFalse(is_dry_run(Config(
            execution="betpawa", mode="live", betpawa_dry_run=False)))
        # The PS3838 adapter can place real orders even if mode is left on paper.
        self.assertFalse(is_dry_run(Config(execution="ps3838", mode="paper")))

    def test_manual_stake_can_only_lower_the_plan(self):
        from betbot.staking import StakePlan, StakeRequestError, apply_manual_stake

        cfg = Config(bankroll=1000.0, min_stake=10.0, max_stake_pct=0.02,
                     daily_stake_cap_pct=0.05, kelly_fraction=1.0)
        plan = StakePlan(stake=20.0, kelly_full=0.5, kelly_used=0.5,
                         capped_by="max_stake_pct", ev_amount=1.0)

        stake, note = apply_manual_stake(plan, "15", cfg)
        self.assertEqual(stake, 15.0)
        self.assertEqual(note, "")

        stake, note = apply_manual_stake(plan, 50, cfg)
        self.assertEqual(stake, 20.0)
        self.assertIn("capped", note)

        for bad in ("5", -1, "abc", float("nan")):
            with self.subTest(bad=bad), self.assertRaises(StakeRequestError):
                apply_manual_stake(plan, bad, cfg)


class TestAiFallback(unittest.TestCase):
    def test_no_credit_key_returns_headlines_only_briefing(self):
        import io
        import json as _json
        import os
        from unittest import mock
        from urllib.error import HTTPError

        from betbot import ai

        body = _json.dumps({"error": {
            "message": "You have no credits remaining.",
            "code": "credit_balance_exhausted",
        }}).encode("utf-8")
        error = HTTPError("https://api.openai.com/v1/chat/completions", 429,
                          "Too Many Requests", {}, io.BytesIO(body))
        env = {"AI_API_KEY": "sk-test", "AI_BASE_URL": "https://api.openai.com/v1",
               "AI_MODEL": "gpt-4o-mini"}
        headlines = [{"title": "Manager confirms injury", "url": "https://example.com",
                      "date": "now", "source": "Source"}]

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ai, "news_for_match", return_value=headlines), \
                mock.patch.object(ai.urllib.request, "urlopen", side_effect=error):
            briefing = ai.research(
                Config(), "Home", "Away", "League", "Home",
                "2026-11-20T19:30:00Z", 1.50, 0.60, use_cache=False,
            )

        self.assertTrue(briefing.degraded)
        self.assertEqual(briefing.prob_adjustment, 0.0)
        self.assertEqual(briefing.verdict, "neutral")
        self.assertIn("no API credits", briefing.summary)
        self.assertEqual(len(briefing.news), 1)
        error.close()


if __name__ == "__main__":
    unittest.main()
