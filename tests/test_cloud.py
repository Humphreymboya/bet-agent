"""Tests for the GitHub Actions cloud runner. Stdlib only."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from betbot.config import load_config
from betbot.util import iso, utcnow
from betbot.webapp import LIVE_CONFIRM
from cloud import shift, state
from cloud.override_config import apply_overrides

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.github.json"


class TestCloudConfig(unittest.TestCase):
    def test_github_config_enables_live_autoplace_fixed_stake(self):
        cfg = load_config(CONFIG)
        self.assertEqual(cfg.mode, "live")
        self.assertEqual(cfg.execution, "betpawa")
        self.assertTrue(cfg.auto_place_enabled)
        self.assertFalse(cfg.betpawa_dry_run)
        self.assertTrue(cfg.fixed_stake_enabled)
        self.assertEqual(cfg.fixed_stake, 200.0)
        self.assertFalse(cfg.allow_zero_edge)
        self.assertEqual(cfg.venue_book, "betpawa")

    def test_live_confirmation_is_required_for_autoplace(self):
        cfg = load_config(CONFIG)
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "session.json"
            session.write_text("{}", encoding="utf-8")
            cfg.betpawa_session_path = str(session)

            self.assertEqual(shift.prepare(cfg, LIVE_CONFIRM), [])
            problems = shift.prepare(cfg, "wrong text")
            self.assertTrue(any("LIVE_CONFIRM" in p for p in problems))


class TestQuotaAwareCadence(unittest.TestCase):
    def test_intervals_back_off_as_credits_run_out(self):
        self.assertEqual(shift.quota_interval(None, 20, 60), 60)
        self.assertEqual(shift.quota_interval(500, 20, 60), 60)
        self.assertEqual(shift.quota_interval(100, 20, 60), 120)
        self.assertEqual(shift.quota_interval(40, 20, 60), 240)
        self.assertIsNone(shift.quota_interval(20, 20, 60))
        self.assertIsNone(shift.quota_interval(3, 20, 60))

    def test_due_for_scan_respects_last_scan(self):
        now = utcnow()
        self.assertTrue(shift.due_for_scan({}, 60, now))
        recent = {"last_scan": iso(now - timedelta(minutes=10))}
        self.assertFalse(shift.due_for_scan(recent, 60, now))
        old = {"last_scan": iso(now - timedelta(minutes=61))}
        self.assertTrue(shift.due_for_scan(old, 60, now))
        self.assertFalse(shift.due_for_scan(old, None, now))

    def test_empty_shift_does_not_touch_the_venue(self):
        called = []
        summary = shift.run_shift(
            load_config(CONFIG), LIVE_CONFIRM, seconds=0,
            base_interval_minutes=60, min_quota=20,
            place=lambda *a, **k: called.append(1),
        )
        self.assertEqual(called, [])
        self.assertEqual(summary["scans"], 0)

    def test_one_scan_per_tick_uses_shared_placer(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            last_run = Path(folder) / "last-run.json"
            monotonic = iter([0, 10, 100])

            with patch.object(shift.theoddsapi, "LAST_QUOTA", "123"):
                summary = shift.run_shift(
                    load_config(CONFIG), LIVE_CONFIRM, seconds=50,
                    base_interval_minutes=60, min_quota=20,
                    status_path=str(status), last_run_path=str(last_run),
                    place=lambda *a, **k: {"ok": True, "message": "bet #1",
                                           "status": "accepted"},
                    sleep=lambda _: None,
                    deadline_fn=lambda: next(monotonic),
                )

            self.assertEqual(summary["scans"], 1)
            self.assertEqual(summary["bets"], 1)
            self.assertEqual(summary["quota"], "123")
            self.assertEqual(shift.load_status(status)["quota_remaining"], 123)
            self.assertTrue(last_run.exists())


class TestPhoneOverrides(unittest.TestCase):
    def test_stake_change_and_pause_are_persisted(self):
        import json

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.github.json"
            target.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            changes = apply_overrides(target, fixed_stake="250", auto_place="off")
            self.assertEqual(len(changes), 2)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["fixed_stake"], 250.0)
            self.assertTrue(raw["fixed_stake_enabled"])
            self.assertFalse(raw["auto_place_enabled"])

    def test_blank_and_keep_inputs_change_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.github.json"
            original = CONFIG.read_text(encoding="utf-8")
            target.write_text(original, encoding="utf-8")
            self.assertEqual(apply_overrides(target, "", "keep"), [])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_bad_stake_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.github.json"
            original = CONFIG.read_text(encoding="utf-8")
            target.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError):
                apply_overrides(target, fixed_stake="abc", auto_place="")
            self.assertEqual(target.read_text(encoding="utf-8"), original)


class TestEncryptedState(unittest.TestCase):
    def test_pack_unpack_round_trip(self):
        binary = state.openssl_path()
        if not binary:
            self.skipTest("openssl is not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payloads = {}
            for plain, _ in state.PAIRS:
                target = root / plain
                target.parent.mkdir(parents=True, exist_ok=True)
                data = (b"session-bytes" if plain.endswith(".json")
                        else bytes(range(256)))
                target.write_bytes(data)
                payloads[plain] = data

            manifest = state.pack(root, passphrase="unit-test-passphrase", openssl=binary)
            self.assertEqual(len(manifest["files"]), 2)
            for plain, _ in state.PAIRS:
                (root / plain).unlink()

            restored = state.unpack(root, passphrase="unit-test-passphrase", openssl=binary)
            self.assertEqual(len(restored), 2)
            for plain, expected in payloads.items():
                self.assertEqual((root / plain).read_bytes(), expected)

    def test_wrong_passphrase_fails_instead_of_returning_garbage(self):
        binary = state.openssl_path()
        if not binary:
            self.skipTest("openssl is not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plain, enc = state.PAIRS[0]
            src = root / plain
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(b"sensitive")
            state.pack(root, passphrase="right", openssl=binary)
            src.unlink()
            with self.assertRaises(RuntimeError):
                state.unpack(root, passphrase="wrong", openssl=binary)


if __name__ == "__main__":
    unittest.main()
