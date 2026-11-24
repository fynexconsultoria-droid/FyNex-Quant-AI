"""
Hard-Example Weighting a partir de Erros Reais (FyNex Quant AI)
===============================================================
Calcula multiplicadores de peso (`>= 1.0`, teto `max_multiplier`) aplicados
exclusivamente no refit final de produção (`refit_full=True`), mantendo
validação walk-forward e holdout 100% livres de vazamento temporal.
"""

from __future__ import annotations

import math
import os
from typing import Optional

import numpy as np
import pandas as pd

from core.error_learning.store import ErrorLearningStore


def compute_hard_example_multipliers(
    df_train: pd.DataFrame,
    db_path: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    max_multiplier: float = 2.5,
) -> np.ndarray:
    """Calcula multiplicadores de peso para reforçar exemplos onde a IA errou recentemente."""
    n = len(df_train)
    if n == 0:
        return np.ones(0, dtype=float)
    mult = np.ones(n, dtype=float)
    if not db_path or not symbol or not timeframe or not os.path.exists(db_path):
        return mult

    max_ts_ms: Optional[int] = None
    if "timestamp" in df_train.columns and len(df_train) > 0:
        try:
            last_ts = df_train["timestamp"].iloc[-1]
            max_ts_ms = int(last_ts.timestamp() * 1000) if hasattr(last_ts, "timestamp") else int(last_ts)
        except Exception:
            max_ts_ms = None

    try:
        store = ErrorLearningStore(db_path)
        profile = store.get_adaptive_learning_profile(
            symbol, timeframe, lookback_days=30, max_candle_ts_ms=max_ts_ms
        )
    except Exception:
        return mult

    failed_ts = profile.get("failed_timestamps") or {}
    recurring = profile.get("recurring_causes") or {}
    if not failed_ts and not recurring:
        return mult

    target_arr = df_train["Target"].to_numpy(dtype=int) if "Target" in df_train.columns else np.zeros(n, dtype=int)

    if failed_ts and "timestamp" in df_train.columns:
        ts_col = df_train["timestamp"]
        for i, ts_val in enumerate(ts_col):
            if target_arr[i] != 0:
                continue
            try:
                ts_ms = int(ts_val.timestamp() * 1000) if hasattr(ts_val, "timestamp") else int(ts_val)
            except Exception:
                continue
            boost = failed_ts.get(ts_ms)
            if boost is not None and boost > 1.0:
                mult[i] = max(mult[i], float(boost))

    if recurring:
        neg_mask = target_arr == 0
        if "OVERBOUGHT_ENTRY" in recurring and "rsi_14" in df_train.columns:
            m = neg_mask & (pd.to_numeric(df_train["rsi_14"], errors="coerce").fillna(0.0).to_numpy() >= 64.0)
            mult[m] *= 1.30
        if "AGAINST_TREND" in recurring and "ema_trend_ratio" in df_train.columns:
            m = neg_mask & (pd.to_numeric(df_train["ema_trend_ratio"], errors="coerce").fillna(0.0).to_numpy() <= -0.0015)
            mult[m] *= 1.30
        if "CHOPPY_SIDEWAYS" in recurring and "adx_14" in df_train.columns:
            adx_v = pd.to_numeric(df_train["adx_14"], errors="coerce").fillna(1.0).to_numpy()
            m = neg_mask & (adx_v >= 0.0) & (adx_v < 0.18)
            mult[m] *= 1.25
        if "LOW_VOLUME_BREAKOUT" in recurring:
            if "volume_zscore_20" in df_train.columns:
                vz = pd.to_numeric(df_train["volume_zscore_20"], errors="coerce").fillna(0.0).to_numpy()
                m = neg_mask & (vz <= -0.50)
                mult[m] *= 1.25
            elif "vol_ratio" in df_train.columns:
                vol_v = pd.to_numeric(df_train["vol_ratio"], errors="coerce").fillna(1.0).to_numpy()
                m = neg_mask & (vol_v >= 0.0) & (vol_v < 0.80)
                mult[m] *= 1.25
        if "VOLATILITY_SPIKE" in recurring and "atr_pct" in df_train.columns:
            atr_v = pd.to_numeric(df_train["atr_pct"], errors="coerce").fillna(0.0).to_numpy()
            m = neg_mask & (atr_v > 0.016)
            mult[m] *= 1.30
        if "NEGATIVE_MOMENTUM" in recurring:
            macd_col = "macd_diff" if "macd_diff" in df_train.columns else ("macd_hist" if "macd_hist" in df_train.columns else None)
            ret_col = "return_5" if "return_5" in df_train.columns else ("roc_5" if "roc_5" in df_train.columns else None)
            if macd_col and ret_col:
                mh = pd.to_numeric(df_train[macd_col], errors="coerce").fillna(0.0).to_numpy()
                r5 = pd.to_numeric(df_train[ret_col], errors="coerce").fillna(0.0).to_numpy()
                m = neg_mask & (mh < 0.0) & (r5 <= -0.002)
                mult[m] *= 1.25

    mult = np.clip(mult, 1.0, max(1.0, float(max_multiplier)))
    mean_val = float(np.mean(mult))
    if mean_val > 0 and math.isfinite(mean_val):
        mult = mult / mean_val
    return mult
