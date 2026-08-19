"""Signal journal: persistence, de-duplication, and outcome tracking.

WHY THIS EXISTS
The confluence gates (72/75/78) are informed guesses. They only become knowledge
if every signal is stored with its full score breakdown and then resolved
against what price actually did. This table is the feedback loop; without it the
engine can never be tuned by anything better than opinion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ofsignals.logging_setup import get_logger
from ofsignals.strategy.base import Signal
from ofsignals.types import Direction, iso, utcnow

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    generated_at     TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    direction        TEXT NOT NULL,
    mode             TEXT NOT NULL,
    entry_high       REAL NOT NULL,
    entry_low        REAL NOT NULL,
    stop_loss        REAL NOT NULL,
    tp1              REAL NOT NULL,
    tp2              REAL NOT NULL,
    tp3              REAL NOT NULL,
    rr1              REAL DEFAULT 0,
    rr2              REAL NOT NULL,
    rr3              REAL DEFAULT 0,
    leverage         INTEGER NOT NULL,
    sl_distance_pct  REAL NOT NULL,
    score            INTEGER NOT NULL,
    score_breakdown  TEXT NOT NULL,
    rationale        TEXT NOT NULL,
    tags             TEXT NOT NULL,
    price_at_signal  REAL NOT NULL,
    valid_until      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    filled_at        TEXT,
    resolved_at      TEXT,
    outcome          TEXT,
    max_favourable_r REAL DEFAULT 0,
    max_adverse_r    REAL DEFAULT 0,
    realised_r       REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_mode ON signals(symbol, mode, generated_at);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(score);
"""

OPEN_STATES = ("pending", "filled", "tp1", "tp2")


@dataclass(slots=True)
class OpenSignal:
    signal_id: str
    symbol: str
    mode: str
    direction: Direction
    entry_high: float
    entry_low: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    status: str
    valid_until: str
    generated_at: str
    max_favourable_r: float
    max_adverse_r: float
    rr1: float = 0.0
    rr2: float = 0.0
    rr3: float = 0.0
    realised_r: float = 0.0

    @property
    def risk(self) -> float:
        return abs((self.entry_high + self.entry_low) / 2.0 - self.stop_loss)


class SignalStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()
        log.info("store_ready", path=str(self._path))

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        assert self._db is not None
        for column, ddl in (("rr1", "REAL DEFAULT 0"), ("rr3", "REAL DEFAULT 0"),
                            ("realised_r", "REAL DEFAULT 0")):
            try:
                await self._db.execute(
                    f"ALTER TABLE signals ADD COLUMN {column} {ddl}")
            except Exception:  # noqa: BLE001 - column already present
                pass
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------ writing
    async def insert(self, signal: Signal) -> None:
        assert self._db is not None
        await self._db.execute(
            """INSERT OR IGNORE INTO signals
               (signal_id, generated_at, symbol, direction, mode, entry_high, entry_low,
                stop_loss, tp1, tp2, tp3, rr1, rr2, rr3, leverage, sl_distance_pct,
                score, score_breakdown, rationale, tags, price_at_signal, valid_until,
                status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.signal_id, signal.generated_at, signal.symbol,
                signal.direction.value, signal.mode,
                signal.entry_range[0], signal.entry_range[1], signal.stop_loss,
                signal.targets["tp1"], signal.targets["tp2"], signal.targets["tp3"],
                signal.rr["tp1"], signal.rr["tp2"], signal.rr["tp3"],
                int(signal.leverage["recommended"]),
                signal.sl_distance_pct, signal.confluence_score,
                json.dumps(signal.score_breakdown), json.dumps(signal.rationale),
                json.dumps(signal.tags), signal.price_at_signal, signal.valid_until,
                signal.status,
            ),
        )
        await self._db.commit()

    async def update_status(self, signal_id: str, status: str, outcome: str | None = None,
                            mfe: float | None = None, mae: float | None = None,
                            realised_delta: float = 0.0) -> None:
        assert self._db is not None
        fields = ["status = ?"]
        params: list[Any] = [status]
        if outcome is not None:
            fields += ["outcome = ?", "resolved_at = ?"]
            params += [outcome, iso(utcnow())]
        if status == "filled":
            fields.append("filled_at = COALESCE(filled_at, ?)")
            params.append(iso(utcnow()))
        if mfe is not None:
            fields.append("max_favourable_r = MAX(max_favourable_r, ?)")
            params.append(mfe)
        if mae is not None:
            fields.append("max_adverse_r = MAX(max_adverse_r, ?)")
            params.append(mae)
        if realised_delta:
            fields.append("realised_r = realised_r + ?")
            params.append(realised_delta)
        params.append(signal_id)
        await self._db.execute(
            f"UPDATE signals SET {', '.join(fields)} WHERE signal_id = ?", params)
        await self._db.commit()

    # ------------------------------------------------------------ reading
    async def in_cooldown(self, symbol: str, mode: str, minutes: int) -> bool:
        assert self._db is not None
        cutoff = iso(utcnow() - timedelta(minutes=minutes))
        async with self._db.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND mode=? AND generated_at>? LIMIT 1",
            (symbol, mode, cutoff),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def count_since(self, minutes: int) -> int:
        assert self._db is not None
        cutoff = iso(utcnow() - timedelta(minutes=minutes))
        async with self._db.execute(
            "SELECT COUNT(*) FROM signals WHERE generated_at > ?", (cutoff,)) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def last_signal_at(self) -> datetime | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT generated_at FROM signals ORDER BY generated_at DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    async def open_signals(self) -> list[OpenSignal]:
        assert self._db is not None
        placeholders = ",".join("?" * len(OPEN_STATES))
        async with self._db.execute(
            f"SELECT * FROM signals WHERE status IN ({placeholders})", OPEN_STATES
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            OpenSignal(
                signal_id=row["signal_id"], symbol=row["symbol"], mode=row["mode"],
                direction=Direction(row["direction"]),
                entry_high=row["entry_high"], entry_low=row["entry_low"],
                stop_loss=row["stop_loss"], tp1=row["tp1"], tp2=row["tp2"], tp3=row["tp3"],
                status=row["status"], valid_until=row["valid_until"],
                generated_at=row["generated_at"],
                max_favourable_r=row["max_favourable_r"] or 0.0,
                max_adverse_r=row["max_adverse_r"] or 0.0,
                rr1=row["rr1"] or 0.0, rr2=row["rr2"] or 0.0, rr3=row["rr3"] or 0.0,
                realised_r=row["realised_r"] or 0.0,
            )
            for row in rows
        ]

    async def open_directions(self) -> list[tuple[str, str]]:
        """(symbol, direction) for every live signal — feeds the correlation guard."""
        assert self._db is not None
        placeholders = ",".join("?" * len(OPEN_STATES))
        async with self._db.execute(
            f"SELECT symbol, direction FROM signals WHERE status IN ({placeholders})",
            OPEN_STATES,
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def performance(self, days: int = 30) -> dict[str, Any]:
        """Outcome summary bucketed by score band — the tuning input."""
        assert self._db is not None
        cutoff = iso(utcnow() - timedelta(days=days))
        async with self._db.execute(
            """SELECT
                 CASE WHEN score >= 85 THEN '85+'
                      WHEN score >= 80 THEN '80-84'
                      WHEN score >= 75 THEN '75-79'
                      ELSE '<75' END AS band,
                 COUNT(*) AS n,
                 SUM(CASE WHEN outcome LIKE 'tp%' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome = 'stopped' THEN 1 ELSE 0 END) AS losses,
                 ROUND(AVG(max_favourable_r), 2) AS avg_mfe,
                 ROUND(AVG(max_adverse_r), 2) AS avg_mae
               FROM signals
               WHERE generated_at > ? AND outcome IS NOT NULL
               GROUP BY band ORDER BY band DESC""",
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["band"]: dict(row) for row in rows}


    async def realised_summary(self, days: int = 30) -> dict[str, Any]:
        """Booked R plus win/loss counts over the window."""
        assert self._db is not None
        cutoff = iso(utcnow() - timedelta(days=days))
        async with self._db.execute(
            """SELECT
                 COALESCE(SUM(realised_r), 0)                                AS realised_r,
                 SUM(CASE WHEN realised_r > 0 THEN 1 ELSE 0 END)             AS wins,
                 SUM(CASE WHEN realised_r < 0 THEN 1 ELSE 0 END)             AS losses,
                 COUNT(*)                                                    AS n
               FROM signals
               WHERE generated_at > ? AND outcome IS NOT NULL""",
            (cutoff,),
        ) as cursor:
            row = await cursor.fetchone()
        return {
            "realised_r": float(row["realised_r"] or 0.0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "n": int(row["n"] or 0),
        }


# ------------------------------------------------------------------ tracking
def evaluate_progress(signal: OpenSignal, price: float) -> tuple[str | None, str | None]:
    """Return (new_status, terminal_outcome) for a live signal at `price`."""
    long = signal.direction is Direction.LONG
    hit = (lambda level: price >= level) if long else (lambda level: price <= level)
    stopped = (price <= signal.stop_loss) if long else (price >= signal.stop_loss)

    if signal.status == "pending":
        in_range = signal.entry_low <= price <= signal.entry_high
        if in_range:
            return "filled", None
        if stopped:
            return "invalidated", "invalidated_before_fill"
        if datetime.strptime(signal.valid_until, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc) < utcnow():
            return "expired", "expired_unfilled"
        return None, None

    if stopped:
        # After TP1 the stop has been moved to break-even, so this is a scratch.
        return ("stopped", "stopped") if signal.status == "filled" else ("closed_be", "breakeven")

    if signal.status == "filled" and hit(signal.tp1):
        return "tp1", None
    if signal.status in ("filled", "tp1") and hit(signal.tp2):
        return "tp2", None
    if signal.status in ("filled", "tp1", "tp2") and hit(signal.tp3):
        return "tp3", "tp3"
    return None, None


def realised_delta(signal: OpenSignal, new_status: str,
                   allocation: dict[str, float] | None = None) -> float:
    """R booked by this transition, weighted by the TP allocation ladder."""
    allocation = allocation or {"tp1": 0.40, "tp2": 0.35, "tp3": 0.25}
    if new_status == "tp1":
        return allocation["tp1"] * signal.rr1
    if new_status == "tp2":
        return allocation["tp2"] * signal.rr2
    if new_status == "tp3":
        return allocation["tp3"] * signal.rr3
    if new_status == "stopped":
        return -1.0                     # full size stopped before any target
    return 0.0                          # break-even exit on the remainder


def excursion_r(signal: OpenSignal, price: float) -> tuple[float, float]:
    risk = signal.risk
    if risk <= 0:
        return 0.0, 0.0
    entry = (signal.entry_high + signal.entry_low) / 2.0
    move = (price - entry) * signal.direction.sign
    return max(move / risk, 0.0), max(-move / risk, 0.0)
