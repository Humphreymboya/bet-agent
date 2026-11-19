"""Encrypted state persistence for the cloud runner.

The GitHub repo can be public (unlimited free Actions minutes), so thebetPawa
session and the SQLite ledger must never be committed in plain text.  This
module packs them into AES-256-CBC blobs with PBKDF2 using the openssl binary
that already ships on GitHub runners.

Usage:
    python cloud/state.py pack      # data/*  -> state/*.enc (before push)
    python cloud/state.py unpack    # state/*.enc -> data/*  (after checkout)

The passphrase comes only from the STATE_PASSPHRASE environment variable.
Keep it in a GitHub Secret; nothing here writes it to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# (plain file, encrypted file) - both relative to the project root.
PAIRS: tuple[tuple[str, str], ...] = (
    ("data/betpawa-session.json", "state/betpawa-session.json.enc"),
    ("data/bets-betpawa.db", "state/bets-betpawa.db.enc"),
)
MANIFEST = "state/manifest.json"
ITERATIONS = 210_000


def openssl_path(explicit: str | None = None) -> str | None:
    """Find an openssl executable on PATH or in common Windows locations."""
    if explicit:
        return explicit
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _crypt(src: Path, dst: Path, passphrase: str, *, decrypt: bool,
           openssl: str | None = None) -> None:
    binary = openssl_path(openssl)
    if not binary:
        raise RuntimeError("openssl not found; install it or pass --openssl PATH")
    if not passphrase:
        raise RuntimeError("STATE_PASSPHRASE is empty")
    if not decrypt and not src.exists():
        raise FileNotFoundError(f"nothing to encrypt: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "enc", "-aes-256-cbc", "-pbkdf2", "-iter",
           str(ITERATIONS), "-salt"]
    if decrypt:
        cmd.append("-d")
    cmd += ["-in", str(src), "-out", str(dst), "-pass", "env:STATE_PASSPHRASE"]
    env = dict(os.environ)
    env["STATE_PASSPHRASE"] = passphrase
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown openssl error").strip()
        raise RuntimeError(f"openssl failed for {src.name}: {detail}")


def pack(root: Path | str = ".", passphrase: str | None = None,
         openssl: str | None = None) -> dict:
    """Encrypt every existing plain state file and write a manifest."""
    base = Path(root)
    secret = passphrase if passphrase is not None else os.environ.get("STATE_PASSPHRASE", "")
    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cipher": f"aes-256-cbc/pbkdf2/{ITERATIONS}",
        "files": [],
    }
    for plain_rel, enc_rel in PAIRS:
        src = base / plain_rel
        dst = base / enc_rel
        if not src.exists():
            continue
        _crypt(src, dst, secret, decrypt=False, openssl=openssl)
        manifest["files"].append({
            "source": plain_rel,
            "blob": enc_rel,
            "bytes": src.stat().st_size,
            "sha256": _sha256(dst),
        })
    (base / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (base / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def unpack(root: Path | str = ".", passphrase: str | None = None,
           openssl: str | None = None) -> list[Path]:
    """Restore plain state files from the encrypted blobs."""
    base = Path(root)
    secret = passphrase if passphrase is not None else os.environ.get("STATE_PASSPHRASE", "")
    manifest_path = base / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    expected = {row["blob"]: row.get("sha256") for row in manifest.get("files", [])}

    restored: list[Path] = []
    for plain_rel, enc_rel in PAIRS:
        src = base / enc_rel
        if not src.exists():
            continue
        checksum = expected.get(enc_rel)
        if checksum and _sha256(src) != checksum:
            raise RuntimeError(f"state blob failed checksum: {enc_rel}")
        dst = base / plain_rel
        _crypt(src, dst, secret, decrypt=True, openssl=openssl)
        restored.append(dst)
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "unpack"))
    parser.add_argument("--root", default=".", help="project root (default: .)")
    parser.add_argument("--openssl", default=None, help="openssl binary path")
    args = parser.parse_args(argv)

    try:
        if args.action == "pack":
            result = pack(args.root, openssl=args.openssl)
            print(f"packed {len(result['files'])} state file(s) -> {MANIFEST}")
        else:
            files = unpack(args.root, openssl=args.openssl)
            print(f"restored {len(files)} state file(s)")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should explain any failure
        print(f"STATE ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
