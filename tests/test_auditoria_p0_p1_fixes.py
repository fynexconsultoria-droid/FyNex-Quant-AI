"""
Testes de regressão para as correções críticas (P0/P1) da Auditoria Arquitetural.
"""

from __future__ import annotations

import ccxt
import pytest

from core.database import TradingDatabase
from core.error_learning.store import ErrorLearningStore
from core.exchange.execution import (
    execute_spot_buy,
    execute_spot_sell,
    execute_spot_sell_with_retry,
    net_filled_quantity,
    place_protective_oco,
)
from core.forex_ai_trader import ForexAITrader
from core.infra.env_store import scrub_secrets, update_env_keys
from core.infra.sqlite import sqlite_connection
from core.markets.xtb_desk import XTBDeskStore
from core.trading.position_manager import PositionManager
from core.trading.risk import RiskManager
from core.trading.store import Position, PositionStore
from train_ai import _resolve_transaction_costs


class _StubExchange:
    def __init__(self, order_response=None, oco_response=None):
        self.urls = {"api": "https://api.binance.com"}
        self._order_response = order_response or {}
        self._oco_response = oco_response or {}
        self.buy_calls = 0
        self.sell_calls = 0

    def load_markets(self):
        return {
            "BTC/USDT": {
                "id": "BTCUSDT",
                "active": True,
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.0001}},
            }
        }

    def fetch_ticker(self, symbol):
        return {"last": 50000.0, "bid": 49995.0, "ask": 50005.0}

    def fetch_balance(self):
        return {
            "free": {"USDT": 1000.0, "BTC": 0.01},
            "used": {"USDT": 0.0, "BTC": 0.0},
            "total": {"USDT": 1000.0, "BTC": 0.01},
        }

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def create_market_buy_order(self, symbol, quantity, params=None):
        self.buy_calls += 1
        return dict(self._order_response)

    def create_market_sell_order(self, symbol, quantity, params=None):
        self.sell_calls += 1
        if isinstance(self._order_response, Exception):
            raise self._order_response
        return dict(self._order_response)

    def private_post_order_oco(self, params):
        return dict(self._oco_response)


def test_net_filled_quantity_preserves_explicit_zero():
    assert net_filled_quantity({"filled": 0.0}, "BTC", fallback_qty=0.5) == 0.0
    assert net_filled_quantity({"filled": None}, "BTC", fallback_qty=0.5) == 0.5


def test_execute_spot_buy_rejects_zero_fill_and_canceled():
    ex_zero = _StubExchange(
        order_response={"id": "1001", "status": "canceled", "filled": 0.0, "average": 50000.0}
    )
    res = execute_spot_buy(ex_zero, "BTC/USDT", amount_usdt=100.0)
    assert res["success"] is False
    assert "0" in res["message"] or "rejeitada" in res["message"].lower()


def test_execute_spot_sell_rejects_zero_fill():
    ex_zero = _StubExchange(
        order_response={"id": "1002", "status": "expired", "filled": 0.0, "average": 50000.0}
    )
    res = execute_spot_sell(ex_zero, "BTC/USDT", quantity=0.002)
    assert res["success"] is False


def test_execute_spot_sell_with_retry_does_not_duplicate_on_reconcile_network_error(monkeypatch):
    ex = _StubExchange(order_response=ccxt.NetworkError("timeout on submit"))

    def _boom_reconcile(exchange, symbol, client_order_id, raise_on_network_error=False):
        if raise_on_network_error:
            raise ccxt.NetworkError("reconcile also unreachable")
        return None

    monkeypatch.setattr("core.exchange.execution.reconcile_order_by_client_id", _boom_reconcile)
    monkeypatch.setattr("core.exchange.execution._sleep", lambda s: None)

    res = execute_spot_sell_with_retry(ex, "BTC/USDT", quantity=0.002, attempts=3, base_delay_seconds=0.01)
    assert res["success"] is False
    assert ex.sell_calls == 1  # Nunca envia uma segunda venda quando reconciliação falhou por rede


def test_place_protective_oco_rejects_empty_order_list_id():
    ex = _StubExchange(oco_response={"orderListId": None})
    res = place_protective_oco(
        ex, "BTC/USDT", quantity=0.002, take_profit_price=52000.0, stop_price=49000.0
    )
    assert res["success"] is False


def test_risk_manager_rejects_nan_inf_and_non_positive(tmp_path):
    db = TradingDatabase(str(tmp_path / "risk.db"))
    rm = RiskManager(
        db=db,
        mode="paper",
        max_daily_loss_usdt=50.0,
        max_trades_per_day=5,
        max_trade_amount_usdt=500.0,
        kill_switch_path=str(tmp_path / "NO_STOP"),
    )

    ok_nan, reason_nan = rm.can_open(float("nan"))
    assert ok_nan is False
    assert "inválido" in reason_nan.lower()

    ok_inf, _ = rm.can_open(float("inf"))
    assert ok_inf is False

    ok_zero, _ = rm.can_open(0.0)
    assert ok_zero is False

    ok_unr_nan, _ = rm.can_open(50.0, unrealized_pnl_usd=float("nan"))
    assert ok_unr_nan is False

    ok_valid, _ = rm.can_open(50.0, unrealized_pnl_usd=0.0)
    assert ok_valid is True


def test_position_manager_no_intracandle_lookahead_phantom_stop(tmp_path):
    """
    Se a vela teve mínima 99.80 (abaixo do futuro breakeven 100.25, acima do SL original 98.00)
    e máxima 102.00 (que ativa o breakeven para 100.25) fechando em 101.00,
    a posição NÃO pode ser encerrada pelo low passado da mesma vela!
    """
    db = TradingDatabase(str(tmp_path / "pm.db"))
    pm = PositionManager(
        db=db,
        exchange=None,
        mode="paper",
        symbol="BTC/USDT",
        timeframe="15m",
        stop_loss_pct=0.02,
        take_profit_pct=0.05,
        trade_amount_usdt=100.0,
    )
    pm.position = Position(
        symbol="BTC/USDT",
        timeframe="15m",
        mode="paper",
        model_version=1,
        entry_time="2026-01-01T00:00:00",
        entry_price=100.0,
        quantity=1.0,
        order_id="paper_1",
        sl_price=98.0,
        tp_price=105.0,
        status="OPEN",
    )
    pm.store.save(pm.position)

    pm.check_exits(
        current_price=101.0,
        candle_high=103.0,
        candle_low=99.80,
    )
    assert pm.position is not None
    assert pm.position.sl_price >= 100.20  # Breakeven foi armado para velas futuras


def test_position_manager_paper_tp_capped_at_tp_price(tmp_path):
    db_path = str(tmp_path / "pm_tp.db")
    db = TradingDatabase(db_path)
    pm = PositionManager(
        db=db,
        exchange=None,
        mode="paper",
        symbol="BTC/USDT",
        timeframe="15m",
        stop_loss_pct=0.02,
        take_profit_pct=0.05,
        trade_amount_usdt=100.0,
    )
    pm.position = Position(
        symbol="BTC/USDT",
        timeframe="15m",
        mode="paper",
        model_version=1,
        entry_time="2026-01-01T00:00:00",
        entry_price=100.0,
        quantity=1.0,
        order_id="paper_2",
        sl_price=98.0,
        tp_price=105.0,
        status="OPEN",
    )
    pm.store.save(pm.position)

    pm.check_exits(current_price=104.0, candle_high=108.0, candle_low=101.0)
    assert pm.position is None
    with sqlite_connection(db_path) as conn:
        row = conn.execute("SELECT exit_price, reason FROM simulated_trades ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row[1] == "TAKE_PROFIT"
    assert float(row[0]) <= 105.0 + 1e-6  # Nunca infla acima do tp_price (aplica apenas slippage)
    assert float(row[0]) >= 104.90


def test_forex_ai_trader_respects_kill_switch_file(tmp_path):
    stop_file = tmp_path / "STOP"
    stop_file.write_text("STOP", encoding="utf-8")
    trader = ForexAITrader(
        db_path=str(tmp_path / "fx.db"),
        kill_switch_path=str(stop_file),
    )
    res = trader.run_cycle(now_utc=None)
    assert "kill-switch" in str(res.get("status_message", "")).lower()


def test_xtb_desk_close_position_raises_when_quote_unavailable(tmp_path):
    store = XTBDeskStore(str(tmp_path / "xtb.db"))
    pos = store.open_position(
        symbol="EUR/USD",
        side="BUY",
        lots=0.10,
        leverage=10,
        quote_override={"price": 1.1000, "bid": 1.0999, "ask": 1.1001},
    )
    with pytest.raises(ValueError, match="Cotação indisponível"):
        store.close_position(pos["id"], quote_override={"price": 0.0, "bid": 0.0, "ask": 0.0})


def test_update_env_keys_applies_updates_with_template_and_blocks_newlines(tmp_path):
    env_file = tmp_path / ".env"
    template = "LIVE_TRADING_ENABLED=false\nTRADE_AMOUNT_USDT=50\n"
    update_env_keys({"TRADE_AMOUNT_USDT": "125"}, str(env_file), default_template=template)
    text = env_file.read_text(encoding="utf-8")
    assert "TRADE_AMOUNT_USDT=125" in text
    assert "LIVE_TRADING_ENABLED=false" in text

    with pytest.raises(ValueError):
        update_env_keys({"TRADE_AMOUNT_USDT": "50\nLIVE_TRADING_ENABLED=true"}, str(env_file))

    scrubbed = scrub_secrets("Header X-MBX-APIKEY: abcdef1234567890 &signature=deadbeef0123456789")
    assert "abcdef1234567890" not in scrubbed
    assert "deadbeef0123456789" not in scrubbed


def test_resolve_transaction_costs_forex_vs_crypto():
    fx_fee, fx_slip = _resolve_transaction_costs("EUR/USD")
    gold_fee, gold_slip = _resolve_transaction_costs("XAU/USD")
    btc_fee, btc_slip = _resolve_transaction_costs("BTC/USDT")
    assert fx_fee == 0.0 and fx_slip < 0.0005
    assert gold_fee == 0.0 and gold_slip < 0.0005
    assert btc_fee > 0.0 and (btc_fee + btc_slip) > (fx_fee + fx_slip)


def test_error_learning_store_dedup_and_xtb_trade_loss(tmp_path):
    store = ErrorLearningStore(str(tmp_path / "err.db"))
    r1 = store.record_error_postmortem(
        source="XTB_TRADE_LOSS",
        symbol="EUR/USD",
        timeframe="1h",
        params={"rsi": 72.0, "adx": 0.14},
        outcome_reason="STOP_LOSS",
        candle_timestamp=1700000000,
        pnl_usd=-12.5,
        pnl_pct=-1.25,
    )
    r2 = store.record_error_postmortem(
        source="XTB_TRADE_LOSS",
        symbol="EUR/USD",
        timeframe="1h",
        params={"rsi": 72.0, "adx": 0.14},
        outcome_reason="STOP_LOSS",
        candle_timestamp=1700000000,
        pnl_usd=-12.5,
        pnl_pct=-1.25,
    )
    assert r1["primary_cause"] == r2["primary_cause"]
    assert r2.get("deduplicated") is True
    report = store.get_error_learning_report("EUR/USD", "1h", limit=10)
    lessons = report["recent_errors"]
    assert len(lessons) == 1
    assert lessons[0]["source"] == "XTB_TRADE_LOSS"


def test_dashboard_status_fresh_db_without_bot_tables_no_crash(tmp_path, monkeypatch):
    from dashboard.routers import status as status_router

    fresh_db = str(tmp_path / "fresh_xtb_only.db")
    XTBDeskStore(fresh_db)  # Cria apenas xtb_positions/xtb_trades, sem ohlcv_candles/predictions/simulated_trades
    monkeypatch.setattr(status_router, "DEFAULT_DB_PATH", fresh_db)
    monkeypatch.setattr(status_router, "_srv_attr", lambda name, default: fresh_db if name == "DEFAULT_DB_PATH" else default)

    with sqlite_connection(fresh_db) as conn:
        latest_candle, rolling_acc, trade_stats, paper_pnl = status_router._query_status_metrics(
            conn, symbol="BTC/USDT", timeframe="15m"
        )
    assert latest_candle is None
    assert rolling_acc["total"] == 0
    assert trade_stats["total_trades"] == 0
    assert paper_pnl == 0.0

    candles_res = status_router.get_candles(symbol="BTC/USDT", timeframe="15m", limit=50)
    assert candles_res == []

    preds_res = status_router.get_predictions(symbol="BTC/USDT", timeframe="15m", limit=20)
    assert preds_res == []

    trades_res = status_router.get_trades(symbol="BTC/USDT", limit=20)
    assert trades_res == []
