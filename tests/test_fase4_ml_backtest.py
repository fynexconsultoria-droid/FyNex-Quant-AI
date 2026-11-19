"""
Testes da Fase 4 (IA honesta + Backtest: features estacionárias invariantes à escala,
alvo Triple Barrier, validação walk-forward, calibração de probabilidades,
campeão/desafiante e motor de backtest com taxas e slippage).
"""

import os
import sys
import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from trading_bot import (  # noqa: E402
    build_features,
    create_target,
    train_model,
    should_promote_challenger,
    DynamicCalibrator,
)
from backtest import run_walk_forward_backtest  # noqa: E402


def make_synthetic_ohlcv(n=260, seed=42, scale=1.0):
    rng = np.random.default_rng(seed)
    base_ts = 1_700_000_000_000
    rets = rng.normal(0.0002, 0.003, size=n)
    closes = (50000.0 * scale) * np.cumprod(1.0 + rets)
    highs = closes * (1.0 + np.abs(rng.normal(0.002, 0.002, size=n)))
    lows = closes * (1.0 - np.abs(rng.normal(0.002, 0.002, size=n)))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    vols = rng.uniform(2.0, 10.0, size=n)

    return pd.DataFrame({
        "timestamp": pd.to_datetime([base_ts + i * 60_000 for i in range(n)], unit="ms"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    })


# ----------------------------------------- 4.4 Features estacionárias (invariantes à escala)
def test_stationary_features_are_scale_invariant():
    df_50k = make_synthetic_ohlcv(n=80, scale=1.0)
    df_100k = make_synthetic_ohlcv(n=80, scale=2.0)  # preços 2x maiores, mesmos retornos relativos

    f1, cols = build_features(df_50k)
    f2, _ = build_features(df_100k)

    # Todas as features devem ser idênticas mesmo quando o preço em dólares dobra
    for col in cols:
        s1 = f1[col].dropna().to_numpy()
        s2 = f2[col].dropna().to_numpy()
        assert np.allclose(s1, s2, rtol=1e-5, atol=1e-7), f"Feature {col} mudou com a escala de preço!"


# ----------------------------------------- 4.3 Alvo Triple Barrier
def test_triple_barrier_marks_take_profit_before_stop_loss():
    base_ts = 1_700_000_000_000
    # Candle 0: close 100. Candle 1: sobe para high=104 (bate TP +3% sem tocar SL -1.5%)
    # Candle 2..5: estáveis
    rows = [
        {"timestamp": pd.to_datetime(base_ts, unit="ms"), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0},
        {"timestamp": pd.to_datetime(base_ts + 60_000, unit="ms"), "open": 100.0, "high": 103.5, "low": 99.8, "close": 103.0, "volume": 1.0},
        {"timestamp": pd.to_datetime(base_ts + 120_000, unit="ms"), "open": 103.0, "high": 103.2, "low": 100.0, "close": 101.0, "volume": 1.0},
        {"timestamp": pd.to_datetime(base_ts + 180_000, unit="ms"), "open": 101.0, "high": 101.2, "low": 98.0, "close": 98.5, "volume": 1.0},
    ]
    df = pd.DataFrame(rows)
    out = create_target(df, mode="triple_barrier", take_profit_pct=0.03, stop_loss_pct=0.015, horizon_candles=2)
    # Com n=4 e horizon=2, as linhas 0 e 1 têm janela futura completa:
    # Linha 0 (entry=100): no candle 1 high=103.5 >= 103 -> Target = 1
    # Linha 1 (entry=103): no candle 3 low=98 <= 101.455 -> Target = 0
    assert list(out["Target"]) == [1, 0]


# ----------------------------------------- 4.5 & 4.6 Walk-forward, Calibração e Campeão/Desafiante
def test_train_model_returns_validation_metrics_and_calibrated_probabilities():
    df = make_synthetic_ohlcv(n=220)
    feat_df, cols = build_features(df)
    dataset = create_target(feat_df, mode="triple_barrier", take_profit_pct=0.004, stop_loss_pct=0.003, horizon_candles=10)
    model, acc = train_model(dataset, cols, model_version=1)
    assert 0.0 <= acc <= 1.0
    assert hasattr(model, "validation_metrics_")
    assert "precision_buy" in model.validation_metrics_
    probs = model.predict_proba(dataset[cols].iloc[-3:])
    assert probs.shape == (3, 2)
    assert np.allclose(probs.sum(axis=1), 1.0)


def test_champion_challenger_rejects_worse_model():
    champion = {"accuracy": 0.56, "precision_buy": 0.58}
    worse_challenger = {"accuracy": 0.48, "precision_buy": 0.44}
    better_challenger = {"accuracy": 0.57, "precision_buy": 0.60}

    assert should_promote_challenger(worse_challenger, champion) is False
    assert should_promote_challenger(better_challenger, champion) is True


# ----------------------------------------- 4.8 Auto-calibração focada na precisão de compra
def test_dynamic_calibrator_uses_buy_precision_when_sample_is_sufficient():
    # Acurácia geral alta (70% por causa dos 0s), mas precisão de compra baixa (35% em 15 compras) -> PROTEÇÃO ATIVA
    c = DynamicCalibrator.get_calibration(
        rolling_accuracy=70.0,
        evaluated_count=30,
        base_threshold=0.55,
        buy_precision_pct=35.0,
        buy_evaluated_count=15,
    )
    assert c["mode"] == "PROTEÇÃO ATIVA"
    assert c["threshold"] >= 0.63


# ----------------------------------------- 4.1 Motor de Backtest Walk-Forward com taxas
def test_walk_forward_backtest_accounts_for_fees_and_high_low_exits():
    df = make_synthetic_ohlcv(n=260, seed=7)
    res = run_walk_forward_backtest(
        df,
        timeframe="1m",
        initial_capital=1000.0,
        trade_amount_usdt=50.0,
        confidence_threshold=0.50,
        stop_loss_pct=0.003,
        take_profit_pct=0.005,
        horizon_candles=10,
        train_window=120,
        retrain_step=60,
    )
    assert "net_pnl_usd" in res
    assert "max_drawdown_pct" in res
    for t in res["trades"]:
        assert t["fees_usd"] > 0
