"""
Copiloto XTB (Modo Híbrido — FyNex Quant AI)
============================================
Registra sinais de alta convicção gerados pela IA para Forex, Metais, Energia e Índices
no formato da xStation 5, reutilizando `core.notifications.telegram` e `core.infra.sqlite`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from core.infra.env_store import scrub_secrets
from core.infra.sqlite import SCHEMA_REGISTRY
from core.notifications.telegram import (
    DEFAULT_ENV_PATH as _DEFAULT_ENV_PATH,
    get_telegram_config,
    save_telegram_config,
    send_telegram_message,
)


def _scrub_telegram_error(err: Any, token: str = "") -> str:
    return scrub_secrets(err, (token,) if token else ())

logger = logging.getLogger("QuantTradingBot")

XTB_SYMBOL_MAP: Dict[str, str] = {
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "USD/BRL": "USDBRL",
    "AUD/USD": "AUDUSD",
    "USD/CHF": "USDCHF",
    "XAU/USD": "GOLD",
    "XAG/USD": "SILVER",
    "XPT/USD": "PLATINUM",
    "COPPER/USD": "COPPER",
    "OIL/USD": "OIL.WTI",
    "BRENT/USD": "OIL",
    "NATGAS/USD": "NATGAS",
    "REMX/USD": "REMX.US",
    "US500/USD": "US500",
    "US100/USD": "US100",
}

VALID_SIGNAL_STATUSES = ("NEW", "EXECUTED_XTB", "DISMISSED")


def to_xtb_symbol(symbol: str) -> str:
    """Converte símbolo interno (ex: XAU/USD) para o ticker usado na xStation 5 (ex: GOLD)."""
    key = (symbol or "").upper().strip()
    return XTB_SYMBOL_MAP.get(key, key.replace("/", ""))


def format_copilot_copy_text(sig: Dict[str, Any]) -> str:
    """Formata um sinal em texto limpo pronto para replicar na xStation 5."""
    side_pt = "COMPRA (BUY)" if sig.get("side") == "BUY" else "VENDA (SELL)"
    entry = float(sig.get("entry_price") or 0.0)
    sl = sig.get("sl_price")
    tp = sig.get("tp_price")
    dist_note = ""
    if entry > 0 and sl is not None and tp is not None:
        sl_pct = abs(float(sl) - entry) / entry * 100.0
        tp_pct = abs(float(tp) - entry) / entry * 100.0
        dist_note = f" (SL {sl_pct:.2f}% | TP {tp_pct:.2f}%)"
    return (
        f"XTB xStation 5 | {sig.get('xtb_symbol')} ({sig.get('symbol')})\n"
        f"Direção: {side_pt} | Score IA: {float(sig.get('score', 0)):.0f}/100\n"
        f"Entrada: {sig.get('entry_price')} | SL: {sig.get('sl_price')} | TP: {sig.get('tp_price')}{dist_note}\n"
        f"Lote sugerido: {sig.get('suggested_lots')} | Alavancagem: 1:{sig.get('leverage')}\n"
        f"Motivo: {sig.get('reason') or 'Confluência técnica + macro'}"
    )


class XTBCopilotStore:
    """Armazena e gerencia sinais do Copiloto XTB em SQLite (WAL)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        SCHEMA_REGISTRY.ensure_initialized("xtb_copilot", self.db_path, self._init_tables)

    @contextmanager
    def _connect(self, *, write_immediate: bool = False) -> Iterator[sqlite3.Connection]:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None if write_immediate else "")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000;")
        try:
            if write_immediate:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            else:
                with conn:
                    yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xtb_copilot_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    xtb_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    score REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    suggested_lots REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    telegram_sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_copilot_dedup
                ON xtb_copilot_signals(symbol, side, created_at)
                """
            )

    def record_signal(
        self,
        symbol: str,
        side: str,
        score: float,
        entry_price: float,
        sl_price: Optional[float],
        tp_price: Optional[float],
        suggested_lots: float,
        leverage: int,
        reason: str = "",
        notify_telegram: bool = True,
        dedup_minutes: int = 120,
    ) -> Dict[str, Any]:
        """Registra um novo sinal para o Copiloto XTB com deduplicação atômica por símbolo/direção."""
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_iso = (now_utc - timedelta(minutes=dedup_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        sym = symbol.upper().strip()
        xtb_sym = to_xtb_symbol(sym)
        side_u = side.upper().strip()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"Direção inválida para sinal XTB: '{side}'. Use 'BUY' ou 'SELL'.")

        with self._connect(write_immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT * FROM xtb_copilot_signals
                WHERE symbol = ? AND side = ? AND substr(created_at, 1, 19) >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (sym, side_u, cutoff_iso),
            ).fetchone()
            if existing is not None:
                d = dict(existing)
                d["copy_text"] = format_copilot_copy_text(d)
                d["deduplicated"] = True
                return d

            cur = conn.execute(
                """
                INSERT INTO xtb_copilot_signals (
                    created_at, symbol, xtb_symbol, side, score,
                    entry_price, sl_price, tp_price, suggested_lots,
                    leverage, reason, status, telegram_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', 0)
                """,
                (
                    now_iso, sym, xtb_sym, side_u, float(score),
                    float(entry_price),
                    float(sl_price) if sl_price is not None else None,
                    float(tp_price) if tp_price is not None else None,
                    float(suggested_lots), int(leverage),
                    reason or "Confluência técnica + macro",
                ),
            )
            sig_id = cur.lastrowid
            row = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (sig_id,)).fetchone()

        sig = dict(row)
        sig["copy_text"] = format_copilot_copy_text(sig)
        sig["deduplicated"] = False

        if notify_telegram and get_telegram_config()["configured"]:
            msg = f"🧭 SINAL COPILOTO XTB (FyNex AI)\n\n{sig['copy_text']}"
            tg_res = send_telegram_message(msg)
            if tg_res.get("sent"):
                with self._connect() as conn:
                    conn.execute("UPDATE xtb_copilot_signals SET telegram_sent = 1 WHERE id = ?", (sig_id,))
                sig["telegram_sent"] = 1
            else:
                logger.debug(f"[COPILOTO XTB] Alerta Telegram não enviado: {tg_res.get('error')}")

        return sig

    def list_signals(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM xtb_copilot_signals ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["copy_text"] = format_copilot_copy_text(d)
            out.append(d)
        return out

    def update_status(self, signal_id: int, status: str) -> Dict[str, Any]:
        st = (status or "").upper().strip()
        if st not in VALID_SIGNAL_STATUSES:
            raise ValueError(f"Status inválido: {status}. Use um de {VALID_SIGNAL_STATUSES}.")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (signal_id,)).fetchone()
            if row is None:
                raise ValueError(f"Sinal #{signal_id} não encontrado.")
            conn.execute("UPDATE xtb_copilot_signals SET status = ? WHERE id = ?", (st, signal_id))
            updated = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (signal_id,)).fetchone()
        d = dict(updated)
        d["copy_text"] = format_copilot_copy_text(d)
        return d


__all__ = [
    "XTB_SYMBOL_MAP",
    "VALID_SIGNAL_STATUSES",
    "_DEFAULT_ENV_PATH",
    "to_xtb_symbol",
    "get_telegram_config",
    "save_telegram_config",
    "format_copilot_copy_text",
    "send_telegram_message",
    "XTBCopilotStore",
]
