"""Position sizing. Fractional Kelly, capped hard, sized in whole cents."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .config import Config


class StakeRequestError(ValueError):
    """A human-supplied stake was invalid or tried to raise the risk plan."""


@dataclass
class StakePlan:
    stake: float
    kelly_full: float
    kelly_used: float
    capped_by: str
    ev_amount: float

    @property
    def ok(self) -> bool:
        return self.stake > 0


def kelly_fraction(prob: float, odds: float) -> float:
    """Kelly stake as a fraction of bankroll for a single bet."""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    f = (prob * b - (1.0 - prob)) / b
    return max(0.0, f)


def plan_stake(
    prob: float,
    odds: float,
    bankroll: float,
    cfg: Config,
    already_staked_today: float = 0.0,
) -> StakePlan:
    full = kelly_fraction(prob, odds)
    used = full * cfg.kelly_fraction

    if cfg.fixed_stake_enabled:
        # Explicit user-controlled mode: stake the chosen amount, still subject
        # to the same hard per-bet and daily caps. No Kelly dependency.
        raw = float(cfg.fixed_stake)
        capped_by = "fixed_stake"
    else:
        raw = bankroll * used
        capped_by = "kelly"
    per_bet_cap = bankroll * cfg.max_stake_pct
    if raw > per_bet_cap:
        raw, capped_by = per_bet_cap, "max_stake_pct"

    daily_cap = bankroll * cfg.daily_stake_cap_pct
    remaining = max(0.0, daily_cap - already_staked_today)
    if raw > remaining:
        raw, capped_by = remaining, "daily_stake_cap"

    stake = round(raw, 2)
    if stake < cfg.min_stake:
        # tiny Kelly edge means the model barely believes it; skip rather than
        # round up to the minimum, which would silently over-stake.
        return StakePlan(0.0, full, used, "below_min_stake", 0.0)

    return StakePlan(
        stake=stake,
        kelly_full=full,
        kelly_used=used,
        capped_by=capped_by,
        ev_amount=stake * (prob * odds - 1.0),
    )


def apply_manual_stake(
    plan: StakePlan,
    requested: float | str | None,
    cfg: Config,
) -> tuple[float, str]:
    """Apply a human stake request without ever raising the Kelly plan.

    Returns ``(stake, note)``. A request may only lower the planned stake;
    raising bankroll/caps in settings is the explicit way to bet more.
    """
    if requested is None:
        return plan.stake, ""

    try:
        value = float(requested)
    except (TypeError, ValueError):
        raise StakeRequestError("stake must be a number") from None
    if not isfinite(value) or value <= 0:
        raise StakeRequestError("stake must be a positive number")

    stake = round(min(value, plan.stake), 2)
    if stake < cfg.min_stake:
        raise StakeRequestError(f"stake below the {cfg.min_stake:.2f} minimum")

    note = ""
    if value > plan.stake:
        note = (f"Stake capped at the Kelly plan {plan.stake:.2f} "
                f"(requested {value:.2f}).")
    return stake, note
