"""Provider registry."""

from __future__ import annotations

from ..config import Config
from ..model import Event
from .base import OddsProvider, ProviderError
from .betpawa import BetpawaProvider
from .demo import DemoProvider
from .merge import attach_reference
from .ps3838 import Ps3838Provider
from .theoddsapi import TheOddsApiProvider


def build_provider(cfg: Config) -> OddsProvider:
    if cfg.provider == "demo":
        return DemoProvider()
    if cfg.provider == "theoddsapi":
        return TheOddsApiProvider()
    if cfg.provider == "ps3838":
        return Ps3838Provider()
    if cfg.provider == "betpawa":
        return BetpawaProvider()
    raise ProviderError(f"unknown provider: {cfg.provider}")


def fetch_with_reference(cfg: Config) -> tuple[list[Event], str]:
    """Fetch the main feed, optionally attaching a sharper book as anchor."""
    provider = build_provider(cfg)
    events = provider.fetch_events(cfg)
    note = provider.name

    if cfg.reference in {"betfair", "ps3838", "theoddsapi"}:
        if cfg.reference == "betfair":
            from .betfair import BetfairProvider

            reference_events = BetfairProvider().fetch_events(cfg)
        elif cfg.reference == "ps3838":
            from .ps3838 import Ps3838Provider

            reference_events = Ps3838Provider().fetch_events(cfg)
        else:
            # the European books, with Pinnacle as the sharp anchor. This is the
            # right shape when betPawa supplies the prices: the venue is the
            # price, the sharp consensus is the opinion it gets judged against.
            reference_events = TheOddsApiProvider().fetch_events(cfg)
        attached = attach_reference(events, reference_events)
        note = f"{provider.name}+{cfg.reference}({attached} matched)"
    return events, note


__all__ = [
    "OddsProvider",
    "ProviderError",
    "DemoProvider",
    "BetpawaProvider",
    "TheOddsApiProvider",
    "build_provider",
    "fetch_with_reference",
    "attach_reference",
]
