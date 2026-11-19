"""Small shared helpers: time handling, name normalisation, formatting."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with or without 'Z') into aware UTC."""
    if not value:
        raise ValueError("empty timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_day(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


# Club-name noise that bookmakers add inconsistently.
_NOISE_WORDS = {
    "fc", "afc", "cf", "sc", "ac", "as", "ss", "us", "cd", "ud", "rc",
    "club", "the", "if", "bk", "sk", "fk", "kf", "nk", "sv", "vfl", "vfb",
}


def norm_name(name: str) -> str:
    """Normalise a team/selection name so different feeds can be matched."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    for ch in ".-'&,()/":
        text = text.replace(ch, " ")
    words = [w for w in text.split() if w and w not in _NOISE_WORDS]
    return " ".join(words)


def tokens(name: str) -> set[str]:
    return {w for w in norm_name(name).split() if len(w) > 1}


def money(value: float, currency: str = "") -> str:
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def signed_pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:+.{digits}f}%"


def rule(char: str = "-", width: int = 78) -> str:
    return char * width
