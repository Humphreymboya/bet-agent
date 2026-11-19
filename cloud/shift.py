"""Long, quota-aware shift runner for GitHub Actions.

Why a long shift: a GitHub job has a 6 h ceiling, but cold-starting a browser
and re-authenticating every 5 minutes wastes both Actions time and API quota.
This runner stays alive for most of a 6 h window, scans on an adaptive
cadence driven by the Odds API's remaining-credit header, and calls the exact
same placement function the phone dashboard uses - including the typed live
confirmation gate, EV filter, slippage checks and ledger writes.

Only one metered scan happens per tick. There is no separate "scan then bet"
pair, so reference credits are never spent twice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable

# `python cloud/shift.py` puts cloud/ first on sys.path; expose the project root
# so the shared betbot package is importable in Actions and from the CLI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from betbot.config import Config, load_config  # noqa: E402
from betbot.providers import theoddsapi  # noqa: E402
from betbot.util import iso, parse_iso, utcnow  # noqa: E402
from betbot.webapp import LIVE_CONFIRM, place_bet  # noqa: E402

DEFAULT_STATUS = "state/quota-status.json"
DEFAULT_LAST_RUN = "cloud/last-run.json"
SLEEP_CHUNK = 60  # seconds; small enough to stop promptly, large enough to idle


# ---------------------------------------------------------------------------
# pure helpers (unit-testable without network or a browser)
# ---------------------------------------------------------------------------
def quota_interval(remaining: int | None, min_quota: int, base_minutes: int) -> int | None:
    """Minutes between scans, or None when the monthly budget is nearly gone.

    Spending the last credits on a full slate is the classic way to wake up on
    the 28th with no reference data at all.  Back off progressively instead.
    """
    if base_minutes <= 0 or min_quota < 0:
        raise ValueError("base_minutes must be > 0 and min_quota >= 0")
    if remaining is None:
        return base_minutes
    if remaining <= min_quota:
        return None
    if remaining <= min_quota * 3:
        return base_minutes * 4
    if remaining <= min_quota * 8:
        return base_minutes * 2
    return base_minutes


def due_for_scan(status: dict, interval_minutes: int | None, now=None) -> bool:
    """True when the stored last-scan time is old enough (or absent)."""
    if interval_minutes is None:
        return False
    now = now or utcnow()
    last = status.get("last_scan")
    if not last:
        return True
    try:
        previous = parse_iso(last)
    except (TypeError, ValueError):
        return True
    return now - previous >= timedelta(minutes=interval_minutes)


def next_month_start(now=None):
    now = now or utcnow()
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1,
                           hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month + 1, day=1,
                       hour=0, minute=0, second=0, microsecond=0)


def load_status(path: Path | str = DEFAULT_STATUS) -> dict:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_status(data: dict, path: Path | str = DEFAULT_STATUS) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def live_confirmation() -> str:
    """Read the typed opt-in from GitHub Secrets (never from the config file)."""
    return os.environ.get("LIVE_CONFIRM", "")


def prepare(cfg: Config, confirm: str) -> list[str]:
    """Fail closed before any browser or API work if the live gates are off."""
    problems: list[str] = []
    if not cfg.auto_place_enabled:
        problems.append("auto_place_enabled is false")
    if cfg.betpawa_dry_run:
        problems.append("betpawa_dry_run is true - nothing would be submitted")
    if cfg.mode != "live":
        problems.append("mode is not live")
    if cfg.execution != "betpawa":
        problems.append("execution is not betpawa")
    if confirm != LIVE_CONFIRM:
        problems.append(f"LIVE_CONFIRM secret must be exactly '{LIVE_CONFIRM}'")
    session = cfg.resolve(cfg.betpawa_session_path)
    if not session.exists():
        problems.append(f"no saved betPawa session at {session}")
    return problems


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def _write_last_run(payload: dict, path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _log(event: str, **fields) -> None:
    row = {"at": iso(utcnow()), "event": event, **fields}
    print(json.dumps(row, default=str), flush=True)


def run_shift(
    cfg: Config,
    confirm: str,
    *,
    seconds: int,
    base_interval_minutes: int,
    min_quota: int,
    status_path: str = DEFAULT_STATUS,
    last_run_path: str = DEFAULT_LAST_RUN,
    place: Callable[..., dict] | None = None,
    sleep: Callable[[float], None] | None = None,
    now_fn=None,
    deadline_fn=None,
) -> dict:
    """Run until the shift deadline, scanning only when the budget allows."""
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    placer = place or place_bet
    nap = sleep or time.sleep
    clock = now_fn or utcnow
    monotonic = deadline_fn or time.monotonic
    deadline = monotonic() + seconds

    summary = {"scans": 0, "bets": 0, "failures": 0, "quota": theoddsapi.LAST_QUOTA}
    _log("shift_start", seconds=seconds, base_interval=base_interval_minutes,
         min_quota=min_quota)

    while monotonic() < deadline:
        now = clock()
        status = load_status(status_path)
        month = now.strftime("%Y-%m")
        if status.get("month") != month:
            status = {"month": month, "scans": 0, "bets": 0}

        paused_until = status.get("paused_until")
        if paused_until:
            try:
                if now < parse_iso(paused_until):
                    _log("paused", until=paused_until)
                    nap(min(SLEEP_CHUNK, max(0, deadline - monotonic())))
                    continue
            except (TypeError, ValueError):
                status.pop("paused_until", None)

        remaining_raw = status.get("quota_remaining")
        try:
            remaining = int(remaining_raw) if remaining_raw is not None else None
        except (TypeError, ValueError):
            remaining = None
        interval = quota_interval(remaining, min_quota, base_interval_minutes)

        if not due_for_scan(status, interval, now):
            nap(min(SLEEP_CHUNK, max(0, deadline - monotonic())))
            continue

        _log("scan_start", quota=remaining, interval=interval)
        summary["scans"] += 1
        try:
            result = placer(cfg, live_confirm=confirm)
        except Exception as exc:  # noqa: BLE001 - keep the long shift alive
            result = {"ok": False, "message": f"placement error: {exc}"}
            summary["failures"] += 1
        if result.get("ok"):
            summary["bets"] += 1

        # theoddsapi.LAST_QUOTA is set by the Odds API response header during
        # this scan. Read the module, not a stale imported copy of the value.
        try:
            fresh = theoddsapi.LAST_QUOTA
            remaining = int(fresh) if fresh is not None else remaining
        except (TypeError, ValueError):
            pass
        summary["quota"] = theoddsapi.LAST_QUOTA

        status.update({
            "month": month,
            "quota_remaining": remaining,
            "last_scan": iso(now),
            "scans": int(status.get("scans", 0)) + 1,
            "bets": int(status.get("bets", 0)) + (1 if result.get("ok") else 0),
            "last_result": {
                "ok": bool(result.get("ok")),
                "message": str(result.get("message", ""))[:300],
                "status": result.get("status", ""),
            },
        })
        if remaining is not None and remaining <= min_quota:
            status["paused_until"] = iso(next_month_start(now))
            _log("quota_low_paused", remaining=remaining, until=status["paused_until"])
        save_status(status, status_path)
        _write_last_run({"at": iso(now), "result": status["last_result"],
                         "quota": remaining, "summary": summary}, last_run_path)
        _log("scan_done", ok=bool(result.get("ok")),
             message=str(result.get("message", ""))[:200], quota=remaining)

    status = load_status(status_path)
    _write_last_run({"at": iso(clock()), "event": "shift_end",
                     "summary": summary, "state": status}, last_run_path)
    _log("shift_end", **summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.github.json")
    parser.add_argument("--minutes", type=int, default=345,
                        help="shift length, max 350 for GitHub Actions (default: 345)")
    parser.add_argument("--scan-interval", type=int, default=60,
                        help="base minutes between scans when quota is healthy")
    parser.add_argument("--min-quota", type=int, default=20,
                        help="pause all scans at or below this credit balance")
    parser.add_argument("--status", default=DEFAULT_STATUS)
    parser.add_argument("--last-run", default=DEFAULT_LAST_RUN)
    args = parser.parse_args(argv)

    if not 0 <= args.minutes <= 350:
        parser.error("--minutes must be between 0 and 350")
    cfg = load_config(args.config)
    confirm = live_confirmation()
    problems = prepare(cfg, confirm)
    if problems:
        payload = {"at": iso(utcnow()), "event": "refused", "reasons": problems}
        _write_last_run(payload, args.last_run)
        for problem in problems:
            _log("refused", reason=problem)
        return 2

    summary = run_shift(
        cfg, confirm,
        seconds=args.minutes * 60,
        base_interval_minutes=args.scan_interval,
        min_quota=args.min_quota,
        status_path=args.status,
        last_run_path=args.last_run,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
