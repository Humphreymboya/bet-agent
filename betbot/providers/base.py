"""Provider interface: anything that can hand back priced fixtures."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config
from ..model import Event


class OddsProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_events(self, cfg: Config) -> list[Event]:
        """Return upcoming events with multi-book prices attached."""


class ProviderError(RuntimeError):
    """Any data-feed failure that should stop the run cleanly."""
