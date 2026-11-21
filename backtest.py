"""
Módulo de Backtest Walk-Forward com Custos Reais (FyNex Quant AI)
=================================================================
Simula a estratégia quantitativa do robô sobre o histórico de candles com rigor:
- Sem look-ahead bias: treina apenas com dados passados com purge entre treino e teste.
- Usa máxima (high) e mínima (low) de cada candle para disparar Stop Loss e Take Profit.
- Em caso de toque simultâneo no mesmo candle, assume conservadoramente Stop Loss primeiro.
- Inclui taxa Spot da Binance (0.1%/lado) e slippage (0.02%/lado).
- Calcula métricas institucionais: retorno líquido, Win Rate, Profit Factor, Max Drawdown e Sharpe.
"""

import math
import os
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.database import timeframe_to_seconds  # noqa: E402
from position_manager import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT  # noqa: E402
from strategy_config import get_strategy_profile  # noqa: E402
from trading_bot import (  # noqa: E402
    InsufficientLabelsError,
    TradingDatabase,
    build_features,
    create_target,
    get_latest_contiguous_segment,
    predict_signal,
    train_model,
)


def _empty_backtest_result(timeframe: str, samples: int, message: str) -> Dict[str, Any]:
    return {
        "timeframe": timeframe,
        "samples": samples,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": 0.0,
        "net_pnl_usd": 0.0,
        "net_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "profit_factor": 0.0,
        "profit_factor_display": "0.00",
        "trades": [],
        "message": message,
    }


def _compute_backtest_metrics(
    timeframe: str,
    samples: int,
    initial_capital: float,
    final_capital: float,
    equity_curve: List[float],
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = total_trades - wins
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    net_pnl = final_capital - initial_capital
    net_ret_pct = (net_pnl / initial_capital) * 100.0 if initial_capital > 0 else 0.0

    # Max Drawdown na curva de capital
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Profit Factor finito (seguro para JSON)
    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0))
    if gross_loss > 0:
        profit_factor = float(gross_profit / gross_loss)
        profit_factor_display = f"{profit_factor:.2f}"
    elif gross_profit > 0:
        profit_factor = 999.99
        profit_factor_display = "∞"
    else:
        profit_factor = 0.0
        profit_factor_display = "0.00"

    # Sharpe Ratio anualizado pela frequência real dos trades
    if total_trades >= 2:
        rets = np.array([t["pnl_pct"] for t in trades], dtype=float)
        std = float(np.std(rets, ddof=1))
        tf_sec = timeframe_to_seconds(timeframe)
        elapsed_years = max((samples * tf_sec) / (365.0 * 86400.0), 1e-6)
        trades_per_year = total_trades / elapsed_years
        sharpe = float((np.mean(rets) / std) * math.sqrt(trades_per_year)) if std > 1e-9 else 0.0
    else:
        sharpe = 0.0

    return {
        "timeframe": timeframe,
        "samples": samples,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "net_pnl_usd": net_pnl,
        "net_return_pct": net_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
        "profit_factor": profit_factor,
        "profit_factor_display": profit_factor_display,
        "final_capital": final_capital,
        "trades": trades,
    }


def run_walk_forward_backtest(
    df: pd.DataFrame,
    timeframe: str = "5m",
    initial_capital: float = 1000.0,
    trade_amount_usdt: float = 50.0,
    confidence_threshold: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    horizon_candles: Optional[int] = None,
    train_window: int = 400,
    retrain_step: int = 100,
    fee_pct: float = PAPER_FEE_PCT,
    slippage_pct: float = PAPER_SLIPPAGE_PCT,
    target_mode: str = "triple_barrier",
) -> Dict[str, Any]:
    """
    Executa simulação walk-forward sobre `df` (ordenado cronologicamente), com purge
    de `horizon_candles` entre treino e avaliação para eliminar look-ahead bias.
    """
    profile = get_strategy_profile(
        timeframe,
        tp_override=take_profit_pct,
        sl_override=stop_loss_pct,
        horizon_override=horizon_candles,
        confidence_override=confidence_threshold,
    )
    tp_pct = profile.tp_pct
    sl_pct = profile.sl_pct
    horizon = profile.horizon_candles
    conf_th = profile.confidence

    clean_df = get_latest_contiguous_segment(df.copy(), timeframe)
    if len(clean_df) < train_window + horizon + 20:
        return _empty_backtest_result(
            timeframe,
            len(clean_df),
            f"Histórico insuficiente ({len(clean_df)} candles < {train_window + horizon + 20}).",
        )

    feat_df, feature_cols = build_features(clean_df)
    labeled_df = create_target(
        feat_df,
        mode=target_mode,
        take_profit_pct=tp_pct,
        stop_loss_pct=sl_pct,
        horizon_candles=horizon,
    ).reset_index(drop=True)

    purge_gap = horizon if target_mode == "triple_barrier" else 1
    start_idx = train_window + purge_gap

    if len(labeled_df) <= start_idx + 5:
        return _empty_backtest_result(
            timeframe,
            len(labeled_df),
            "Amostras rotuladas insuficientes para walk-forward com purge.",
        )

    capital = float(initial_capital)
    equity_curve: List[float] = [capital]
    trades: List[Dict[str, Any]] = []

    in_pos = False
    entry_price = 0.0
    entry_idx = 0
    qty = 0.0
    sl_price = 0.0
    tp_price = 0.0

    model = None
    last_train_idx = -retrain_step

    closes = labeled_df["close"].to_numpy(dtype=float)
    highs = labeled_df["high"].to_numpy(dtype=float)
    lows = labeled_df["low"].to_numpy(dtype=float)
    X_all = labeled_df[feature_cols]

    for i in range(start_idx, len(labeled_df)):
        # 1. Verifica saída pela mínima/máxima do candle i
        if in_pos:
            hit_sl = lows[i] <= sl_price
            hit_tp = highs[i] >= tp_price
            exit_reason: Optional[str] = None
            raw_exit_price = closes[i]

            if hit_sl:
                exit_reason = "STOP_LOSS"
                raw_exit_price = sl_price
            elif hit_tp:
                exit_reason = "TAKE_PROFIT"
                raw_exit_price = tp_price
            elif (i - entry_idx) >= horizon and target_mode == "triple_barrier":
                exit_reason = "TIME_BARRIER"
                raw_exit_price = closes[i]

            if exit_reason is not None:
                effective_exit = raw_exit_price * (1.0 - slippage_pct)
                gross_entry = entry_price * qty
                gross_exit = effective_exit * qty
                fees = (gross_entry + gross_exit) * fee_pct
                pnl_usd = (gross_exit - gross_entry) - fees
                pnl_pct = (pnl_usd / gross_entry) * 100.0 if gross_entry > 0 else 0.0
                capital += pnl_usd
                equity_curve.append(capital)
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_price": entry_price,
                    "exit_price": effective_exit,
                    "reason": exit_reason,
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_pct,
                    "fees_usd": fees,
                })
                in_pos = False

        # 2. Retreina periodicamente usando APENAS dados passados já resolvidos [i - purge - W : i - purge]
        if model is None or (i - last_train_idx) >= retrain_step:
            train_end = i - purge_gap
            train_start = max(0, train_end - train_window)
            train_slice = labeled_df.iloc[train_start:train_end]
            if train_slice["Target"].nunique() >= 2:
                try:
                    model, _ = train_model(
                        train_slice,
                        feature_cols,
                        model_version=1,
                        calibrate_probabilities=True,
                        refit_full=True,
                        horizon_candles=purge_gap,
                        tp_pct=tp_pct,
                        sl_pct=sl_pct,
                        confidence_threshold=conf_th,
                    )
                    last_train_idx = i
                except InsufficientLabelsError:
                    pass

        # 3. Avalia sinal de entrada no fechamento do candle i
        if not in_pos and model is not None:
            pred, _, prob_up = predict_signal(model, X_all.iloc[i:i + 1])
            if pred == 1 and prob_up >= conf_th:
                entry_price = closes[i] * (1.0 + slippage_pct)
                qty = trade_amount_usdt / entry_price
                sl_price = entry_price * (1.0 - sl_pct)
                tp_price = entry_price * (1.0 + tp_pct)
                entry_idx = i
                in_pos = True

    return _compute_backtest_metrics(
        timeframe=timeframe,
        samples=len(labeled_df),
        initial_capital=initial_capital,
        final_capital=capital,
        equity_curve=equity_curve,
        trades=trades,
    )


def compare_timeframes_from_db(db_path: str = "trading_data.db", symbol: str = "BTC/USDT") -> List[Dict[str, Any]]:
    """Compara o desempenho walk-forward com custos reais usando o perfil único por timeframe."""
    db = TradingDatabase(db_path=db_path)
    results = []
    for tf in ("1m", "5m", "15m"):
        df = db.get_recent_candles(symbol, tf, limit=2000)
        if df.empty:
            continue
        results.append(
            run_walk_forward_backtest(df, timeframe=tf, train_window=400, retrain_step=150)
        )
    return results


if __name__ == "__main__":
    print("=" * 72)
    print("  FyNex Quant AI - Relatório de Backtest Walk-Forward (com Taxas 0.1%/lado)")
    print("=" * 72)
    for r in compare_timeframes_from_db():
        print(
            f"[{r['timeframe']:>3s}] Candles: {r['samples']:4d} | Trades: {r['total_trades']:3d} "
            f"({r['wins']}V/{r['losses']}D | WinRate: {r['win_rate_pct']:5.1f}%) | "
            f"PnL Líquido: ${r['net_pnl_usd']:+7.2f} ({r['net_return_pct']:+5.2f}%) | "
            f"MaxDD: {r['max_drawdown_pct']:4.2f}% | Sharpe: {r['sharpe_ratio']:+5.2f}"
        )
    print("=" * 72)
