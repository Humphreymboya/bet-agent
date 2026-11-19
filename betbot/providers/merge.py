"""Attach Betfair exchange prices to soft-book fixtures as a fair-value anchor.

Matching is deliberately conservative: same two clubs (fuzzy on abbreviations),
kickoff within a few hours. A wrongly matched match would poison the fair-price
estimate, so anything ambiguous is dropped rather than guessed.
"""

from __future__ import annotations

from datetime import timedelta

from ..model import Event
from ..util import norm_name, tokens


def attach_reference(
    events: list[Event], references: list[Event], max_hours: int = 6
) -> int:
    if not references:
        return 0
    attached = 0
    for event in events:
        best, best_score = None, 0.0
        for ref in references:
            if abs(ref.commence_time - event.commence_time) > timedelta(hours=max_hours):
                continue
            score = _pair_score(event.home, event.away, ref.home, ref.away)
            if score > best_score:
                best, best_score = ref, score
        if best is None or best_score < 1.0:
            continue
        if _copy_market(best, event, "h2h") or _copy_market(best, event, "totals"):
            attached += 1
    return attached


def _copy_market(source: Event, target: Event, market: str) -> bool:
    books = source.quotes.get(market, {})
    if not books:
        return False
    target_labels = set()
    for book_prices in target.quotes.get(market, {}).values():
        target_labels |= set(book_prices)
    if not target_labels:
        return False

    copied_any = False
    venues: dict[str, dict] = {}
    for book, prices in books.items():
        mapped: dict[str, float] = {}
        for label in target_labels:
            # h2h labels are club names, and feeds disagree on transliteration
            # ("Cologne" vs "Köln"). The source event tells us which label is
            # the home side and which the away side, so translate by position
            # before falling back to fuzzy name overlap.
            price = source_label = None
            if label == target.home and source.home in prices:
                price, source_label = prices[source.home], source.home
            elif label == target.away and source.away in prices:
                price, source_label = prices[source.away], source.away
            else:
                price, source_label = _match_label(label, prices)
            if price is None:
                continue
            mapped[label] = price
            venue = source.venue.get(market, {}).get(source_label)
            if venue:
                venues[label] = venue
        if len(mapped) < max(2, len(target_labels) - 1):
            continue
        target.quotes.setdefault(market, {})[book] = mapped
        copied_any = True

    if not copied_any:
        return False
    if venues:
        # the placement payload travels with the price, so live execution can
        # use a candidate discovered on the soft-book feed
        target.venue.setdefault(market, {}).update(venues)
    return True


def _match_label(label: str, prices: dict[str, float]) -> tuple[float | None, str]:
    if label.lower() == "draw" and "Draw" in prices:
        return prices["Draw"], "Draw"
    if label in prices:
        return prices[label], label
    best_name, best_score = None, 0.0
    for name in prices:
        score = _overlap(label, name)
        if score > best_score:
            best_name, best_score = name, score
    if best_name is not None and best_score >= 0.5:
        return prices[best_name], best_name
    return None, ""


def _pair_score(home_a: str, away_a: str, home_b: str, away_b: str) -> float:
    direct = _overlap(home_a, home_b) + _overlap(away_a, away_b)
    swapped = _overlap(home_a, away_b) + _overlap(away_a, home_b)
    return min(direct, swapped) if min(direct, swapped) > 0 else max(direct, swapped)


def _overlap(a: str, b: str) -> float:
    """Token overlap in [0, 1], tolerant of 'Man' vs 'Manchester'."""
    left, right = tokens(a), tokens(b)
    if not left or not right:
        return 0.0
    if norm_name(a) == norm_name(b):
        return 1.0
    matched = 0
    for token in left:
        if any(_token_equals(token, other) for other in right):
            matched += 1
    return matched / min(len(left), len(right))


def _token_equals(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long.startswith(short)
