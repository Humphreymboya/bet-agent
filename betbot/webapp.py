"""Small mobile dashboard for the betting agent.

Standard library only: http.server + one HTML file served from memory. It is a
*control panel*, not a betting engine - every action calls the same functions
the CLI uses, so the risk gates are identical.

Security:
* every /api call needs the token from .env or data/web-token.txt;
* binds to 127.0.0.1 unless --host is given explicitly;
* live placement needs a typed confirmation in addition to config flags.

Phone access: run with --host 0.0.0.0, join the same WiFi, open
http://<pc-ip>:8000 and paste the token printed at startup. For access away
from home use Tailscale rather than opening a router port.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import prod
from typing import Any

from . import cli
from .config import Config
from .ledger import Ledger
from .model import Candidate
from .staking import StakeRequestError, apply_manual_stake, plan_stake
from .util import iso, parse_iso, utcnow

LIVE_CONFIRM = "I UNDERSTAND - PLACE REAL BET"

# Only these keys can be changed from the phone. Everything else stays under
# your direct control in config.betpawa.json.
EDITABLE: dict[str, tuple[type, Any, Any]] = {
    "bankroll": (float, 1.0, 1e12),
    "min_stake": (float, 0.0, 1e9),
    "max_stake_pct": (float, 0.001, 1.0),
    "daily_stake_cap_pct": (float, 0.001, 1.0),
    "kelly_fraction": (float, 0.0, 1.0),
    "fixed_stake_enabled": (bool, None, None),
    "fixed_stake": (float, 0.0, 1e9),
    "min_edge": (float, 0.0, 1.0),
    "pool_edge_floor": (float, -1.0, 0.0),
    "target_odds": (float, 1.01, 10.0),
    "odds_tolerance": (float, 0.0, 0.9),
    "min_leg_odds": (float, 1.01, 50.0),
    "max_leg_odds": (float, 1.01, 50.0),
    "min_probability": (float, 0.01, 0.99),
    "min_combo_probability": (float, 0.01, 0.99),
    "max_bets_per_day": (int, 0, 20),
    "stop_loss_pct": (float, 0.0, 1.0),
    "betpawa_dry_run": (bool, None, None),
    "auto_place_enabled": (bool, None, None),
    "betpawa_max_slippage": (float, 0.0, 0.5),
    "ai_enabled": (bool, None, None),
    "ai_influence": (bool, None, None),
    "ai_max_prob_adjustment": (float, 0.0, 0.05),
}


def is_dry_run(cfg: Config) -> bool:
    """True when no money can move automatically from this configuration."""
    if cfg.execution in {"paper", "ticket"}:
        return True
    if cfg.execution == "betpawa":
        return cfg.betpawa_dry_run
    # Betfair/PS3838 adapters place real orders without checking `mode`, so a
    # stray mode="paper" must not be treated as a free pass.
    return False


def validated_updates(
    current: dict,
    updates: dict,
    live_confirm: str = "",
) -> tuple[dict, dict, list[str]]:
    """Coerce phone-supplied settings into safe, typed config values.

    Only whitelisted keys survive. Disabling the betPawa dry run needs the same
    typed confirmation as placing a live bet, so a stray tap cannot arm it.
    """
    applied: dict = {}
    errors: list[str] = []
    for key, value in updates.items():
        if key not in EDITABLE:
            errors.append(f"{key}: not editable")
            continue
        kind, low, high = EDITABLE[key]
        try:
            if kind is bool:
                if isinstance(value, str):
                    value = value.strip().lower() in {"1", "true", "yes", "on"}
                if not isinstance(value, bool):
                    raise ValueError("must be true/false")
            elif kind is int:
                if isinstance(value, bool):
                    raise ValueError("must be a whole number")
                value = int(value)
            else:
                if isinstance(value, bool):
                    raise ValueError("must be a number")
                value = float(value)
            if low is not None and not low <= value <= high:
                raise ValueError(f"must be between {low} and {high}")
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: {exc}")
            continue
        if key == "betpawa_dry_run" and value is False and live_confirm != LIVE_CONFIRM:
            errors.append("disabling betpawa_dry_run needs the typed live confirmation")
            continue
        if key == "auto_place_enabled" and value is True and live_confirm != LIVE_CONFIRM:
            errors.append("enabling auto-place needs the typed live confirmation")
            continue
        applied[key] = value

    merged = dict(current)
    merged.update(applied)
    if merged.get("min_leg_odds", 1) >= merged.get("max_leg_odds", 2):
        errors.append("min_leg_odds must be < max_leg_odds")
    if errors:
        return dict(current), {}, errors
    return merged, applied, []


def selected_leg(index: int) -> Candidate:
    """Rebuild a Candidate from the last stored scan for AI research."""
    with STATE.lock:
        last = STATE.last_scan or {}
    legs = (last.get("selected") or {}).get("legs", [])
    if index < 0 or index >= len(legs):
        raise ValueError("run a scan that selects a bet first")
    data = dict(legs[index])
    data["commence_time"] = parse_iso(data["commence_time"])
    return Candidate(**data)


def save_briefing(briefing, label: str) -> dict:
    payload = briefing.to_dict()
    with STATE.lock:
        STATE.briefing = payload
        STATE.briefing_for = label
    return payload


# ----------------------------------------------------------------------
class AgentState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.last_scan: dict | None = None
        self.activity: list[dict] = []
        self.scan_running = False
        self.bet_running = False
        self.scheduler_running = False
        self.scheduler_interval = 180
        self.briefing: dict | None = None
        self.briefing_for = ""

    def log(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.activity.insert(0, {
                "at": iso(utcnow()), "message": str(message)[:400], "level": level,
            })
            del self.activity[100:]


STATE = AgentState()


# ----------------------------------------------------------------------
def _candidate_dict(c) -> dict:
    row = asdict(c)
    row["commence_time"] = c.commence_time.isoformat()
    return row


def scan_payload(cfg: Config, scan, combo, notes, source: str,
                 stake: float) -> dict:
    account = cli._account(cfg, Ledger(cfg.db_file))
    selected = None
    if combo is not None:
        floor = 1.0 - cfg.betpawa_max_slippage
        selected = {
            "odds": round(combo.odds, 4),
            "prob": round(combo.prob, 4),
            "ev": round(combo.ev, 4),
            "stake": round(stake, 2),
            "max_stake": round(stake, 2),
            "price_floor": [round(leg.price * floor, 2) for leg in combo.legs],
            "combined_floor": round(
                prod(leg.price * floor for leg in combo.legs), 3),
            "legs": [_candidate_dict(leg) for leg in combo.legs],
        }
    return {
        "at": iso(utcnow()),
        "source": source,
        "events": scan.events,
        "markets_priced": scan.markets_priced,
        "candidates": [_candidate_dict(c) for c in scan.candidates[:20]],
        "rejects": scan.rejects,
        "selected": selected,
        "notes": notes,
        "account": account,
    }


def run_scan(cfg: Config) -> bool:
    """Queue a scan in the background. Returns False if one is already running."""
    with STATE.lock:
        if STATE.scan_running:
            return False
        STATE.scan_running = True
    STATE.log("scan started")

    def worker() -> None:
        try:
            scan, combo, notes, source = cli._gather(cfg)
            account = cli._account(cfg, Ledger(cfg.db_file))
            stake = 0.0
            if combo is not None:
                plan = plan_stake(combo.prob, combo.odds, account["available"],
                                  cfg, account["staked_today"])
                stake = plan.stake if plan.ok else 0.0
            payload = scan_payload(cfg, scan, combo, notes, source, stake)
            path = cli._write_report(cfg, scan, combo, notes, source, stake)
            payload["report"] = path
            with STATE.lock:
                STATE.last_scan = payload
            STATE.log(
                f"scan done: {scan.events} fixtures, "
                f"{len(scan.candidates)} candidates, "
                + (f"best {combo.ev * 100:+.1f}% EV" if combo else "NO BET"),
                "ok" if combo else "warn",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the activity log
            STATE.log(f"scan failed: {exc}", "error")
        finally:
            with STATE.lock:
                STATE.scan_running = False

    threading.Thread(target=worker, daemon=True, name="scan").start()
    return True


def place_bet(cfg: Config, stake_request: float | None = None,
              live_confirm: str = "", use_ai: bool = False) -> dict:
    """Shared placement path for the dashboard, with the CLI's gates intact."""
    with STATE.lock:
        if STATE.bet_running:
            return {"ok": False, "message": "a bet is already being placed"}
        STATE.bet_running = True
    try:
        return _place_bet(cfg, stake_request, live_confirm, use_ai)
    finally:
        with STATE.lock:
            STATE.bet_running = False


def _place_bet(cfg: Config, stake_request: float | None,
               live_confirm: str, use_ai: bool) -> dict:
    ledger = Ledger(cfg.db_file)
    account = cli._account(cfg, ledger)
    is_dry = is_dry_run(cfg)

    if not is_dry and live_confirm != LIVE_CONFIRM:
        return {"ok": False, "message":
                f"real money needs the typed confirmation '{LIVE_CONFIRM}'"}

    STATE.log("checking today's slate")
    scan, combo, notes, source = cli._gather(cfg)
    if combo is None:
        with STATE.lock:
            STATE.last_scan = scan_payload(cfg, scan, combo, notes, source, 0.0)
        return {"ok": False, "message":
                notes.get("reason", "no qualifying bet") or "no qualifying bet"}

    if account["bets_today"] >= cfg.max_bets_per_day:
        return {"ok": False, "message": "daily bet limit already reached"}
    if account["realised"] <= -cfg.stop_loss_pct * cfg.bankroll:
        return {"ok": False, "message": "stop-loss hit - review before betting"}

    briefings = []
    if use_ai and cfg.ai_enabled and cfg.ai_influence:
        try:
            from .ai import ai_adjusted_combo
            combo, briefings = ai_adjusted_combo(cfg, combo)
            STATE.log("AI adjustment applied to probabilities", "warn")
        except Exception as exc:  # noqa: BLE001
            STATE.log(f"AI adjustment failed, using market probabilities: {exc}",
                      "warn")

    plan = plan_stake(combo.prob, combo.odds, account["available"],
                      cfg, account["staked_today"])
    if not plan.ok:
        return {"ok": False, "message": "stake below the minimum after caps"}

    try:
        stake, note = apply_manual_stake(plan, stake_request, cfg)
    except StakeRequestError as exc:
        return {"ok": False, "message": str(exc)}
    if note:
        STATE.log(note, "warn")
    if stake_request is not None:
        plan.stake = stake
        plan.capped_by = "manual stake (never above the Kelly plan)"

    STATE.log(f"placing {'DRY RUN' if is_dry else 'LIVE'} bet, stake {stake:.2f}")
    try:
        placement = cli.build_broker(cfg).place(combo, stake, cfg)
    except Exception as exc:  # noqa: BLE001
        STATE.log(f"execution failed: {exc}", "error")
        return {"ok": False, "message": f"execution failed: {exc}"}

    status = ("dry_run" if is_dry and cfg.execution == "betpawa"
              and placement.status == "pending_user" else placement.status)
    bet_id = ledger.add_bet(
        combo=combo,
        stake=placement.stake or stake,
        mode=cfg.mode, provider=source, execution=cfg.execution,
        broker_ref=placement.ref,
        note=f"{placement.status}: {placement.detail}",
        status=status,
    )
    cli._write_report(cfg, scan, combo, notes, source, placement.stake or stake)
    with STATE.lock:
        STATE.last_scan = scan_payload(cfg, scan, combo, notes, source, stake)
    STATE.log(f"bet #{bet_id}: {placement.status} - {placement.detail}",
              "ok" if status == "accepted" else "warn")
    return {
        "ok": True, "bet_id": bet_id, "status": status,
        "detail": placement.detail, "stake": placement.stake or stake,
        "dry_run": is_dry,
        "briefings": [b.to_dict() for b in briefings],
    }


def scheduler_loop(server) -> None:
    STATE.log(f"scheduler on: every {STATE.scheduler_interval} min")
    while STATE.scheduler_running:
        # read the live config each tick so Settings changes take effect at once
        cfg: Config = server.cfg
        if cfg.auto_place_enabled and not is_dry_run(cfg):
            # place_bet() scans for a qualifying combo itself, so don't also
            # run_scan() here - that would spend the reference API quota twice.
            STATE.log("auto-place: checking today's slate")
            try:
                result = place_bet(cfg, live_confirm=LIVE_CONFIRM)
                if result.get("ok"):
                    STATE.log(f"auto-place: bet #{result.get('bet_id')} "
                              f"{result.get('status')} - {result.get('detail')}", "ok")
                else:
                    STATE.log(f"auto-place skipped: {result.get('message', 'no bet')}",
                              "warn")
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                STATE.log(f"auto-place failed: {exc}", "error")
        else:
            # normal auto-scan mode: no money moves, just refresh the slate
            run_scan(cfg)
        waited = 0
        while waited < STATE.scheduler_interval * 10 and STATE.scheduler_running:
            time.sleep(10)
            waited += 10


def token_for(cfg: Config) -> str:
    token = os.environ.get("WEB_TOKEN", "").strip()
    if token:
        return token
    path = cfg.resolve("data/web-token.txt")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    return token


# ----------------------------------------------------------------------
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b1020">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bet Agent">
<link rel="manifest" href="/manifest.webmanifest?v=2">
<title>Bet Agent v2</title>
<style>
  :root {
    --bg:#0b1020; --panel:#141b2e; --panel2:#1b2440; --line:#2a3554;
    --text:#eaf0ff; --muted:#95a2c4; --accent:#5eead4; --warn:#fbbf24;
    --bad:#fb7185; --good:#4ade80;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    padding-bottom:calc(76px + env(safe-area-inset-bottom));
  }
  header {
    padding:calc(14px + env(safe-area-inset-top)) 16px 12px;
    background:linear-gradient(140deg,#132246,#0b1020);
    border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5;
  }
  h1 { margin:0; font-size:20px; letter-spacing:.2px; }
  .sub { color:var(--muted); font-size:12px; margin-top:3px; }
  .chip {
    display:inline-block; padding:2px 8px; border-radius:999px;
    background:#223054; color:var(--accent); font-size:11px; margin-left:6px;
  }
  main { padding:14px; max-width:760px; margin:0 auto; }
  .panel {
    background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:14px; margin-bottom:12px;
  }
  h2 { margin:0 0 10px; font-size:15px; }
  h3 { margin:14px 0 6px; font-size:13px; color:var(--muted); }
  .muted { color:var(--muted); font-size:12px; }
  .cards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }
  .stat { background:var(--panel2); border-radius:11px; padding:10px; }
  .stat .k { color:var(--muted); font-size:11px; text-transform:uppercase; }
  .stat .v { font-size:16px; font-weight:650; margin-top:2px; word-break:break-word; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .row > input, .row > select { flex:1 1 150px; min-width:0; }
  input, select, button, textarea {
    font:inherit; color:var(--text); background:#0e1528;
    border:1px solid var(--line); border-radius:9px; padding:9px 10px;
  }
  input[type=checkbox] { width:auto; accent-color:var(--accent); }
  button {
    background:var(--accent); color:#06231f; border:0; font-weight:700;
    cursor:pointer; padding:10px 14px;
  }
  button.secondary { background:var(--panel2); color:var(--text); border:1px solid var(--line); }
  button.danger { background:var(--bad); color:#2b0710; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .check { display:flex; gap:6px; align-items:center; font-size:12px; color:var(--muted); }
  .badge {
    display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
    background:#233154; color:var(--muted); margin:2px 4px 2px 0;
  }
  .badge.good { background:#123d2c; color:var(--good); }
  .badge.bad { background:#43202b; color:var(--bad); }
  .badge.warn { background:#453718; color:var(--warn); }
  .leg { padding:9px 0; border-top:1px solid var(--line); }
  .leg:first-of-type { border-top:0; }
  .leg b { display:block; font-size:14px; }
  .list-item { padding:9px 0; border-top:1px solid var(--line); font-size:13px; }
  .list-item:first-child { border-top:0; }
  a { color:var(--accent); }
  .tabs {
    position:fixed; left:0; right:0; bottom:0; z-index:6;
    display:grid; grid-template-columns:repeat(6,1fr);
    background:#111a30; border-top:1px solid var(--line);
    padding-bottom:env(safe-area-inset-bottom);
  }
  .tabs button {
    background:none; color:var(--muted); border-radius:0; padding:10px 2px 9px;
    font-size:10px; font-weight:600;
  }
  .tabs button.active { color:var(--accent); box-shadow:inset 0 2px var(--accent); }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }
  .modal {
    position:fixed; inset:0; z-index:20; background:rgba(5,9,20,.94);
    display:flex; align-items:center; justify-content:center; padding:18px;
  }
  .modal.hidden { display:none; }
  .modal .panel { width:100%; max-width:400px; }
  #toast {
    position:fixed; left:50%; bottom:88px; transform:translateX(-50%);
    background:#243354; border:1px solid var(--line); color:var(--text);
    padding:9px 14px; border-radius:999px; font-size:12px; z-index:30;
    max-width:90vw; text-align:center; display:none;
  }
  #toast.error { background:#4a2130; border-color:var(--bad); }
  .setting { padding:8px 0; border-top:1px solid var(--line); }
  .setting:first-of-type { border-top:0; }
  .setting label { display:block; font-size:12px; margin-bottom:4px; }
  .setting .field { width:100%; }
  .pos { color:var(--good); } .neg { color:var(--bad); }
  @media (min-width:600px) { .cards { grid-template-columns:repeat(4,1fr); } }
</style>
</head>
<body>
<header>
  <h1>Bet Agent <span style="opacity:.6;font-size:.7em">v2</span> <span class="chip" id="modeChip">--</span></h1>
  <div class="sub" id="headerSub">Loading status...</div>
</header>

<main>
  <section class="tab-panel active" id="tab-overview">
    <div class="cards" id="cards"></div>
    <div class="panel">
      <h2>Selected bet</h2>
      <div id="selected">No scan yet.</div>
      <h3>Adjust this trade</h3>
      <div class="row">
        <input id="stakeInput" inputmode="decimal" placeholder="Auto (Kelly plan)" aria-label="Stake for this trade">
        <label class="check"><input type="checkbox" id="useAi"> AI adjust</label>
      </div>
      <input id="liveConfirm" style="width:100%;margin-top:8px" placeholder="__LIVE_CONFIRM__" aria-label="Live confirmation">
      <div class="row" style="margin-top:8px">
        <button id="scanBtn" class="secondary">Scan now</button>
        <button id="placeBtn">Loading...</button>
      </div>
      <p class="muted" id="placeNote"></p>
    </div>
    <div class="panel">
      <h2>Auto scan</h2>
      <div class="row">
        <input id="intervalInput" inputmode="numeric" value="180" aria-label="Scan interval in minutes">
        <button id="schedStart" class="secondary">Start</button>
        <button id="schedStop" class="secondary">Stop</button>
      </div>
      <p class="muted">Scans the slate on a timer. It still will not bet unless a bet is explicitly placed.</p>
    </div>
  </section>

  <section class="tab-panel" id="tab-scanner">
    <div class="panel">
      <h2>Latest scan</h2>
      <div id="scanHead" class="muted">No scan yet.</div>
      <div id="scanList" style="margin-top:10px"></div>
      <p class="muted" id="scanReport"></p>
      <button id="scanRefresh" class="secondary">Refresh</button>
    </div>
  </section>

  <section class="tab-panel" id="tab-bets">
    <div class="panel">
      <h2>Bet log</h2>
      <div id="betList">Loading...</div>
    </div>
  </section>

  <section class="tab-panel" id="tab-settings">
    <div class="panel">
      <h2>Settings</h2>
      <p class="muted">Only these keys are writable here. Edit config.betpawa.json for everything else.</p>
      <div id="settings"></div>
      <input id="settingsConfirm" style="width:100%;margin-top:10px" placeholder="__LIVE_CONFIRM__" aria-label="Live confirmation for settings">
      <button id="saveSettings" style="width:100%;margin-top:8px">Save settings</button>
      <p class="muted" id="readOnly"></p>
    </div>
  </section>

  <section class="tab-panel" id="tab-ai">
    <div class="panel">
      <h2>AI match research</h2>
      <p class="muted">Real headlines + a bounded opinion. AI never places a bet, and its probability change is capped. If the API has no credits, real headlines are still shown.</p>
      <h3>From the selected bet</h3>
      <div class="row">
        <select id="legSelect"><option value="">No selected legs yet</option></select>
        <button id="researchLeg">Research leg</button>
      </div>
      <h3>Or any fixture</h3>
      <div class="row">
        <input id="aiHome" placeholder="Home team">
        <input id="aiAway" placeholder="Away team">
      </div>
      <div class="row" style="margin-top:8px">
        <input id="aiLeague" placeholder="League (optional)">
        <input id="aiSelection" placeholder="Selection (e.g. Home)">
      </div>
      <div class="row" style="margin-top:8px">
        <input id="aiKickoff" placeholder="Kickoff ISO (optional)">
        <input id="aiPrice" inputmode="decimal" placeholder="Price 1.44">
        <input id="aiProb" inputmode="decimal" placeholder="Fair prob 0.70">
      </div>
      <button id="researchCustom" style="width:100%;margin-top:8px">Get prediction briefing</button>
      <div id="briefing" style="margin-top:12px">No briefing yet.</div>
    </div>
  </section>

  <section class="tab-panel" id="tab-activity">
    <div class="panel">
      <h2>Activity</h2>
      <div id="activity">Loading...</div>
    </div>
  </section>
</main>

<nav class="tabs">
  <button class="tab active" data-tab="overview">Overview</button>
  <button class="tab" data-tab="scanner">Scanner</button>
  <button class="tab" data-tab="bets">Bets</button>
  <button class="tab" data-tab="settings">Settings</button>
  <button class="tab" data-tab="ai">AI</button>
  <button class="tab" data-tab="activity">Activity</button>
</nav>

<div id="toast"></div>

<div class="modal" id="login">
  <div class="panel">
    <h2>Unlock dashboard</h2>
    <p class="muted">Enter the token printed by <code>main.py web</code>. It stays in this browser only.</p>
    <input id="tokenInput" type="password" style="width:100%" placeholder="Dashboard token">
    <button id="tokenSave" style="width:100%;margin-top:10px">Unlock</button>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const state = { token:localStorage.getItem("betToken") || "", status:null,
                scan:null, config:null, bets:[], activity:[] };
const LIVE_CONFIRM = "__LIVE_CONFIRM__";
const LABELS = {
  bankroll:"Bankroll", min_stake:"Minimum stake", max_stake_pct:"Max stake per bet (%)",
  daily_stake_cap_pct:"Daily stake cap (%)", kelly_fraction:"Kelly fraction",
  fixed_stake_enabled:"Use fixed stake", fixed_stake:"Fixed stake (each bet)",
  auto_place_enabled:"Auto-place qualifying bets",
  min_edge:"Minimum edge (%)", pool_edge_floor:"Pool edge floor (%)",
  target_odds:"Target odds", odds_tolerance:"Odds tolerance",
  min_leg_odds:"Min leg odds", max_leg_odds:"Max leg odds",
  min_probability:"Min leg probability", min_combo_probability:"Min combo probability",
  max_bets_per_day:"Max bets per day", stop_loss_pct:"Stop loss (%)",
  betpawa_dry_run:"betPawa dry run (leave ON until slip verified)",
  betpawa_max_slippage:"Max slippage (%)", ai_enabled:"AI research",
  ai_influence:"Allow AI to adjust probabilities (capped)",
  ai_max_prob_adjustment:"Max AI probability adjustment"
};

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  })[c]);
}
function safeUrl(u) {
  try { const x = new URL(u); return ["http:","https:"].includes(x.protocol) ? x.href : "#"; }
  catch (e) { return "#"; }
}
function fmtMoney(v) {
  const n = Number(v || 0);
  const cur = state.status ? (state.status.currency || "") : "";
  return n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) + (cur ? " " + cur : "");
}
function fmtPct(v, digits=1) { return v === null || v === undefined ? "--" : (Number(v)*100).toFixed(digits)+"%"; }
function fmtTime(v) { if (!v) return "--"; const d = new Date(v); return isNaN(d) ? String(v) : d.toLocaleString(); }
function showLogin() { $("#login").classList.remove("hidden"); }
function hideLogin() { $("#login").classList.add("hidden"); }

let toastTimer;
function toast(msg, isError=false) {
  const el = $("#toast");
  el.textContent = String(msg);
  el.className = isError ? "error" : "";
  el.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = "none"; }, 4200);
}

async function api(path, opts={}) {
  const headers = Object.assign({"Content-Type":"application/json"}, opts.headers || {});
  if (state.token) headers.Authorization = "Bearer " + state.token;
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  if (res.status === 401) { showLogin(); throw new Error("Token required"); }
  let data;
  try { data = await res.json(); } catch (e) { data = {error:res.statusText}; }
  if (!res.ok) throw new Error(data.error || data.message || ("HTTP " + res.status));
  return data;
}

function renderStatus(s) {
  $("#modeChip").textContent = s.mode + " / " + s.execution;
  $("#headerSub").textContent = "feed " + s.provider + " | reference " + s.reference +
    (s.venue_book ? " | venue " + s.venue_book : "") + " | updated " + fmtTime(s.at);
  const cards = [
    ["Equity", fmtMoney(s.account.equity)],
    ["Available", fmtMoney(s.account.available)],
    ["Staked today", fmtMoney(s.account.staked_today)],
    ["Auto-place", s.auto_place ? "ON" : "OFF"],
    ["Feed credits", s.credits || "--"]
  ];
  $("#cards").innerHTML = cards.map(c =>
    '<div class="stat"><div class="k">'+esc(c[0])+'</div><div class="v">'+esc(c[1])+'</div></div>'
  ).join("");
  $("#placeBtn").textContent = s.dry_run ? "Run dry run" : "PLACE REAL BET";
  $("#placeBtn").className = s.dry_run ? "secondary" : "danger";
  $("#placeNote").textContent = s.dry_run
    ? "Dry run: the slip is filled and screenshotted, nothing is submitted."
    : (s.auto_place
        ? "LIVE + AUTO: the scheduler places qualifying bets. Type the confirmation to place one now."
        : "LIVE: type the exact confirmation below. Change the stake any time in Settings.");
  $("#intervalInput").value = s.scheduler.interval_minutes;
  if (s.session) $("#placeNote").title = s.session;
}

function renderSelected() {
  const sel = state.scan && state.scan.selected;
  if (!sel) {
    $("#selected").innerHTML = '<p class="muted">NO BET — ' +
      esc(state.scan && state.scan.notes && state.scan.notes.reason || "no qualifying bet") + "</p>";
    return;
  }
  const legs = sel.legs.map((l,i) =>
    '<div class="leg"><b>'+esc(i+1)+". "+esc(l.selection)+" @ "+Number(l.price).toFixed(2)+"</b>" +
    '<span class="muted">'+esc(l.match)+" · "+esc(l.league)+" · "+fmtTime(l.commence_time)+"</span>" +
    '<div><span class="badge">take ≥ '+esc(sel.price_floor[i] || "--")+'</span>' +
    '<span class="badge">book '+esc(l.book)+"</span></div></div>"
  ).join("");
  $("#selected").innerHTML =
    '<div><span class="badge good">Combined '+Number(sel.odds).toFixed(2)+"</span>" +
    '<span class="badge">Chance '+fmtPct(sel.prob)+"</span>" +
    '<span class="badge '+(sel.ev>0?"good":"bad")+'">EV '+fmtPct(sel.ev)+"</span>" +
    '<span class="badge">Stake '+fmtMoney(sel.stake)+"</span>" +
    '<span class="badge">Floor '+esc(sel.combined_floor)+"</span></div>" + legs;
}

function renderScanner() {
  const s = state.scan;
  if (!s || !s.at) { $("#scanHead").textContent = "No scan yet."; return; }
  $("#scanHead").innerHTML = "Updated " + esc(fmtTime(s.at)) + " · " +
    esc(s.source) + " · " + esc(s.events) + " fixtures · " +
    esc(s.markets_priced) + " priced markets · " + esc(s.candidates.length) + " candidates";
  const rows = s.candidates.slice(0,12).map(c =>
    '<div class="list-item"><span class="badge '+(c.edge>0?"good":"bad")+'">'+
    fmtPct(c.edge)+'</span> '+esc(c.selection)+" @ "+Number(c.price).toFixed(2)+
    " <span class=\"muted\">vs fair "+fmtPct(c.fair_prob)+" · "+esc(c.match)+" · "+esc(c.book)+"</span></div>"
  ).join("");
  $("#scanList").innerHTML = rows || '<p class="muted">No value candidates in this scan.</p>';
  const rej = Object.entries(s.rejects || {}).filter(x => x[1]).map(x => x[0]+"="+x[1]).join(", ");
  $("#scanReport").textContent = (rej ? "Filtered: " + rej + " · " : "") +
    (s.report ? "report saved locally" : "");
}

function renderSettings() {
  const cfg = state.config;
  if (!cfg) return;
  $("#settings").innerHTML = Object.entries(cfg.editable).map(([k,v]) => {
    const label = LABELS[k] || k;
    if (typeof v === "boolean") {
      return '<div class="setting"><label class="check"><input type="checkbox" id="cfg-'+k+'" '+
        (v?"checked":"")+"> "+esc(label)+"</label></div>";
    }
    return '<div class="setting"><label for="cfg-'+k+'">'+esc(label)+"</label>"+
      '<input class="field" id="cfg-'+k+'" inputmode="decimal" value="'+esc(v)+'"></div>';
  }).join("");
  const ro = cfg.read_only;
  $("#readOnly").textContent = "Locked here: mode " + ro.mode + ", execution " + ro.execution +
    ", provider " + ro.provider + ", " + (ro.sports || []).length + " leagues.";
}

function renderBets() {
  if (!state.bets.length) { $("#betList").innerHTML = '<p class="muted">No bets logged yet.</p>'; return; }
  $("#betList").innerHTML = state.bets.map(b => {
    const legs = (b.legs || []).map(l => '<div class="muted">· '+esc(l.selection)+" @ "+Number(l.price).toFixed(2)+" — "+esc(l.match)+"</div>").join("");
    const open = ["open","pending_user","pending","dry_run"].includes(b.status);
    const settle = open ?
      '<div class="row" style="margin-top:8px"><select id="res-'+b.id+'">'+
      '<option value="win">Win</option><option value="loss">Loss</option><option value="void">Void</option></select>'+
      '<input id="prices-'+b.id+'" placeholder="Actual prices 1.42,1.45">'+
      '<button class="secondary" data-settle="'+b.id+'">Settle</button></div>' : "";
    return '<div class="list-item"><span class="badge '+(
      b.status === "accepted" ? "good" : b.status === "dry_run" ? "warn" : "")+'">'+esc(b.status)+"</span>"+
      " #"+b.id+" · "+fmtMoney(b.stake)+" @ "+Number(b.odds).toFixed(2)+
      " · "+fmtTime(b.created_at)+
      (b.actual_odds ? " · actual "+Number(b.actual_odds).toFixed(2) : "")+
      (b.pnl !== null && b.pnl !== undefined ? " · P/L "+fmtMoney(b.pnl) : "")+
      legs + (b.note ? '<div class="muted">'+esc(b.note)+"</div>" : "") + settle + "</div>";
  }).join("");
  document.querySelectorAll("[data-settle]").forEach(btn => {
    btn.onclick = async () => {
      const id = Number(btn.dataset.settle);
      const result = $("#res-"+id).value;
      const raw = $("#prices-"+id).value.trim();
      const prices = raw ? raw.split(/[ ,]+/).map(Number).filter(n => n > 1) : null;
      try {
        await api("/api/settle",{method:"POST",body:JSON.stringify({bet_id:id,result,prices})});
        toast("Bet #"+id+" settled"); refreshAll();
      } catch(e) { toast(e.message,true); }
    };
  });
}

function renderBriefing(b) {
  const cls = b.verdict === "back" ? "good" : b.verdict === "avoid" ? "bad" : "warn";
  const list = (arr, cls2) => (arr || []).map(x => "<li class=\""+cls2+"\">"+esc(x)+"</li>").join("");
  const news = (b.news || []).map(n =>
    '<div class="list-item"><a href="'+esc(safeUrl(n.url))+'" target="_blank" rel="noopener noreferrer">'+
    esc(n.title)+"</a><div class=\"muted\">"+esc(n.source)+" · "+esc(n.date)+"</div></div>"
  ).join("");
  $("#briefing").innerHTML =
    '<div><span class="badge '+cls+'">'+esc(b.verdict)+"</span>"+
    '<span class="badge">Confidence '+fmtPct(b.confidence)+"</span>"+
    '<span class="badge">Prob adjust '+(b.prob_adjustment>=0?"+":"")+fmtPct(b.prob_adjustment)+"</span>"+
    (b.degraded ? '<span class="badge warn">headlines only</span>' : "")+"</div>"+
    "<p>"+esc(b.summary)+"</p>"+
    (b.positives && b.positives.length ? "<h3>Positives</h3><ul>"+list(b.positives,"pos")+"</ul>" : "")+
    (b.negatives && b.negatives.length ? "<h3>Negatives / risks</h3><ul>"+list(b.negatives,"neg")+"</ul>" : "")+
    "<h3>Real headlines</h3>"+(news || '<p class="muted">No headlines found.</p>')+
    '<p class="muted">Generated '+fmtTime(b.generated_at)+(b.model ? " · "+esc(b.model) : "")+"</p>";
}

function renderActivity() {
  $("#activity").innerHTML = (state.activity || []).map(a =>
    '<div class="list-item"><span class="badge '+(
      a.level === "ok" ? "good" : a.level === "error" ? "bad" : a.level === "warn" ? "warn" : "")+
    '">'+esc(a.level)+"</span> <span class=\"muted\">"+fmtTime(a.at)+"</span><br>"+esc(a.message)+"</div>"
  ).join("") || '<p class="muted">Nothing logged yet.</p>';
}

function populateLegs() {
  const legs = (state.scan && state.scan.selected && state.scan.selected.legs) || [];
  $("#legSelect").innerHTML = legs.length
    ? legs.map((l,i) => '<option value="'+i+'">Leg '+(i+1)+" — "+esc(l.match)+" / "+esc(l.selection)+"</option>").join("")
    : '<option value="">No selected legs yet</option>';
}

async function refreshStatus() { state.status = await api("/api/status"); renderStatus(state.status); }
async function refreshScan() {
  state.scan = await api("/api/scan"); renderSelected(); renderScanner(); populateLegs();
}
async function refreshConfig() { state.config = await api("/api/config"); renderSettings(); }
async function refreshBets() { state.bets = await api("/api/bets"); renderBets(); }
async function refreshActivity() { state.activity = await api("/api/status"); renderActivity(); renderStatus(state.status); }
async function refreshAll() {
  await refreshStatus();
  await Promise.allSettled([refreshScan(), refreshConfig(), refreshBets()]);
  renderActivity();
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-"+btn.dataset.tab).classList.add("active");
  };
});

$("#tokenSave").onclick = () => {
  const token = $("#tokenInput").value.trim();
  if (!token) return;
  localStorage.setItem("betToken",token); state.token = token; hideLogin();
  refreshAll().catch(e => toast(e.message,true));
};
$("#tokenInput").addEventListener("keydown", e => { if (e.key === "Enter") $("#tokenSave").click(); });

$("#scanBtn").onclick = $("#scanRefresh").onclick = async () => {
  try { const r = await api("/api/scan",{method:"POST",body:"{}"});
    toast(r.started ? "Scan started" : "A scan is already running");
    setTimeout(() => refreshAll().catch(()=>{}), 2500);
  } catch(e) { toast(e.message,true); }
};

$("#placeBtn").onclick = async () => {
  const btn = $("#placeBtn"); btn.disabled = true;
  const stakeRaw = $("#stakeInput").value.trim();
  const body = {
    stake: stakeRaw ? Number(stakeRaw) : null,
    live_confirm: $("#liveConfirm").value,
    use_ai: $("#useAi").checked
  };
  try {
    const r = await api("/api/bet",{method:"POST",body:JSON.stringify(body)});
    toast(r.ok ? "Bet #"+r.bet_id+" · "+r.status+" · "+r.detail : (r.message || "No bet placed"), !r.ok);
    refreshAll().catch(()=>{});
  } catch(e) { toast(e.message,true); }
  finally { btn.disabled = false; }
};

$("#schedStart").onclick = async () => {
  try { await api("/api/scheduler/start",{method:"POST",
    body:JSON.stringify({interval_minutes:Number($("#intervalInput").value)})});
    toast("Auto scan started"); refreshStatus();
  } catch(e) { toast(e.message,true); }
};
$("#schedStop").onclick = async () => {
  try { await api("/api/scheduler/stop",{method:"POST",body:"{}"});
    toast("Auto scan stopped"); refreshStatus();
  } catch(e) { toast(e.message,true); }
};

$("#saveSettings").onclick = async () => {
  const values = {};
  Object.keys(state.config.editable).forEach(k => {
    const el = $("#cfg-"+k);
    if (!el) return;
    values[k] = el.type === "checkbox" ? el.checked : el.value;
  });
  try {
    const r = await api("/api/config",{method:"POST",body:JSON.stringify({
      values, live_confirm:$("#settingsConfirm").value})});
    toast("Settings saved"); refreshAll().catch(()=>{});
  } catch(e) { toast(e.message,true); }
};

$("#researchLeg").onclick = async () => {
  const idx = $("#legSelect").value;
  if (idx === "") { toast("Run a scan that selects a bet first",true); return; }
  try { renderBriefing(await api("/api/research",{method:"POST",body:JSON.stringify({index:Number(idx)})})); }
  catch(e) { toast(e.message,true); }
};
$("#researchCustom").onclick = async () => {
  const body = {
    home:$("#aiHome").value.trim(), away:$("#aiAway").value.trim(),
    league:$("#aiLeague").value.trim(), selection:$("#aiSelection").value.trim(),
    kickoff:$("#aiKickoff").value.trim(), price:Number($("#aiPrice").value),
    fair_prob:Number($("#aiProb").value)
  };
  try { renderBriefing(await api("/api/research",{method:"POST",body:JSON.stringify(body)})); }
  catch(e) { toast(e.message,true); }
};

if (state.token) {
  hideLogin();
  refreshAll().catch(e => toast(e.message,true));
} else { showLogin(); }
setInterval(() => {
  if (!state.token) return;
  refreshStatus().then(() => { renderActivity(); }).catch(()=>{});
}, 10000);
</script>
</body>
</html>
"""
HTML = HTML.replace("__LIVE_CONFIRM__", LIVE_CONFIRM)


# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BetAgent/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # never write the auth token into the console log
        message = fmt % args
        token = getattr(self.server, "token", "")
        if token:
            message = message.replace(token, "<redacted>")
        print(f"[web] {message}")

    # -- plumbing -----------------------------------------------------
    def _url(self):
        return urllib.parse.urlparse(self.path)

    def _authorised(self, parsed) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        supplied = supplied or self.headers.get("X-Auth-Token", "")
        supplied = supplied or urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return bool(supplied) and secrets.compare_digest(
            supplied, getattr(self.server, "token", ""))

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, indent=1, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = self._url()
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            return self._html(HTML)
        if path == "/manifest.webmanifest":
            return self._json(200, {
                "name": "Bet Agent v2",
                "short_name": "Bet Agent",
                "id": "/?v=2",
                "start_url": "/?v=2",
                "display": "standalone",
                "background_color": "#0b1020",
                "theme_color": "#0b1020",
                "description": "Phone control panel for the Tanzania betting agent",
            })
        if not self._authorised(parsed):
            return self._json(401, {"error": "token required"})
        cfg: Config = self.server.cfg

        if path == "/api/status":
            ledger = Ledger(cfg.db_file)
            account = cli._account(cfg, ledger)
            from .betpawa import describe_session
            from .providers.theoddsapi import LAST_QUOTA
            with STATE.lock:
                last = STATE.last_scan
                return self._json(200, {
                    "at": iso(utcnow()),
                    "mode": cfg.mode,
                    "execution": cfg.execution,
                    "provider": cfg.provider,
                    "reference": cfg.reference,
                    "venue_book": cfg.venue_book,
                    "currency": cfg.currency,
                    "dry_run": is_dry_run(cfg),
                    "auto_place": bool(cfg.auto_place_enabled),
                    "session": describe_session(cfg),
                    "credits": LAST_QUOTA,
                    "account": account,
                    "scan_running": STATE.scan_running,
                    "bet_running": STATE.bet_running,
                    "scheduler": {
                        "running": STATE.scheduler_running,
                        "interval_minutes": STATE.scheduler_interval,
                    },
                    "last_scan_at": last["at"] if last else None,
                    "activity": STATE.activity[:20],
                })

        if path == "/api/scan":
            with STATE.lock:
                return self._json(200, STATE.last_scan or {})

        if path == "/api/bets":
            ledger = Ledger(cfg.db_file)
            rows = ledger.recent_bets(cfg.mode, 12)
            return self._json(200, [{
                "id": b.id, "created_at": b.created_at, "status": b.status,
                "stake": b.stake, "odds": b.odds, "actual_odds": b.actual_odds,
                "pnl": b.pnl, "note": (b.note or "")[:300],
                "legs": [{"match": l["match"], "selection": l["selection"],
                          "price": l["price"]} for l in b.legs],
            } for b in rows])

        if path == "/api/config":
            return self._json(200, {
                "config_path": str(cfg.config_path),
                "editable": {k: getattr(cfg, k) for k in EDITABLE},
                "read_only": {
                    "mode": cfg.mode, "provider": cfg.provider,
                    "execution": cfg.execution, "reference": cfg.reference,
                    "sports": cfg.sports,
                },
            })

        if path == "/api/briefing":
            query = urllib.parse.parse_qs(parsed.query)
            with STATE.lock:
                cached = STATE.briefing
            if "index" not in query:
                if cached:
                    return self._json(200, cached)
                return self._json(400, {"error": "no research yet - pick a leg or enter a fixture"})
            try:
                index = int(query.get("index", ["0"])[0])
                leg = selected_leg(index)
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})
            from .ai import briefing_for_leg
            try:
                briefing = briefing_for_leg(cfg, leg)
                return self._json(200, save_briefing(
                    briefing, leg.match + " / " + leg.selection))
            except Exception as exc:  # noqa: BLE001
                return self._json(502, {"error": str(exc)})

        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = self._url()
        path = parsed.path.rstrip("/")
        if not self._authorised(parsed):
            return self._json(401, {"error": "token required"})
        cfg: Config = self.server.cfg
        body = self._body()

        if path == "/api/scan":
            started = run_scan(cfg)
            return self._json(200, {"started": started})

        if path == "/api/bet":
            result = place_bet(
                cfg,
                stake_request=body.get("stake"),
                live_confirm=str(body.get("live_confirm", "")),
                use_ai=bool(body.get("use_ai", False)),
            )
            return self._json(200, result)

        if path == "/api/research":
            from .ai import briefing_for_leg, research

            try:
                if body.get("index") not in (None, ""):
                    leg = selected_leg(int(body["index"]))
                    briefing = briefing_for_leg(cfg, leg)
                    label = leg.match + " / " + leg.selection
                else:
                    home = str(body.get("home", "")).strip()[:80]
                    away = str(body.get("away", "")).strip()[:80]
                    if not home or not away:
                        return self._json(400, {"error": "home and away teams are required"})
                    league = str(body.get("league", "")).strip()[:80]
                    selection = str(body.get("selection", "")).strip()[:80] or home
                    kickoff = str(body.get("kickoff", "")).strip()[:40]
                    if not kickoff:
                        kickoff = iso(utcnow())
                    parse_iso(kickoff)
                    price = float(body.get("price", 0))
                    fair_prob = float(body.get("fair_prob", 0))
                    if price <= 1.0 or not 0 < fair_prob < 1:
                        return self._json(400, {"error": "price must be > 1 and fair_prob in (0, 1)"})
                    briefing = research(cfg, home, away, league, selection,
                                        kickoff, price, fair_prob)
                    label = f"{home} vs {away} / {selection}"
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._json(502, {"error": str(exc)})
            STATE.log(f"AI research: {label}", "info")
            return self._json(200, save_briefing(briefing, label))

        if path == "/api/settle":
            try:
                prices = body.get("prices")
                pnl = Ledger(cfg.db_file).settle(
                    int(body["bet_id"]), str(body["result"]), prices=prices)
                STATE.log(f"bet #{body['bet_id']} settled: {body['result']}", "ok")
                return self._json(200, {"ok": True, "pnl": pnl})
            except (KeyError, ValueError, TypeError) as exc:
                return self._json(400, {"ok": False, "message": str(exc)})

        if path == "/api/config":
            updates = body.get("values", body)
            if not isinstance(updates, dict):
                return self._json(400, {"error": "values must be an object"})
            # live_confirm is an API-level guard, not a config key.
            updates = {k: v for k, v in updates.items() if k != "live_confirm"}
            raw_path = cfg.config_path
            if raw_path is None or not raw_path.exists():
                return self._json(400, {"error": "no writable config file"})
            try:
                current = json.loads(raw_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": f"cannot read config: {exc}"})

            confirm = str(body.get("live_confirm", ""))
            merged, applied, errors = validated_updates(current, updates, confirm)
            if errors:
                return self._json(400, {"error": "; ".join(errors)})

            # Validate before touching the live file so a bad value cannot leave
            # the config unloadable for the next scheduled run.
            new_cfg = Config(**merged)
            new_cfg.config_dir = cfg.config_dir
            new_cfg.config_path = cfg.config_path
            try:
                new_cfg.validate()
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})

            tmp = raw_path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
                os.replace(tmp, raw_path)
            except OSError as exc:
                return self._json(500, {"error": f"cannot save config: {exc}"})
            self.server.cfg = new_cfg
            STATE.log("settings saved from phone: " + ", ".join(applied))
            return self._json(200, {"ok": True, "applied": applied})

        if path == "/api/scheduler/start":
            interval = int(body.get("interval_minutes", 180))
            if interval < 15:
                return self._json(400, {"error": "interval must be >= 15 minutes"})
            with STATE.lock:
                if STATE.scheduler_running:
                    return self._json(200, {"ok": True, "already_running": True})
                STATE.scheduler_running = True
                STATE.scheduler_interval = interval
            threading.Thread(target=scheduler_loop, args=(self.server,), daemon=True,
                             name="scheduler").start()
            return self._json(200, {"ok": True})

        if path == "/api/scheduler/stop":
            with STATE.lock:
                STATE.scheduler_running = False
            STATE.log("scheduler off")
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8000) -> None:
    token = token_for(cfg)
    server = ThreadingHTTPServer((host, port), Handler)
    server.cfg = cfg            # type: ignore[attr-defined]
    server.token = token        # type: ignore[attr-defined]
    print(f"Dashboard: http://{host}:{port}")
    print(f"Token:     {token}")
    print("Store this token in the phone browser when prompted.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: listening on all interfaces. Use only on a trusted WiFi, "
              "or preferably over Tailscale. Never expose this port to the internet.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        STATE.scheduler_running = False
        server.server_close()
