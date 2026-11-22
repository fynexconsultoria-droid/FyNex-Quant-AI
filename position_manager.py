"""
Gestão de Posição e Risco (FyNex Quant AI)
==========================================
- PositionStore: persiste a posição aberta no SQLite (sobrevive a reinícios/crashes).
- RiskManager: limites de risco no nível da conta (perda diária, trades/dia, kill-switch).
- PositionManager: único caminho para abrir/fechar posições (paper, testnet ou live),
  com venda robusta (retry, nunca zera a posição se a venda falhar) e proteção OCO
  do lado da exchange.
"""

import os
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

import alerts
import binance_client
from core.database import timeframe_to_seconds
from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT

logger = logging.getLogger("QuantTradingBot")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
KILL_SWITCH_PATH = os.path.join(BASE_DIR, "STOP")

# Se o preço cair este percentual ABAIXO do stop e a OCO ainda não executou
# (ex.: gap que pulou o stop-limit), o robô cancela a OCO e vende a mercado.
STOP_FALLBACK_EXTRA_PCT = 0.005


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Position:
    symbol: str
    timeframe: str
    mode: str
    model_version: int
    entry_time: str
    entry_price: float
    quantity: float
    order_id: str
    sl_price: float
    tp_price: float
    oco_list_id: str = ""
    base_balance_before: float = 0.0
    status: str = "OPEN"


# -----------------------------------------------------------------------------
# Persistência da posição
# -----------------------------------------------------------------------------
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
            for col_sql in [
                "ALTER TABLE open_positions ADD COLUMN base_balance_before REAL DEFAULT 0.0;",
                "ALTER TABLE open_positions ADD COLUMN status TEXT DEFAULT 'OPEN';",
            ]:
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass
            conn.commit()

    def save(self, pos: Position) -> None:
        data = asdict(pos)
        data["updated_at"] = utc_now_str()
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        with self.db._get_connection() as conn:
            conn.execute(f"INSERT OR REPLACE INTO open_positions ({cols}) VALUES ({marks})", tuple(data.values()))
            conn.commit()

    def load(self, symbol: str, timeframe: str, mode: str) -> Optional[Position]:
        with self.db._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM open_positions WHERE symbol = ? AND timeframe = ? AND mode = ?",
                (symbol, timeframe, mode)
            ).fetchone()
        if not row:
            return None
        fields = {k: row[k] for k in row.keys() if k != "updated_at"}
        return Position(**fields)

    def delete(self, symbol: str, timeframe: str, mode: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute(
                "DELETE FROM open_positions WHERE symbol = ? AND timeframe = ? AND mode = ?",
                (symbol, timeframe, mode)
            )
            conn.commit()


# -----------------------------------------------------------------------------
# Limites de risco
# -----------------------------------------------------------------------------
class RiskManager:
    def __init__(self, db, mode: str, max_daily_loss_usdt: float, max_trades_per_day: int,
                 max_trade_amount_usdt: float, kill_switch_path: str = KILL_SWITCH_PATH):
        self.db = db
        self.mode = mode
        self.max_daily_loss_usdt = abs(max_daily_loss_usdt)
        self.max_trades_per_day = max_trades_per_day
        self.max_trade_amount_usdt = max_trade_amount_usdt
        self.kill_switch_path = kill_switch_path

    def _today_stats(self) -> Tuple[int, float]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.db._get_connection() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(pnl_usd), 0.0) AS pnl
                FROM simulated_trades
                WHERE mode = ? AND substr(entry_time, 1, 10) = ?
            """, (self.mode, today)).fetchone()
        return int(row["n"] or 0), float(row["pnl"] or 0.0)

    def kill_switch_active(self) -> bool:
        return os.path.exists(self.kill_switch_path)

    def can_open(self, amount_usdt: float, unrealized_pnl_usd: float = 0.0) -> Tuple[bool, str]:
        if self.kill_switch_active():
            return False, f"Kill-switch ativo (arquivo '{self.kill_switch_path}' existe)."
        if amount_usdt > self.max_trade_amount_usdt:
            return False, (f"Valor por ordem ${amount_usdt:.2f} acima do máximo permitido "
                           f"${self.max_trade_amount_usdt:.2f} (MAX_TRADE_AMOUNT_USDT).")
        trades_today, pnl_today = self._today_stats()
        if trades_today >= self.max_trades_per_day:
            return False, f"Limite diário de trades atingido ({trades_today}/{self.max_trades_per_day})."
        effective_pnl = pnl_today + min(0.0, float(unrealized_pnl_usd))
        if effective_pnl <= -self.max_daily_loss_usdt:
            return False, (f"Limite de perda diária atingido (PnL efetivo hoje ${effective_pnl:+.2f} | "
                           f"limite -${self.max_daily_loss_usdt:.2f}).")
        return True, "OK"


# -----------------------------------------------------------------------------
# Gestão de posição
# -----------------------------------------------------------------------------
class PositionManager:
    def __init__(self, db, exchange, mode: str, symbol: str, timeframe: str,
                 stop_loss_pct: float, take_profit_pct: float, trade_amount_usdt: float,
                 use_exchange_oco: bool = True, risk: Optional[RiskManager] = None,
                 horizon_candles: Optional[int] = None):
        self.db = db
        self.exchange = exchange
        self.mode = mode
        self.symbol = symbol
        self.timeframe = timeframe
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trade_amount_usdt = trade_amount_usdt
        self.use_exchange_oco = use_exchange_oco and mode in ("live", "testnet")
        self.risk = risk
        self.horizon_candles = horizon_candles
        self.store = PositionStore(db)
        self.position: Optional[Position] = None
        self.consecutive_sell_failures = 0

    # ----------------------------------------------------------------- helpers
    @property
    def is_real(self) -> bool:
        return self.mode in ("live", "testnet")

    @property
    def in_position(self) -> bool:
        return self.position is not None

    def _persist(self) -> None:
        if self.position:
            self.store.save(self.position)

    def _clear(self) -> None:
        self.store.delete(self.symbol, self.timeframe, self.mode)
        self.position = None
        self.consecutive_sell_failures = 0

    def _set_order_in_flight(self, active: bool) -> None:
        setter = getattr(self.db, "set_order_in_flight", None)
        if callable(setter):
            try:
                setter(active)
            except Exception:
                pass

    def unrealized_pnl_usd(self, current_price: float) -> float:
        if not self.position or current_price <= 0:
            return 0.0
        return (current_price - self.position.entry_price) * self.position.quantity

    def _record_exit(self, exit_price: float, reason: str, exit_order_id: str = "",
                     quantity: Optional[float] = None) -> Dict[str, Any]:
        pos = self.position
        qty = quantity if quantity is not None else pos.quantity
        if not self.is_real:
            # No modo paper, aplica slippage na entrada e na saída + taxa Spot (0.1% cada lado)
            effective_entry = pos.entry_price * (1.0 + PAPER_SLIPPAGE_PCT)
            effective_exit = exit_price * (1.0 - PAPER_SLIPPAGE_PCT)
            gross_entry_cost = effective_entry * qty
            gross_exit_proceeds = effective_exit * qty
            total_fees = (gross_entry_cost + gross_exit_proceeds) * PAPER_FEE_PCT
            pnl_usd = (gross_exit_proceeds - gross_entry_cost) - total_fees
            pnl_pct = (pnl_usd / gross_entry_cost) * 100.0 if gross_entry_cost > 0 else 0.0
            recorded_exit_price = effective_exit
        else:
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100.0
            pnl_usd = (exit_price - pos.entry_price) * qty
            recorded_exit_price = exit_price

        self.db.record_trade(
            self.symbol, self.timeframe, pos.model_version, pos.entry_time, pos.entry_price,
            utc_now_str(), recorded_exit_price, reason, pnl_pct, pnl_usd,
            mode=self.mode, order_id=exit_order_id or pos.order_id, quantity=qty
        )
        icon = "🎯" if pnl_usd > 0 else "🚨"
        msg = (
            f"{icon} [{reason} ({self.mode.upper()})] Saída: ${recorded_exit_price:,.2f} | Entrada: ${pos.entry_price:,.2f} "
            f"| PnL Líquido: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | Ordem: {exit_order_id or '-'}"
        )
        logger.warning(msg)
        alerts.send_alert(msg, level="TRADE_EXIT")
        self._clear()
        return {"pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    # ----------------------------------------------------------------- restore
    def restore(self) -> None:
        """Recupera a posição persistida e a reconcilia com a conta real."""
        pos = self.store.load(self.symbol, self.timeframe, self.mode)
        if not pos:
            return
        self.position = pos
        logger.warning(
            f"♻️ [POSIÇÃO RESTAURADA] {pos.symbol} {pos.mode.upper()} | Entrada ${pos.entry_price:,.2f} "
            f"| Qtd {pos.quantity:.6f} | SL ${pos.sl_price:,.2f} | TP ${pos.tp_price:,.2f}"
        )
        if not self.is_real:
            return

        # 0. Reconciliação de ordem PENDING caso o robô tenha encerrado durante o envio da compra
        if pos.status == "PENDING" and pos.order_id and pos.order_id != "PENDING":
            reconciled = binance_client.reconcile_order_by_client_id(self.exchange, self.symbol, pos.order_id)
            if reconciled and float(reconciled.get("filled") or 0.0) > 0:
                base = self.symbol.split("/")[0]
                net_qty = binance_client.net_filled_quantity(reconciled, base, float(reconciled["filled"]))
                avg_price = float(reconciled.get("average") or pos.entry_price)
                pos.order_id = str(reconciled.get("id") or pos.order_id)
                pos.entry_price = avg_price
                pos.quantity = net_qty
                pos.sl_price = avg_price * (1.0 - self.stop_loss_pct)
                pos.tp_price = avg_price * (1.0 + self.take_profit_pct)
                pos.status = "OPEN"
                self._persist()
                logger.warning(
                    f"   ✅ [RECONCILIAÇÃO PENDING] Compra confirmada na Binance! "
                    f"Entrada ${avg_price:,.2f} | Qtd {net_qty:.6f}"
                )
            else:
                logger.warning("   ℹ️ Ordem PENDING não foi executada na Binance. Limpando registro local.")
                self._clear()
                return

        # 1. A OCO pode ter executado enquanto o robô estava desligado
        if pos.oco_list_id:
            status = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
            if status.get("success") and status.get("filled"):
                logger.warning("   A OCO de proteção foi executada enquanto o robô estava offline.")
                self._record_exit(status["price"], status["reason"], status["order_id"], status["quantity"])
                return
            if status.get("success") and status.get("list_status") != "EXECUTING":
                pos.oco_list_id = ""
                self._persist()

        # 2. Confere se o ativo ainda está na conta
        try:
            balance = self.exchange.fetch_balance()
            base = self.symbol.split("/")[0]
            total_base = float(balance.get("total", {}).get(base, 0.0) or 0.0)
        except Exception as e:
            logger.error(f"   Não foi possível conferir o saldo para reconciliar a posição: {e}")
            return

        expected_total_base = pos.base_balance_before + pos.quantity
        if total_base < expected_total_base * 0.98:
            logger.error(
                f"   ⚠️ Saldo de {base} ({total_base:.6f}) menor que o esperado "
                f"({expected_total_base:.6f} = antes {pos.base_balance_before:.6f} + posição {pos.quantity:.6f}). "
                f"A posição foi provavelmente encerrada manualmente. Removendo o registro local SEM gravar trade."
            )
            self._clear()
            return

        # 3. Garante a proteção na exchange
        if self.use_exchange_oco and not pos.oco_list_id:
            self._place_oco()

    # -------------------------------------------------------------------- open
    def _place_oco(self) -> None:
        pos = self.position
        res = binance_client.place_protective_oco(
            self.exchange, self.symbol, pos.quantity, pos.tp_price, pos.sl_price
        )
        if res.get("success"):
            pos.oco_list_id = res["order_list_id"]
            self._persist()
        else:
            logger.error("   ⚠️ Sem proteção OCO na exchange: SL/TP serão geridos apenas pelo robô.")

    def try_open(
        self,
        current_price: float,
        model_version: int,
        confidence: float,
        tp_pct_override: Optional[float] = None,
        sl_pct_override: Optional[float] = None,
    ) -> bool:
        if self.in_position:
            return False

        eff_tp_pct = float(tp_pct_override) if tp_pct_override is not None and tp_pct_override > 0 else self.take_profit_pct
        eff_sl_pct = float(sl_pct_override) if sl_pct_override is not None and sl_pct_override > 0 else self.stop_loss_pct

        if self.risk:
            allowed, reason = self.risk.can_open(
                self.trade_amount_usdt,
                unrealized_pnl_usd=self.unrealized_pnl_usd(current_price),
            )
            if not allowed:
                logger.warning(f"   ⛔ [RISCO] Entrada bloqueada: {reason}")
                alerts.send_alert(f"Entrada bloqueada pelo RiskManager: {reason}", level="RISK_LIMIT")
                return False

        base_before = 0.0
        if self.is_real:
            base = self.symbol.split("/")[0]
            try:
                base_before = binance_client.get_free_balance(self.exchange, base)
            except Exception:
                base_before = 0.0

            # M6: Persiste intenção PENDING com client_order_id e marca order_in_flight ANTES do envio à exchange
            cid = binance_client.generate_client_order_id("fynexb")
            pending_pos = Position(
                symbol=self.symbol, timeframe=self.timeframe, mode=self.mode,
                model_version=model_version, entry_time=utc_now_str(),
                entry_price=current_price, quantity=0.0, order_id=cid,
                sl_price=current_price * (1.0 - eff_sl_pct),
                tp_price=current_price * (1.0 + eff_tp_pct),
                base_balance_before=base_before, status="PENDING",
            )
            self.store.save(pending_pos)
            self._set_order_in_flight(True)
            try:
                buy = binance_client.execute_spot_buy(
                    self.exchange, self.symbol, self.trade_amount_usdt, client_order_id=cid
                )
            finally:
                self._set_order_in_flight(False)

            if not buy.get("success"):
                self.store.delete(self.symbol, self.timeframe, self.mode)
                logger.error(f"   ❌ [FALHA AO ENVIAR ORDEM NA BINANCE] {buy.get('message')}")
                return False
            entry_price, qty, order_id = buy["price"], buy["quantity"], buy["order_id"]
        else:
            if current_price <= 0:
                return False
            entry_price = current_price
            qty = self.trade_amount_usdt / current_price
            order_id = "SIMULATED_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

        self.position = Position(
            symbol=self.symbol, timeframe=self.timeframe, mode=self.mode,
            model_version=model_version, entry_time=utc_now_str(),
            entry_price=entry_price, quantity=qty, order_id=order_id,
            sl_price=entry_price * (1.0 - eff_sl_pct),
            tp_price=entry_price * (1.0 + eff_tp_pct),
            base_balance_before=base_before, status="OPEN",
        )
        self._persist()

        logger.info("   ==================================================")
        logger.info(f"   🚀 [COMPRA EXECUTADA ({self.mode.upper()})] Ordem {order_id}")
        logger.info(f"      Entrada: ${entry_price:,.2f} | Qtd líquida: {qty:.6f} | Confiança: {confidence:.1%}")
        logger.info(f"      Stop Loss: ${self.position.sl_price:,.2f} | Take Profit: ${self.position.tp_price:,.2f}")
        logger.info("   ==================================================")

        if self.use_exchange_oco:
            self._place_oco()
        return True

    # ------------------------------------------------------------------- close
    def close(self, reason: str, current_price: float) -> bool:
        """
        Fecha a posição. Em modo real, só considera fechada se a venda for confirmada;
        caso contrário mantém a posição aberta e tenta novamente no próximo ciclo.
        """
        if not self.in_position:
            return False
        pos = self.position

        if not self.is_real:
            self._record_exit(current_price, reason)
            return True

        had_oco = bool(pos.oco_list_id)
        self._set_order_in_flight(True)
        try:
            if pos.oco_list_id:
                cancel = binance_client.cancel_oco(self.exchange, self.symbol, pos.oco_list_id)
                if not cancel.get("success"):
                    # Pode ter sido executada nesse meio tempo
                    status = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
                    if status.get("success") and status.get("filled"):
                        self._record_exit(status["price"], status["reason"], status["order_id"], status["quantity"])
                        return True
                    if not (status.get("success") and status.get("list_status") != "EXECUTING"):
                        logger.critical("   🆘 Não foi possível cancelar a OCO; a venda será tentada no próximo ciclo.")
                        return False
                pos.oco_list_id = ""
                self._persist()

            sell = binance_client.execute_spot_sell_with_retry(self.exchange, self.symbol, pos.quantity)
            if not sell.get("success"):
                self.consecutive_sell_failures += 1
                err_msg = (
                    f"🆘 [VENDA FALHOU] {sell.get('message')} | A posição CONTINUA ABERTA e será tentada "
                    f"novamente (falhas consecutivas: {self.consecutive_sell_failures}). Verifique a conta Binance!"
                )
                logger.critical("   " + err_msg)
                # M5: Recoloca OCO imediatamente se ela foi cancelada antes da venda falhar
                if had_oco and self.use_exchange_oco:
                    logger.warning("   🛡️ Recolocando OCO de proteção após falha na venda...")
                    self._place_oco()
                alerts.send_alert(err_msg, level="CRITICAL")
                return False

            self._record_exit(sell["price"], reason, sell["order_id"], sell["quantity"])
            return True
        finally:
            self._set_order_in_flight(False)

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        return timeframe_to_seconds(timeframe)

    def _is_time_barrier_expired(self, pos: Position, now_utc: Optional[datetime] = None) -> bool:
        if not self.horizon_candles or self.horizon_candles <= 0:
            return False
        try:
            entry_dt = datetime.strptime(pos.entry_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now_dt = now_utc or datetime.now(timezone.utc)
            max_seconds = self.horizon_candles * self._timeframe_seconds(self.timeframe)
            return (now_dt - entry_dt).total_seconds() >= max_seconds
        except Exception:
            return False

    # ------------------------------------------------------------ check exits
    def check_exits(
        self,
        current_price: float,
        candle_high: Optional[float] = None,
        candle_low: Optional[float] = None,
        now_utc: Optional[datetime] = None,
    ) -> None:
        if not self.in_position:
            return
        pos = self.position

        # Proteção na exchange ativa: ela é a fonte da verdade
        if self.is_real and pos.oco_list_id:
            status = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
            if status.get("success"):
                if status.get("filled"):
                    self._record_exit(status["price"], status["reason"], status["order_id"], status["quantity"])
                    return
                if status.get("list_status") != "EXECUTING":
                    logger.warning(f"   OCO {pos.oco_list_id} não está mais ativa ({status.get('list_status')}). "
                                   f"Assumindo gestão de SL/TP pelo robô.")
                    pos.oco_list_id = ""
                    self._persist()
                elif current_price <= pos.sl_price * (1.0 - STOP_FALLBACK_EXTRA_PCT):
                    logger.warning("   O preço ultrapassou o stop-limit sem execução. Saída a mercado (fallback).")
                    self.close("STOP_LOSS_FALLBACK", current_price)
                    return
                elif self._is_time_barrier_expired(pos, now_utc=now_utc):
                    logger.info(f"   ⏱️ Barreira de tempo atingida ({self.horizon_candles} candles). Cancelando OCO e encerrando.")
                    self.close("TIME_BARRIER", current_price)
                    return
                else:
                    return
            else:
                logger.warning(f"   Falha ao consultar OCO: {status.get('message')}")
                return

        low_check = min(current_price, candle_low) if candle_low is not None else current_price
        high_check = max(current_price, candle_high) if candle_high is not None else current_price

        # Proteção dinâmica de lucro (Breakeven + Trailing):
        # Ao atingir 60% do alvo, protege no breakeven (+taxas); acima de 80%, trava metade do lucro.
        tp_dist = pos.tp_price - pos.entry_price
        if tp_dist > 0 and high_check > pos.entry_price:
            progress = (high_check - pos.entry_price) / tp_dist
            new_sl = pos.sl_price
            if progress >= 0.80:
                trail_sl = pos.entry_price + (high_check - pos.entry_price) * 0.50
                new_sl = max(new_sl, trail_sl)
            elif progress >= 0.60:
                be_sl = pos.entry_price * 1.0025
                new_sl = max(new_sl, be_sl)
            if new_sl > pos.sl_price * 1.0001:
                pos.sl_price = new_sl
                self._persist()
                logger.info(
                    f"   🛡️ [PROTEÇÃO DE LUCRO] Stop ajustado para ${pos.sl_price:,.2f} "
                    f"(progresso {progress * 100:.0f}% até o TP ${pos.tp_price:,.2f})."
                )

        # Conservador: se um mesmo candle tocou tanto SL quanto TP, prioriza STOP_LOSS
        if low_check <= pos.sl_price:
            # M7: No paper, se houve gap abaixo do stop, preenche no pior preço real
            exit_p = min(pos.sl_price, current_price) if not self.is_real else current_price
            reason = "TRAILING_PROFIT" if pos.sl_price > pos.entry_price else "STOP_LOSS"
            self.close(reason, exit_p)
        elif high_check >= pos.tp_price:
            exit_p = pos.tp_price if not self.is_real else current_price
            self.close("TAKE_PROFIT", exit_p)
        elif self._is_time_barrier_expired(pos, now_utc=now_utc):
            self.close("TIME_BARRIER", current_price)

    def status_label(self) -> str:
        if not self.position:
            return "Aguardando Sinal"
        prot = " +OCO" if self.position.oco_list_id else ""
        return f"Comprado (${self.position.entry_price:,.2f}{prot})"
