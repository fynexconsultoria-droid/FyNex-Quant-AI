"""Persistência SQLite de posições abertas (sobrevive a reinícios/crashes)."""

from __future__ import annotations

from dataclasses import asdict, fields as dataclass_fields
from typing import Optional

from core.infra.sqlite import ensure_column
from core.infra.utils import utc_now_str
from core.trading.models import Position

_POSITION_FIELD_NAMES = frozenset(f.name for f in dataclass_fields(Position))


class PositionStore:
    def __init__(self, db):
        self.db = db
        with self.db._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    order_id TEXT DEFAULT '',
                    sl_price REAL NOT NULL,
                    tp_price REAL NOT NULL,
                    oco_list_id TEXT DEFAULT '',
                    base_balance_before REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'OPEN',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, mode)
                );
            """)
            cursor = conn.cursor()
            ensure_column(cursor, "open_positions", "base_balance_before", "REAL DEFAULT 0.0")
            ensure_column(cursor, "open_positions", "status", "TEXT DEFAULT 'OPEN'")
            conn.commit()

    def save(self, pos: Position) -> None:
        data = asdict(pos)
        data["updated_at"] = utc_now_str()
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        with self.db._get_connection() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO open_positions ({cols}) VALUES ({marks})",
                tuple(data.values()),
            )
            conn.commit()

    def load(self, symbol: str, timeframe: str, mode: str) -> Optional[Position]:
        with self.db._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM open_positions WHERE symbol = ? AND timeframe = ? AND mode = ?",
                (symbol, timeframe, mode),
            ).fetchone()
        if not row:
            return None
        fields = {k: row[k] for k in row.keys() if k in _POSITION_FIELD_NAMES}
        return Position(**fields)

    def delete(self, symbol: str, timeframe: str, mode: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute(
                "DELETE FROM open_positions WHERE symbol = ? AND timeframe = ? AND mode = ?",
                (symbol, timeframe, mode),
            )
            conn.commit()
