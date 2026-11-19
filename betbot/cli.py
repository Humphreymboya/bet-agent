"""Command line interface.

    py main.py check                 health + config audit
    py main.py scan                  what the model sees today, no bet placed
    py main.py bet                   place today's bet (paper by default)
    py main.py open                  unsettled bets
    py main.py settle --bet-id 3 --result win [--price 1.42 --price 1.45]
    py main.py report --days 30
    py main.py discover              list soccer leagues available on the feed
    py main.py login                 save a betPawa browser session (SMS code)
    py main.py probe                 dump the betPawa page to tune selectors
    py main.py web                   phone dashboard (token-protected)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from math import prod

from .config import Config, load_config
from .execution import build_broker
from .ledger import Ledger
from .model import Combo, Scan, best_combo, build_candidates
from .providers import ProviderError, fetch_with_reference
from .staking import StakeRequestError, apply_manual_stake, plan_stake
from .util import iso_day, money, pct, rule, signed_pct, utcnow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _playwright_status() -> str:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "not installed (pip install playwright; playwright install chromium)"
    return "installed"


def _account(cfg: Config, ledger: Ledger) -> dict:
    realised = ledger.realised_pnl(cfg.mode)
    equity = cfg.bankroll + realised
    open_bets = ledger.open_bets(cfg.mode)
    open_stake = sum(float(b.stake) for b in open_bets)
    return {
        "equity": equity,
        "realised": realised,
        "open_stake": open_stake,
        "available": max(0.0, equity - open_stake),
        "staked_today": ledger.staked_today(cfg.mode),
        "bets_today": ledger.bets_today(cfg.mode),
        "open_bets": open_bets,
    }


def _header(cfg: Config, account: dict, source: str) -> None:
    strategy = ("max-probability (EV gate OFF)" if cfg.allow_zero_edge
                else f"value only (combo edge >= {pct(cfg.min_edge)})")
    print(rule("="))
    print(f" {utcnow():%Y-%m-%d %H:%M} UTC | mode={cfg.mode.upper()} | "
          f"execution={cfg.execution} | feed={source}")
    print(f" strategy: {strategy}")
    print(f" equity {money(account['equity'], cfg.currency)} | "
          f"available {money(account['available'], cfg.currency)} | "
          f"open {money(account['open_stake'], cfg.currency)} | "
          f"staked today {money(account['staked_today'], cfg.currency)}")
    quota = _quota_left()
    if quota is not None:
        print(f" odds-api credits left this month: {quota}")
    print(rule("="))


def _quota_left() -> str | None:
    try:
        from .providers.theoddsapi import LAST_QUOTA
    except Exception:  # noqa: BLE001 - provider module is always importable
        return None
    return LAST_QUOTA


def _print_scan(scan: Scan, top: int = 12) -> None:
    print(f"\nFixtures priced: {scan.events}   complete markets: {scan.markets_priced}   "
          f"value candidates: {len(scan.candidates)}")
    rejects = ", ".join(f"{k}={v}" for k, v in scan.rejects.items() if v)
    if rejects:
        print(f"Filtered out: {rejects}")
    if not scan.candidates:
        return
    print("\nBest available value (price vs sharp fair price):")
    print(f"  {'edge':>7}  {'price':>6}  {'fair':>6}  selection / fixture")
    for candidate in scan.candidates[:top]:
        print(
            f"  {signed_pct(candidate.edge):>7}  {candidate.price:>6.2f}  "
            f"{pct(candidate.fair_prob):>6}  {candidate.selection} "
            f"[{candidate.market}] - {candidate.match} ({candidate.league}, {candidate.book})"
        )


def _print_combo(combo: Combo, cfg: Config, stake: float, plan=None) -> None:
    print(rule())
    print(f" SELECTED BET: {combo.n_legs}-leg "
          f"{'accumulator' if combo.n_legs > 1 else 'single'}")
    print(rule())
    for index, leg in enumerate(combo.legs, start=1):
        print(f"  {index}. {leg.selection} @ {leg.price:.2f}  ({leg.book})")
        print(f"     {leg.match} | {leg.league} | {leg.market} | "
              f"kickoff {leg.commence_time:%Y-%m-%d %H:%M} UTC")
        print(f"     model probability {pct(leg.fair_prob)}, edge {signed_pct(leg.edge)}, "
              f"reference books used: {leg.n_books}"
              f"{' (sharp)' if leg.sharp_reference else ''}")
    if cfg.execution == "betpawa":
        floor = 1.0 - cfg.betpawa_max_slippage
        floors = [leg.price * floor for leg in combo.legs]
        print(f"\n  take only at    {' / '.join(f'{f:.2f}' for f in floors)} "
              f"per leg (combined >= {prod(floors):.3f}); worse = no bet")
    print(f"\n  combined odds   {combo.odds:.3f}")
    print(f"  model chance    {pct(combo.prob)}")
    print(f"  expected value  {signed_pct(combo.ev)}")
    print(f"  stake           {money(stake, cfg.currency)}")
    print(f"  potential return {money(stake * combo.odds, cfg.currency)}")
    if plan is not None:
        print(f"  sizing          kelly {pct(plan.kelly_full, 2)} x {cfg.kelly_fraction} "
              f"= {pct(plan.kelly_used, 2)} of bankroll, capped by {plan.capped_by}")


def _write_report(cfg: Config, scan: Scan, combo: Combo | None, notes: dict,
                  source: str, stake: float) -> str:
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": stamp,
        "mode": cfg.mode,
        "feed": source,
        "config": cfg.public_dict(),
        "scan": {
            "events": scan.events,
            "markets_priced": scan.markets_priced,
            "rejects": scan.rejects,
            "candidates": [
                {**asdict(c), "commence_time": c.commence_time.isoformat()}
                for c in scan.candidates[:40]
            ],
        },
        "selected": None if combo is None else {
            "odds": combo.odds,
            "prob": combo.prob,
            "ev": combo.ev,
            "stake": stake,
            "legs": [
                {**asdict(leg), "commence_time": leg.commence_time.isoformat()}
                for leg in combo.legs
            ],
        },
        "notes": notes,
    }
    path = cfg.report_dir / f"scan-{stamp}.json"
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return str(path)


def _gather(cfg: Config) -> tuple[Scan, Combo | None, dict, str]:
    events, source = fetch_with_reference(cfg)
    scan = build_candidates(events, cfg, utcnow())
    combo, notes = best_combo(scan.candidates, cfg)
    return scan, combo, notes, source


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_scan(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    account = _account(cfg, ledger)
    scan, combo, notes, source = _gather(cfg)
    _header(cfg, account, source)
    _print_scan(scan)

    stake = 0.0
    plan = None
    if combo is None:
        print(f"\nNO BET: {notes.get('reason', 'no qualifying bet')} "
              f"(pool={notes.get('pool', 0)}, in odds band={notes.get('in_odds_band', 0)})")
    else:
        base = account["available"]
        plan = plan_stake(combo.prob, combo.odds, base, cfg, account["staked_today"])
        stake = plan.stake
        if not plan.ok and not cfg.allow_zero_edge:
            print(f"\nNO BET: stake would be below minimum ({plan.capped_by})")
        else:
            _print_combo(combo, cfg, stake, plan)

    path = _write_report(cfg, scan, combo, notes, source, stake)
    print(f"\nReport written: {path}")
    if args.json:
        print(json.dumps({"selected": None if combo is None else {
            "odds": combo.odds, "prob": combo.prob, "ev": combo.ev, "stake": stake,
            "legs": [{"match": l.match, "selection": l.selection, "price": l.price,
                      "book": l.book} for l in combo.legs]},
            "notes": notes, "report": path}, indent=1))
    return 0


def cmd_bet(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    account = _account(cfg, ledger)
    scan, combo, notes, source = _gather(cfg)
    _header(cfg, account, source)
    _print_scan(scan, top=8)

    # ---- gates ---------------------------------------------------------
    if combo is None:
        print(f"\nNO BET: {notes.get('reason', 'no qualifying bet outside the odds band')}")
        _write_report(cfg, scan, None, notes, source, 0.0)
        return 0

    if account["bets_today"] >= cfg.max_bets_per_day:
        print(f"\nREFUSED: already placed {account['bets_today']} bet(s) today "
              f"(max_bets_per_day={cfg.max_bets_per_day}).")
        return 0

    loss_floor = -cfg.stop_loss_pct * cfg.bankroll
    if account["realised"] <= loss_floor:
        print(f"\nREFUSED: stop-loss hit. Realised {money(account['realised'], cfg.currency)} "
              f"<= {money(loss_floor, cfg.currency)}. Review the model before continuing.")
        return 0

    plan = plan_stake(combo.prob, combo.odds, account["available"], cfg, account["staked_today"])
    if not plan.ok:
        print(f"\nNO BET: stake below minimum ({plan.capped_by}) - the edge is too thin "
              f"to size sensibly.")
        return 0

    requested = getattr(args, "stake", None)
    try:
        stake, stake_note = apply_manual_stake(plan, requested, cfg)
    except StakeRequestError as exc:
        print(f"\nREFUSED: {exc}.")
        return 0
    if stake_note:
        print(f"\n{stake_note}")
    if requested is not None:
        plan.stake = stake
        plan.capped_by = "manual stake (never above the Kelly plan)"

    _print_combo(combo, cfg, stake, plan)

    # Paper/ticket execution and a betPawa dry run move no money. Everything
    # else does, even if mode was left on "paper" by mistake.
    real_execution = (
        cfg.execution not in {"paper", "ticket"}
        and not (cfg.execution == "betpawa" and cfg.betpawa_dry_run)
    )
    if real_execution and cfg.mode != "live":
        print("\nREFUSED: real execution needs \"mode\": \"live\" in config. "
              "Set mode to paper to simulate, or use execution \"ticket\".")
        return 0

    if cfg.mode == "live" and not args.yes:
        print("\nLIVE MODE: refusing to place without --yes. Re-run as:")
        print("  py main.py bet --yes")
        _write_report(cfg, scan, combo, notes, source, plan.stake)
        return 0

    try:
        placement = build_broker(cfg).place(combo, plan.stake, cfg)
    except Exception as exc:  # noqa: BLE001 - surface any venue error verbatim
        print(f"\nEXECUTION FAILED: {exc}")
        _write_report(cfg, scan, combo, notes, source, plan.stake)
        return 1

    bet_id = ledger.add_bet(
        combo=combo,
        stake=(placement.stake or plan.stake),
        mode=cfg.mode,
        provider=source,
        execution=cfg.execution,
        broker_ref=placement.ref,
        note=f"{placement.status}: {placement.detail}",
        status=(
            "dry_run"
            if cfg.execution == "betpawa" and cfg.betpawa_dry_run
            and placement.status == "pending_user"
            else placement.status
        ),
    )
    if placement.stake and placement.stake != plan.stake:
        print(f"\nVenue limited the stake to {money(placement.stake, cfg.currency)} "
              f"(planned {money(plan.stake, cfg.currency)}) - recorded as filled.")
    print(f"\nBET #{bet_id} {placement.status.upper()} | ref {placement.ref} | {placement.detail}")
    if placement.status == "pending_user":
        if cfg.execution == "betpawa":
            print("Dry run only: the betPawa slip is filled in the browser, nothing was "
                  "submitted. Set \"betpawa_dry_run\": false to let it confirm.")
        else:
            print("Confirm the slip in your bookmaker app, then settle it here when it lands.")
    _write_report(cfg, scan, combo, notes, source, placement.stake or plan.stake)
    return 0


def cmd_open(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    bets = ledger.open_bets(cfg.mode)
    if not bets:
        print("No open bets.")
        return 0
    total = 0.0
    for bet in bets:
        total += float(bet.stake)
        print(rule())
        print(f"BET #{bet.id}  {bet.status}  {bet.created_at}  stake "
              f"{money(bet.stake, cfg.currency)} @ {bet.odds:.2f}  (ref {bet.broker_ref})")
        for leg in bet.legs:
            print(f"   - {leg['match']:<38} {leg['selection']:<18} {leg['price']:.2f}  "
                  f"{leg['commence_time']}")
    print(rule())
    print(f"Open exposure: {money(total, cfg.currency)}")
    return 0


def cmd_settle(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    bet = ledger.get_bet(args.bet_id)
    if bet is None:
        print(f"Bet {args.bet_id} not found.")
        return 1
    try:
        pnl = ledger.settle(args.bet_id, args.result, prices=args.price)
    except ValueError as exc:
        print(f"Cannot settle: {exc}")
        return 1
    account = _account(cfg, ledger)
    print(f"Bet #{args.bet_id} settled: {args.result.upper()}  P/L "
          f"{money(pnl, cfg.currency)}")
    if args.price:
        print(f"Recorded actual prices: {', '.join(f'{p:.2f}' for p in args.price)} "
              f"(combined {prod(args.price):.3f})")
    print(f"New equity: {money(account['equity'], cfg.currency)}  "
          f"(realised {money(account['realised'], cfg.currency)})")
    return 0


def cmd_report(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    summary = ledger.summary(cfg.mode)
    account = _account(cfg, ledger)
    print(rule("="))
    print(f" PERFORMANCE REPORT - {cfg.mode.upper()} - last {args.days if args.days else 'all'} day(s)")
    print(rule("="))
    if summary["bets"] == 0:
        print("No bets recorded yet. Run `py main.py bet` a few times (paper mode) first.")
        return 0
    decided = summary["wins"] + summary["losses"]
    win_rate = summary["wins"] / decided if decided else 0.0
    print(f" bets {summary['bets']} | settled {summary['settled']} "
          f"(W {summary['wins']} / L {summary['losses']} / V {summary['voids']}) "
          f"| open {summary['open']}")
    print(f" win rate {pct(win_rate)} | avg odds {summary['avg_odds']:.2f}")
    print(f" staked {money(summary['staked'], cfg.currency)} | "
          f"P/L {money(summary['pnl'], cfg.currency)} | ROI {signed_pct(summary['roi'])}")
    print(f" equity {money(account['equity'], cfg.currency)} | "
          f"available {money(account['available'], cfg.currency)}")

    rows = ledger.by_day(cfg.mode, limit=args.days or 30)
    if rows:
        print("\n by day:")
        print(f"  {'date':<12}{'bets':>6}{'staked':>12}{'P/L':>12}")
        for row in rows:
            print(f"  {row['day']:<12}{row['n']:>6}{row['staked']:>12.2f}{row['pnl']:>12.2f}")
    return 0


def cmd_check(cfg: Config, args) -> int:
    ledger = Ledger(cfg.db_file)
    account = _account(cfg, ledger)
    print(rule("="))
    print(" CONFIG AUDIT")
    print(rule("="))
    print(f" file            {cfg.config_path or (cfg.config_dir / 'config.json')}")
    print(f" mode            {cfg.mode}   execution={cfg.execution}   feed={cfg.provider}"
          f"   reference={cfg.reference}")
    print(f" bankroll        {money(cfg.bankroll, cfg.currency)}")
    print(f" target          {cfg.legs} legs @ {cfg.target_odds:.2f}"
          f" (+/-{cfg.odds_tolerance * 100:.0f}%)")
    print(f" filters         leg odds {cfg.min_leg_odds:.2f}-{cfg.max_leg_odds:.2f}, "
          f"fair prob >= {pct(cfg.min_probability)}, combo prob >= "
          f"{pct(cfg.min_combo_probability)}, min edge {pct(cfg.min_edge)}")
    sizing = (f"FIXED {money(cfg.fixed_stake, cfg.currency)}"
              if cfg.fixed_stake_enabled
              else f"quarter-Kelly={cfg.kelly_fraction}")
    print(f" risk            {sizing}, max stake "
          f"{pct(cfg.max_stake_pct)}, daily cap {pct(cfg.daily_stake_cap_pct)}, "
          f"stop-loss {pct(cfg.stop_loss_pct)}")
    print(f" leagues         {len(cfg.sports)} configured, markets={cfg.markets}")
    print(f" ledger          {cfg.db_file} ({'exists' if cfg.db_file.exists() else 'new'})")
    print(f" equity          {money(account['equity'], cfg.currency)}   "
          f"open {money(account['open_stake'], cfg.currency)}")

    print("\n credentials:")
    print(f"  THE_ODDS_API_KEY   {'set' if os.environ.get('THE_ODDS_API_KEY') else 'missing'}")
    betfair_keys = ["BETFAIR_APP_KEY", "BETFAIR_SESSION_TOKEN", "BETFAIR_CERT_FILE"]
    print(f"  betfair            "
          f"{'set' if any(os.environ.get(k) for k in betfair_keys) else 'missing'}")
    ps3838_ok = bool(os.environ.get("PS3838_USERNAME") and os.environ.get("PS3838_PASSWORD"))
    print(f"  ps3838/pinnacle    {'set' if ps3838_ok else 'missing'}")
    print(f"  betpawa browser    {_playwright_status()}")

    print("\n betpawa:")
    from .betpawa import describe_session

    print(f"  url                {cfg.betpawa_url}")
    print(f"  session            {describe_session(cfg)}")
    print(f"  submit             {'DRY RUN (fills slip, never confirms)' if cfg.betpawa_dry_run else 'LIVE (clicks confirm)'}")
    print(f"  slippage           abort past {pct(cfg.betpawa_max_slippage)} worse than scan")

    problems: list[str] = []
    if cfg.provider == "theoddsapi" and not os.environ.get("THE_ODDS_API_KEY"):
        problems.append("provider=theoddsapi but THE_ODDS_API_KEY is missing")
    if cfg.reference == "betfair" and not any(os.environ.get(k) for k in betfair_keys):
        problems.append("reference=betfair but no Betfair credentials")
    if cfg.provider == "ps3838" and not ps3838_ok:
        problems.append("provider=ps3838 but PS3838_USERNAME/PS3838_PASSWORD are missing")
    if cfg.reference == "ps3838" and not ps3838_ok:
        problems.append("reference=ps3838 but PS3838_USERNAME/PS3838_PASSWORD are missing")
    if cfg.execution == "ps3838" and not ps3838_ok:
        problems.append("execution=ps3838 but PS3838_USERNAME/PS3838_PASSWORD are missing")
    if cfg.execution == "betfair" and cfg.legs != 1:
        problems.append("execution=betfair only supports legs=1 (no exchange doubles)")
    if cfg.execution == "ps3838" and cfg.legs > 8:
        problems.append("ps3838 parlays support at most 8 legs")
    if cfg.mode == "live" and cfg.execution == "paper":
        problems.append("mode=live but execution=paper - nothing will be placed")
    if cfg.mode == "paper" and cfg.execution in {"betfair", "ps3838"}:
        problems.append(f"mode=paper but execution={cfg.execution} - orders would be real")
    if cfg.execution == "betpawa":
        from .betpawa import session_age_hours

        if _playwright_status().startswith("not installed"):
            problems.append("execution=betpawa but Playwright is not installed")
        age = session_age_hours(cfg)
        if age is None:
            problems.append("execution=betpawa but no saved session - run `py main.py login`")
        elif age > 24 * 7:
            problems.append(f"betPawa session is {age:.0f}h old; re-run `py main.py login` if it lapses")
        if cfg.mode == "live" and cfg.betpawa_dry_run:
            problems.append("mode=live but betpawa_dry_run=true - the slip will never be submitted")
        if cfg.mode == "paper" and not cfg.betpawa_dry_run:
            problems.append("mode=paper with betpawa_dry_run=false - real money would be placed")
        if cfg.auto_place_enabled and cfg.betpawa_dry_run:
            problems.append("auto_place_enabled but betpawa_dry_run=true - auto-place will not fire")
    if not cfg.allow_zero_edge and cfg.min_edge <= 0:
        problems.append("min_edge <= 0 with allow_zero_edge=false: no bet can qualify")

    print("\n warnings:" if problems else "\n warnings: none")
    for problem in problems:
        print(f"  ! {problem}")
    return 0


def cmd_login(cfg: Config, args) -> int:
    """Open a visible browser so the user can authenticate betPawa once."""
    from .betpawa import BetpawaBrowser, describe_session

    try:
        with BetpawaBrowser(cfg, headless=False) as browser:
            browser.login_interactive()
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\ncancelled before saving the session")
        return 130
    print(f"\n{describe_session(cfg)}")
    print("Next: `py main.py probe` to check the page loads with this session.")
    return 0


def cmd_probe(cfg: Config, args) -> int:
    """Dump the live page so selectors can be tuned against the real DOM."""
    from .betpawa import BetpawaBrowser

    try:
        with BetpawaBrowser(cfg, headless=not args.headed) as browser:
            png, html, net = browser.probe()
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    print(f"\nsaved {png.name}, {html.name} and {net.name} in {cfg.report_dir}")
    print("Open the screenshot and confirm these still match, then override any "
          "broken ones in config.json under \"betpawa_selectors\".")
    print("Prices come from betPawa's public JSON feed, so the network dump is "
          "only needed if that feed changes. The screenshot is for the slip "
          "selectors, which still need a logged-in tuning pass.")
    return 0


def cmd_web(cfg: Config, args) -> int:
    """Serve the phone dashboard. Bound to localhost unless --host is given."""
    from .webapp import serve

    serve(cfg, host=args.host, port=args.port)
    return 0


def cmd_discover(cfg: Config, args) -> int:
    from .providers.theoddsapi import TheOddsApiProvider

    provider = TheOddsApiProvider()
    rows = provider.discover_soccer()
    print(f"{len(rows)} active soccer leagues on the feed:\n")
    for row in rows:
        print(f"  {row.get('key', ''):<38} {row.get('title', '')}")
    print("\nCopy the keys you want into \"sports\" in config.json.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bet-agent",
        description="Value-betting agent: finds mispriced legs, builds a ~2.0 bet, "
                    "sizes it with fractional Kelly, and logs everything.",
    )
    parser.add_argument("--config", default=None, help="path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="show today's slate and the chosen bet")
    p_scan.add_argument("--json", action="store_true", help="also print JSON to stdout")
    p_scan.set_defaults(func=cmd_scan)

    p_bet = sub.add_parser("bet", help="place today's bet")
    p_bet.add_argument("--yes", action="store_true", help="required to fire in live mode")
    p_bet.add_argument(
        "--stake", type=float, default=None,
        help="stake for this trade; capped at the Kelly plan and never raised above it",
    )
    p_bet.set_defaults(func=cmd_bet)

    p_open = sub.add_parser("open", help="list unsettled bets")
    p_open.set_defaults(func=cmd_open)

    p_settle = sub.add_parser("settle", help="record a result")
    p_settle.add_argument("--bet-id", type=int, required=True)
    p_settle.add_argument("--result", required=True, choices=["win", "loss", "void"])
    p_settle.add_argument(
        "--price", type=float, action="append", default=None,
        help="actual decimal price of each leg, in ticket order; repeat once per leg",
    )
    p_settle.set_defaults(func=cmd_settle)

    p_report = sub.add_parser("report", help="performance summary")
    p_report.add_argument("--days", type=int, default=30)
    p_report.set_defaults(func=cmd_report)

    p_check = sub.add_parser("check", help="audit config and credentials")
    p_check.set_defaults(func=cmd_check)

    p_disc = sub.add_parser("discover", help="list soccer leagues on The Odds API")
    p_disc.set_defaults(func=cmd_discover)

    p_login = sub.add_parser("login", help="save a betPawa browser session (SMS code)")
    p_login.set_defaults(func=cmd_login)

    p_probe = sub.add_parser("probe", help="dump the betPawa page to tune selectors")
    p_probe.add_argument("--headed", action="store_true",
                         help="show the browser window instead of running headless")
    p_probe.set_defaults(func=cmd_probe)

    p_web = sub.add_parser("web", help="phone dashboard for monitoring and control")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="bind address (default 127.0.0.1; use 0.0.0.0 only on trusted WiFi)")
    p_web.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which mangles league names like
    # "Série A" coming back from the feed.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"config error: {exc}")
        return 1

    try:
        return args.func(cfg, args)
    except ProviderError as exc:
        print(f"data feed error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
