"""Limites de risco no nível da conta (perda diária, trades/dia e kill-switch)."""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from core.logging_setup import BASE_DIR

KILL_SWITCH_PATH = os.path.join(BASE_DIR, "STOP")


class RiskManager:
    def __init__(
        self,
        db,
        mode: str,
        max_daily_loss_usdt: float,
        max_trades_per_day: int,
        max_trade_amount_usdt: float,
        kill_switch_path: str = KILL_SWITCH_PATH,
    ):
        self.db = db
        self.mode = mode
        self.max_daily_loss_usdt = abs(max_daily_loss_usdt)
        self.max_trades_per_day = max_trades_per_day
        self.max_trade_amount_usdt = max_trade_amount_usdt
        self.kill_switch_path = kill_switch_path

    def _today_stats(self) -> Tuple[int, float]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.db._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(pnl_usd), 0.0) AS pnl
                FROM simulated_trades
                WHERE mode = ? AND substr(COALESCE(exit_time, entry_time), 1, 10) = ?
                """,
                (self.mode, today),
            ).fetchone()
        return int(row["n"] or 0), float(row["pnl"] or 0.0)

    def consecutive_losses(self, window_hours: Optional[float] = 24.0) -> int:
        """Conta quantas operações seguidas terminaram no prejuízo no modo atual (dentro da janela)."""
        with self.db._get_connection() as conn:
            if window_hours is not None and window_hours > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    "SELECT pnl_usd FROM simulated_trades WHERE mode = ? "
                    "AND COALESCE(exit_time, entry_time) >= ? ORDER BY id DESC LIMIT 10",
                    (self.mode, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT pnl_usd FROM simulated_trades WHERE mode = ? ORDER BY id DESC LIMIT 10",
                    (self.mode,),
                ).fetchall()
        count = 0
        for r in rows:
            if float(r["pnl_usd"] or 0.0) < 0:
                count += 1
            else:
                break
        return count

    def daily_pnl_usd(self) -> float:
        _, pnl = self._today_stats()
        return pnl

    def kill_switch_active(self) -> bool:
        return os.path.exists(self.kill_switch_path)

    def can_open(self, amount_usdt: float, unrealized_pnl_usd: float = 0.0) -> Tuple[bool, str]:
        if self.kill_switch_active():
            return False, f"Kill-switch ativo (arquivo '{self.kill_switch_path}' existe)."
        try:
            amt = float(amount_usdt)
            unr = float(unrealized_pnl_usd)
        except (TypeError, ValueError):
            return False, "Valor de ordem ou PnL flutuante inválido."
        if not (math.isfinite(amt) and amt > 0.0):
            return False, f"Valor por ordem inválido (${amount_usdt})."
        if not math.isfinite(unr):
            return False, "PnL flutuante inválido (NaN/Inf)."
        if amt > self.max_trade_amount_usdt:
            return False, (
                f"Valor por ordem ${amt:.2f} acima do máximo permitido "
                f"${self.max_trade_amount_usdt:.2f} (MAX_TRADE_AMOUNT_USDT)."
            )
        trades_today, pnl_today = self._today_stats()
        if trades_today >= self.max_trades_per_day:
            return False, f"Limite diário de trades atingido ({trades_today}/{self.max_trades_per_day})."
        effective_pnl = pnl_today + min(0.0, unr)
        if effective_pnl <= -self.max_daily_loss_usdt:
            return False, (
                f"Limite de perda diária atingido (PnL efetivo hoje ${effective_pnl:+.2f} | "
                f"limite -${self.max_daily_loss_usdt:.2f})."
            )
        return True, "OK"
