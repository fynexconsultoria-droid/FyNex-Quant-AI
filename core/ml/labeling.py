"""
Rotulagem de Variável Alvo (Triple Barrier / Custo-Consciente) — FyNex Quant AI
===============================================================================
Constrói a coluna `Target` binária sem look-ahead bias, suportando barreiras
fixas ou adaptativas por ATR.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def create_target(
    df: pd.DataFrame,
    mode: str = "next_candle_cost",
    take_profit_pct: float = 0.004,
    stop_loss_pct: float = 0.003,
    horizon_candles: int = 15,
    min_net_return_pct: float = 0.0,
    use_atr_barriers: bool = False,
    atr_tp_mult: float = 2.0,
    atr_sl_mult: float = 1.3,
    min_tp_pct: float = 0.008,
    max_tp_pct: float = 0.025,
    min_sl_pct: float = 0.005,
    max_sl_pct: float = 0.015,
) -> pd.DataFrame:
    """
    Constrói a variável dependente binária (`Target`) sem look-ahead bias:
    - `triple_barrier`: Target=1 se TP for atingido antes do SL dentro de `horizon_candles`.
      Quando `use_atr_barriers=True` e `atr_pct` existe no DataFrame, escala TP/SL
      dinamicamente pela volatilidade (ATR) de cada candle.
    - `next_candle_cost`: Target=1 se o próximo fechamento superar `close * (1 + min_net_return_pct)`.
    """
    df = df.copy()

    if mode == "triple_barrier":
        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        atr_vals = df["atr_pct"].to_numpy(dtype=float) if (use_atr_barriers and "atr_pct" in df.columns) else None
        n = len(df)
        targets = np.full(n, np.nan, dtype=float)
        h = max(1, int(horizon_candles))

        for i in range(0, max(0, n - h)):
            entry = closes[i]
            if not (np.isfinite(entry) and entry > 0.0):
                continue
            fut_highs = highs[i + 1: i + h + 1]
            fut_lows = lows[i + 1: i + h + 1]
            if not (np.all(np.isfinite(fut_highs)) and np.all(np.isfinite(fut_lows))):
                continue
            if atr_vals is not None and np.isfinite(atr_vals[i]) and atr_vals[i] > 0:
                eff_tp = min(max(atr_vals[i] * atr_tp_mult, min_tp_pct), max_tp_pct)
                eff_sl = min(max(atr_vals[i] * atr_sl_mult, min_sl_pct), max_sl_pct)
            else:
                eff_tp = take_profit_pct
                eff_sl = stop_loss_pct
            tp_level = entry * (1.0 + eff_tp)
            sl_level = entry * (1.0 - eff_sl)
            outcome = 0.0
            for j in range(i + 1, i + h + 1):
                hit_sl = lows[j] <= sl_level
                hit_tp = highs[j] >= tp_level
                if hit_sl and hit_tp:
                    outcome = 0.0
                    break
                if hit_sl:
                    outcome = 0.0
                    break
                if hit_tp:
                    outcome = 1.0
                    break
            targets[i] = outcome

        df["Target"] = targets
    else:
        threshold = df["close"] * (1.0 + float(min_net_return_pct))
        df["Target"] = np.where(
            df["close"].shift(-1).isna(),
            np.nan,
            (df["close"].shift(-1) > threshold).astype(float),
        )

    num_cols = df.select_dtypes(include=[np.number]).columns
    df[num_cols] = df[num_cols].replace([np.inf, -np.inf], np.nan)
    df_clean = df.dropna().copy()
    df_clean["Target"] = df_clean["Target"].astype(int)
    return df_clean
