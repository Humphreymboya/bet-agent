"""Apply optional phone overrides from workflow_dispatch inputs.

Changes are written back to config.github.json, so a stake change or an
auto-place pause persists into later scheduled shifts. No input = no change.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


def apply_overrides(config_path: Path | str,
                    fixed_stake: str | None = None,
                    auto_place: str | None = None) -> list[str]:
    target = Path(config_path)
    raw = json.loads(target.read_text(encoding="utf-8-sig"))
    changed: list[str] = []

    stake = (fixed_stake or "").strip()
    if stake:
        try:
            value = float(stake)
        except ValueError as exc:
            raise ValueError(f"fixed_stake must be a number, got {stake!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("fixed_stake must be a positive number")
        raw["fixed_stake"] = value
        raw["fixed_stake_enabled"] = True
        changed.append(f"fixed_stake={value:g} TZS")

    setting = (auto_place or "").strip().lower()
    if setting in {"on", "off"}:
        enabled = setting == "on"
        raw["auto_place_enabled"] = enabled
        changed.append(f"auto_place={'ON' if enabled else 'OFF'}")

    if changed:
        target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.github.json")
    args = parser.parse_args(argv)

    try:
        changes = apply_overrides(
            args.config,
            fixed_stake=os.environ.get("FIXED_STAKE", ""),
            auto_place=os.environ.get("AUTO_PLACE", ""),
        )
    except (OSError, ValueError) as exc:
        print(f"OVERRIDE ERROR: {exc}")
        return 1
    print("overrides applied: " + (", ".join(changes) if changes else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
