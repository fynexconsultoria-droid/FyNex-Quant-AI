"""
Treinamento Histórico Profundo da IA (FyNex Quant AI)
=====================================================
Baixa histórico longo de candles contíguos da Binance (ex.: 5.000 a 20.000 candles),
salva no SQLite, compara arquiteturas (`rf`, `hgb`, `ensemble`) via validação
walk-forward com purge (Triple Barrier) e promove o melhor modelo para `models/`.

Uso:
    python train_ai.py --timeframe 5m --candles 5000
    python train_ai.py --timeframe 15m --candles 6000 --symbol BTC/USDT
    python train_ai.py --timeframe 1m --offline
"""

import argparse
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import binance_client
from core import error_learning
from core.database import TradingDatabase, timeframe_to_ms
from core.logging_setup import DEFAULT_DB_PATH, DEFAULT_MODELS_DIR, setup_logging
from core.market_data import fetch_live_ohlcv, get_latest_contiguous_segment
from core.ml import (
    InsufficientLabelsError,
    ModelPersistence,
    build_features,
    create_target,
    train_model,
)
from strategy_config import DEFAULT_TIMEFRAME, get_strategy_profile


def download_deep_history(
    db: TradingDatabase,
    symbol: str = "BTC/USDT",
    timeframe: str = DEFAULT_TIMEFRAME,
    target_candles: int = 5000,
) -> int:
    """Baixa `target_candles` candles fechados paginando para trás/frente na API pública da Binance."""
    exchange = binance_client.get_binance_exchange(testnet=False)
    tf_ms = timeframe_to_ms(timeframe)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - (target_candles + 5) * tf_ms

    total_saved = 0
    cursor = since_ms
    batches = max(1, (target_candles // 1000) + 2)

    for _ in range(batches):
        df = fetch_live_ohlcv(exchange, symbol=symbol, timeframe=timeframe, limit=1000, since=cursor)
        if df.empty:
            break
        # Filtra apenas candles já fechados por timestamp (nunca descarta o candle #1000 histórico fechado)
        ts_ms = df["timestamp"].map(lambda t: int(t.timestamp() * 1000))
        closed_df = df.loc[(ts_ms + tf_ms) <= now_ms].reset_index(drop=True)
        if closed_df.empty:
            break
        total_saved += db.save_candles(symbol, timeframe, closed_df, drop_last_open=False)
        last_saved_ts = int(closed_df["timestamp"].iloc[-1].timestamp() * 1000)
        if len(df) < 1000 or last_saved_ts >= now_ms - tf_ms or last_saved_ts < cursor:
            break
        cursor = last_saved_ts + tf_ms
        time.sleep(0.25)

    return total_saved


def train_and_select_best_model(
    df: pd.DataFrame,
    symbol: str = "BTC/USDT",
    timeframe: str = DEFAULT_TIMEFRAME,
    candidates: Tuple[str, ...] = ("rf", "et", "hgb", "ensemble"),
    models_dir: str = DEFAULT_MODELS_DIR,
    save_winner: bool = True,
    progress_cb: Optional[Any] = None,
    db_path: Optional[str] = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    """
    Treina cada arquitetura candidata sobre o mesmo dataset contíguo (Triple Barrier + purge
    + ponderação temporal + otimização de limiar), seleciona a de melhor desempenho fora da
    amostra (PnL simulado + Profit Factor + precisão de compra) e salva o vencedor em `models/`.
    """
    profile = get_strategy_profile(timeframe)
    clean_df = get_latest_contiguous_segment(df.copy(), timeframe)
    if len(clean_df) < 120:
        raise InsufficientLabelsError(
            f"Histórico contíguo insuficiente para treino ({len(clean_df)} candles < 120)."
        )

    feat_df, feature_cols = build_features(clean_df)
    dataset = create_target(
        feat_df,
        mode="triple_barrier",
        take_profit_pct=profile.tp_pct,
        stop_loss_pct=profile.sl_pct,
        horizon_candles=profile.horizon_candles,
        use_atr_barriers=profile.use_atr_barriers,
        atr_tp_mult=profile.atr_tp_mult,
        atr_sl_mult=profile.atr_sl_mult,
        min_tp_pct=profile.min_tp_pct,
        max_tp_pct=profile.max_tp_pct,
        min_sl_pct=profile.min_sl_pct,
        max_sl_pct=profile.max_sl_pct,
    )

    storage = ModelPersistence(models_dir=models_dir)
    next_ver = storage.next_version(symbol, timeframe)
    hard_weights = error_learning.compute_hard_example_multipliers(
        dataset, db_path=db_path, symbol=symbol, timeframe=timeframe
    )

    results: List[Dict[str, Any]] = []
    best_model: Optional[Any] = None
    best_score: Tuple[int, float, float, float, float] = (-2, -1e9, -1e9, -1e9, -1e9)
    best_entry: Optional[Dict[str, Any]] = None

    total_cands = max(1, len(candidates))
    for idx, model_type in enumerate(candidates, start=1):
        if callable(progress_cb):
            progress_cb(f"Treinando arquitetura {model_type.upper()} ({idx}/{total_cands})...", 35 + int(idx / total_cands * 55))

        model, prec_buy = train_model(
            dataset,
            feature_cols,
            model_version=next_ver,
            horizon_candles=profile.horizon_candles,
            tp_pct=profile.tp_pct,
            sl_pct=profile.sl_pct,
            confidence_threshold=profile.confidence,
            model_type=model_type,
            extra_sample_weights=hard_weights,
        )
        m = getattr(model, "validation_metrics_", {})
        entry = {
            "model_type": model_type,
            "precision_buy": float(m.get("precision_buy", prec_buy)),
            "sim_pnl_pct": float(m.get("sim_pnl_pct", 0.0)),
            "profit_factor": float(m.get("profit_factor", 0.0)),
            "expectancy_pct": float(m.get("expectancy_pct", 0.0)),
            "roc_auc": float(m.get("roc_auc", 0.5)),
            "optimal_threshold": float(m.get("optimal_threshold", profile.confidence)),
            "optimal_sim_pnl_pct": float(m.get("optimal_sim_pnl_pct", m.get("sim_pnl_pct", 0.0))),
            "top_features": m.get("top_features", []),
            "brier_score": float(m.get("brier_score", 0.25)),
            "accuracy": float(m.get("accuracy", 0.5)),
            "buy_signals": int(m.get("buy_signals", 0)),
            "samples": int(m.get("samples", len(dataset))),
        }
        results.append(entry)

        # Critério quantitativo: penaliza modelos mortos (0 sinais) para não vencerem modelos ativos
        has_signals = 1 if entry["buy_signals"] >= 5 else (0 if entry["buy_signals"] > 0 else -1)
        score = (has_signals, entry["sim_pnl_pct"], entry["profit_factor"], entry["precision_buy"], -entry["brier_score"])
        if best_entry is None or score > best_score:
            best_score = score
            best_model = model
            best_entry = entry

    saved_path = None
    if save_winner and best_model is not None and best_entry is not None:
        # Arquiva modelos antigos caso o schema de features tenha mudado
        old_meta = storage.load_metadata(symbol, timeframe)
        if old_meta and old_meta.get("feature_cols") != feature_cols:
            storage.archive_incompatible_models(symbol, timeframe)
            next_ver = storage.next_version(symbol, timeframe)

        saved_path = storage.save_model(
            best_model,
            next_ver,
            best_entry["precision_buy"],
            feature_cols,
            symbol,
            timeframe,
            extra_meta={"validation": getattr(best_model, "validation_metrics_", best_entry)},
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "model_version": next_ver,
        "candles_used": len(clean_df),
        "samples": len(dataset),
        "features_count": len(feature_cols),
        "winner": best_entry,
        "candidates": results,
        "saved_path": saved_path,
    }


def main() -> None:
    setup_logging(console=True)

    parser = argparse.ArgumentParser(description="Treinamento Histórico Profundo da IA FyNex")
    parser.add_argument("--symbol", type=str, default="BTC/USDT", help="Par negociado (padrão: BTC/USDT)")
    parser.add_argument(
        "--timeframe",
        type=str,
        default=DEFAULT_TIMEFRAME,
        choices=["1m", "3m", "5m", "15m", "30m", "1h"],
        help=f"Timeframe para treino (padrão: {DEFAULT_TIMEFRAME})",
    )
    parser.add_argument("--candles", type=int, default=5000, help="Quantidade de candles históricos a baixar (padrão: 5000)")
    parser.add_argument("--offline", action="store_true", help="Usa apenas os candles já salvos no SQLite (sem baixar da Binance)")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help="Caminho do banco SQLite")
    args = parser.parse_args()

    db = TradingDatabase(db_path=args.db_path)

    print("=" * 72)
    print(f"  FyNex Quant AI - Treinamento Histórico ({args.symbol} | {args.timeframe})")
    print("=" * 72)

    if not args.offline:
        print(f"[1/2] Baixando até {args.candles} candles contíguos da Binance...")
        saved = download_deep_history(db, symbol=args.symbol, timeframe=args.timeframe, target_candles=args.candles)
        print(f"      {saved} candles gravados/atualizados no banco.")
    else:
        print("[1/2] Modo offline: usando candles existentes no SQLite.")

    df = db.get_recent_candles(args.symbol, args.timeframe, limit=max(args.candles, 2000))
    print(f"[2/2] Treinando e comparando arquiteturas (RF vs ExtraTrees vs HistGradientBoosting vs Ensemble)...")

    summary = train_and_select_best_model(df, symbol=args.symbol, timeframe=args.timeframe, db_path=args.db_path)

    print("\nResultados na Validação Walk-Forward (com purge e taxas):")
    for c in summary["candidates"]:
        mark = " <-- CAMPEÃO" if summary["winner"] and c["model_type"] == summary["winner"]["model_type"] else ""
        print(
            f"  - {c['model_type'].upper():<8s} | Prec.Compra: {c['precision_buy']*100:5.1f}% "
            f"({c['buy_signals']:3d} sinais) | PnL Sim: {c['sim_pnl_pct']:+6.2f}% | "
            f"Brier: {c['brier_score']:.4f}{mark}"
        )
    print(f"\nModelo salvo em: {summary['saved_path']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
