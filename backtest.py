"""
Módulo de Backtest Walk-Forward com Custos Reais (Fase 4.1)
===========================================================
Simula a estratégia quantitativa do robô sobre o histórico de candles com rigor:
- Sem look-ahead bias: treina apenas com dados passados e avança em janela deslizante.
- Usa a máxima (high) e a mínima (low) de cada candle para disparar Stop Loss e Take Profit.
- Se um mesmo candle tocar SL e TP, assume conservadoramente que o Stop Loss ocorreu primeiro.
- Inclui taxa Spot da Binance (0.1% na compra + 0.1% na venda) e slippage (0.02% por lado).
- Calcula métricas institucionais: retorno líquido, Win Rate, Profit Factor, Max Drawdown e Sharpe.
"""

import os
import sys
import math
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from trading_bot import (  # noqa: E402
    TradingDatabase,
    build_features,
    create_target,
    train_model,
    get_latest_contiguous_segment,
)
from position_manager import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT  # noqa: E402


def run_walk_forward_backtest(
    df: pd.DataFrame,
    timeframe: str = "1m",
    initial_capital: float = 1000.0,
    trade_amount_usdt: float = 50.0,
    confidence_threshold: float = 0.55,
    stop_loss_pct: float = 0.015,
    take_profit_pct: float = 0.03,
    horizon_candles: int = 15,
    train_window: int = 400,
    retrain_step: int = 100,
    fee_pct: float = PAPER_FEE_PCT,
    slippage_pct: float = PAPER_SLIPPAGE_PCT,
    target_mode: str = "triple_barrier",
) -> Dict[str, Any]:
    """
    Executa simulação walk-forward sobre `df` (ordenado cronologicamente).
    Retorna dicionário com estatísticas completas e lista de trades simulados.
    """
    clean_df = get_latest_contiguous_segment(df.copy(), timeframe)
    if len(clean_df) < train_window + 30:
        return {
            "timeframe": timeframe,
            "samples": len(clean_df),
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "net_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "profit_factor": 0.0,
            "trades": [],
            "message": f"Histórico insuficiente ({len(clean_df)} candles < {train_window + 30}).",
        }

    feat_df, feature_cols = build_features(clean_df)
    labeled_df = create_target(
        feat_df,
        mode=target_mode,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        horizon_candles=horizon_candles,
    ).reset_index(drop=True)

    if len(labeled_df) < train_window + 10:
        return {
            "timeframe": timeframe,
            "samples": len(labeled_df),
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "net_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "profit_factor": 0.0,
            "trades": [],
            "message": "Amostras rotuladas insuficientes para walk-forward.",
        }

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

    for i in range(train_window, len(labeled_df)):
        # 1. Se estiver posicionado, verifica saída pela mínima/máxima do candle i
        if in_pos:
            hit_sl = lows[i] <= sl_price
            hit_tp = highs[i] >= tp_price
            exit_reason: Optional[str] = None
            raw_exit_price = closes[i]

            if hit_sl and hit_tp:
                # Conservador: Stop Loss primeiro quando ambos ocorrem no mesmo candle
                exit_reason = "STOP_LOSS"
                raw_exit_price = sl_price
            elif hit_sl:
                exit_reason = "STOP_LOSS"
                raw_exit_price = sl_price
            elif hit_tp:
                exit_reason = "TAKE_PROFIT"
                raw_exit_price = tp_price
            elif (i - entry_idx) >= horizon_candles and target_mode == "triple_barrier":
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

        # 2. Retreina o modelo periodicamente usando APENAS dados passados [i - train_window : i]
        if model is None or (i - last_train_idx) >= retrain_step:
            train_slice = labeled_df.iloc[max(0, i - train_window):i]
            if train_slice["Target"].nunique() >= 2:
                model, _ = train_model(
                    train_slice,
                    feature_cols,
                    model_version=1,
                    calibrate_probabilities=True,
                    refit_full=True,
                )
                last_train_idx = i

        # 3. Se não estiver posicionado, avalia sinal de entrada no fechamento do candle i
        if not in_pos and model is not None:
            row_X = X_all.iloc[i:i + 1]
            pred = int(model.predict(row_X)[0])
            probs = model.predict_proba(row_X)[0]
            prob_up = float(probs[1]) if len(probs) > 1 else 0.0
            if pred == 1 and prob_up >= confidence_threshold:
                entry_price = closes[i] * (1.0 + slippage_pct)
                qty = trade_amount_usdt / entry_price
                sl_price = entry_price * (1.0 - stop_loss_pct)
                tp_price = entry_price * (1.0 + take_profit_pct)
                entry_idx = i
                in_pos = True

    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = total_trades - wins
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    net_pnl = capital - initial_capital
    net_ret_pct = (net_pnl / initial_capital) * 100.0

    # Max Drawdown na curva de capital
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Profit Factor & Sharpe Ratio por trade
    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    if total_trades >= 2:
        rets = np.array([t["pnl_pct"] for t in trades], dtype=float)
        std = float(np.std(rets, ddof=1))
        sharpe = float((np.mean(rets) / std) * math.sqrt(total_trades)) if std > 1e-9 else 0.0
    else:
        sharpe = 0.0

    return {
        "timeframe": timeframe,
        "samples": len(labeled_df),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "net_pnl_usd": net_pnl,
        "net_return_pct": net_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
        "profit_factor": profit_factor,
        "final_capital": capital,
        "trades": trades,
    }


def compare_timeframes_from_db(db_path: str = "trading_data.db", symbol: str = "BTC/USDT") -> List[Dict[str, Any]]:
    """Compara o desempenho walk-forward com custos reais entre todos os timeframes presentes no banco."""
    db = TradingDatabase(db_path=db_path)
    results = []
    for tf, sl, tp, h in [
        ("1m", 0.004, 0.006, 15),
        ("15m", 0.010, 0.018, 16),
    ]:
        df = db.get_recent_candles(symbol, tf, limit=2000)
        if df.empty:
            continue
        res = run_walk_forward_backtest(
            df,
            timeframe=tf,
            stop_loss_pct=sl,
            take_profit_pct=tp,
            horizon_candles=h,
            train_window=400,
            retrain_step=150,
        )
        results.append(res)
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
