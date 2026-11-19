"""Pricing model: de-vig bookmaker prices, measure edge, assemble the combo.

The core idea, stated honestly:

* Bookmaker prices are not probabilities. Each price contains a margin
  (overround). Dividing out that margin per book gives each book's own opinion.
* The *sharp* books (Pinnacle, exchanges) are the closest thing to a fair price
  that exists publicly. Averaging their de-vigged probabilities gives a usable
  fair-probability estimate `p`.
* A bet only has value when the best price you can actually take beats 1/p.
  Edge = price * p - 1. Anything else is paying the margin for entertainment.

So the agent does not look for "sure things". It looks for prices that are
mispriced by 2%+, and it stays silent when they are not there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from math import prod
from statistics import median

from .config import Config

# Books whose de-vigged prices we treat as a fair-value reference.
SHARP_BOOKS = {
    "pinnacle",
    "betfair",
    "betfair_ex_eu",
    "betfair_ex_uk",
    "betfair_ex_au",
    "matchbook",
    "smarkets",
    "betdaq",
    "sbobet",
}


# ---------------------------------------------------------------------------
# data containers
# ---------------------------------------------------------------------------
@dataclass
class Event:
    event_id: str
    sport: str
    league: str
    home: str
    away: str
    commence_time: datetime
    # quotes[market][book][selection_label] = decimal price
    quotes: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # runner_ids[market][selection_label] = venue selection id (Betfair only)
    runner_ids: dict[str, dict[str, str]] = field(default_factory=dict)
    # venue[market][selection_label] = venue-specific placement payload (PS3838)
    venue: dict[str, dict[str, dict]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"


@dataclass
class Reference:
    probs: dict[str, float]
    books: list[str]
    sharp: bool
    overround: float | None


@dataclass
class Candidate:
    event_id: str
    league: str
    match: str
    commence_time: datetime
    market: str
    selection: str
    price: float
    book: str
    fair_prob: float
    edge: float
    overround: float | None
    n_books: int
    sharp_reference: bool
    runner_id: str | None = None
    venue: dict | None = None


@dataclass
class Combo:
    legs: list[Candidate]
    odds: float
    prob: float
    ev: float

    @property
    def n_legs(self) -> int:
        return len(self.legs)


@dataclass
class Scan:
    events: int
    markets_priced: int
    candidates: list[Candidate]
    rejects: dict[str, int]


# ---------------------------------------------------------------------------
# math
# ---------------------------------------------------------------------------
def _clean(prices: dict[str, float]) -> dict[str, float]:
    out = {}
    for label, price in (prices or {}).items():
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value > 1.0:
            out[label] = value
    return out


def overround(prices: dict[str, float]) -> float | None:
    """Bookmaker margin. 0.05 means a 5% theoretical hold."""
    clean = _clean(prices)
    if len(clean) < 2:
        return None
    return sum(1.0 / p for p in clean.values()) - 1.0


def normalise(prices: dict[str, float]) -> dict[str, float] | None:
    """Turn one book's prices into probabilities (proportional de-vig)."""
    clean = _clean(prices)
    if len(clean) < 2:
        return None
    inv = {label: 1.0 / price for label, price in clean.items()}
    total = sum(inv.values())
    if total <= 0:
        return None
    return {label: value / total for label, value in inv.items()}


def reference_probs(market_quotes: dict[str, dict[str, float]]) -> Reference | None:
    """Consensus fair probabilities for one market.

    Sharp books win when present; otherwise every priced book is averaged.
    """
    per_book: dict[str, dict[str, float]] = {}
    for book, prices in market_quotes.items():
        norm = normalise(prices)
        if norm:
            per_book[book] = norm
    if not per_book:
        return None

    sharp = {b: v for b, v in per_book.items() if b.lower() in SHARP_BOOKS}
    chosen = sharp or per_book

    shared = set.intersection(*(set(v) for v in chosen.values()))
    if not shared:
        return None

    count = len(chosen)
    probs = {label: sum(v[label] for v in chosen.values()) / count for label in shared}
    total = sum(probs.values())
    if total <= 0:
        return None
    probs = {label: value / total for label, value in probs.items()}

    overs = [overround(market_quotes[book]) for book in chosen]
    overs = [o for o in overs if o is not None]

    return Reference(
        probs=probs,
        books=sorted(chosen),
        sharp=bool(sharp),
        overround=median(overs) if overs else None,
    )


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def build_candidates(events: list[Event], cfg: Config, now: datetime) -> Scan:
    rejects = {
        "starts_too_soon": 0,
        "no_reference": 0,
        "no_price_in_band": 0,
        "probability_too_low": 0,
        "no_edge": 0,
    }
    candidates: list[Candidate] = []
    priced_markets = 0
    horizon = now + timedelta(minutes=cfg.min_lead_minutes)

    for event in events:
        if event.commence_time < horizon:
            rejects["starts_too_soon"] += 1
            continue

        for market_key, market_quotes in event.quotes.items():
            if market_key not in cfg.markets:
                continue

            ref = reference_probs(market_quotes)
            if not ref:
                rejects["no_reference"] += 1
                continue
            priced_markets += 1

            # Best price per selection. When the feed is the venue itself,
            # restrict this to the book we can actually take: a better price at
            # a reference book is useful as *evidence*, not as a bet we can
            # place. Using it would make every downstream price check fail.
            venue_book = (cfg.venue_book or "").strip().lower()
            best: dict[str, tuple[float, str]] = {}
            for book, prices in market_quotes.items():
                if venue_book and book.strip().lower() != venue_book:
                    continue
                over = overround(prices)
                if over is None or over > cfg.max_overround:
                    continue
                for label, price in _clean(prices).items():
                    if price < cfg.min_leg_odds or price > cfg.max_leg_odds:
                        continue
                    current = best.get(label)
                    if current is None or price > current[0]:
                        best[label] = (price, book)

            for label, fair_prob in ref.probs.items():
                if label not in best:
                    rejects["no_price_in_band"] += 1
                    continue
                if fair_prob < cfg.min_probability:
                    rejects["probability_too_low"] += 1
                    continue
                price, book = best[label]
                edge = price * fair_prob - 1.0
                # The pool keeps legs that are close to fair, not only the
                # standout ones: a 2.0 double needs a ~1.40 leg to pair with,
                # and value can sit on either side of that pair.
                if edge < cfg.pool_edge_floor and not cfg.allow_zero_edge:
                    rejects["no_edge"] += 1
                    continue
                candidates.append(
                    Candidate(
                        event_id=event.event_id,
                        league=event.league,
                        match=event.name,
                        commence_time=event.commence_time,
                        market=market_key,
                        selection=label,
                        price=price,
                        book=book,
                        fair_prob=fair_prob,
                        edge=edge,
                        overround=ref.overround,
                        n_books=len(ref.books),
                        sharp_reference=ref.sharp,
                        runner_id=event.runner_ids.get(market_key, {}).get(label),
                        venue=event.venue.get(market_key, {}).get(label),
                    )
                )

    candidates.sort(key=lambda c: c.edge, reverse=True)
    return Scan(
        events=len(events),
        markets_priced=priced_markets,
        candidates=candidates,
        rejects=rejects,
    )


def best_combo(candidates: list[Candidate], cfg: Config) -> tuple[Combo | None, dict]:
    """Pick the highest-EV set of legs whose combined odds land near target."""
    notes: dict = {"pool": 0, "in_odds_band": 0, "probable_enough": 0, "best_ev": None}
    if len(candidates) < cfg.legs:
        notes["reason"] = "not enough value candidates"
        return None, notes

    pool = candidates[: cfg.max_candidates]
    notes["pool"] = len(pool)

    lo = cfg.target_odds * (1.0 - cfg.odds_tolerance)
    hi = cfg.target_odds * (1.0 + cfg.odds_tolerance)

    best: Combo | None = None
    for group in combinations(pool, cfg.legs):
        # legs must come from different fixtures, otherwise it is one correlated bet
        if len({c.event_id for c in group}) != len(group):
            continue
        odds = prod(c.price for c in group)
        if not lo <= odds <= hi:
            continue
        notes["in_odds_band"] += 1
        prob = prod(c.fair_prob for c in group)
        if prob < cfg.min_combo_probability:
            continue
        notes["probable_enough"] += 1
        ev = prob * odds - 1.0
        score = (prob, ev) if cfg.allow_zero_edge else (ev, prob)
        if best is None or score > _score(best, cfg):
            best = Combo(legs=list(group), odds=odds, prob=prob, ev=ev)

    if best is None:
        notes["reason"] = (
            "no combination of legs landed in the odds band with enough probability"
        )
        return None, notes

    notes["best_ev"] = best.ev
    if cfg.allow_zero_edge:
        notes["mode"] = "max-probability (EV gate disabled)"
        return best, notes

    if best.ev < cfg.min_edge:
        notes["reason"] = (
            f"best available combo has {best.ev * 100:+.1f}% EV, "
            f"below the {cfg.min_edge * 100:.1f}% threshold"
        )
        return None, notes

    return best, notes


def _score(combo: Combo, cfg: Config) -> tuple[float, float]:
    return (combo.prob, combo.ev) if cfg.allow_zero_edge else (combo.ev, combo.prob)
