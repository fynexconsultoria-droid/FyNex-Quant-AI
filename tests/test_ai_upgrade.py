"""
Testes do Upgrade da IA:
- 22 features estacionárias (Bollinger, ADX, EMA trend, volume z-score, estrutura do candle)
- Arquiteturas RF, HistGradientBoosting e Ensemble calibrado
- Pipeline de treino e seleção de campeão (`train_ai.train_and_select_best_model`)
"""

import os
import sys
import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from core.ml import build_features, create_target, train_model  # noqa: E402
from train_ai import train_and_select_best_model  # noqa: E402


def _make_ohlcv(n=260, seed=101, scale=1.0):
    rng = np.random.default_rng(seed)
    base_ts = 1_700_000_000_000
    rets = rng.normal(0.0003, 0.004, size=n)
    closes = (50000.0 * scale) * np.cumprod(1.0 + rets)
    highs = closes * (1.0 + np.abs(rng.normal(0.0025, 0.002, size=n)))
    lows = closes * (1.0 - np.abs(rng.normal(0.0025, 0.002, size=n)))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    vols = rng.uniform(5.0, 50.0, size=n)
    return pd.DataFrame({
        "timestamp": pd.to_datetime([base_ts + i * 300_000 for i in range(n)], unit="ms"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    })


def test_upgraded_features_count_and_scale_invariance():
    df1 = _make_ohlcv(n=100, scale=1.0)
    df2 = _make_ohlcv(n=100, scale=3.5)

    f1, cols = build_features(df1)
    f2, _ = build_features(df2)

    assert len(cols) == 22
    for expected in ("bb_pos", "bb_width", "adx_14", "ema_trend_ratio", "volume_zscore_20", "candle_body_pct"):
        assert expected in cols

    for col in cols:
        s1 = f1[col].dropna().to_numpy()
        s2 = f2[col].dropna().to_numpy()
        assert len(s1) >= 60
        assert np.allclose(s1, s2, rtol=1e-5, atol=1e-6), f"Feature {col} não é invariante à escala!"


def test_train_model_supports_rf_hgb_and_ensemble():
    df = _make_ohlcv(n=240, seed=77)
    feat_df, cols = build_features(df)
    dataset = create_target(feat_df, mode="triple_barrier", take_profit_pct=0.006, stop_loss_pct=0.004, horizon_candles=10)

    for mtype in ("rf", "hgb", "ensemble"):
        model, prec = train_model(dataset, cols, model_version=1, model_type=mtype, horizon_candles=10)
        assert 0.0 <= prec <= 1.0
        assert model.validation_metrics_["model_type"] == mtype
        probs = model.predict_proba(dataset[cols].iloc[-3:])
        assert probs.shape == (3, 2)
        assert np.allclose(probs.sum(axis=1), 1.0)


def test_train_and_select_best_model_saves_winner(tmp_path):
    df = _make_ohlcv(n=260, seed=55)
    summary = train_and_select_best_model(
        df,
        symbol="BTC/USDT",
        timeframe="5m",
        candidates=("rf", "hgb", "ensemble"),
        models_dir=str(tmp_path / "models"),
        save_winner=True,
    )
    assert summary["winner"] is not None
    assert summary["winner"]["model_type"] in ("rf", "hgb", "ensemble")
    assert len(summary["candidates"]) == 3
    assert summary["saved_path"] is not None
    assert os.path.exists(summary["saved_path"])


def test_recency_weights_and_threshold_optimizer_and_et_model():
    from core.ml import _compute_recency_weights, optimize_confidence_threshold

    w = _compute_recency_weights(100, decay_floor=0.25)
    assert len(w) == 100
    assert w[-1] > w[0]
    assert np.isclose(np.mean(w), 1.0, atol=1e-6)

    # Probabilidades altas sempre acertam -> limiar ótimo deve escolher faixa alta e gerar PnL > 0
    y = np.array([0] * 30 + [1] * 20)
    p = np.concatenate([np.linspace(0.20, 0.55, 30), np.linspace(0.60, 0.85, 20)])
    th, pnl = optimize_confidence_threshold(y, p, tp_pct=0.01, sl_pct=0.005, min_signals=5)
    assert 0.55 <= th <= 0.68
    assert pnl > 0

    df = _make_ohlcv(n=240, seed=88)
    feat_df, cols = build_features(df)
    dataset = create_target(feat_df, mode="triple_barrier", take_profit_pct=0.006, stop_loss_pct=0.004, horizon_candles=10)
    model, _ = train_model(dataset, cols, model_version=2, model_type="et", horizon_candles=10)
    vm = model.validation_metrics_
    assert vm["model_type"] == "et"
    assert "profit_factor" in vm
    assert "expectancy_pct" in vm
    assert "roc_auc" in vm
    assert "optimal_threshold" in vm
    assert isinstance(vm.get("top_features"), list) and len(vm["top_features"]) > 0
