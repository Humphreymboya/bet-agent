"""Configuration loading and validation.

The config is plain JSON on purpose: no third-party dependency, easy to audit.
Secrets (API keys, Betfair session tokens) live in .env, never in config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

VALID_MODES = {"paper", "live"}
VALID_PROVIDERS = {"demo", "theoddsapi", "ps3838", "betpawa"}
VALID_EXECUTION = {"paper", "ticket", "betfair", "ps3838", "betpawa"}
VALID_REFERENCE = {"none", "betfair", "ps3838", "theoddsapi"}


@dataclass
class Config:
    # --- run mode -------------------------------------------------------
    mode: str = "paper"                 # paper = simulate, live = real money
    provider: str = "demo"              # odds data source
    execution: str = "paper"            # paper | ticket | betfair | ps3838 | betpawa
    reference: str = "none"             # extra sharp reference feed (betfair)

    # --- money ----------------------------------------------------------
    bankroll: float = 1000.0            # starting bankroll
    currency: str = "USD"
    kelly_fraction: float = 0.25        # 0.25 = quarter Kelly
    fixed_stake_enabled: bool = False   # True = bet a chosen amount instead of Kelly
    fixed_stake: float = 0.0            # amount in TZS used when fixed mode is on
    max_stake_pct: float = 0.02         # hard cap per bet, % of bankroll
    daily_stake_cap_pct: float = 0.05   # hard cap per day, % of bankroll
    min_stake: float = 1.0
    stop_loss_pct: float = 0.15         # halt if realised loss exceeds this % of bankroll

    # --- bet shape ------------------------------------------------------
    target_odds: float = 2.0            # the "2 odds" target
    odds_tolerance: float = 0.08        # accept 1.84 - 2.16
    legs: int = 2
    min_leg_odds: float = 1.20
    max_leg_odds: float = 1.85
    min_probability: float = 0.55       # per-leg fair probability floor
    min_combo_probability: float = 0.40 # joint probability floor
    min_edge: float = 0.02              # required EV on the combo (2%)
    pool_edge_floor: float = -0.03      # legs allowed in the pool down to this edge
    max_overround: float = 0.12         # ignore books worse than this margin
    allow_zero_edge: bool = False       # True = also bet when EV <= 0 (not advised)
    max_bets_per_day: int = 1

    # --- market selection ----------------------------------------------
    # When the price feed and the execution venue differ, reference books are
    # merged in for fair value only. Setting this to the venue's book key stops
    # the scanner from choosing a *foreign* price it cannot actually take.
    venue_book: str = ""

    sports: list[str] = field(default_factory=lambda: [
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
    ])
    markets: list[str] = field(default_factory=lambda: ["h2h"])
    regions: str = "eu,uk"
    max_candidates: int = 60            # combo search pool size
    min_lead_minutes: int = 45          # ignore games starting sooner
    ps3838_sport_ids: list[int] = field(default_factory=lambda: [29])  # 29 = soccer

    # --- betPawa (browser automation) -----------------------------------
    betpawa_url: str = "https://www.betpawa.co.tz"
    betpawa_session_path: str = "data/betpawa-session.json"
    betpawa_headless: bool = True
    betpawa_dry_run: bool = True        # fill the slip but never submit
    auto_place_enabled: bool = False    # scheduler may place qualifying bets itself
    betpawa_max_slippage: float = 0.02  # abort if betPawa is worse than scan by this
    betpawa_timeout_ms: int = 20000
    betpawa_selectors: dict[str, str] = field(default_factory=dict)  # override heuristics

    # --- AI research (advisory by default) ------------------------------
    ai_enabled: bool = True             # show briefings in the dashboard
    ai_influence: bool = False          # opt-in: let AI shift leg probabilities
    ai_max_prob_adjustment: float = 0.03  # hard clamp, regardless of the model

    # --- storage --------------------------------------------------------
    db_path: str = "data/bets.db"
    reports_dir: str = "reports"
    auto_confirm_live: bool = False

    # runtime only, never serialised
    config_dir: Path | None = field(default=None, repr=False, compare=False)
    config_path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        base = self.config_dir or Path.cwd()
        p = Path(path)
        return p if p.is_absolute() else (base / p)

    @property
    def db_file(self) -> Path:
        return self.resolve(self.db_path)

    @property
    def report_dir(self) -> Path:
        return self.resolve(self.reports_dir)

    def validate(self) -> None:
        errors: list[str] = []
        if self.mode not in VALID_MODES:
            errors.append(f"mode must be one of {sorted(VALID_MODES)}")
        if self.provider not in VALID_PROVIDERS:
            errors.append(f"provider must be one of {sorted(VALID_PROVIDERS)}")
        if self.execution not in VALID_EXECUTION:
            errors.append(f"execution must be one of {sorted(VALID_EXECUTION)}")
        if self.reference not in VALID_REFERENCE:
            errors.append(f"reference must be one of {sorted(VALID_REFERENCE)}")
        if self.bankroll <= 0:
            errors.append("bankroll must be > 0")
        if self.target_odds <= 1.0:
            errors.append("target_odds must be > 1.0")
        if not 0 <= self.odds_tolerance < 1:
            errors.append("odds_tolerance must be in [0, 1)")
        if self.legs < 1:
            errors.append("legs must be >= 1")
        if self.min_leg_odds >= self.max_leg_odds:
            errors.append("min_leg_odds must be < max_leg_odds")
        if not 0 < self.min_probability < 1:
            errors.append("min_probability must be in (0, 1)")
        if not 0 < self.min_combo_probability < 1:
            errors.append("min_combo_probability must be in (0, 1)")
        if not -1 < self.pool_edge_floor < 1:
            errors.append("pool_edge_floor must be in (-1, 1)")
        if self.pool_edge_floor > self.min_edge:
            errors.append("pool_edge_floor must be <= min_edge")
        if not 0 <= self.kelly_fraction <= 1:
            errors.append("kelly_fraction must be in [0, 1]")
        if self.fixed_stake < 0:
            errors.append("fixed_stake must be >= 0")
        if not 0 < self.max_stake_pct <= 1:
            errors.append("max_stake_pct must be in (0, 1]")
        if not 0 < self.daily_stake_cap_pct <= 1:
            errors.append("daily_stake_cap_pct must be in (0, 1]")
        if self.min_lead_minutes < 0:
            errors.append("min_lead_minutes must be >= 0")
        if self.max_candidates < self.legs:
            errors.append("max_candidates must be >= legs")
        if not self.ps3838_sport_ids:
            errors.append("ps3838_sport_ids must contain at least one sport id (29 = soccer)")
        if self.execution == "ps3838" and self.legs > 8:
            errors.append("PS3838 parlays support at most 8 legs")
        if not 0 <= self.betpawa_max_slippage < 1:
            errors.append("betpawa_max_slippage must be in [0, 1)")
        if self.betpawa_timeout_ms <= 0:
            errors.append("betpawa_timeout_ms must be > 0")
        if not self.betpawa_url.startswith(("http://", "https://")):
            errors.append("betpawa_url must start with http:// or https://")
        if self.execution == "betpawa" and self.legs > 20:
            errors.append("betPawa accumulators support at most 20 legs")
        if not 0 <= self.ai_max_prob_adjustment <= 0.05:
            errors.append("ai_max_prob_adjustment must be in [0, 0.05]")
        if errors:
            raise ValueError("invalid config: " + "; ".join(errors))

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("config_dir", None)
        data.pop("config_path", None)
        return data


def load_dotenv(directory: Path) -> None:
    """Minimal .env loader. Does not overwrite real environment variables."""
    path = directory / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    explicit = path or os.environ.get("BETBOT_CONFIG") or "config.json"
    config_path = Path(explicit).expanduser().resolve()
    raw: dict = {}
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    load_dotenv(config_path.parent)
    # a config kept outside the project (a per-book preset, say) still gets the
    # project's .env; load_dotenv never overwrites what is already set
    cwd = Path.cwd()
    if cwd != config_path.parent:
        load_dotenv(cwd)

    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")

    cfg = Config(**raw)
    cfg.config_dir = config_path.parent
    cfg.config_path = config_path
    cfg.validate()
    return cfg
