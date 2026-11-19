"""Materialise .env from environment variables supplied by GitHub Secrets.

Keeping this in a tiny script avoids quoting secrets inside workflow YAML and
never prints their values. Existing variables are left alone by load_dotenv().
"""

from __future__ import annotations

import os
from pathlib import Path

NAMES = (
    "THE_ODDS_API_KEY",
    "LIVE_CONFIRM",
    "STATE_PASSPHRASE",
    "OPENAI_API_KEY",
)


def write_env(root: Path | str = ".") -> Path:
    target = Path(root) / ".env"
    lines = []
    for name in NAMES:
        value = os.environ.get(name, "").strip()
        if value and "\n" not in value and "\r" not in value:
            # Simple key=value; load_dotenv() strips the outer quotes.
            lines.append(f"{name}={value}")
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


if __name__ == "__main__":
    path = write_env()
    print(f"wrote {path} ({len(path.read_text(encoding='utf-8').splitlines())} secret(s))")
