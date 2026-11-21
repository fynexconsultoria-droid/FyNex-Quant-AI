"""
Testes da Auditoria - Fase 2 (ML correto: perfil único, avaliação Triple Barrier ao vivo,
barreira de tempo no PositionManager, purge/embargo sem look-ahead e promoção por PnL).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import trading_bot as tb  # noqa: E402
import position_manager as pm  # noqa: E402
from strategy_config import get_strategy_profile, resolve_barriers  # noqa: E402
from backtest import run_walk_forward_backtest  # noqa: E402


def test_strategy_profile_and_atr_barriers():
    p1 = get_strategy_profile("1m")
    p5 = get_strategy_profile("5m")
    assert p1.tp_pct < p5.tp_pct
    assert p1.horizon_candles >= 20

    p_atr = get_strategy_profile("5m", use_atr_barriers=True)
    tp, sl = resolve_barriers(p_atr, atr_pct=0.005)
    assert p_atr.min_tp_pct <= tp <= p_atr.max_tp_pct
    assert p_atr.min_sl_pct <= sl <= p_atr.max_sl_pct
    assert tp > sl


def test_train_model_rejects_extreme_imbalance():
    df = pd.DataFrame({
        "f1": np.linspace(0, 1, 100),
        "f2": np.linspace(1, 2, 100),
        "Target": [1] * 2 + [0] * 98,  # 2% positivos (< 5%)
    })
    with pytest.raises(tb.InsufficientLabelsError):
        tb.train_model(df, ["f1", "f2"], model_version=1, horizon_candles=5)


def test_live_prediction_evaluation_uses_triple_barrier_rules(tmp_path):
    db = tb.TradingDatabase(db_path=str(tmp_path / "tb_eval.db"))
    t0 = 1_700_000_000_000
    tf_ms = 60_000
    # Candle t0: close 100. Candle t0+1: sobe +0.1% (sem bater TP 1%). Candle t0+2: bate TP +1.2%.
    candles = pd.DataFrame([
        {"timestamp": pd.to_datetime(t0, unit="ms"), "open": 100, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1.0},
        {"timestamp": pd.to_datetime(t0 + tf_ms, unit="ms"), "open": 100, "high": 100.3, "low": 99.9, "close": 100.1, "volume": 1.0},
        {"timestamp": pd.to_datetime(t0 + 2 * tf_ms, unit="ms"), "open": 100.1, "high": 101.2, "low": 100.0, "close": 101.0, "volume": 1.0},
    ])
    db.save_candles("BTC/USDT", "1m", candles.iloc[:2], drop_last_open=False)
    db.record_prediction(
        "BTC/USDT", "1m", 1, t0, prediction=1, prob_up=0.7, prob_down=0.3,
        entry_price=100.0, tp_pct=0.01, sl_pct=0.005, horizon_candles=5,
    )

    # Após 1 candle, ainda não bateu TP/SL nem venceu horizonte -> continua pendente
    ev1 = db.evaluate_pending_predictions("BTC/USDT", "1m", t0 + tf_ms, 100.1)
    assert ev1 == []

    # Quando chega o candle t0+2 que bate TP -> resolve como TP (actual_direction=1, is_correct=True)
    db.save_candles("BTC/USDT", "1m", candles, drop_last_open=False)
    ev2 = db.evaluate_pending_predictions("BTC/USDT", "1m", t0 + 2 * tf_ms, 101.0)
    assert len(ev2) == 1
    assert ev2[0]["outcome"] == "TP"
    assert ev2[0]["actual_direction"] == 1
    assert ev2[0]["is_correct"] is True


def test_position_manager_closes_on_time_barrier(tmp_path):
    db = tb.TradingDatabase(db_path=str(tmp_path / "time_barrier.db"))
    manager = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="1m",
        stop_loss_pct=0.02, take_profit_pct=0.04, trade_amount_usdt=50.0,
        use_exchange_oco=False, horizon_candles=10,
    )
    assert manager.try_open(100_000.0, model_version=1, confidence=0.6)
    entry_dt = datetime.strptime(manager.position.entry_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    # 5 min depois: ainda dentro do horizonte
    manager.check_exits(100_100.0, now_utc=entry_dt + timedelta(minutes=5))
    assert manager.in_position

    # 10 min depois: barreira de tempo dispara
    manager.check_exits(100_100.0, now_utc=entry_dt + timedelta(minutes=10))
    assert not manager.in_position
    with db._get_connection() as conn:
        row = conn.execute("SELECT reason FROM simulated_trades").fetchone()
    assert row["reason"] == "TIME_BARRIER"


def test_walk_forward_purge_prevents_future_leakage():
    rng = np.random.default_rng(123)
    n = 300
    # Ruído puro: sem vazamento de futuro, a acurácia fora da amostra deve ficar perto de 50%
    df = pd.DataFrame({
        "f1": rng.normal(size=n),
        "f2": rng.normal(size=n),
        "Target": rng.integers(0, 2, size=n),
    })
    metrics = tb.evaluate_walk_forward(df, ["f1", "f2"], n_splits=4, horizon_candles=15)
    assert metrics["folds"] >= 2
    assert 0.35 <= metrics["accuracy"] <= 0.65
    assert "brier_score" in metrics and "sim_pnl_pct" in metrics


def test_champion_challenger_promotes_by_sim_pnl_and_buy_signals():
    champ = {"precision_buy": 0.52, "sim_pnl_pct": 1.2, "buy_signals": 30}
    better = {"precision_buy": 0.54, "sim_pnl_pct": 2.5, "buy_signals": 28}
    too_few = {"precision_buy": 0.80, "sim_pnl_pct": 5.0, "buy_signals": 5}
    worse = {"precision_buy": 0.55, "sim_pnl_pct": 0.4, "buy_signals": 25}

    assert tb.should_promote_challenger(better, champ) is True
    assert tb.should_promote_challenger(too_few, champ) is False
    assert tb.should_promote_challenger(worse, champ) is False
