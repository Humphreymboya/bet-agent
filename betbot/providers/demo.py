"""Synthetic but realistic odds feed, for dry runs and tests.

Fixtures come from a latent "true" probability, then several books quote it with
different margins and noise. One book gets occasional price boosts, which is
what creates the small, genuine +EV spots the agent is looking for.
Deterministic per calendar day, so a re-run reproduces the same slate.
"""

from __future__ import annotations

import random
from datetime import timedelta

from ..config import Config
from ..model import Event
from ..util import iso_day, utcnow
from .base import OddsProvider

LEAGUES: dict[str, tuple[str, list[str]]] = {
    "soccer_epl": (
        "Premier League",
        ["Arsenal", "Burnley", "Liverpool", "Everton", "Man City", "Sheffield Utd",
         "Newcastle", "Bournemouth", "Brighton", "Wolves"],
    ),
    "soccer_spain_la_liga": (
        "La Liga",
        ["Real Madrid", "Almeria", "Barcelona", "Cadiz", "Atletico Madrid", "Mallorca",
         "Real Sociedad", "Granada", "Villarreal", "Las Palmas"],
    ),
    "soccer_italy_serie_a": (
        "Serie A",
        ["Inter", "Salernitana", "AC Milan", "Empoli", "Juventus", "Frosinone",
         "Napoli", "Cagliari", "Roma", "Verona"],
    ),
    "soccer_germany_bundesliga": (
        "Bundesliga",
        ["Bayern Munich", "Darmstadt", "Leverkusen", "Mainz", "Dortmund", "Bochum",
         "RB Leipzig", "Heidenheim", "Stuttgart", "Augsburg"],
    ),
    "soccer_france_ligue_one": (
        "Ligue 1",
        ["PSG", "Metz", "Monaco", "Lorient", "Marseille", "Clermont",
         "Lille", "Nantes", "Lyon", "Toulouse"],
    ),
    "soccer_uefa_champs_league": (
        "Champions League",
        ["Real Madrid", "Copenhagen", "Man City", "Young Boys", "Bayern Munich",
         "Galatasaray", "Barcelona", "Shakhtar Donetsk", "Arsenal", "Lens"],
    ),
}

# key, typical margin, quoting noise
BOOKS = [
    ("pinnacle", 0.015, 0.012),
    ("bet365", 0.045, 0.055),
    ("williamhill", 0.050, 0.065),
    ("unibet_eu", 0.048, 0.075),
    ("marathonbet", 0.038, 0.060),
]


class DemoProvider(OddsProvider):
    name = "demo"

    def __init__(self, fixtures_per_league: int = 7, seed: int | None = None):
        self.fixtures_per_league = fixtures_per_league
        self.seed = seed

    # ------------------------------------------------------------------
    def fetch_events(self, cfg: Config) -> list[Event]:
        now = utcnow()
        seed = self.seed if self.seed is not None else int(iso_day(now).replace("-", ""))
        rng = random.Random(seed)

        events: list[Event] = []
        leagues = [k for k in cfg.sports if k in LEAGUES] or list(LEAGUES)
        kickoff = now + timedelta(hours=2)

        for league_key in leagues:
            league_name, teams = LEAGUES[league_key]
            for index in range(self.fixtures_per_league):
                home, away = rng.sample(teams, 2)
                strength = rng.uniform(-1.0, 1.0) + rng.uniform(-0.35, 0.35)
                probs = _h2h_probs(strength, rng)
                over_prob = min(0.72, max(0.34, 0.50 + rng.uniform(-0.12, 0.16)))

                start = kickoff + timedelta(minutes=75 * index + rng.randint(0, 40))

                event = Event(
                    event_id=f"demo-{league_key}-{index:02d}-{home[:3].lower()}{away[:3].lower()}",
                    sport=league_key,
                    league=league_name,
                    home=home,
                    away=away,
                    commence_time=start,
                )

                h2h = _quote_market(
                    {"Home": probs[0], "Draw": probs[1], "Away": probs[2]}, rng
                )
                totals = _quote_market({"Over 2.5": over_prob, "Under 2.5": 1 - over_prob}, rng)
                event.quotes["h2h"] = h2h
                event.quotes["totals"] = totals
                events.append(event)

        events.sort(key=lambda e: e.commence_time)
        return events


# ----------------------------------------------------------------------
def _h2h_probs(edge: float, rng: random.Random) -> tuple[float, float, float]:
    # a realistic slate is mostly close games with a handful of clear favourites
    roll = rng.random()
    if roll < 0.30:
        home = rng.uniform(0.66, 0.82)
    elif roll < 0.50:
        home = rng.uniform(0.55, 0.66)
    else:
        home = rng.uniform(0.30, 0.55)
    home = min(0.86, max(0.20, home + 0.02 * edge))
    draw = rng.uniform(0.16, 0.27)
    away = max(0.05, 1.0 - home - draw)
    total = home + draw + away
    return home / total, draw / total, away / total


def _quote_market(
    true_probs: dict[str, float], rng: random.Random
) -> dict[str, dict[str, float]]:
    market: dict[str, dict[str, float]] = {}
    favourite = max(true_probs, key=true_probs.get)
    boost = rng.random() < 0.40

    for book, margin, noise in BOOKS:
        # each desk has its own opinion around the true price
        adjusted = {
            label: max(1e-4, prob * (1.0 + rng.gauss(0.0, noise)))
            for label, prob in true_probs.items()
        }
        total = sum(adjusted.values())
        adjusted = {label: prob / total for label, prob in adjusted.items()}

        prices = {
            label: round(1.0 / (prob * (1.0 + margin)), 3)
            for label, prob in adjusted.items()
        }

        # occasional price boost on the favourite, like a real promo
        if boost and book in {"bet365", "williamhill"}:
            bump = rng.uniform(0.05, 0.11)
            prices[favourite] = round(prices[favourite] * (1.0 + bump), 3)

        market[book] = {label: max(1.01, price) for label, price in prices.items()}

    return market
