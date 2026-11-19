"""betPawa price provider.

betPawa has no documented betting API, but its public site reads prices from an
undocumented JSON endpoint:

    GET /api/sportsbook/v4/events/lists/by-queries?q=<urlencoded json>

Two non-obvious details, both discovered from the live site:

* the request needs the `x-pawa-brand: betpawa-tanzania` header, and
* the default `Accept` is protobuf; asking for `application/json` gets plain
  JSON with the same data.

Markets are numeric IDs. `3743` is `1X2 | Full Time`, whose three outcomes are
named `1`, `X` and `2`. They are mapped to the home team name, `Draw` and the
away team name so the rest of the agent (and the reference merger) sees the
same h2h shape as The Odds API. The original outcome code and price ID travel
in `Event.venue` so the browser adapter can click the exact selection later.

This feed is public and needs no login. It is a price source only; placing is
still browser automation via `betbot/betpawa.py`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from ..config import Config
from ..model import Event
from ..util import parse_iso, utcnow
from .base import OddsProvider, ProviderError

DEFAULT_BASE_URL = "https://www.betpawa.co.tz"
DEFAULT_BRAND = "betpawa-tanzania"
ONE_X_TWO = "3743"          # "1X2 | Full Time"
FOOTBALL_CATEGORY = "2"
LOOKAHEAD_DAYS = 3
MAX_TAKE = 100              # server rejects more than this per query

# Five differently-sorted queries in one request give ~290 unique fixtures
# instead of 100, and therefore a usable pool of near-2.0 doubles. Every extra
# query is an illusion of coverage if it just returns the same games, so these
# orders were chosen to be meaningfully different.
SORTS = (
    {"popularity": "DESC"},
    {"popularity": "ASC"},
    {"startTime": "ASC"},
    {"startTime": "DESC"},
    {"competitionPriority": "DESC"},
)


class BetpawaProvider(OddsProvider):
    name = "betpawa"

    def __init__(
        self,
        base_url: str | None = None,
        brand: str | None = None,
        timeout: int = 25,
        cache: bool = True,
    ):
        self.base_url = (base_url or os.environ.get("BETPAWA_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.brand = brand or os.environ.get("BETPAWA_BRAND") or DEFAULT_BRAND
        self.timeout = timeout
        self.cache = cache

    # ------------------------------------------------------------------
    def fetch_events(self, cfg: Config) -> list[Event]:
        payload = self._get_events()
        if self.cache:
            _cache_payload(cfg, payload)
        events = to_events(payload, cfg)
        if not events:
            raise ProviderError(
                "betPawa returned no priced fixtures in the next "
                f"{LOOKAHEAD_DAYS} days (check BETPAWA_BRAND and the feed)"
            )
        return events

    def _get_events(self) -> dict:
        queries = [
            {
                "query": {
                    "eventType": "UPCOMING",
                    "categories": [FOOTBALL_CATEGORY],
                    "hasOdds": True,
                },
                "sort": sort,
                "take": MAX_TAKE,
                "view": {"marketTypes": [ONE_X_TWO]},
            }
            for sort in SORTS
        ]
        q = json.dumps({"queries": queries}, separators=(",", ":"))
        url = (f"{self.base_url}/api/sportsbook/v4/events/lists/by-queries"
               f"?q={urllib.parse.quote(q)}")
        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "x-pawa-brand": self.brand,
            "x-pawa-language": "en",
            "devicetype": "web",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            hint = {
                400: "check BETPAWA_BRAND and the endpoint query shape",
                403: "betPawa blocked this request; retry later (do not hammer it)",
            }.get(exc.code, "")
            raise ProviderError(
                f"betPawa HTTP {exc.code}: {hint} {body}".strip()
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"betPawa unreachable: {exc.reason}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(f"betPawa returned unreadable JSON: {exc}") from exc


# ----------------------------------------------------------------------
def to_events(payload: dict, cfg: Config) -> list[Event]:
    """Turn the site's grouped response into de-duplicated model events."""
    now = utcnow()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    end = now + timedelta(days=LOOKAHEAD_DAYS)

    events: dict[str, Event] = {}
    for group in payload.get("responses", []) or []:
        for item in group.get("responses", []) or []:
            event = _to_event(item, start, end)
            if event is not None:
                events[event.event_id] = event
    return sorted(events.values(), key=lambda e: e.commence_time)


def _to_event(item: dict, start, end) -> Event | None:
    try:
        kickoff = parse_iso(item.get("startTime", ""))
    except ValueError:
        return None
    if not start <= kickoff <= end:
        return None

    home, away = _participants(item.get("participants", []) or [])
    if not home or not away or home == away:
        return None

    odds = _one_x_two(item.get("markets", []) or [])
    if odds is None:
        return None
    code_1, code_x, code_2 = odds

    labels = {"1": home, "X": "Draw", "2": away}
    prices: dict[str, float] = {}
    venues: dict[str, dict] = {}
    for code, price in (("1", code_1), ("X", code_x), ("2", code_2)):
        if price is None:
            return None
        try:
            value = float(price.get("odds"))
        except (TypeError, ValueError):
            return None
        if value <= 1.0 or price.get("suspended"):
            return None
        label = labels[code]
        prices[label] = value
        venues[label] = {
            "outcome": code,
            "price_id": str(price.get("id", "")),
            "type_id": str(price.get("typeId", "")),
            "event_id": str(item.get("id", "")),
            "market_type_id": ONE_X_TWO,
        }

    competition = item.get("competition") or {}
    event = Event(
        event_id=str(item.get("id", "")),
        sport="soccer",
        league=competition.get("name") or "betPawa",
        home=home,
        away=away,
        commence_time=kickoff,
    )
    event.quotes["h2h"] = {"betpawa": prices}
    event.venue["h2h"] = venues
    return event


def _participants(rows: list[dict]) -> tuple[str, str]:
    by_position: dict[int, str] = {}
    for row in rows:
        try:
            position = int(row.get("position"))
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            by_position[position] = name
    return by_position.get(1, ""), by_position.get(2, "")


def _one_x_two(markets: list[dict]) -> tuple[dict, dict, dict] | None:
    """Return the three 1X2 outcome objects, or None if not priced."""
    for market in markets:
        market_type = market.get("marketType") or {}
        if str(market_type.get("id")) != ONE_X_TWO:
            continue
        rows = market.get("row") or []
        if not rows:
            continue
        prices = rows[0].get("prices") or []
        by_code = {str(p.get("name")): p for p in prices}
        if all(code in by_code for code in ("1", "X", "2")):
            return by_code["1"], by_code["X"], by_code["2"]
    return None


def _cache_payload(cfg: Config, payload: dict) -> None:
    raw_dir = cfg.resolve("data/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{stamp}-betpawa.json"
    try:
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        pass
