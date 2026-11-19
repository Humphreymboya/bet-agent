"""Execution adapters. Paper by default; real money requires an explicit gate."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .config import Config
from .model import Combo
from .util import iso_day, utcnow


@dataclass
class Placement:
    ref: str
    status: str       # accepted | pending | pending_user | rejected
    detail: str = ""
    stake: float | None = None   # set when the venue filled a different size


class PaperBroker:
    """Simulates a fill at the quoted price. Never touches an account."""

    name = "paper"

    def place(self, combo: Combo, stake: float, cfg: Config) -> Placement:
        ref = f"PAPER-{iso_day(utcnow())}-{secrets.token_hex(3).upper()}"
        return Placement(ref=ref, status="accepted", detail="simulated fill at quoted price")


class TicketBroker:
    """Writes a copy-paste betting slip and waits for the human to confirm."""

    name = "ticket"

    def place(self, combo: Combo, stake: float, cfg: Config) -> Placement:
        cfg.report_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.report_dir / f"ticket-{iso_day(utcnow())}.txt"
        lines = [
            f"BET SLIP  {iso_day(utcnow())}  (provider: {cfg.provider})",
            "",
            f"Stake:        {stake:.2f} {cfg.currency}",
            f"Combined odds: {combo.odds:.2f}",
            f"Potential return: {stake * combo.odds:.2f} {cfg.currency}",
            "",
            "Legs:",
        ]
        for index, leg in enumerate(combo.legs, start=1):
            lines.append(
                f"  {index}. {leg.league} | {leg.match} | {leg.selection}"
                f" @ {leg.price:.2f} ({leg.book})"
                f"  [model {leg.fair_prob * 100:.1f}%, edge {leg.edge * 100:+.1f}%]"
            )
        lines += [
            "",
            f"Model joint probability: {combo.prob * 100:.1f}%",
            f"Expected value: {combo.ev * 100:+.1f}%  ({combo.ev * stake:+.2f} {cfg.currency})",
            "",
            "Confirm this slip in the bookmaker app, then run:",
            "  py main.py settle --bet-id <ID> --result win|loss",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Placement(ref=str(path.name), status="pending_user", detail=str(path))


class BetfairBroker:
    """Live BACK order on the exchange. Single leg only - see module notes."""

    name = "betfair"

    def __init__(self, client=None):
        if client is None:
            from .providers.betfair import BetfairClient

            client = BetfairClient()
        self.client = client

    def place(self, combo: Combo, stake: float, cfg: Config) -> Placement:
        if combo.n_legs != 1:
            raise ValueError(
                "Betfair placeOrders cannot submit a multi-leg double as one order. "
                "Use execution=\"ticket\" for accumulators, or set legs=1."
            )
        leg = combo.legs[0]
        if not leg.runner_id:
            raise ValueError("missing Betfair selection id for this leg")
        result = self.client.place_back(
            market_id=leg.event_id,
            selection_id=leg.runner_id,
            price=leg.price,
            size=stake,
        )
        matched = float(result.get("size", 0) or 0)
        status = "accepted" if matched > 0 else "rejected"
        return Placement(
            ref=str(result.get("bet_id", "")),
            status=status,
            detail=f"matched {matched:.2f} @ {leg.price:.2f}",
        )


class Ps3838Broker:
    """Live money on a PS3838/Pinnacle account, including 2-leg doubles.

    Unlike the Betfair exchange, Pinnacle's API can submit a parlay as a single
    order, which is exactly what a "2 odds" bet needs.
    """

    name = "ps3838"

    def __init__(self, client=None):
        if client is None:
            from .providers.ps3838 import Ps3838Client

            client = Ps3838Client()
        self.client = client

    def place(self, combo: Combo, stake: float, cfg: Config) -> Placement:
        from .providers.ps3838 import (
            parlay_line_legs,
            parlay_max_stake,
            parlay_odds_from_line,
            parlay_request,
            straight_request,
            summarize_response,
        )

        if combo.n_legs == 1:
            leg = combo.legs[0]
            venue_price = float((leg.venue or {}).get("price") or 0.0)
            if venue_price and venue_price < leg.price * 0.98:
                return Placement(
                    ref="", status="rejected",
                    detail=(f"venue prices this at {venue_price:.3f} but the scanned "
                            f"price was {leg.price:.3f}; nothing placed"),
                )
            request = straight_request(leg, stake)
            response = self.client.place_straight(request)
            ref = response.get("betId") or request["uniqueRequestId"]
        else:
            # confirm the venue still prices the parlay at what we expect
            line = self.client.parlay_line(parlay_line_legs(combo.legs))
            actual = parlay_odds_from_line(line)
            floor = combo.odds * 0.97
            if actual is None:
                return Placement(
                    ref="", status="rejected",
                    detail="venue did not return valid parlay odds; nothing placed",
                )
            if actual < floor:
                return Placement(
                    ref="", status="rejected",
                    detail=f"venue parlay price {actual:.3f} below floor {floor:.3f}; nothing placed",
                )
            venue_max = parlay_max_stake(line)
            filled = stake
            if venue_max is not None and stake > venue_max:
                filled = round(venue_max, 2)
                if filled < cfg.min_stake:
                    return Placement(
                        ref="", status="rejected",
                        detail=f"venue parlay max {venue_max:.2f} is below min stake; nothing placed",
                    )
            request = parlay_request(combo.legs, filled)
            response = self.client.place_parlay(request)
            ref = response.get("betId") or request["uniqueRequestId"]
            status, detail = summarize_response(response)
            if status == "pending" and request.get("uniqueRequestId"):
                status, detail = self._poll(request["uniqueRequestId"])
            return Placement(
                ref=str(ref), status=status, detail=detail,
                stake=filled if filled != stake else None,
            )

        status, detail = summarize_response(response)
        if status == "pending" and request.get("uniqueRequestId"):
            status, detail = self._poll(request["uniqueRequestId"])
        return Placement(ref=str(ref), status=status, detail=detail)

    def _poll(self, unique_request_id: str, attempts: int = 4, pause: float = 2.5) -> tuple[str, str]:
        import time

        from .providers.ps3838 import summarize_response

        for _ in range(attempts):
            time.sleep(pause)
            try:
                payload = self.client.bet_by_request_id(unique_request_id)
            except Exception:  # noqa: BLE001 - network hiccup mid-poll
                continue
            bets = payload.get("bets", payload.get("data", [])) or []
            if isinstance(bets, list) and bets:
                return summarize_response(bets[0])
        return "pending", "still pending after polling; check the account manually"


class BetpawaBroker:
    """Live money on betPawa via the website, driven by a logged-in browser.

    betPawa publishes no betting API, so this is a Playwright session against
    the real site. Two protections are enforced inside the adapter: prices are
    read back from the betslip before staking, and `betpawa_dry_run` (default
    true) fills the slip without submitting it.
    """

    name = "betpawa"

    def __init__(self, browser=None):
        self._browser = browser

    def place(self, combo: Combo, stake: float, cfg: Config) -> Placement:
        from .betpawa import BetpawaBrowser
        from .ledger import Ledger

        # the browser adapter needs the live bankroll, not the configured one
        ledger = Ledger(cfg.db_file)
        realised = ledger.realised_pnl(cfg.mode)
        open_stake = sum(float(b.stake) for b in ledger.open_bets(cfg.mode))
        bankroll = max(0.0, cfg.bankroll + realised - open_stake)
        staked_today = ledger.staked_today(cfg.mode)

        if self._browser is not None:
            return self._browser.place(combo, stake, bankroll, staked_today)

        with BetpawaBrowser(cfg) as browser:
            return browser.place(combo, stake, bankroll, staked_today)


def build_broker(cfg: Config):
    if cfg.execution == "paper":
        return PaperBroker()
    if cfg.execution == "ticket":
        return TicketBroker()
    if cfg.execution == "betfair":
        return BetfairBroker()
    if cfg.execution == "ps3838":
        return Ps3838Broker()
    if cfg.execution == "betpawa":
        return BetpawaBroker()
    raise ValueError(f"unknown execution adapter: {cfg.execution}")
