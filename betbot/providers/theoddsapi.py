"""The Odds API provider (https://the-odds-api.com).

Free tier is enough to test: a few hundred requests per month, which a daily
run of ~6 leagues does not get near. Set the key in .env:

    THE_ODDS_API_KEY=xxxxxxxx

Notes:
* `regions=eu,uk` returns many books per event, including Pinnacle in `eu`.
  Multiple books are essential - a single book can never tell you if its own
  price is good.
* Every response is cached to data/raw/ so a run can be audited later.
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
from ..util import iso, parse_iso, utcnow
from .base import OddsProvider, ProviderError

BASE = "https://api.the-odds-api.com/v4"
DEFAULT_TIMEOUT = 25

# last x-requests-remaining header seen, so the CLI can show the monthly budget
LAST_QUOTA: str | None = None


class TheOddsApiProvider(OddsProvider):
    name = "theoddsapi"

    def __init__(self, api_key: str | None = None, cache: bool = True):
        self.api_key = api_key or os.environ.get("THE_ODDS_API_KEY", "")
        self.cache = cache
        self.quota_remaining: str | None = None
        if not self.api_key:
            raise ProviderError(
                "THE_ODDS_API_KEY is not set. Put it in bet-agent/.env "
                "(get a free key at the-odds-api.com) or run with provider=demo."
            )

    # ------------------------------------------------------------------
    def fetch_events(self, cfg: Config) -> list[Event]:
        events: dict[str, Event] = {}
        windows = _sport_windows(cfg)
        for sport, (from_iso, to_iso) in windows.items():
            payload = self._get(
                f"/sports/{sport}/odds",
                {
                    "regions": cfg.regions,
                    "markets": ",".join(cfg.markets),
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                    "commenceTimeFrom": from_iso,
                    "commenceTimeTo": to_iso,
                },
            )
            if self.cache:
                _cache_payload(cfg, sport, payload)
            for item in payload:
                event = _to_event(item)
                if event is not None:
                    events[event.event_id] = event
        return sorted(events.values(), key=lambda e: e.commence_time)

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> list:
        global LAST_QUOTA
        query = dict(params)
        query["apiKey"] = self.api_key
        url = f"{BASE}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                self.quota_remaining = response.headers.get("x-requests-remaining")
                LAST_QUOTA = self.quota_remaining
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            hint = {
                401: "API key rejected - check THE_ODDS_API_KEY",
                422: "bad request params - check sports/markets/regions in config.json",
                429: "monthly quota exhausted",
            }.get(exc.code, "")
            raise ProviderError(f"The Odds API HTTP {exc.code}: {hint} {body}".strip()) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"The Odds API unreachable: {exc.reason}") from exc

    def discover_soccer(self, only_active: bool = True) -> list[dict]:
        payload = self._get("/sports", {"all": "true"})
        rows = []
        for item in payload:
            if item.get("group") != "Soccer":
                continue
            if item.get("has_outrights"):
                continue
            if only_active and not item.get("active"):
                continue
            rows.append(item)
        return sorted(rows, key=lambda r: r.get("title", ""))


# ----------------------------------------------------------------------
def _sport_windows(cfg: Config) -> dict[str, tuple[str, str]]:
    """Ask each league for the next 3 days of fixtures only."""
    now = utcnow()
    start = (now - timedelta(minutes=30)).replace(microsecond=0)
    end = now + timedelta(days=3)
    return {sport: (iso(start), iso(end)) for sport in cfg.sports}


def _to_event(item: dict) -> Event | None:
    try:
        commence = parse_iso(item["commence_time"])
    except (KeyError, ValueError):
        return None

    event = Event(
        event_id=str(item.get("id", "")),
        sport=item.get("sport_key", ""),
        league=item.get("sport_title") or item.get("sport_key", ""),
        home=item.get("home_team", ""),
        away=item.get("away_team", ""),
        commence_time=commence,
    )

    for bookmaker in item.get("bookmakers", []) or []:
        book = bookmaker.get("key") or bookmaker.get("title", "")
        if not book:
            continue
        for market in bookmaker.get("markets", []) or []:
            key = market.get("key", "")
            prices: dict[str, float] = {}
            for outcome in market.get("outcomes", []) or []:
                label = outcome.get("name", "")
                if key == "totals" and outcome.get("point") is not None:
                    label = f"{label} {outcome['point']}"
                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                if price > 1.0:
                    prices[label] = price
            if len(prices) >= 2:
                event.quotes.setdefault(key, {})[book] = prices

    return event if event.quotes else None


def _cache_payload(cfg: Config, sport: str, payload: list) -> None:
    raw_dir = cfg.resolve("data/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{stamp}-{sport}.json"
    try:
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        pass
