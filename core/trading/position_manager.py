"""
Gestor Único de Posições (Paper / Testnet / Live — FyNex Quant AI)
==================================================================
Orquestra abertura, reconciliação de ordens PENDING, proteção OCO na exchange,
proteção dinâmica de lucro (Breakeven + Trailing via `profit_protection.py`),
barreira temporal e fechamento com retry seguro.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import alerts
import binance_client
from core import error_learning
from core.database import timeframe_to_seconds
from core.infra.utils import parse_utc_str, utc_now_str
from core.trading.models import Position
from core.trading.profit_protection import compute_profit_protection_stop
from core.trading.risk import RiskManager
from core.trading.store import PositionStore
from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT

logger = logging.getLogger("QuantTradingBot")

# Se o preço cair este percentual ABAIXO do stop e a OCO ainda não executou
# (ex.: gap que pulou o stop-limit), o robô cancela a OCO e vende a mercado.
STOP_FALLBACK_EXTRA_PCT = 0.005


class PositionManager:
    def __init__(
        self,
        db,
        exchange,
        mode: str,
        symbol: str,
        timeframe: str,
        stop_loss_pct: float,
        take_profit_pct: float,
        trade_amount_usdt: float,
        use_exchange_oco: bool = True,
        risk: Optional[RiskManager] = None,
        horizon_candles: Optional[int] = None,
    ):
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

    def _record_exit(
        self,
        exit_price: float,
        reason: str,
        exit_order_id: str = "",
        quantity: Optional[float] = None,
        fee_usd: float = 0.0,
    ) -> Dict[str, Any]:
        pos = self.position
        assert pos is not None
        qty = quantity if quantity is not None else pos.quantity
        if not self.is_real:
            effective_entry = pos.entry_price * (1.0 + PAPER_SLIPPAGE_PCT)
            effective_exit = exit_price * (1.0 - PAPER_SLIPPAGE_PCT)
            gross_entry_cost = effective_entry * qty
            gross_exit_proceeds = effective_exit * qty
            total_fees = (gross_entry_cost + gross_exit_proceeds) * PAPER_FEE_PCT
            pnl_usd = (gross_exit_proceeds - gross_entry_cost) - total_fees
            pnl_pct = (pnl_usd / gross_entry_cost) * 100.0 if gross_entry_cost > 0 else 0.0
            recorded_exit_price = effective_exit
        else:
            gross_entry_cost = pos.entry_price * qty
            pnl_usd = (exit_price - pos.entry_price) * qty - max(0.0, float(fee_usd or 0.0))
            pnl_pct = (pnl_usd / gross_entry_cost) * 100.0 if gross_entry_cost > 0 else 0.0
            recorded_exit_price = exit_price

        self.db.record_trade(
            self.symbol,
            self.timeframe,
            pos.model_version,
            pos.entry_time,
            pos.entry_price,
            utc_now_str(),
            recorded_exit_price,
            reason,
            pnl_pct,
            pnl_usd,
            mode=self.mode,
            order_id=exit_order_id or pos.order_id,
            quantity=qty,
        )

        lesson_suffix = ""
        if pnl_usd < 0 and getattr(self.db, "db_path", None):
            try:
                el_store = error_learning.ErrorLearningStore(self.db.db_path)
                snap = el_store.get_snapshot(self.symbol, self.timeframe, order_id=pos.order_id)
                params = dict(snap["parameters"]) if snap else {}
                if pos.entry_price > 0:
                    actual_sl = max(0.0, (pos.entry_price - pos.sl_price) / pos.entry_price)
                    actual_tp = max(0.0, (pos.tp_price - pos.entry_price) / pos.entry_price)
                    params.setdefault("sl_pct", round(actual_sl if actual_sl > 0 else self.stop_loss_pct, 6))
                    params.setdefault("tp_pct", round(actual_tp if actual_tp > 0 else self.take_profit_pct, 6))
                else:
                    params.setdefault("sl_pct", round(self.stop_loss_pct, 6))
                    params.setdefault("tp_pct", round(self.take_profit_pct, 6))
                pm_res = el_store.record_error_postmortem(
                    source="TRADE_LOSS",
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    params=params,
                    outcome_reason=reason,
                    candle_timestamp=snap.get("candle_timestamp") if snap else None,
                    model_version=pos.model_version,
                    prob_up=float(snap.get("prob_up") or 0.0) if snap else 0.0,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                )
                logger.warning(
                    f"🔬 [AUTO-ANÁLISE DE ERRO #{pm_res['id']}] Causa principal: {pm_res['primary_label']} | "
                    f"{pm_res['lesson_text']}"
                )
                lesson_suffix = f"\n🔍 Auto-análise: {pm_res['lesson_text']}"
            except Exception as el_err:
                logger.debug(f"Falha ao registrar auto-análise de erro: {el_err}")

        icon = "✅" if pnl_usd > 0 else ("❌" if pnl_usd < 0 else "⚪")
        verdict = "DEU RESULTADO (LUCRO)" if pnl_usd > 0 else ("PREJUÍZO" if pnl_usd < 0 else "EMPATE (0x0)")
        msg = (
            f"{icon} [{reason} ({self.mode.upper()})] {self.symbol} — {verdict}\n"
            f"Entrada: ${pos.entry_price:,.2f} | Saída: ${recorded_exit_price:,.2f}\n"
            f"PnL Líquido: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | Ordem: {exit_order_id or '-'}"
            f"{lesson_suffix}"
        )
        logger.warning(msg.replace("\n", " | "))
        alerts.send_alert(msg, level="TRADE_EXIT")
        self._clear()
        return {"pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

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

        if pos.status == "PENDING" and pos.order_id and pos.order_id != "PENDING":
            try:
                try:
                    reconciled = binance_client.reconcile_order_by_client_id(
                        self.exchange, self.symbol, pos.order_id, raise_on_network_error=True
                    )
                except TypeError:
                    reconciled = binance_client.reconcile_order_by_client_id(
                        self.exchange, self.symbol, pos.order_id
                    )
            except Exception as e:
                logger.warning(
                    f"   ⚠️ Falha transitória ao reconciliar ordem PENDING [{pos.order_id}]: {e}. "
                    f"Mantendo registro PENDING para próxima verificação."
                )
                return
            if reconciled and float(reconciled.get("filled") or 0.0) > 0:
                base = self.symbol.split("/")[0]
                net_qty = binance_client.net_filled_quantity(reconciled, base, float(reconciled["filled"]))
                avg_price = float(reconciled.get("average") or pos.entry_price)
                old_cid = pos.order_id
                pos.order_id = str(reconciled.get("id") or pos.order_id)
                pos.entry_price = avg_price
                pos.quantity = net_qty
                pos.sl_price = avg_price * (1.0 - self.stop_loss_pct)
                pos.tp_price = avg_price * (1.0 + self.take_profit_pct)
                pos.status = "OPEN"
                self._persist()
                if getattr(self.db, "db_path", None):
                    try:
                        error_learning.ErrorLearningStore(self.db.db_path).update_snapshot_order_id(
                            self.symbol, self.timeframe, old_cid, pos.order_id
                        )
                    except Exception:
                        pass
                logger.warning(
                    f"   ✅ [RECONCILIAÇÃO PENDING] Compra confirmada na Binance! "
                    f"Entrada ${avg_price:,.2f} | Qtd {net_qty:.6f}"
                )
            elif reconciled is None or str(reconciled.get("status", "")).lower() in (
                "canceled", "cancelled", "rejected", "expired", "closed"
            ):
                logger.warning("   ℹ️ Ordem PENDING não foi executada na Binance. Limpando registro local.")
                self._clear()
                return
            else:
                logger.warning(
                    f"   ⏳ Ordem PENDING [{pos.order_id}] ainda em estado '{reconciled.get('status')}' na Binance. "
                    f"Mantendo registro PENDING."
                )
                return

        if pos.oco_list_id:
            status = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
            if status.get("success") and status.get("filled"):
                logger.warning("   A OCO de proteção foi executada enquanto o robô estava offline.")
                self._record_exit(status["price"], status["reason"], status["order_id"], status["quantity"])
                return
            if status.get("success") and status.get("list_status") != "EXECUTING":
                pos.oco_list_id = ""
                self._persist()

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

        if self.use_exchange_oco and not pos.oco_list_id:
            self._place_oco()

    def _place_oco(self) -> None:
        pos = self.position
        assert pos is not None
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
        amount_usdt_override: Optional[float] = None,
        entry_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self.in_position:
            return False

        eff_tp_pct = float(tp_pct_override) if tp_pct_override is not None and tp_pct_override > 0 else self.take_profit_pct
        eff_sl_pct = float(sl_pct_override) if sl_pct_override is not None and sl_pct_override > 0 else self.stop_loss_pct
        eff_amount_usdt = (
            float(amount_usdt_override)
            if amount_usdt_override is not None and amount_usdt_override > 0
            else self.trade_amount_usdt
        )

        if self.risk:
            allowed, reason = self.risk.can_open(
                eff_amount_usdt,
                unrealized_pnl_usd=self.unrealized_pnl_usd(current_price),
            )
            if not allowed:
                logger.warning(f"   ⛔ [RISCO] Entrada bloqueada: {reason}")
                alerts.send_alert(f"Entrada bloqueada pelo RiskManager: {reason}", level="RISK_LIMIT")
                return False

        base_before = 0.0
        cid = ""
        if self.is_real:
            base = self.symbol.split("/")[0]
            try:
                _, _, base_before = binance_client.get_asset_balances(self.exchange, base)
            except Exception:
                try:
                    base_before = binance_client.get_free_balance(self.exchange, base)
                except Exception:
                    base_before = 0.0

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
            if getattr(self.db, "db_path", None):
                try:
                    ctx_pre = entry_context if isinstance(entry_context, dict) else {}
                    error_learning.ErrorLearningStore(self.db.db_path).save_decision_snapshot(
                        symbol=self.symbol,
                        timeframe=self.timeframe,
                        features_row=ctx_pre.get("features"),
                        macro_snapshot=ctx_pre.get("macro"),
                        candle_timestamp=ctx_pre.get("candle_timestamp"),
                        prediction_id=ctx_pre.get("prediction_id"),
                        order_id=cid,
                        model_version=model_version,
                        prob_up=confidence,
                        extra={
                            "tp_pct": eff_tp_pct,
                            "sl_pct": eff_sl_pct,
                            "amount_usdt": eff_amount_usdt,
                            "entry_price": current_price,
                            "mode": self.mode,
                        },
                    )
                except Exception:
                    pass
            self._set_order_in_flight(True)
            try:
                buy = binance_client.execute_spot_buy(
                    self.exchange, self.symbol, eff_amount_usdt, client_order_id=cid
                )
            finally:
                self._set_order_in_flight(False)

            if not buy.get("success"):
                if buy.get("ambiguous_network"):
                    err_txt = (
                        f"⚠️ TIMEOUT AMBÍGUO NA COMPRA ({self.symbol} / {self.mode.upper()})\n"
                        f"Ordem {cid} mantida como PENDING para reconciliação segura.\n"
                        f"Motivo: {buy.get('message')}"
                    )
                    logger.critical("   " + err_txt.replace("\n", " | "))
                    self.position = pending_pos
                    alerts.send_alert(err_txt, level="TRADE_FAIL")
                else:
                    self.store.delete(self.symbol, self.timeframe, self.mode)
                    err_txt = (
                        f"❌ OPERAÇÃO FALHOU NA BINANCE ({self.symbol} / {self.mode.upper()})\n"
                        f"Motivo: {buy.get('message')}"
                    )
                    logger.error("   " + err_txt.replace("\n", " | "))
                    alerts.send_alert(err_txt, level="TRADE_FAIL")
                return False
            entry_price, qty, order_id = buy["price"], buy["quantity"], buy["order_id"]
        else:
            if current_price <= 0:
                return False
            entry_price = current_price
            qty = eff_amount_usdt / current_price
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

        if self.use_exchange_oco:
            self._place_oco()

        if getattr(self.db, "db_path", None):
            try:
                ctx = entry_context if isinstance(entry_context, dict) else {}
                el_store = error_learning.ErrorLearningStore(self.db.db_path)
                if self.is_real:
                    el_store.update_snapshot_order_id(self.symbol, self.timeframe, cid, order_id)
                else:
                    el_store.save_decision_snapshot(
                        symbol=self.symbol,
                        timeframe=self.timeframe,
                        features_row=ctx.get("features"),
                        macro_snapshot=ctx.get("macro"),
                        candle_timestamp=ctx.get("candle_timestamp"),
                        prediction_id=ctx.get("prediction_id"),
                        order_id=order_id,
                        model_version=model_version,
                        prob_up=confidence,
                        extra={
                            "tp_pct": eff_tp_pct,
                            "sl_pct": eff_sl_pct,
                            "amount_usdt": eff_amount_usdt,
                            "entry_price": entry_price,
                            "mode": self.mode,
                        },
                    )
            except Exception:
                pass

        logger.info("   ==================================================")
        logger.info(f"   🚀 [COMPRA EXECUTADA ({self.mode.upper()})] Ordem {order_id}")
        logger.info(f"      Entrada: ${entry_price:,.2f} | Qtd líquida: {qty:.6f} | Confiança: {confidence:.1%}")
        logger.info(f"      Stop Loss: ${self.position.sl_price:,.2f} | Take Profit: ${self.position.tp_price:,.2f}")
        logger.info("   ==================================================")

        alerts.send_alert(
            (
                f"🚀 COMPRA {self.symbol} ({self.mode.upper()})\n"
                f"Entrada: ${entry_price:,.2f} | Valor: ${eff_amount_usdt:,.2f}\n"
                f"Take Profit: ${self.position.tp_price:,.2f} (+{eff_tp_pct * 100:.2f}%)\n"
                f"Stop Loss: ${self.position.sl_price:,.2f} (-{eff_sl_pct * 100:.2f}%)\n"
                f"Confiança IA: {confidence:.1%} | Ordem: {order_id}"
            ),
            level="TRADE_ENTRY",
        )
        return True

    def close(self, reason: str, current_price: float) -> bool:
        if not self.in_position:
            return False
        pos = self.position
        assert pos is not None

        if not self.is_real:
            self._record_exit(current_price, reason)
            return True

        had_oco = bool(pos.oco_list_id)
        self._set_order_in_flight(True)
        try:
            if pos.oco_list_id:
                cancel = binance_client.cancel_oco(self.exchange, self.symbol, pos.oco_list_id)
                if not cancel.get("success"):
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
                if had_oco and self.use_exchange_oco:
                    logger.warning("   🛡️ Recolocando OCO de proteção após falha na venda...")
                    self._place_oco()
                alerts.send_alert(err_msg, level="CRITICAL")
                return False

            sell_fee_usd = 0.0
            raw_sell = sell.get("raw_order")
            if isinstance(raw_sell, dict):
                quote = self.symbol.split("/")[1] if "/" in self.symbol else "USDT"
                sell_fee_usd = binance_client.quote_fee_from_order(raw_sell, quote)
            self._record_exit(sell["price"], reason, sell["order_id"], sell["quantity"], fee_usd=sell_fee_usd)
            return True
        finally:
            self._set_order_in_flight(False)

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        return timeframe_to_seconds(timeframe)

    def _is_time_barrier_expired(self, pos: Position, now_utc: Optional[datetime] = None) -> bool:
        if not self.horizon_candles or self.horizon_candles <= 0:
            return False
        entry_dt = parse_utc_str(pos.entry_time)
        if entry_dt is None:
            return False
        now_dt = now_utc or datetime.now(timezone.utc)
        max_seconds = self.horizon_candles * self._timeframe_seconds(self.timeframe)
        return (now_dt - entry_dt).total_seconds() >= max_seconds

    def _apply_profit_protection(self, high_check: float, current_price: float) -> bool:
        pos = self.position
        if not pos:
            return False

        is_real_oco = bool(self.is_real and pos.oco_list_id)
        decision = compute_profit_protection_stop(
            entry_price=pos.entry_price,
            tp_price=pos.tp_price,
            current_sl=pos.sl_price,
            high_check=high_check,
            current_price=current_price,
            is_real_with_oco=is_real_oco,
        )
        if not decision.should_update:
            return False

        if is_real_oco:
            self._set_order_in_flight(True)
            try:
                cancel = binance_client.cancel_oco(self.exchange, self.symbol, pos.oco_list_id)
                if not cancel.get("success"):
                    st = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
                    if st.get("success") and st.get("filled"):
                        self._record_exit(st["price"], st["reason"], st["order_id"], st["quantity"])
                        return True
                    return False
                pos.oco_list_id = ""
                pos.sl_price = decision.new_sl
                self._persist()
                logger.info(
                    f"   🛡️ [PROTEÇÃO DE LUCRO REAL] OCO atualizada: novo Stop ${pos.sl_price:,.2f} "
                    f"(progresso {decision.progress * 100:.0f}% até o TP ${pos.tp_price:,.2f})."
                )
                self._place_oco()
            finally:
                self._set_order_in_flight(False)
            return False

        pos.sl_price = decision.new_sl
        self._persist()
        logger.info(
            f"   🛡️ [PROTEÇÃO DE LUCRO] Stop ajustado para ${pos.sl_price:,.2f} "
            f"(progresso {decision.progress * 100:.0f}% até o TP ${pos.tp_price:,.2f})."
        )
        return False

    def check_exits(
        self,
        current_price: float,
        candle_high: Optional[float] = None,
        candle_low: Optional[float] = None,
        now_utc: Optional[datetime] = None,
        candle_open_ts_ms: Optional[int] = None,
    ) -> None:
        if not self.in_position:
            return
        pos = self.position
        assert pos is not None

        if self.is_real and pos.status == "PENDING":
            self.restore()
            if not self.in_position or (self.position and self.position.status == "PENDING"):
                return
            pos = self.position
            assert pos is not None

        use_hl = True
        if candle_open_ts_ms is not None and pos.entry_time:
            entry_dt = parse_utc_str(pos.entry_time)
            if entry_dt is not None and candle_open_ts_ms < int(entry_dt.timestamp() * 1000):
                use_hl = False

        low_check = min(current_price, candle_low) if (use_hl and candle_low is not None) else current_price
        high_check = max(current_price, candle_high) if (use_hl and candle_high is not None) else current_price

        if self.is_real and pos.oco_list_id:
            status = binance_client.get_oco_status(self.exchange, self.symbol, pos.oco_list_id)
            if status.get("success"):
                if status.get("filled"):
                    self._record_exit(status["price"], status["reason"], status["order_id"], status["quantity"])
                    return
                if status.get("list_status") != "EXECUTING":
                    logger.warning(
                        f"   OCO {pos.oco_list_id} não está mais ativa ({status.get('list_status')}). "
                        f"Assumindo gestão de SL/TP pelo robô."
                    )
                    pos.oco_list_id = ""
                    self._persist()
                elif current_price <= pos.sl_price * (1.0 - STOP_FALLBACK_EXTRA_PCT):
                    logger.warning("   O preço ultrapassou o stop-limit sem execução. Saída a mercado (fallback).")
                    self.close("STOP_LOSS_FALLBACK", current_price)
                    return
                elif self._is_time_barrier_expired(pos, now_utc=now_utc):
                    logger.info(
                        f"   ⏱️ Barreira de tempo atingida ({self.horizon_candles} candles). Cancelando OCO e encerrando."
                    )
                    self.close("TIME_BARRIER", current_price)
                    return
                else:
                    self._apply_profit_protection(high_check, current_price)
                    return
            else:
                logger.warning(f"   Falha ao consultar OCO: {status.get('message')}")
                return

        if self._apply_profit_protection(high_check, current_price):
            return

        if low_check <= pos.sl_price:
            exit_reason = "TRAILING_PROFIT" if pos.sl_price > pos.entry_price else "STOP_LOSS"
            self.close(exit_reason, min(current_price, pos.sl_price))
        elif high_check >= pos.tp_price:
            self.close("TAKE_PROFIT", max(current_price, pos.tp_price))
        elif self._is_time_barrier_expired(pos, now_utc=now_utc):
            self.close("TIME_BARRIER", current_price)

    def status_label(self) -> str:
        if not self.position:
            return "FORA (Aguardando)"
        oco_tag = " [OCO]" if self.position.oco_list_id else ""
        return f"COMPRADO @ ${self.position.entry_price:,.2f} ({self.mode.upper()}){oco_tag}"
