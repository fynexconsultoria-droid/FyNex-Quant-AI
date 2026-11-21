"""
Testes das melhorias de Acompanhamento de Mercado ao Vivo e Conexões Binance.
100% offline (todas as chamadas à Binance são simuladas com mocks).
"""

import os
import sys
import time

import ccxt
import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import binance_client  # noqa: E402
import position_manager as pm  # noqa: E402
import trading_bot as tb  # noqa: E402
from dashboard import server  # noqa: E402


@pytest.fixture(autouse=True)
def reset_binance_caches():
    binance_client.clear_exchange_cache()
    yield
    binance_client.clear_exchange_cache()


# ----------------------------------------- 1. Retry, Mirror Failover e -1021
def test_call_with_retry_rotates_mirror_on_network_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    start_mirror = binance_client._get_active_mirror()
    attempts = {"n": 0}

    class FakeExchange:
        urls = {"api": {"public": "https://api.binance.com/api/v3"}}

    ex = FakeExchange()

    def flaky_call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ccxt.NetworkError("connection reset")
        return {"ok": True}

    res = binance_client.call_with_retry(flaky_call, exchange=ex, testnet=False, max_attempts=3, base_delay=0.01)
    assert res == {"ok": True}
    assert attempts["n"] == 2
    assert binance_client._get_active_mirror() != start_mirror
    assert ex.urls["api"]["public"].startswith(binance_client._get_active_mirror())


def test_call_with_retry_resyncs_clock_on_error_1021(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    resynced = {"called": 0}
    attempts = {"n": 0}

    class FakeExchange:
        def load_time_difference(self):
            resynced["called"] += 1

    def call_with_clock_err():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ccxt.ExchangeError("binance -1021 Timestamp for this request is outside of the recvWindow")
        return "synced_ok"

    res = binance_client.call_with_retry(call_with_clock_err, exchange=FakeExchange(), max_attempts=3)
    assert res == "synced_ok"
    assert resynced["called"] == 1


def test_call_with_retry_does_not_retry_auth_error():
    attempts = {"n": 0}

    def auth_fail():
        attempts["n"] += 1
        raise ccxt.AuthenticationError("invalid api key")

    with pytest.raises(ccxt.AuthenticationError):
        binance_client.call_with_retry(auth_fail, max_attempts=3)
    assert attempts["n"] == 1


# ----------------------------------------- 2. Health Check, Order Book, Candles & Balance
def test_check_binance_health_reports_latency_and_clock_drift(monkeypatch):
    now_ms = int(time.time() * 1000)

    class FakeExchange:
        def fetch_time(self):
            return now_ms + 120  # +120ms drift -> sincronizado

    monkeypatch.setattr(binance_client, "get_binance_exchange", lambda **kw: FakeExchange())
    health = binance_client.check_binance_health(testnet=False, ttl_seconds=0.0)
    assert health["ok"] is True
    assert health["status"] == "healthy"
    assert health["clock_synced"] is True
    assert abs(health["clock_drift_ms"]) < 1000
    assert health["latency_ms"] >= 0.0
    assert "metrics" in health


def test_fetch_order_book_summary_computes_spread_and_pressure(monkeypatch):
    class FakeExchange:
        def fetch_order_book(self, symbol, limit=15):
            return {
                "bids": [[100000.0, 3.0], [99990.0, 2.0]],  # total bid = 5.0
                "asks": [[100010.0, 1.0], [100020.0, 1.5]],  # total ask = 2.5
                "timestamp": 1_700_000_000_000,
            }

    monkeypatch.setattr(binance_client, "get_binance_exchange", lambda **kw: FakeExchange())
    ob = binance_client.fetch_order_book_summary("BTC/USDT", limit=10, ttl_seconds=0.0)
    assert ob["best_bid"] == pytest.approx(100000.0)
    assert ob["best_ask"] == pytest.approx(100010.0)
    assert ob["spread"] == pytest.approx(10.0)
    # 5.0 / (5.0 + 2.5) = 66.7%
    assert ob["buy_pressure_pct"] == pytest.approx(66.7)
    assert ob["sell_pressure_pct"] == pytest.approx(33.3)


def test_fetch_live_candles_formats_for_tradingview(monkeypatch):
    class FakeExchange:
        def fetch_ohlcv(self, symbol, timeframe="1m", limit=150):
            return [
                [1_700_000_000_000, 90000.0, 90100.0, 89900.0, 90050.0, 12.5],
                [1_700_000_060_000, 90050.0, 90200.0, 90000.0, 90150.0, 15.0],
            ]

    monkeypatch.setattr(binance_client, "get_binance_exchange", lambda **kw: FakeExchange())
    candles = binance_client.fetch_live_candles("ETH/USDT", "1m", limit=2, ttl_seconds=0.0)
    assert len(candles) == 2
    assert candles[0]["time"] == 1_700_000_000
    assert candles[1]["close"] == pytest.approx(90150.0)


def test_fetch_cached_balance_returns_disconnected_without_keys(monkeypatch):
    monkeypatch.setattr(
        binance_client,
        "load_binance_config",
        lambda: {"has_keys": False, "api_key": "", "api_secret": "", "api_key_masked": "", "testnet": False, "trading_mode": "paper"},
    )
    res = binance_client.fetch_cached_balance()
    assert res["connected"] is False
    assert res["has_keys"] is False


# ----------------------------------------- 3. Endpoints do Dashboard
def test_dashboard_live_endpoints_and_candles_auto_fallback(tmp_path, monkeypatch):
    db_path = str(tmp_path / "live_dash.db")
    db = tb.TradingDatabase(db_path=db_path)

    # Cria uma posição aberta para verificar exposição em /api/status
    store = pm.PositionStore(db)
    store.save(
        pm.Position(
            symbol="BTC/USDT",
            timeframe="1m",
            mode="paper",
            model_version=2,
            entry_time="2026-11-20 20:00:00",
            entry_price=95000.0,
            quantity=0.001,
            order_id="PAPER-1",
            sl_price=93500.0,
            tp_price=97850.0,
        )
    )

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "is_bot_running", lambda: (False, None))
    monkeypatch.setattr(
        binance_client,
        "fetch_live_candles",
        lambda symbol, timeframe, limit=150, testnet=False: [
            {"time": int(time.time()) - 60, "open": 95000.0, "high": 95200.0, "low": 94900.0, "close": 95100.0, "volume": 8.0}
        ],
    )
    monkeypatch.setattr(
        binance_client,
        "fetch_order_book_summary",
        lambda symbol, limit=15, testnet=False: {
            "symbol": symbol, "best_bid": 95090.0, "best_ask": 95100.0,
            "spread": 10.0, "spread_pct": 0.0105, "buy_pressure_pct": 62.0, "sell_pressure_pct": 38.0,
        },
    )
    monkeypatch.setattr(
        binance_client,
        "check_binance_health",
        lambda testnet=False: {
            "ok": True, "status": "healthy", "latency_ms": 42.5, "clock_drift_ms": 15,
            "clock_synced": True, "active_mirror": "https://api.binance.com",
        },
    )

    client = TestClient(server.app)

    # DB está vazio de candles -> source=auto faz fallback automático para live!
    r_candles = client.get("/api/candles?symbol=BTC/USDT&timeframe=1m&limit=10")
    assert r_candles.status_code == 200
    assert len(r_candles.json()) == 1
    assert r_candles.json()[0]["close"] == pytest.approx(95100.0)

    # source=db retorna vazio quando DB não tem candles
    r_candles_db = client.get("/api/candles?symbol=BTC/USDT&timeframe=1m&source=db")
    assert r_candles_db.status_code == 200
    assert r_candles_db.json() == []

    # /api/market/depth
    r_depth = client.get("/api/market/depth?symbol=BTC/USDT&limit=15")
    assert r_depth.status_code == 200
    assert r_depth.json()["buy_pressure_pct"] == pytest.approx(62.0)

    # /api/binance/health
    r_health = client.get("/api/binance/health")
    assert r_health.status_code == 200
    assert r_health.json()["ok"] is True
    assert r_health.json()["latency_ms"] == pytest.approx(42.5)

    # /api/status inclui open_position para desenhar linhas no gráfico
    r_status = client.get("/api/status?symbol=BTC/USDT&timeframe=1m")
    assert r_status.status_code == 200
    pos = r_status.json()["open_position"]
    assert pos is not None
    assert pos["entry_price"] == pytest.approx(95000.0)
    assert pos["tp_price"] == pytest.approx(97850.0)
    assert pos["sl_price"] == pytest.approx(93500.0)

    # CSP permite porta 443 para fallback de WebSocket
    assert "wss://stream.binance.com:443" in r_status.headers["content-security-policy"]


# ----------------------------------------- 4. Execução de Entradas e Saídas (Idempotência, Preflight, Reconciliação)
def test_execute_spot_buy_blocks_abnormal_spread():
    class WideSpreadExchange:
        urls = {"api": {"public": "https://api.binance.com/api/v3"}}

        def load_markets(self):
            return {"BTC/USDT": {"id": "BTCUSDT", "active": True, "limits": {"cost": {"min": 5.0}}}}

        def fetch_ticker(self, symbol):
            # Spread de ~2% (muito acima do limite de 0.50%)
            return {"last": 100000.0, "bid": 99000.0, "ask": 101000.0}

    res = binance_client.execute_spot_buy(WideSpreadExchange(), "BTC/USDT", 50.0, max_spread_pct=0.50)
    assert res["success"] is False
    assert "Spread anormal" in res["message"]


def test_execute_spot_buy_reconciles_after_network_timeout():
    captured_cid = {}

    class TimeoutThenReconcileExchange:
        urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}

        def load_markets(self):
            return {"BTC/USDT": {"id": "BTCUSDT", "active": True, "limits": {"cost": {"min": 5.0}}}}

        def fetch_ticker(self, symbol):
            return {"last": 100000.0, "bid": 99995.0, "ask": 100005.0}

        def fetch_balance(self):
            return {"free": {"USDT": 500.0, "BTC": 0.0}, "used": {}, "total": {"USDT": 500.0}}

        def amount_to_precision(self, symbol, qty):
            return f"{qty:.6f}"

        def create_market_buy_order(self, symbol, quantity, params=None):
            captured_cid["cid"] = (params or {}).get("newClientOrderId")
            raise ccxt.RequestTimeout("read timed out after order reached binance")

        def privateGetOrder(self, params):
            assert params["origClientOrderId"] == captured_cid["cid"]
            return {
                "orderId": "987654",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
                "executedQty": "0.000500",
                "cummulativeQuoteQty": "50.025",
            }

    res = binance_client.execute_spot_buy(TimeoutThenReconcileExchange(), "BTC/USDT", 50.0)
    assert res["success"] is True
    assert res["order_id"] == "987654"
    assert res["client_order_id"] == captured_cid["cid"]
    assert res["quantity"] == pytest.approx(0.0005)
    assert res["price"] == pytest.approx(100050.0)


def test_execute_spot_sell_reconciles_after_timeout():
    captured_cid = {}

    class SellTimeoutExchange:
        urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}

        def load_markets(self):
            return {"BTC/USDT": {"id": "BTCUSDT", "active": True, "limits": {"amount": {"min": 0.00001}}}}

        def fetch_balance(self):
            return {"free": {"BTC": 0.001}, "used": {"BTC": 0.0}, "total": {"BTC": 0.001}}

        def amount_to_precision(self, symbol, qty):
            return f"{qty:.6f}"

        def fetch_ticker(self, symbol):
            return {"last": 102000.0, "bid": 101990.0, "ask": 102010.0}

        def create_market_sell_order(self, symbol, quantity, params=None):
            captured_cid["cid"] = (params or {}).get("newClientOrderId")
            raise ccxt.NetworkError("connection dropped")

        def privateGetOrder(self, params):
            assert params["origClientOrderId"] == captured_cid["cid"]
            return {
                "orderId": "555333",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
                "executedQty": "0.000500",
                "cummulativeQuoteQty": "50.99",
            }

    res = binance_client.execute_spot_sell(SellTimeoutExchange(), "BTC/USDT", 0.0005)
    assert res["success"] is True
    assert res["order_id"] == "555333"
    assert res["quantity"] == pytest.approx(0.0005)
    assert res["price"] == pytest.approx(101980.0)


def test_position_manager_restores_pending_order_via_client_order_id(tmp_path, monkeypatch):
    db = tb.TradingDatabase(db_path=str(tmp_path / "pending_reconcile.db"))
    store = pm.PositionStore(db)
    store.save(
        pm.Position(
            symbol="BTC/USDT",
            timeframe="1m",
            mode="testnet",
            model_version=1,
            entry_time="2026-11-21 12:00:00",
            entry_price=100000.0,
            quantity=0.0,
            order_id="fynexb_abc_12345678",
            sl_price=98500.0,
            tp_price=103000.0,
            base_balance_before=0.0,
            status="PENDING",
        )
    )

    class DummyExchange:
        def fetch_balance(self):
            return {"total": {"BTC": 0.0005}, "free": {"BTC": 0.0005}}

    monkeypatch.setattr(
        binance_client,
        "reconcile_order_by_client_id",
        lambda ex, sym, cid: {"id": "777888", "filled": 0.0005, "average": 100100.0, "fees": []},
    )
    oco_called = []
    monkeypatch.setattr(
        binance_client,
        "place_protective_oco",
        lambda *a, **k: (oco_called.append(1) or {"success": True, "order_list_id": "OCO99"}),
    )

    manager = pm.PositionManager(
        db, DummyExchange(), mode="testnet", symbol="BTC/USDT", timeframe="1m",
        stop_loss_pct=0.015, take_profit_pct=0.03, trade_amount_usdt=50.0, use_exchange_oco=True,
    )
    manager.restore()
    assert manager.in_position
    assert manager.position.status == "OPEN"
    assert manager.position.order_id == "777888"
    assert manager.position.entry_price == pytest.approx(100100.0)
    assert manager.position.oco_list_id == "OCO99"
    assert oco_called == [1]


def test_execution_readiness_endpoint(monkeypatch):
    monkeypatch.setattr(
        binance_client,
        "check_execution_readiness",
        lambda symbol="BTC/USDT": {
            "has_keys": True, "trading_mode": "testnet", "testnet": True,
            "api_reachable": True, "clock_synced": True, "can_trade": True,
            "withdraw_disabled": True, "usdt_free": 500.0, "ready_for_orders": True, "warnings": [],
        },
    )
    client = TestClient(server.app)
    r = client.get("/api/binance/execution-ready?symbol=BTC/USDT")
    assert r.status_code == 200
    assert r.json()["ready_for_orders"] is True
