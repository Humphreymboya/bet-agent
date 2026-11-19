"""betPawa execution adapter (browser automation).

betPawa has no betting API, so the only way to place a bet programmatically is
to drive their website in a real browser, logged in as the account holder.

What this module does:

1. `login_interactive()` - opens a visible browser so you can log in yourself
   (betPawa sends an SMS code). The session cookies are then saved and reused.
2. `probe()` - dumps the page HTML and a screenshot so selectors can be tuned
   against the live DOM instead of guesswork.
3. `place()` - adds each leg to the betslip, reads the prices betPawa actually
   offers, applies the slippage policy, fills the stake and confirms.

Safety design:

* `betpawa_dry_run` defaults to true: the browser fills the slip and stops,
  screenshots it, and reports back. Nothing is submitted until you flip it.
* Prices are read back from the betslip, never assumed. If a leg is worse than
  the scanned price by more than `betpawa_max_slippage` the bet is abandoned,
  and if the venue's combined price no longer clears `min_edge` it is abandoned
  too - a worse price is a different bet, not the same bet.
* Stake is re-sized at the real prices with the same Kelly settings, and the
  smaller of "planned" and "re-sized" wins.

Terms of service: betPawa prohibits automated access. An account can be limited
or closed for it. Nothing in this file removes that risk - it is your call, and
`execution: "ticket"` is the version that carries none of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from math import prod
from pathlib import Path

from .config import Config
from .model import Combo
from .staking import plan_stake
from .util import utcnow

PLAYWRIGHT_HINT = (
    "Playwright is not installed. Run:\n"
    "  .venv\\Scripts\\pip install playwright\n"
    "  .venv\\Scripts\\playwright install chromium"
)

# Heuristics, not guarantees. betPawa re-skins often; `py main.py probe` exists
# so these can be corrected against the real DOM in one session.
DEFAULT_SELECTORS: dict[str, str] = {
    "search_button": '[data-testid="search-button"], button[aria-label*="Search" i], button:has-text("Search")',
    "search_input": 'input[placeholder*="Search" i], input[type="search"]',
    "event_row": '[data-testid*="event"], a[href*="/event/"]',
    "odds_button": '[data-testid*="odd"], [role="button"][data-testid*="price"], button[class*="odd" i]',
    # {id} / {code} are filled from the venue payload captured by the price feed
    "price_by_id": '[data-price-id="{id}"], [data-testid="price-{id}"]',
    # live DOM uses the feed's market/outcome type ids, e.g. odd-3743-3744
    "odd_by_type": ('[data-test-id="odd-{market}-{type}"], '
                    '[data-test-id="odd-{type}"]'),
    "outcome_button": '[data-outcome="{code}"], [data-selection="{code}"]',
    # keep these precise: a broad div[class*="betslip"] matches the header first
    "betslip": '[class*="Betslip_container"], [data-test-id="auto-betslip-form"]',
    "betslip_row": '[data-test-id="betslip-game"], li[class*="slip" i]',
    # one price node per leg, in slip order; safer than parsing the whole panel
    "slip_price": ('[data-test-id="betslip-change-indicator"], '
                   '[class*="BetDetails_priceIndicator"]'),
    "stake_input": ('[data-test-id="stake-input"], #stake, '
                    'input[inputmode="decimal"], input[name*="stake" i], input[data-testid*="stake"]'),
    "place_button": ('[data-test-id="place-bet-button"], '
                     'button:has-text("Place bet"), button:has-text("Place Bet"), button[data-testid*="place"]'),
    # second-step confirmation only; never re-match the original place button or
    # a retry could submit the same slip twice
    "confirm_button": ('div[role="dialog"] button:has-text("Confirm"), '
                       'div[role="dialog"] button:has-text("Place bet"), '
                       'button:has-text("Confirm bet"), button:has-text("Yes, place")'),
    "success_marker": 'text=/bet placed|bet successfully placed|your bet is on|bet accepted/i',
    "error_marker": 'text=/insufficient|minimum stake|odds have changed|bet not accepted|error/i',
}

ODDS_RE = re.compile(r"\b\d{1,3}\.\d{1,2}\b")

# A leg priced this much better than the scan means the slip was read wrong
# (typically the combined odds line picked up as a leg), not that the book
# improved. Aborting is the only safe interpretation.
MAX_UPSIDE = 0.25

# Lines that carry stakes/totals rather than individual prices. Their numbers
# sit in the same shape as odds, so they are removed before a text fallback.
SLIP_NOISE_RE = re.compile(
    r"(total|combined|accumulat|potential|return|stake|balance|bonus|boost)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# pure logic (unit tested, no browser needed)
# ---------------------------------------------------------------------------
@dataclass
class SlippageDecision:
    ok: bool
    reason: str = ""
    stake: float = 0.0
    odds: float = 0.0
    ev: float = 0.0
    reduced: bool = False


def parse_prices(text: str, expected: int) -> list[float]:
    """Pull odds-like numbers out of betslip text, in display order."""
    found: list[float] = []
    for match in ODDS_RE.findall(text or ""):
        try:
            value = float(match)
        except ValueError:
            continue
        if 1.01 <= value <= 1000.0:
            found.append(value)
    # a slip shows each price once; if the page repeats them, keep the first run
    return found[:expected]


def assess_slippage(
    combo: Combo,
    planned_stake: float,
    actual_prices: list[float],
    cfg: Config,
    bankroll: float,
    already_staked_today: float = 0.0,
) -> SlippageDecision:
    """Decide whether the price betPawa shows is still a bet worth taking."""
    if len(actual_prices) != len(combo.legs):
        return SlippageDecision(
            ok=False,
            reason=f"read {len(actual_prices)} price(s) from the slip, expected {len(combo.legs)}",
        )

    for leg, price in zip(combo.legs, actual_prices):
        if price <= 1.0:
            return SlippageDecision(ok=False, reason=f"unusable price {price} for {leg.selection}")
        floor = leg.price * (1.0 - cfg.betpawa_max_slippage)
        if price < floor:
            return SlippageDecision(
                ok=False,
                reason=(
                    f"{leg.selection} is {price:.3f} at betPawa, scanned at {leg.price:.3f} "
                    f"(tolerance {cfg.betpawa_max_slippage * 100:.1f}%)"
                ),
            )
        if price > leg.price * (1.0 + MAX_UPSIDE):
            return SlippageDecision(
                ok=False,
                reason=(
                    f"{leg.selection} read as {price:.3f} but scanned at {leg.price:.3f}; "
                    f"that is too far above the scan to be the same selection - "
                    f"the slip was probably read wrong, nothing placed"
                ),
            )

    odds = prod(actual_prices)
    ev = combo.prob * odds - 1.0
    floor_ev = float("-inf") if cfg.allow_zero_edge else cfg.min_edge
    if ev < floor_ev:
        return SlippageDecision(
            ok=False,
            reason=f"venue price leaves EV at {ev * 100:+.1f}%, below min_edge {cfg.min_edge * 100:.1f}%",
            odds=odds,
            ev=ev,
        )

    replanned = plan_stake(combo.prob, odds, bankroll, cfg, already_staked_today)
    if not replanned.ok:
        return SlippageDecision(
            ok=False,
            reason=f"slippage left no sensible stake ({replanned.capped_by})",
            odds=odds,
            ev=ev,
        )

    stake = round(min(planned_stake, replanned.stake), 2)
    if stake < cfg.min_stake:
        return SlippageDecision(ok=False, reason="re-sized stake is below the minimum")

    return SlippageDecision(
        ok=True,
        stake=stake,
        odds=odds,
        ev=ev,
        reduced=stake < round(planned_stake, 2),
    )


def slip_summary(combo: Combo, prices: list[float]) -> str:
    lines = []
    for leg, price in zip(combo.legs, prices):
        lines.append(f"{leg.selection} @ {price:.2f} ({leg.book} scan {leg.price:.2f})")
    return " | ".join(lines)


def strip_slip_noise(text: str) -> str:
    """Drop stake/total lines so a text fallback reads leg prices, not amounts."""
    keep = [line for line in (text or "").splitlines()
            if not SLIP_NOISE_RE.search(line)]
    return "\n".join(keep)


def _summarise_captures(captured: list[dict]) -> list[str]:
    """Collapse the network log into one line per endpoint, busiest first."""
    seen: dict[str, int] = {}
    for entry in captured:
        url = str(entry.get("url", "")).split("?")[0]
        seen[url] = seen.get(url, 0) + 1
    rows = sorted(seen.items(), key=lambda item: -item[1])
    return [f"{count:>3}x {url[:150]}" for url, count in rows]


# ---------------------------------------------------------------------------
# browser driver
# ---------------------------------------------------------------------------
class BetpawaBrowser:
    def __init__(self, cfg: Config, headless: bool | None = None, slow_mo: int = 0):
        self.cfg = cfg
        self.headless = cfg.betpawa_headless if headless is None else headless
        self.slow_mo = slow_mo
        self.selectors = {**DEFAULT_SELECTORS, **(cfg.betpawa_selectors or {})}
        self.session_path = cfg.resolve(cfg.betpawa_session_path)
        self.timeout = cfg.betpawa_timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        # JSON responses seen while browsing, so `probe` can reveal the site's
        # own odds endpoints instead of guessing at the DOM
        self.captured: list[dict] = []

    # -- lifecycle -----------------------------------------------------
    def __enter__(self) -> "BetpawaBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(PLAYWRIGHT_HINT) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo
        )
        storage = str(self.session_path) if self.session_path.exists() else None
        self._context = self._browser.new_context(
            storage_state=storage,
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
            timezone_id="Africa/Dar_es_Salaam",
        )
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.timeout)
        self.page.on("response", self._capture_json)
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001 - teardown must never mask a real error
            pass

    # -- commands ------------------------------------------------------
    def login_interactive(self) -> Path:
        self.cfg.report_dir.mkdir(parents=True, exist_ok=True)
        self.page.goto(self.cfg.betpawa_url, wait_until="domcontentloaded")
        print("\nA browser window is open. Log in to betPawa there.")
        print("Complete the SMS code, wait until you can see your balance, then")
        input("press Enter here to save the session... ")
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.session_path))
        print(f"session saved: {self.session_path}")
        return self.session_path

    def probe(self) -> tuple[Path, Path, Path]:
        self.cfg.report_dir.mkdir(parents=True, exist_ok=True)
        self.page.goto(self.cfg.betpawa_url, wait_until="domcontentloaded")
        self._dismiss_overlays()
        # give the SPA time to fire its odds/feed requests, and scroll once so
        # lazily-loaded lists render
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - never block the dump on this
            pass
        try:
            self.page.mouse.wheel(0, 2000)
            self.page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            pass

        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        png = self.cfg.report_dir / f"betpawa-probe-{stamp}.png"
        html = self.cfg.report_dir / f"betpawa-probe-{stamp}.html"
        net = self.cfg.report_dir / f"betpawa-probe-{stamp}-network.json"

        self.page.screenshot(path=str(png), full_page=True)
        html.write_text(self.page.content(), encoding="utf-8")
        net.write_text(json.dumps(self.captured, indent=1, default=str),
                       encoding="utf-8")

        print(f"page title: {self.page.title()}")
        print(f"screenshot: {png}")
        print(f"html:       {html}")
        print(f"network:    {net}  ({len(self.captured)} JSON responses)")
        if self.captured:
            print("\nJSON endpoints seen (candidates for a price feed):")
            for entry in _summarise_captures(self.captured):
                print(f"  {entry}")
        print("\nUseful when tuning betpawa_selectors, and for finding the odds feed.")
        return png, html, net

    def _capture_json(self, response) -> None:
        """Record JSON responses so a probe can find the site's own data API."""
        try:
            if len(self.captured) >= 200:
                return
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type.lower():
                return
            length = (response.headers or {}).get("content-length")
            if length and int(length) > 3_000_000:
                return
            self.captured.append({
                "url": response.url,
                "status": response.status,
                "body": response.json(),
            })
        except Exception:  # noqa: BLE001 - capture is best-effort instrumentation
            pass

    def place(self, combo: Combo, stake: float, bankroll: float,
              already_staked_today: float = 0.0) -> "Placement":
        from .execution import Placement  # local import avoids a cycle

        self.page.goto(self.cfg.betpawa_url, wait_until="domcontentloaded")
        self._dismiss_overlays()

        for index, leg in enumerate(combo.legs, start=1):
            self._add_selection(leg, index)

        prices = self._read_betslip_prices(len(combo.legs))
        decision = assess_slippage(
            combo, stake, prices, self.cfg, bankroll, already_staked_today
        )
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        self.cfg.report_dir.mkdir(parents=True, exist_ok=True)
        before = self.cfg.report_dir / f"betpawa-{stamp}-slip.png"
        self.page.screenshot(path=str(before), full_page=True)

        if not decision.ok:
            return Placement(
                ref=str(before.name),
                status="rejected",
                detail=f"{decision.reason}. Slip saved: {before.name}",
            )

        if self.cfg.betpawa_dry_run:
            # fill the stake so the screenshot shows the exact slip that would
            # have been placed, but never press the submit button
            try:
                self._fill_stake(decision.stake)
            except RuntimeError as exc:
                print(f"note: {exc}")
            self.page.screenshot(path=str(before), full_page=True)
            return Placement(
                ref=str(before.name),
                status="pending_user",
                detail=(
                    f"dry run: slip filled at {slip_summary(combo, prices)}, "
                    f"stake {decision.stake:.2f}; set betpawa_dry_run=false to submit"
                ),
                stake=decision.stake,
            )

        self._fill_stake(decision.stake)
        self._submit()

        after = self.cfg.report_dir / f"betpawa-{stamp}-result.png"
        self.page.screenshot(path=str(after), full_page=True)

        if self._has(self.selectors["success_marker"]):
            return Placement(
                ref=stamp,
                status="accepted",
                detail=(
                    f"betPawa accepted: {slip_summary(combo, prices)} "
                    f"@ {decision.odds:.3f}, stake {decision.stake:.2f}"
                ),
                stake=decision.stake,
            )
        if self._has(self.selectors["error_marker"]):
            text = self._text_of(self.selectors["error_marker"])
            return Placement(
                ref=stamp, status="rejected",
                detail=f"betPawa refused the bet: {text[:200]}",
            )
        return Placement(
            ref=stamp,
            status="pending",
            detail=f"submitted but no confirmation seen; check the account. Screenshot: {after.name}",
            stake=decision.stake,
        )

    # -- page actions --------------------------------------------------
    def _dismiss_overlays(self) -> None:
        for label in ("Accept all", "Accept", "Got it", "OK", "Allow all", "Close"):
            try:
                button = self.page.get_by_role("button", name=label, exact=False)
                if button.count():
                    button.first.click(timeout=1500)
                    self.page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001 - overlay handling is best-effort
                continue

    def _add_selection(self, leg, index: int) -> None:
        home = leg.match.split(" vs ")[0].strip()
        away = leg.match.split(" vs ")[-1].strip()
        # BetPawaProvider puts the venue outcome code and price id on the
        # candidate. They let us open the exact event and click the exact
        # price instead of guessing from a club name.
        venue = leg.venue or {}
        event_id = str(venue.get("event_id") or "")
        if event_id:
            self.page.goto(
                f"{self.cfg.betpawa_url.rstrip('/')}/event/{event_id}",
                wait_until="domcontentloaded",
            )
            self._dismiss_overlays()
            # the event page is a Next.js SPA: price buttons render a beat later
            self.page.wait_for_timeout(1800)
        else:
            self._search(home)
            self._open_event(home, away)
        self._click_price(leg.selection, venue)

        count = self._betslip_count()
        if count < index:
            raise RuntimeError(
                f"selection {index} ({leg.selection}) did not reach the betslip "
                f"(slip shows {count} selection(s)). Run `py main.py probe` and "
                f"adjust betpawa_selectors."
            )

    def _search(self, query: str) -> None:
        try:
            button = self._first(self.selectors["search_button"])
            if button is not None and button.count():
                button.first.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            field = self._first(self.selectors["search_input"])
            if field is not None and field.count():
                field.first.fill(query)
                field.first.press("Enter")
                self.page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass

    def _open_event(self, home: str, away: str) -> None:
        for pattern in (f"{home}.*{away}", home, away):
            try:
                row = self.page.get_by_text(re.compile(pattern, re.IGNORECASE))
                if row.count():
                    row.first.click(timeout=3000)
                    self.page.wait_for_timeout(1200)
                    return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(
            f"could not open the fixture {home} vs {away}. Run `py main.py probe` "
            f"to capture the current page and tune betpawa_selectors."
        )

    def _click_node(self, node) -> bool:
        """Click a price button, keeping it clear of betPawa's sticky header."""
        try:
            node.scroll_into_view_if_needed(timeout=2000)
            # centre the element so the fixed navbar cannot cover the click point
            node.evaluate("el => el.scrollIntoView({block: 'center'})")
            self.page.wait_for_timeout(150)
            node.click(timeout=2500)
            self.page.wait_for_timeout(600)
            return True
        except Exception:  # noqa: BLE001 - fall through to a forced click
            pass
        try:
            node.click(timeout=1500, force=True)
            self.page.wait_for_timeout(600)
            return True
        except Exception:  # noqa: BLE001 - caller tries the next strategy
            return False

    def _click_price(self, selection: str, venue: dict | None = None) -> None:
        venue = venue or {}
        price_id = str(venue.get("price_id") or "")
        outcome = str(venue.get("outcome") or "")
        type_id = str(venue.get("type_id") or "")
        market_id = str(venue.get("market_type_id") or "")

        # most reliable: the price id comes straight from betPawa's feed. The
        # template is configurable because their DOM attribute names move.
        if price_id:
            template = self.selectors.get(
                "price_by_id", '[data-price-id="{id}"], [data-testid="price-{id}"]'
            )
            for selector in template.split(","):
                try:
                    node = self._first(selector.strip().format(id=price_id))
                    if node is not None and node.count():
                        if self._click_node(node.first):
                            return
                except Exception:  # noqa: BLE001 - try the next strategy
                    continue

        # current live DOM exposes odd-<market>-<outcome type>, not the feed's
        # internal price id. Both ids come from the same price object.
        if type_id:
            template = self.selectors.get(
                "odd_by_type",
                '[data-test-id="odd-{market}-{type}"], [data-test-id="odd-{type}"]',
            )
            for selector in template.split(","):
                try:
                    node = self._first(
                        selector.strip().format(market=market_id, type=type_id)
                    )
                    if node is not None and node.count():
                        if self._click_node(node.first):
                            return
                except Exception:  # noqa: BLE001 - try the next strategy
                    continue

        # next best: the 1 / X / 2 code, qualified by the selection text
        if outcome:
            template = self.selectors.get(
                "outcome_button", '[data-outcome="{code}"], [data-selection="{code}"]'
            )
            for selector in template.split(","):
                try:
                    node = self._first(selector.strip().format(code=outcome))
                    if node is not None and node.count():
                        if self._click_node(node.first):
                            return
                except Exception:  # noqa: BLE001
                    continue

        # the price button sits in the same row as the selection name
        candidates = [
            self.page.locator(self.selectors["odds_button"]),
            self.page.get_by_role("button"),
        ]
        for locator in candidates:
            try:
                total = min(locator.count(), 400)
                for i in range(total):
                    node = locator.nth(i)
                    if selection.lower() in (node.inner_text(timeout=800) or "").lower():
                        if self._click_node(node):
                            return
            except Exception:  # noqa: BLE001
                continue
        # last resort: click the text itself, many skins make the whole row clickable
        try:
            node = self.page.get_by_text(selection, exact=False).first
            if not self._click_node(node):
                raise RuntimeError("text node could not be clicked")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"could not click a price for '{selection}'. Run `py main.py probe` "
                f"and tune betpawa_selectors.odds_button."
            ) from exc

    def _betslip_count(self) -> int:
        slip = self._first(self.selectors["betslip"])
        if slip is None or not slip.count():
            return 0
        rows = slip.first.locator(self.selectors["betslip_row"])
        try:
            count = rows.count()
            if count:
                return count
        except Exception:  # noqa: BLE001
            pass
        return len(ODDS_RE.findall(slip.first.inner_text(timeout=1500) or ""))

    def _read_betslip_prices(self, expected: int) -> list[float]:
        slip = self._first(self.selectors["betslip"])
        if slip is None or not slip.count():
            raise RuntimeError(
                "betslip panel not found after adding selections. "
                "Run `py main.py probe` and tune betpawa_selectors.betslip."
            )

        # preferred: one price node per selection, in slip order
        for key in ("slip_price", "odds_button"):
            nodes = slip.first.locator(self.selectors[key])
            try:
                if 0 < nodes.count() <= 40:
                    collected: list[float] = []
                    for i in range(nodes.count()):
                        collected.extend(
                            parse_prices(nodes.nth(i).inner_text(timeout=500) or "", 1)
                        )
                    if len(collected) >= expected:
                        return collected[:expected]
            except Exception:  # noqa: BLE001 - try the next selector set
                continue

        text = slip.first.inner_text(timeout=3000) or ""
        prices = parse_prices(strip_slip_noise(text), expected)
        if len(prices) < expected:
            raise RuntimeError(
                f"betslip shows {len(prices)} readable price(s), expected {expected}. "
                f"Run `py main.py probe` and tune betpawa_selectors.odds_button."
            )
        return prices

    def _fill_stake(self, stake: float) -> None:
        field = self._first(self.selectors["stake_input"])
        if field is None or not field.count():
            raise RuntimeError(
                "stake input not found. Run `py main.py probe` and tune "
                "betpawa_selectors.stake_input."
            )
        field.first.click(timeout=2000)
        field.first.fill(f"{stake:.2f}")
        self.page.wait_for_timeout(400)

    def _submit(self) -> None:
        button = self._first(self.selectors["place_button"])
        if button is None or not button.count():
            raise RuntimeError(
                "place-bet button not found. Run `py main.py probe` and tune "
                "betpawa_selectors.place_button."
            )
        # exactly one click: this method is never called in a retry loop
        button.first.click(timeout=4000)
        self.page.wait_for_timeout(1500)

        # some skins ask for a second confirmation; click it at most once
        try:
            confirm = self._first(self.selectors["confirm_button"])
            if confirm is not None and confirm.count() and confirm.first.is_visible():
                confirm.first.click(timeout=2500)
                self.page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001 - a missing dialog is normal
            pass
        self.page.wait_for_timeout(2000)

    # -- helpers -------------------------------------------------------
    def _first(self, selector: str):
        if not selector:
            return None
        try:
            return self.page.locator(selector)
        except Exception:  # noqa: BLE001
            return None

    def _has(self, selector: str) -> bool:
        locator = self._first(selector)
        try:
            return bool(locator and locator.count())
        except Exception:  # noqa: BLE001
            return False

    def _text_of(self, selector: str) -> str:
        locator = self._first(selector)
        try:
            return (locator.first.inner_text(timeout=1000) or "").strip()
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------
def session_age_hours(cfg: Config) -> float | None:
    path = cfg.resolve(cfg.betpawa_session_path)
    if not path.exists():
        return None
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=None)
    return (datetime.now() - stamp).total_seconds() / 3600.0


def describe_session(cfg: Config) -> str:
    hours = session_age_hours(cfg)
    if hours is None:
        return "no saved session (run `py main.py login`)"
    return f"saved session {hours:.1f}h old ({cfg.betpawa_session_path})"
