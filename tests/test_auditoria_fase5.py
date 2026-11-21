"""
Testes da Auditoria - Fase 5 (Segurança da execução: M5, M6, M7, M9, B10).
"""

import os
import sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import binance_client  # noqa: E402
import position_manager as pm  # noqa: E402
from trading_bot import TradingDatabase  # noqa: E402


def test_failed_sell_after_oco_cancel_replaces_oco(tmp_path, monkeypatch):
    db = TradingDatabase(db_path=str(tmp_path / "m5.db"))
    placed_oco = []

    monkeypatch.setattr(binance_client, "cancel_oco", lambda *a, **k: {"success": True})
    monkeypatch.setattr(binance_client, "execute_spot_sell_with_retry",
                        lambda *a, **k: {"success": False, "message": "network error"})
    monkeypatch.setattr(binance_client, "place_protective_oco",
                        lambda *a, **k: (placed_oco.append(1) or {"success": True, "order_list_id": "NEW_OCO_99"}))

    manager = pm.PositionManager(
        db, object(), mode="testnet", symbol="BTC/USDT", timeframe="5m",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0,
        use_exchange_oco=True,
    )
    manager.position = pm.Position(
        "BTC/USDT", "5m", "testnet", 1, "2026-01-01 00:00:00",
        100_000.0, 0.0005, "B1", 99_000.0, 102_000.0, oco_list_id="OLD_OCO_1",
    )

    assert manager.close("STOP_LOSS_FALLBACK", 98_000.0) is False
    assert manager.in_position
    assert len(placed_oco) == 1
    assert manager.position.oco_list_id == "NEW_OCO_99"


def test_paper_stop_loss_gap_fills_at_worse_current_price(tmp_path):
    db = TradingDatabase(db_path=str(tmp_path / "m7.db"))
    manager = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="5m",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0,
        use_exchange_oco=False,
    )
    assert manager.try_open(100_000.0, model_version=1, confidence=0.6)
    # Gap violento abaixo do SL ($99,000 -> $95,000)
    manager.check_exits(95_000.0)
    assert not manager.in_position
    with db._get_connection() as conn:
        row = conn.execute("SELECT exit_price, pnl_pct FROM simulated_trades").fetchone()
    # Deve preencher perto de $95,000 (com slippage), nunca otimisticamente em $99,000
    assert row["exit_price"] < 95_100.0
    assert row["pnl_pct"] < -5.0


def test_risk_manager_counts_unrealized_loss(tmp_path):
    db = TradingDatabase(db_path=str(tmp_path / "m9.db"))
    risk = pm.RiskManager(
        db, mode="paper", max_daily_loss_usdt=20.0,
        max_trades_per_day=10, max_trade_amount_usdt=200.0,
        kill_switch_path=str(tmp_path / "STOP"),
    )
    ok_before, _ = risk.can_open(50.0, unrealized_pnl_usd=-5.0)
    assert ok_before is True

    ok_blocked, reason = risk.can_open(50.0, unrealized_pnl_usd=-25.0)
    assert ok_blocked is False
    assert "perda diária" in reason


def test_restore_uses_base_balance_before_for_reconciliation(tmp_path):
    db = TradingDatabase(db_path=str(tmp_path / "b10.db"))

    class DummyExchange:
        def fetch_balance(self):
            # Usuário tinha 0.01 BTC antes e comprou 0.0005, mas vendeu manualmente os 0.0005
            # ficando exatamente com os 0.01 BTC originais!
            return {"total": {"BTC": 0.0100}}

    store = pm.PositionStore(db)
    store.save(pm.Position(
        "BTC/USDT", "5m", "testnet", 1, "2026-01-01 00:00:00",
        100_000.0, 0.0005, "B1", 99_000.0, 102_000.0,
        base_balance_before=0.0100,
    ))
    manager = pm.PositionManager(
        db, DummyExchange(), mode="testnet", symbol="BTC/USDT", timeframe="5m",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0,
        use_exchange_oco=False,
    )
    manager.restore()
    # 0.0100 < (0.0100 + 0.0005) * 0.98 = 0.01029 -> detecta encerramento manual!
    assert not manager.in_position
