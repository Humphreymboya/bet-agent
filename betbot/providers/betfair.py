"""Betfair Exchange access: sharp prices for reference, plus real order placement.

Two uses:

1. `BetfairProvider` - pulls MATCH_ODDS / OVER_UNDER_25 markets so the exchange
   price can be attached to soft-book events as a fair-value anchor.
2. `BetfairClient.place_back` - used by execution.BetfairBroker for live money.

Credentials (put in .env):
    BETFAIR_APP_KEY=...
    BETFAIR_USERNAME=...
    BETFAIR_PASSWORD=...
    BETFAIR_CERT_FILE=C:/certs/client-2048.crt
    BETFAIR_KEY_FILE=C:/certs/client-2048.key
    # or, if you already have a token:
    BETFAIR_SESSION_TOKEN=...

Non-interactive login requires the certificate Betfair issues per account.
Exchanges do not accept 2-leg accumulators as a single order: `place_back`
places one selection at a time, so it is only used when legs=1. For a true
2-leg double, use execution="ticket" and confirm in the bookmaker app.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from ..config import Config
from ..model import Event
from ..util import iso, parse_iso, utcnow
from .base import OddsProvider, ProviderError

BETTING_RPC = "https://api.betfair.com/exchange/betting/json-rpc/v1"
CERT_LOGIN = "https://identitysso-cert.betfair.com/api/certlogin"

# Betfair market type -> our internal market key
MARKET_TYPES = {"MATCH_ODDS": "h2h", "OVER_UNDER_25": "totals"}


class BetfairClient:
    def __init__(
        self,
        app_key: str | None = None,
        session_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        cert_file: str | None = None,
        key_file: str | None = None,
    ):
        self.app_key = app_key or os.environ.get("BETFAIR_APP_KEY", "")
        self.session_token = session_token or os.environ.get("BETFAIR_SESSION_TOKEN", "")
        self.username = username or os.environ.get("BETFAIR_USERNAME", "")
        self.password = password or os.environ.get("BETFAIR_PASSWORD", "")
        self.cert_file = cert_file or os.environ.get("BETFAIR_CERT_FILE", "")
        self.key_file = key_file or os.environ.get("BETFAIR_KEY_FILE", "")
        self._request_id = 0
        self._runner_names: dict[str, dict[str, str]] = {}

    # -- auth ----------------------------------------------------------
    def login(self) -> str:
        if self.session_token:
            return self.session_token
        if not (self.app_key and self.username and self.password and self.cert_file):
            raise ProviderError(
                "Betfair credentials missing: need BETFAIR_APP_KEY, BETFAIR_USERNAME, "
                "BETFAIR_PASSWORD and BETFAIR_CERT_FILE (or BETFAIR_SESSION_TOKEN)."
            )
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file or None)
        data = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode()
        request = urllib.request.Request(
            CERT_LOGIN,
            data=data,
            headers={
                "X-Application": self.app_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=25, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(f"Betfair login failed: {exc}") from exc
        if payload.get("loginStatus") != "SUCCESS":
            raise ProviderError(f"Betfair login rejected: {payload.get('loginStatus')}")
        self.session_token = payload["sessionToken"]
        return self.session_token

    # -- rpc -----------------------------------------------------------
    def rpc(self, method: str, params: dict, timeout: int = 25) -> object:
        token = self.login()
        self._request_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{method}", "params": params,
             "id": self._request_id}
        ).encode()
        request = urllib.request.Request(
            BETTING_RPC,
            data=body,
            headers={
                "X-Application": self.app_key,
                "X-Authentication": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ProviderError(f"Betfair HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(f"Betfair RPC failed: {exc}") from exc
        if "error" in payload:
            error = payload["error"]
            raise ProviderError(
                f"Betfair {method} error {error.get('code')}: "
                f"{error.get('message')} / {error.get('data', {}).get('APINGException', {})}"
            )
        return payload.get("result")

    # -- data ----------------------------------------------------------
    def list_markets(self, hours_ahead: int = 72, max_results: int = 200) -> list[dict]:
        now = utcnow()
        params = {
            "filter": {
                "eventTypeIds": ["1"],  # soccer
                "marketTypeCodes": sorted(MARKET_TYPES),
                "marketStartTime": {
                    "from": iso(now - timedelta(minutes=30)),
                    "to": iso(now + timedelta(hours=hours_ahead)),
                },
            },
            "marketProjection": [
                "EVENT", "RUNNER_DESCRIPTION", "COMPETITION", "MARKET_START_TIME",
            ],
            "sort": "FIRST_TO_START",
            "maxResults": str(max_results),
        }
        return self.rpc("listMarketCatalogue", params) or []

    def list_prices(self, market_ids: list[str]) -> dict[str, dict[str, float]]:
        """marketId -> {runner_name: best back price}."""
        if not market_ids:
            return {}
        out: dict[str, dict[str, float]] = {}
        for chunk in _chunks(market_ids, 25):
            result = self.rpc(
                "listMarketBook",
                {"marketIds": chunk, "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}},
            ) or []
            for book in result:
                market_id = book.get("marketId", "")
                names = self._runner_names.get(market_id, {})
                prices: dict[str, float] = {}
                for runner in book.get("runners", []) or []:
                    available = (runner.get("ex") or {}).get("availableToBack") or []
                    if not available:
                        continue
                    best = max(available, key=lambda level: level.get("price", 0))
                    name = names.get(str(runner.get("selectionId")), "")
                    if name and best.get("price"):
                        prices[name] = float(best["price"])
                if prices:
                    out[market_id] = prices
        return out

    # -- orders --------------------------------------------------------
    def place_back(
        self, market_id: str, selection_id: str, price: float, size: float,
        persistence: str = "LAPSE",
    ) -> dict:
        params = {
            "marketId": market_id,
            "instructions": [
                {
                    "selectionId": str(selection_id),
                    "side": "BACK",
                    "orderType": "LIMIT",
                    "limitOrder": {
                        "size": round(float(size), 2),
                        "price": round(float(price), 2),
                        "persistenceType": persistence,
                    },
                }
            ],
        }
        result = self.rpc("placeOrders", params) or {}
        report = (result.get("instructionReports") or [{}])[0]
        if result.get("status") != "SUCCESS":
            raise ProviderError(
                f"Betfair order rejected: {result.get('status')} "
                f"{report.get('errorCode', '')}"
            )
        return {
            "bet_id": report.get("betId", ""),
            "status": report.get("status", ""),
            "size": report.get("sizeMatched", 0.0),
            "price": price,
            "market_id": market_id,
        }

    def cleared_orders(self, days_back: int = 3) -> list[dict]:
        now = utcnow()
        params = {
            "betStatus": "SETTLED",
            "settledDateRange": {
                "from": iso(now - timedelta(days=days_back)),
                "to": iso(now),
            },
            "groupBy": "BET",
            "includeItemDescription": True,
        }
        result = self.rpc("listClearedOrders", params) or {}
        return result.get("clearedOrders", []) or []

    def _load_runner_names(self, catalogue: list[dict]) -> None:
        self._runner_names = {
            market.get("marketId", ""): {
                str(runner.get("selectionId")): runner.get("runnerName", "")
                for runner in market.get("runners", []) or []
            }
            for market in catalogue
        }


class BetfairProvider(OddsProvider):
    name = "betfair"

    def __init__(self, client: BetfairClient | None = None):
        self.client = client or BetfairClient()
        self._runner_names: dict[str, dict[str, str]] = {}

    def fetch_events(self, cfg: Config) -> list[Event]:
        catalogue = self.client.list_markets()
        self.client._load_runner_names(catalogue)
        self._runner_names = self.client._runner_names
        prices = self.client.list_prices([m.get("marketId", "") for m in catalogue])

        events: dict[str, Event] = {}
        for market in catalogue:
            market_id = market.get("marketId", "")
            if market_id not in prices:
                continue
            market_key = MARKET_TYPES.get(market.get("marketType", ""), "")
            if not market_key:
                continue
            event_name = (market.get("event") or {}).get("name", "")
            home, away = _split_event_name(event_name)
            event = events.get(event_name)
            if event is None:
                event = Event(
                    event_id=market_id,
                    sport="soccer",
                    league=(market.get("competition") or {}).get("name", ""),
                    home=home,
                    away=away,
                    commence_time=parse_iso(market.get("marketStartTime", "")),
                )
                events[event_name] = event
            mapped: dict[str, float] = {}
            for runner_name, price in prices[market_id].items():
                label = _map_runner(runner_name)
                if not label:
                    continue
                mapped[label] = price
                selection_id = _selection_id(self.client._runner_names, market_id, runner_name)
                if selection_id:
                    event.runner_ids.setdefault(market_key, {})[label] = selection_id
            if mapped:
                event.quotes.setdefault(market_key, {})["betfair"] = mapped
        return sorted(events.values(), key=lambda e: e.commence_time)


# ----------------------------------------------------------------------
def _split_event_name(name: str) -> tuple[str, str]:
    for sep in (" v ", " vs ", " v. "):
        if sep in name:
            home, away = name.split(sep, 1)
            return home.strip(), away.strip()
    return name.strip(), ""


def _map_runner(runner_name: str) -> str:
    text = runner_name.strip()
    if text.lower() in {"the draw", "draw"}:
        return "Draw"
    if text.lower().startswith("over"):
        return "Over 2.5"
    if text.lower().startswith("under"):
        return "Under 2.5"
    return text


def _selection_id(names: dict[str, dict[str, str]], market_id: str, runner_name: str) -> str:
    for selection_id, name in names.get(market_id, {}).items():
        if name == runner_name:
            return str(selection_id)
    return ""


def _chunks(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]
