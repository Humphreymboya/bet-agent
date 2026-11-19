"""PS3838 / Pinnacle API: sharp prices plus real order placement.

Why this file exists: Tanzania-licensed books (GSB, Betway TZ, SportPesa, 22Bet,
1xBet, BetWinner, Melbet, Mostbet, SportyBet, betPawa, Premier Bet, M-Bet) have
no public betting API, and Betfair does not accept Tanzanian accounts. The only
route that accepts Tanzania *and* exposes a real betting API is a Pinnacle
white-label account (PS3838) obtained through a broker.

Facts this client is built against (verify before trusting, things change):

* Base URL: https://api.ps3838.com for broker-mediated accounts,
  https://api.pinnacle.com for direct B2B. Identical endpoints.
* Auth: HTTP Basic. Account must be funded.
* Public self-service API access closed 23 July 2025. Access is now by
  application (api@pinnacle.com) or via a broker that issues credentials.
* Odds endpoints are rate limited to 1 request / 2 min / endpoint / sportId.
  A once-daily run with a single odds call sits comfortably inside that.
* Bets: POST /v4/bets/straight, POST /v4/bets/parlay. Parlay supports
  2+ legs, which is what a "2 odds" double needs - unlike the Betfair exchange,
  which cannot submit an accumulator as one order.

Env:
    PS3838_USERNAME=...
    PS3838_PASSWORD=...
    PS3838_API_URL=https://api.ps3838.com   (optional)
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from ..config import Config
from ..model import Combo, Event
from ..util import parse_iso, utcnow
from .base import OddsProvider, ProviderError

SOCCER_SPORT_ID = 29
DEFAULT_BASE = "https://api.ps3838.com"


class Ps3838Client:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        base_url: str | None = None,
        timeout: int = 25,
    ):
        self.username = username or os.environ.get("PS3838_USERNAME", "")
        self.password = password or os.environ.get("PS3838_PASSWORD", "")
        self.base_url = (base_url or os.environ.get("PS3838_API_URL") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        if not (self.username and self.password):
            raise ProviderError(
                "PS3838 credentials missing: set PS3838_USERNAME and PS3838_PASSWORD in .env. "
                "Direct Pinnacle API access is application-only since July 2025 - "
                "otherwise get credentials from a broker that provisions PS3838."
            )

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "bet-agent/0.1 (+daily pre-match value scan)",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = {
                401: "credentials rejected - check PS3838_USERNAME/PASSWORD and that the account is funded",
                403: "forbidden - this account is not authorised for the API",
                429: "rate limit: odds calls are capped at 1 per 2 minutes per sport. Wait and retry.",
            }.get(exc.code, "")
            raise ProviderError(f"PS3838 HTTP {exc.code} {path}: {hint} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"PS3838 unreachable: {exc.reason}") from exc

        if isinstance(payload, dict) and payload.get("code") and payload.get("message"):
            raise ProviderError(f"PS3838 {path}: {payload['code']} - {payload['message']}")
        return payload if isinstance(payload, dict) else {"data": payload}

    # -- lines ---------------------------------------------------------
    def odds(self, sport_id: int = SOCCER_SPORT_ID) -> dict:
        return self._request("GET", "/v2/odds", {"sportId": sport_id, "oddsFormat": "Decimal"})

    def leagues(self, sport_id: int = SOCCER_SPORT_ID) -> dict:
        return self._request("GET", "/v2/leagues", {"sportId": sport_id})

    def parlay_line(self, legs: list[dict]) -> dict:
        return self._request(
            "POST",
            "/v2/line/parlay",
            body={"legs": legs, "oddsFormat": "Decimal"},
        )

    # -- bets ----------------------------------------------------------
    def place_straight(self, request: dict) -> dict:
        return self._request("POST", "/v4/bets/straight", body=request)

    def place_parlay(self, request: dict) -> dict:
        return self._request("POST", "/v4/bets/parlay", body=request)

    def bet_by_request_id(self, unique_request_id: str) -> dict:
        return self._request("GET", "/v4/bets", {"uniqueRequestIds": unique_request_id})


class Ps3838Provider(OddsProvider):
    name = "ps3838"

    def __init__(self, client: Ps3838Client | None = None, cache: bool = True):
        self.client = client or Ps3838Client()
        self.cache = cache

    def fetch_events(self, cfg: Config) -> list[Event]:
        events: list[Event] = []
        for sport_id in cfg.ps3838_sport_ids:
            payload = self.client.odds(sport_id)
            if self.cache:
                _cache(cfg, sport_id, payload)
            leagues = _league_names(self.client, sport_id)
            events.extend(_events_from_odds(payload, leagues))
        return sorted(events, key=lambda e: e.commence_time)


# ----------------------------------------------------------------------
# pure helpers (unit tested, no network)
# ----------------------------------------------------------------------
def _events_from_odds(payload: dict, leagues: dict[int, str] | None = None) -> list[Event]:
    leagues = leagues or {}
    events: list[Event] = []

    for league in payload.get("leagues", []) or []:
        league_id = league.get("id")
        league_name = league.get("name") or leagues.get(league_id, f"League {league_id}")
        for item in league.get("events", []) or []:
            event = _event_from_odds(item, league_name)
            if event is not None:
                events.append(event)
    return events


def _event_from_odds(item: dict, league_name: str) -> Event | None:
    try:
        event_id = str(item["id"])
        starts = parse_iso(item["starts"])
        home = item.get("home", "")
        away = item.get("away", "")
    except (KeyError, ValueError):
        return None

    period = _full_match_period(item)
    if period is None:
        return None
    moneyline = period.get("moneyline") or {}
    home_price = _price(moneyline.get("home"))
    away_price = _price(moneyline.get("away"))
    draw_price = _price(moneyline.get("draw"))
    if not (home_price and away_price):
        return None

    event = Event(
        event_id=event_id,
        sport="soccer",
        league=league_name,
        home=home,
        away=away,
        commence_time=starts,
    )

    prices = {home: home_price, away: away_price}
    if draw_price:
        prices["Draw"] = draw_price
    event.quotes["h2h"] = {"pinnacle": prices}

    # placement payload per selection, taken straight from the odds response
    line_id = period.get("lineId")
    venue: dict[str, dict] = {}
    for label, team in ((home, "TEAM1"), (away, "TEAM2"), ("Draw", "DRAW")):
        if label in prices:
            venue[label] = {
                "sport_id": _sport_id(item, period),
                "event_id": int(item["id"]),
                "period_number": int(period.get("number", 0)),
                "line_id": line_id,
                "bet_type": "MONEYLINE",
                "team": team,
                "price": prices[label],
            }
    event.venue["h2h"] = venue
    return event


def _full_match_period(item: dict) -> dict | None:
    for period in item.get("periods", []) or []:
        if int(period.get("number", -1)) == 0:
            if int(period.get("status", 1)) != 1:
                return None
            return period
    return None


def _price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 1.0 else None


def _sport_id(item: dict, period: dict) -> int:
    for source in (period, item):
        try:
            return int(source["sportId"])
        except (KeyError, TypeError, ValueError):
            continue
    return SOCCER_SPORT_ID


def _league_names(client: Ps3838Client, sport_id: int) -> dict[int, str]:
    try:
        payload = client.leagues(sport_id)
    except ProviderError:
        return {}
    rows = payload.get("leagues", payload.get("data", [])) or []
    names: dict[int, str] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id") is not None:
            names[int(row["id"])] = row.get("name", "")
    return names


def _cache(cfg: Config, sport_id: int, payload: dict) -> None:
    raw_dir = cfg.resolve("data/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{utcnow():%Y%m%dT%H%M%SZ}-ps3838-{sport_id}.json"
    try:
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------
# request builders (used by execution.Ps3838Broker)
# ----------------------------------------------------------------------
def straight_request(leg, stake: float, accept_better_line: bool = True) -> dict:
    venue = _venue(leg)
    return {
        "uniqueRequestId": str(uuid.uuid4()),
        "acceptBetterLine": accept_better_line,
        "oddsFormat": "Decimal",
        "stake": round(float(stake), 2),
        "winRiskStake": "RISK",
        "lineId": venue["line_id"],
        "altLineId": None,
        "fillType": "NORMAL",
        "sportId": venue["sport_id"],
        "eventId": venue["event_id"],
        "periodNumber": venue["period_number"],
        "betType": venue["bet_type"],
        "team": venue["team"],
        "side": None,
    }


def parlay_request(legs, stake: float, accept_better_line: bool = True) -> dict:
    return {
        "uniqueRequestId": str(uuid.uuid4()),
        "acceptBetterLine": accept_better_line,
        "riskAmount": round(float(stake), 2),
        "oddsFormat": "Decimal",
        "legs": [parlay_leg(leg) for leg in legs],
        "roundRobinOptions": ["Parlay"],
    }


def parlay_leg(leg) -> dict:
    venue = _venue(leg)
    return {
        "uniqueLegId": str(uuid.uuid4()),
        "lineId": venue["line_id"],
        "altLineId": None,
        "sportId": venue["sport_id"],
        "eventId": venue["event_id"],
        "periodNumber": venue["period_number"],
        "legBetType": venue["bet_type"],
        "team": venue["team"],
        "side": None,
    }


def parlay_line_legs(legs) -> list[dict]:
    """Legs for /v2/line/parlay.

    This endpoint uses different field names to the bet endpoint: no lineId or
    sportId, and team values are spelled Team1/Team2/Draw.
    """
    out: list[dict] = []
    for leg in legs:
        venue = _venue(leg)
        out.append(
            {
                "uniqueLegId": str(uuid.uuid4()),
                "eventId": venue["event_id"],
                "periodNumber": venue["period_number"],
                "legBetType": venue["bet_type"],
                "team": _line_team(venue["team"]),
            }
        )
    return out


def _line_team(team: str) -> str:
    return {"TEAM1": "Team1", "TEAM2": "Team2", "DRAW": "Draw"}.get(team, team)


def _venue(leg) -> dict:
    venue = getattr(leg, "venue", None)
    if not venue:
        raise ValueError(
            f"no PS3838 placement data for {getattr(leg, 'match', '?')} / "
            f"{getattr(leg, 'selection', '?')} - the price did not come from the PS3838 feed"
        )
    return venue


def parlay_odds_from_line(payload: dict) -> float | None:
    """Pull the real combined price out of a /v2/line/parlay response."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("status", "")).upper() == "PROCESSED_WITH_ERROR":
        return None
    options = payload.get("roundRobinOptionWithOdds") or payload.get("roundRobinOptions") or []
    for option in options:
        if isinstance(option, dict) and str(option.get("roundRobinOption", "")).lower() == "parlay":
            try:
                return float(option.get("odds"))
            except (TypeError, ValueError):
                return None
    try:
        return float(payload["odds"])
    except (KeyError, TypeError, ValueError):
        return None


def parlay_max_stake(payload: dict) -> float | None:
    """Venue ceiling for a parlay, if it told us one."""
    try:
        return float(payload["maxParlayRiskStake"])
    except (KeyError, TypeError, ValueError):
        return None


def summarize_response(response: dict) -> tuple[str, str]:
    """Map a place-bet response to (status, detail)."""
    status = str(response.get("status", "")).upper()
    if status == "ACCEPTED":
        bet_id = response.get("betId", "")
        price = response.get("price")
        price_note = f" @ {float(price) / 100:.3f}" if price and float(price) > 10 else ""
        return "accepted", f"betId {bet_id}{price_note}"
    if status == "PENDING_ACCEPTANCE":
        return "pending", "live delay: poll bet status with the same uniqueRequestId"
    if status == "PROCESSED_WITH_ERROR":
        return "rejected", str(response.get("errorCode", "unknown error"))
    return "rejected", f"unexpected status {status or 'empty'}"


def stake_currency_note(cfg: Config) -> str:
    return f"amounts are in the account currency ({cfg.currency} by config)"


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
