"""
Engenharia de Features Técnicas Estacionárias (FyNex Quant AI)
==============================================================
Calcula 22 indicadores estacionários (invariantes à escala de preço):
momentum, tendência, volatilidade, estrutura de candle, volume e sazonalidade cíclica.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

FEATURE_COLUMNS: Tuple[str, ...] = (
    "rsi_14",
    "macd_line",
    "macd_signal",
    "macd_diff",
    "atr_pct",
    "sma_ratio",
    "ema_trend_ratio",
    "bb_pos",
    "bb_width",
    "adx_14",
    "return_1",
    "return_3",
    "return_5",
    "return_10",
    "volatility_10",
    "volume_change",
    "volume_zscore_20",
    "candle_body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "hour_sin",
    "hour_cos",
)


def build_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Calcula 22 indicadores estacionários (100% invariantes à escala de preço):
    - Momentum e tendência: RSI 14, MACD normalizado, razão SMA9/SMA21, razão EMA12/EMA26, retornos multi-horizonte
    - Volatilidade e regime: ATR %, Bollinger %B, Bollinger Width, ADX 14, volatilidade realizada
    - Volume e estrutura do candle: variação de volume, z-score de volume, corpo e pavios relativos
    - Sazonalidade intradiária cíclica (sin/cos da hora)
    """
    df = df.copy()
    close = df["close"].replace(0, np.nan)

    # 1. RSI (14)
    df["rsi_14"] = RSIIndicator(close=df["close"], window=14).rsi()

    # 2. MACD (12, 26, 9) normalizado pelo preço
    macd_indicator = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_line"] = macd_indicator.macd() / close
    df["macd_signal"] = macd_indicator.macd_signal() / close
    df["macd_diff"] = macd_indicator.macd_diff() / close

    # 3. ATR Normalizado (% do preço)
    atr_indicator = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14)
    df["atr_pct"] = atr_indicator.average_true_range() / close

    # 4. Médias Móveis (SMA 9/21 e EMA 12/26)
    sma_9 = SMAIndicator(close=df["close"], window=9).sma_indicator()
    sma_21 = SMAIndicator(close=df["close"], window=21).sma_indicator()
    df["sma_ratio"] = (sma_9 / sma_21) - 1.0

    ema_12 = EMAIndicator(close=df["close"], window=12).ema_indicator()
    ema_26 = EMAIndicator(close=df["close"], window=26).ema_indicator()
    df["ema_trend_ratio"] = (ema_12 / ema_26) - 1.0

    # 5. Bandas de Bollinger (posição relativa %B e largura normalizada)
    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low = bb.bollinger_lband()
    bb_mid = bb.bollinger_mavg().replace(0, np.nan)
    bb_span = (bb_high - bb_low).replace(0, np.nan)
    df["bb_pos"] = ((df["close"] - bb_low) / bb_span).clip(-0.5, 1.5)
    df["bb_width"] = (bb_high - bb_low) / bb_mid

    # 6. ADX 14 (força da tendência, normalizado em 0..1)
    if len(df) >= 28:
        adx_val = ADXIndicator(high=df["high"], low=df["low"], close=df["close"], window=14).adx()
        df["adx_14"] = (adx_val / 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    else:
        df["adx_14"] = 0.0

    # 7. Momentum em múltiplos horizontes, volatilidade e dinâmica de volume
    df["return_1"] = df["close"].pct_change(1)
    df["return_3"] = df["close"].pct_change(3)
    df["return_5"] = df["close"].pct_change(5)
    df["return_10"] = df["close"].pct_change(10)
    df["volatility_10"] = df["return_1"].rolling(10).std()
    df["volume_change"] = df["volume"].pct_change(1).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-2.0, 5.0)

    vol_mean = df["volume"].rolling(20).mean()
    vol_std = df["volume"].rolling(20).std().replace(0, np.nan)
    df["volume_zscore_20"] = (
        ((df["volume"] - vol_mean) / vol_std)
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .clip(-3.0, 5.0)
    )

    # 8. Estrutura do candle (invariante à escala)
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    body_top = np.maximum(df["open"], df["close"])
    body_bot = np.minimum(df["open"], df["close"])
    df["candle_body_pct"] = ((df["close"] - df["open"]) / candle_range).fillna(0.0).clip(-1.0, 1.0)
    df["upper_wick_pct"] = ((df["high"] - body_top) / candle_range).fillna(0.0).clip(0.0, 1.0)
    df["lower_wick_pct"] = ((body_bot - df["low"]) / candle_range).fillna(0.0).clip(0.0, 1.0)

    # 9. Sazonalidade intradiária cíclica (auto-detecta timestamps em segundos, ms ou strings ISO)
    if "timestamp" in df.columns:
        ts_col = df["timestamp"]
        if pd.api.types.is_datetime64_any_dtype(ts_col):
            hours = ts_col.dt.hour + ts_col.dt.minute / 60.0
        elif pd.api.types.is_numeric_dtype(ts_col):
            num_ts = pd.to_numeric(ts_col, errors="coerce")
            med_ts = float(num_ts.dropna().median()) if not num_ts.dropna().empty else 0.0
            ts_unit = "s" if 0 < med_ts < 100_000_000_000 else "ms"
            ts_dt = pd.to_datetime(num_ts, unit=ts_unit, errors="coerce")
            hours = (ts_dt.dt.hour + ts_dt.dt.minute / 60.0).fillna(0.0)
        else:
            ts_dt = pd.to_datetime(ts_col, errors="coerce")
            hours = (ts_dt.dt.hour + ts_dt.dt.minute / 60.0).fillna(0.0)
    else:
        hours = pd.Series(0.0, index=df.index)
    df["hour_sin"] = np.sin(2.0 * np.pi * hours / 24.0)
    df["hour_cos"] = np.cos(2.0 * np.pi * hours / 24.0)

    feature_cols = list(FEATURE_COLUMNS)
    for col in feature_cols:
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    return df, feature_cols
