"""
Testes da Fase 1 (Segurança do dinheiro).
Rodar com:  python -m pytest -q
Nenhum teste acessa a rede: as chamadas à Binance são substituídas por mocks.
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import binance_client  # noqa: E402
import position_manager as pm  # noqa: E402
from trading_bot import TradingDatabase  # noqa: E402


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def db(tmp_path):
    return TradingDatabase(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def no_kill_switch(tmp_path):
    return str(tmp_path / "STOP")


def make_risk(db, mode="paper", kill_path=None, **kw):
    return pm.RiskManager(
        db, mode=mode,
        max_daily_loss_usdt=kw.get("max_loss", 20.0),
        max_trades_per_day=kw.get("max_trades", 10),
        max_trade_amount_usdt=kw.get("max_amount", 200.0),
        kill_switch_path=kill_path,
    )


def make_pm(db, mode="paper", risk=None, use_oco=False, exchange=None):
    return pm.PositionManager(
        db, exchange or object(), mode=mode, symbol="BTC/USDT", timeframe="1m",
        stop_loss_pct=0.015, take_profit_pct=0.03, trade_amount_usdt=50.0,
        use_exchange_oco=use_oco, risk=risk,
    )


def trades(db):
    with db._get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM simulated_trades").fetchall()]


# ------------------------------------------------- 1.1 modo x ambiente (C2)
def test_testnet_mode_always_uses_sandbox():
    assert binance_client.resolve_environment("testnet", True) is True


def test_testnet_mode_with_production_keys_is_refused():
    with pytest.raises(binance_client.EnvironmentConfigError):
        binance_client.resolve_environment("testnet", False)


def test_live_mode_with_testnet_keys_is_refused():
    with pytest.raises(binance_client.EnvironmentConfigError):
        binance_client.resolve_environment("live", True)


def test_live_mode_uses_production_and_paper_uses_real_data():
    assert binance_client.resolve_environment("live", False) is False
    assert binance_client.resolve_environment("paper", True) is False


def test_sandbox_exchange_points_to_testnet():
    ex = binance_client.get_binance_exchange(testnet=True)
    assert "testnet" in str(ex.urls["api"]["public"])


# ----------------------------------------------------- 1.7 validação .env (A6)
def test_credential_with_newline_is_rejected():
    with pytest.raises(ValueError):
        binance_client.validate_api_credential("abcdefghijklmnop\nTRADING_MODE=live", "API Key")


def test_save_config_rejects_injection_and_keeps_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=paper\n", encoding="utf-8")
    monkeypatch.setattr(binance_client, "ENV_PATH", str(env))
    with pytest.raises(ValueError):
        binance_client.save_binance_config("A" * 20 + "\nTRADING_MODE=live", "", False, "paper", 50)
    assert env.read_text(encoding="utf-8") == "TRADING_MODE=paper\n"


def test_save_config_derives_environment_from_mode(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(binance_client, "ENV_PATH", str(env))
    for var in ["BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET", "TRADING_MODE"]:
        monkeypatch.delenv(var, raising=False)
    binance_client.save_binance_config("A" * 20, "B" * 20, False, "testnet", 50)
    cfg = binance_client.load_binance_config()
    assert cfg["trading_mode"] == "testnet" and cfg["testnet"] is True


# ------------------------------------------------ 1.3 quantidade líquida (C4)
def test_net_quantity_discounts_base_fee():
    order = {"filled": 0.001, "fees": [{"currency": "BTC", "cost": 0.000001}, {"currency": "USDT", "cost": 0.05}]}
    assert binance_client.net_filled_quantity(order, "BTC", 0.001) == pytest.approx(0.000999)


def test_net_quantity_without_base_fee_is_gross():
    order = {"filled": 0.001, "fees": [{"currency": "BNB", "cost": 0.0001}]}
    assert binance_client.net_filled_quantity(order, "BTC", 0.001) == pytest.approx(0.001)


# ----------------------------------------------------------- paper trading
def test_paper_open_and_take_profit_pnl_matches_position_size(db, no_kill_switch):
    manager = make_pm(db, risk=make_risk(db, kill_path=no_kill_switch))
    assert manager.try_open(100_000.0, model_version=1, confidence=0.6)
    assert manager.position.quantity == pytest.approx(50.0 / 100_000.0)
    manager.check_exits(103_000.0)
    assert not manager.in_position
    t = trades(db)[0]
    assert t["reason"] == "TAKE_PROFIT"
    # 3% de $50 = $1.50 bruto; menos slippage (0.02%) e taxas Spot (0.1% entrada + 0.1% saída)
    assert 1.35 < t["pnl_usd"] < 1.50
    assert 2.70 < t["pnl_pct"] < 3.00


# ----------------------------------------------- 1.4 posição persistida (C5)
def test_position_survives_restart(db, no_kill_switch):
    first = make_pm(db, risk=make_risk(db, kill_path=no_kill_switch))
    first.try_open(100_000.0, 1, 0.6)
    second = make_pm(db)  # "reinício" do processo
    assert not second.in_position
    second.restore()
    assert second.in_position
    assert second.position.entry_price == pytest.approx(100_000.0)
    assert second.position.sl_price == pytest.approx(98_500.0)


# ------------------------------------------------- 1.2 venda robusta (C3)
def test_failed_sell_keeps_position_open_and_records_nothing(db, monkeypatch):
    monkeypatch.setattr(binance_client, "execute_spot_sell_with_retry",
                        lambda *a, **k: {"success": False, "message": "insufficient balance"})
    manager = make_pm(db, mode="testnet")
    manager.position = pm.Position("BTC/USDT", "1m", "testnet", 1, "2026-01-01 00:00:00",
                                   100_000.0, 0.0005, "1", 98_500.0, 103_000.0)
    manager.check_exits(98_000.0)
    assert manager.in_position
    assert trades(db) == []
    assert manager.consecutive_sell_failures == 1


def test_successful_sell_records_real_exit_price(db, monkeypatch):
    monkeypatch.setattr(binance_client, "execute_spot_sell_with_retry",
                        lambda *a, **k: {"success": True, "price": 98_400.0, "order_id": "S1", "quantity": 0.0005})
    manager = make_pm(db, mode="testnet")
    manager.position = pm.Position("BTC/USDT", "1m", "testnet", 1, "2026-01-01 00:00:00",
                                   100_000.0, 0.0005, "1", 98_500.0, 103_000.0)
    manager.check_exits(98_450.0)
    assert not manager.in_position
    t = trades(db)[0]
    assert t["exit_price"] == pytest.approx(98_400.0)
    assert t["pnl_pct"] == pytest.approx(-1.6)  # calculado com o preço REAL de saída


# ---------------------------------------------------------------- 1.5 OCO
def test_oco_fill_is_recorded_as_exit(db, monkeypatch):
    monkeypatch.setattr(binance_client, "get_oco_status", lambda *a, **k: {
        "success": True, "list_status": "ALL_DONE", "filled": True,
        "order_id": "TP1", "price": 103_000.0, "quantity": 0.0005, "reason": "TAKE_PROFIT"})
    manager = make_pm(db, mode="testnet", use_oco=True)
    manager.position = pm.Position("BTC/USDT", "1m", "testnet", 1, "2026-01-01 00:00:00",
                                   100_000.0, 0.0005, "1", 98_500.0, 103_000.0, oco_list_id="77")
    manager.check_exits(103_100.0)
    assert not manager.in_position
    assert trades(db)[0]["reason"] == "TAKE_PROFIT"


def test_active_oco_prevents_bot_side_sell(db, monkeypatch):
    monkeypatch.setattr(binance_client, "get_oco_status",
                        lambda *a, **k: {"success": True, "list_status": "EXECUTING", "filled": False})
    called = []
    monkeypatch.setattr(binance_client, "execute_spot_sell_with_retry", lambda *a, **k: called.append(1))
    manager = make_pm(db, mode="testnet", use_oco=True)
    manager.position = pm.Position("BTC/USDT", "1m", "testnet", 1, "2026-01-01 00:00:00",
                                   100_000.0, 0.0005, "1", 98_500.0, 103_000.0, oco_list_id="77")
    manager.check_exits(98_490.0)  # abaixo do SL, mas a OCO é a responsável
    assert manager.in_position and called == []


def test_restore_detects_oco_filled_while_offline(db, monkeypatch):
    monkeypatch.setattr(binance_client, "get_oco_status", lambda *a, **k: {
        "success": True, "list_status": "ALL_DONE", "filled": True,
        "order_id": "SL1", "price": 98_400.0, "quantity": 0.0005, "reason": "STOP_LOSS"})
    store = pm.PositionStore(db)
    store.save(pm.Position("BTC/USDT", "1m", "testnet", 1, "2026-01-01 00:00:00",
                           100_000.0, 0.0005, "1", 98_500.0, 103_000.0, oco_list_id="77"))
    manager = make_pm(db, mode="testnet", use_oco=True)
    manager.restore()
    assert not manager.in_position
    assert trades(db)[0]["reason"] == "STOP_LOSS"


# ----------------------------------------------------- 1.8 limites de risco
def test_kill_switch_blocks_entries(db, tmp_path):
    kill = tmp_path / "STOP"
    kill.write_text("")
    manager = make_pm(db, risk=make_risk(db, kill_path=str(kill)))
    assert not manager.try_open(100_000.0, 1, 0.6)


def test_daily_loss_limit_blocks_entries(db, no_kill_switch):
    today = pm.utc_now_str()
    db.record_trade("BTC/USDT", "1m", 1, today, 100.0, today, 50.0, "STOP_LOSS", -50.0, -25.0, mode="paper")
    ok, reason = make_risk(db, kill_path=no_kill_switch).can_open(50.0)
    assert not ok and "perda diária" in reason


def test_max_trades_per_day_blocks_entries(db, no_kill_switch):
    today = pm.utc_now_str()
    for _ in range(3):
        db.record_trade("BTC/USDT", "1m", 1, today, 100.0, today, 101.0, "TAKE_PROFIT", 1.0, 0.5, mode="paper")
    ok, _ = make_risk(db, kill_path=no_kill_switch, max_trades=3).can_open(50.0)
    assert not ok


def test_trade_amount_above_max_is_blocked(db, no_kill_switch):
    ok, _ = make_risk(db, kill_path=no_kill_switch, max_amount=100.0).can_open(150.0)
    assert not ok


def test_limits_are_per_mode(db, no_kill_switch):
    today = pm.utc_now_str()
    db.record_trade("BTC/USDT", "1m", 1, today, 100.0, today, 50.0, "STOP_LOSS", -50.0, -25.0, mode="testnet")
    ok, _ = make_risk(db, mode="paper", kill_path=no_kill_switch).can_open(50.0)
    assert ok


# ----------------------------------------------------- 0.4 trava do modo live
def test_live_trading_is_locked():
    import trading_bot
    assert trading_bot.LIVE_TRADING_ENABLED is False
