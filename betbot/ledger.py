"""SQLite ledger. Every bet and every leg is persisted before money moves."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from math import prod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .model import Combo
from .util import iso, iso_day, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    mode        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    execution   TEXT NOT NULL,
    stake       REAL NOT NULL,
    odds        REAL NOT NULL,
    actual_odds REAL,
    joint_prob  REAL NOT NULL,
    ev          REAL NOT NULL,
    status      TEXT NOT NULL,
    pnl         REAL,
    settled_at  TEXT,
    broker_ref  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS legs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id         INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
    event_id       TEXT NOT NULL,
    commence_time  TEXT NOT NULL,
    league         TEXT NOT NULL,
    match          TEXT NOT NULL,
    market         TEXT NOT NULL,
    selection      TEXT NOT NULL,
    price          REAL NOT NULL,
    actual_price   REAL,
    book           TEXT NOT NULL,
    fair_prob      REAL NOT NULL,
    edge           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bets_created ON bets(created_at);
CREATE INDEX IF NOT EXISTS idx_bets_status  ON bets(status);
CREATE INDEX IF NOT EXISTS idx_legs_bet      ON legs(bet_id);
"""


@dataclass
class BetRecord:
    id: int
    created_at: str
    mode: str
    stake: float
    odds: float
    actual_odds: float | None
    joint_prob: float
    ev: float
    status: str
    pnl: float | None
    broker_ref: str | None
    legs: list[dict]
    note: str | None = None
    provider: str = ""
    execution: str = ""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS does not add columns to an old ledger.
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, column in (("bets", "actual_odds"), ("legs", "actual_price")):
            names = {row["name"] for row in
                     conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in names:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL")

    # -- writing -------------------------------------------------------
    def add_bet(
        self,
        combo: Combo,
        stake: float,
        mode: str,
        provider: str,
        execution: str,
        broker_ref: str = "",
        note: str = "",
        status: str = "open",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO bets (created_at, mode, provider, execution, stake, odds,
                                     joint_prob, ev, status, broker_ref, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    iso(utcnow()),
                    mode,
                    provider,
                    execution,
                    stake,
                    combo.odds,
                    combo.prob,
                    combo.ev,
                    status,
                    broker_ref,
                    note,
                ),
            )
            bet_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO legs (bet_id, event_id, commence_time, league, match, market,
                                     selection, price, book, fair_prob, edge)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        bet_id,
                        leg.event_id,
                        iso(leg.commence_time),
                        leg.league,
                        leg.match,
                        leg.market,
                        leg.selection,
                        leg.price,
                        leg.book,
                        leg.fair_prob,
                        leg.edge,
                    )
                    for leg in combo.legs
                ],
            )
        return bet_id

    def set_ref(self, bet_id: int, broker_ref: str, status: str = "accepted") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bets SET broker_ref=?, status=? WHERE id=?",
                (broker_ref, status, bet_id),
            )

    def settle(self, bet_id: int, result: str,
               prices: list[float] | None = None) -> float:
        """Settle a bet, optionally recording the prices actually taken.

        Slippage means the scanned odds can differ from the slip. Passing the
        real per-leg prices keeps P/L honest and keeps the audit trail intact:
        `odds` stays the scanned price, `actual_odds` records what was taken.
        """
        result = result.lower()
        if result not in {"win", "loss", "void"}:
            raise ValueError("result must be win, loss or void")
        with self._conn() as conn:
            row = conn.execute("SELECT stake, odds, status FROM bets WHERE id=?", (bet_id,)).fetchone()
            if row is None:
                raise ValueError(f"bet {bet_id} not found")
            if row["status"] in {"win", "loss", "void"}:
                raise ValueError(f"bet {bet_id} already settled as {row['status']}")

            leg_rows = conn.execute(
                "SELECT id FROM legs WHERE bet_id=? ORDER BY id", (bet_id,)
            ).fetchall()
            if prices is not None:
                if len(prices) != len(leg_rows):
                    raise ValueError(
                        f"got {len(prices)} price(s) but this bet has {len(leg_rows)} leg(s)"
                    )
                clean: list[float] = []
                for value in prices:
                    try:
                        price = float(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"invalid price: {value!r}") from exc
                    if price <= 1.0:
                        raise ValueError(f"price must be > 1.0, got {price}")
                    clean.append(price)
                actual_odds = round(prod(clean), 6)
                conn.executemany(
                    "UPDATE legs SET actual_price=? WHERE id=?",
                    [(price, leg["id"]) for price, leg in zip(clean, leg_rows)],
                )
            else:
                actual_odds = float(row["odds"])

            stake = float(row["stake"])
            pnl = (stake * (actual_odds - 1.0) if result == "win"
                   else (-stake if result == "loss" else 0.0))
            conn.execute(
                "UPDATE bets SET status=?, pnl=?, settled_at=?, actual_odds=? WHERE id=?",
                (result, round(pnl, 2), iso(utcnow()), actual_odds, bet_id),
            )
        return round(pnl, 2)

    # -- reading -------------------------------------------------------
    def open_bets(self, mode: str | None = None) -> list[BetRecord]:
        query = "SELECT * FROM bets WHERE status IN ('open','accepted','pending','pending_user')"
        params: list = []
        if mode:
            query += " AND mode=?"
            params.append(mode)
        query += " ORDER BY id DESC"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._hydrate(conn, r) for r in rows]

    def get_bet(self, bet_id: int) -> BetRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
            return self._hydrate(conn, row) if row else None

    def recent_bets(self, mode: str | None = None, limit: int = 12) -> list[BetRecord]:
        query, params = "SELECT * FROM bets", []
        if mode:
            query += " WHERE mode=?"
            params.append(mode)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._hydrate(conn, r) for r in rows]

    def _hydrate(self, conn: sqlite3.Connection, row: sqlite3.Row) -> BetRecord:
        legs = conn.execute("SELECT * FROM legs WHERE bet_id=? ORDER BY id", (row["id"],)).fetchall()
        return BetRecord(
            id=row["id"],
            created_at=row["created_at"],
            mode=row["mode"],
            stake=row["stake"],
            odds=row["odds"],
            actual_odds=row["actual_odds"],
            joint_prob=row["joint_prob"],
            ev=row["ev"],
            status=row["status"],
            pnl=row["pnl"],
            broker_ref=row["broker_ref"],
            legs=[dict(leg) for leg in legs],
            note=row["note"],
            provider=row["provider"],
            execution=row["execution"],
        )

    def staked_today(self, mode: str) -> float:
        today = iso_day(utcnow())
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(stake), 0) AS total FROM bets
                   WHERE mode=? AND substr(created_at, 1, 10)=?
                     AND status NOT IN ('rejected','failed','cancelled','dry_run')""",
                (mode, today),
            ).fetchone()
        return float(row["total"])

    def bets_today(self, mode: str) -> int:
        today = iso_day(utcnow())
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM bets
                   WHERE mode=? AND substr(created_at, 1, 10)=?
                     AND status NOT IN ('rejected','failed','cancelled','dry_run')""",
                (mode, today),
            ).fetchone()
        return int(row["n"])

    def realised_pnl(self, mode: str, days: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(pnl), 0) AS total FROM bets WHERE mode=? AND pnl IS NOT NULL"
        params: list = [mode]
        if days:
            query += " AND created_at >= ?"
            params.append(iso(utcnow().replace(hour=0, minute=0, second=0, microsecond=0)))
        with self._conn() as conn:
            row = conn.execute(query, params).fetchone()
        return float(row["total"])

    def summary(self, mode: str) -> dict:
        with self._conn() as conn:
            agg = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(stake), 0) AS staked,
                          COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) AS pnl,
                          SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN status='loss' THEN 1 ELSE 0 END) AS losses,
                          SUM(CASE WHEN status='void' THEN 1 ELSE 0 END) AS voids,
                          SUM(CASE WHEN status IN ('open','accepted','pending','pending_user') THEN 1 ELSE 0 END) AS open
                   FROM bets WHERE mode=?""",
                (mode,),
            ).fetchone()
            avg = conn.execute(
                "SELECT AVG(odds) AS avg FROM bets WHERE mode=? AND pnl IS NOT NULL", (mode,)
            ).fetchone()
        settled = (agg["wins"] or 0) + (agg["losses"] or 0) + (agg["voids"] or 0)
        staked = float(agg["staked"] or 0.0)
        pnl = float(agg["pnl"] or 0.0)
        return {
            "bets": int(agg["n"] or 0),
            "settled": settled,
            "open": int(agg["open"] or 0),
            "wins": int(agg["wins"] or 0),
            "losses": int(agg["losses"] or 0),
            "voids": int(agg["voids"] or 0),
            "staked": staked,
            "pnl": pnl,
            "roi": (pnl / staked) if staked else 0.0,
            "avg_odds": float(avg["avg"] or 0.0),
        }

    def by_day(self, mode: str, limit: int = 30) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT substr(created_at, 1, 10) AS day,
                          COUNT(*) AS n,
                          SUM(stake) AS staked,
                          COALESCE(SUM(pnl), 0) AS pnl
                   FROM bets WHERE mode=?
                   GROUP BY day ORDER BY day DESC LIMIT ?""",
                (mode, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def now_iso() -> str:
    return iso(utcnow())


def parse_bet_time(value: str) -> datetime:
    from .util import parse_iso

    return parse_iso(value)
