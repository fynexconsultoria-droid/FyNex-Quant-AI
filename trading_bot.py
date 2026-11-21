"""
Robô de Trading Quantitativo de Criptomoedas com Machine Learning Contínuo (FyNex Quant AI)
===========================================================================================
Orquestrador principal do loop de trading em tempo real.
A lógica interna está modularizada no pacote `core/`:
- `core.logging_setup`: caminhos base, logging rotativo e hooks de exceção
- `core.database`: persistência SQLite (WAL) de candles, previsões, trades e estado vivo
- `core.market_data`: coleta OHLCV da Binance, backfill paginado e segmentos contíguos
- `core.ml`: features estacionárias, Triple Barrier, treino walk-forward, calibração e persistência
- `core.process`: lock de instância única (`bot.pid`) e parada limpa (`bot.stop`)
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd

import binance_client
from core import entry_guard, macro_intelligence
from core.database import TIMEFRAME_MS, TradingDatabase, timeframe_to_ms, timeframe_to_seconds
from core.logging_setup import (
    BASE_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_LOGS_DIR,
    DEFAULT_LOG_PATH,
    DEFAULT_MODELS_DIR,
    LOG_DATEFMT,
    LOG_FORMAT,
    PID_FILE_PATH,
    STOP_SIGNAL_PATH,
    install_exception_hooks,
    logger,
    setup_logging,
)
from core.market_data import (
    backfill_candles,
    fetch_live_ohlcv,
    get_latest_contiguous_segment,
)
from core.ml import (
    DynamicCalibrator,
    InsufficientLabelsError,
    ModelPersistence,
    _base_rf,
    _compute_validation_metrics,
    build_features,
    create_target,
    evaluate_model_on_dataset,
    evaluate_walk_forward,
    predict_prob_up,
    predict_signal,
    should_promote_challenger,
    train_model,
)
from core.process import (
    acquire_single_instance_lock,
    consume_stop_signal,
    get_running_bot_pid,
    is_pid_alive_bot,
    release_single_instance_lock,
    request_bot_stop,
)
from position_manager import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT, PositionManager, RiskManager
from strategy_config import DEFAULT_TIMEFRAME, get_strategy_profile, resolve_barriers

# Trava de segurança: o modo 'live' (dinheiro real) permanece desativado até validação
# completa em backtest com taxas + paper trading.
LIVE_TRADING_ENABLED = False

# Garante suporte completo a UTF-8 (emojis e acentos) no terminal Windows
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

__all__ = [
    "LIVE_TRADING_ENABLED",
    "BASE_DIR",
    "DEFAULT_DB_PATH",
    "DEFAULT_LOGS_DIR",
    "DEFAULT_LOG_PATH",
    "DEFAULT_MODELS_DIR",
    "LOG_DATEFMT",
    "LOG_FORMAT",
    "PID_FILE_PATH",
    "STOP_SIGNAL_PATH",
    "TIMEFRAME_MS",
    "PAPER_FEE_PCT",
    "PAPER_SLIPPAGE_PCT",
    "DEFAULT_TIMEFRAME",
    "logger",
    "setup_logging",
    "install_exception_hooks",
    "timeframe_to_ms",
    "timeframe_to_seconds",
    "TradingDatabase",
    "ModelPersistence",
    "DynamicCalibrator",
    "InsufficientLabelsError",
    "fetch_live_ohlcv",
    "backfill_candles",
    "get_latest_contiguous_segment",
    "build_features",
    "create_target",
    "predict_prob_up",
    "predict_signal",
    "_base_rf",
    "_compute_validation_metrics",
    "evaluate_model_on_dataset",
    "evaluate_walk_forward",
    "should_promote_challenger",
    "train_model",
    "is_pid_alive_bot",
    "get_running_bot_pid",
    "acquire_single_instance_lock",
    "release_single_instance_lock",
    "request_bot_stop",
    "consume_stop_signal",
    "get_strategy_profile",
    "resolve_barriers",
    "TradingBotRunner",
    "run_continuous_trading_bot",
]


# -----------------------------------------------------------------------------
# Orquestrador Orientado a Objetos do Loop de Trading
# -----------------------------------------------------------------------------
class TradingBotRunner:
    """
    Encapsula o ciclo de vida completo do robô quantitativo:
    inicialização segura com lock, sincronização de candles, carregamento/treino
    do modelo, loop de monitoramento com retreino assíncrono e encerramento limpo.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = DEFAULT_TIMEFRAME,
        retrain_every_n_candles: Optional[int] = None,
        confidence_threshold: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        horizon_candles: Optional[int] = None,
        check_interval_seconds: int = 10,
        db_path: str = DEFAULT_DB_PATH,
        trading_mode: Optional[str] = None,
        trade_amount_usdt: Optional[float] = None,
        testnet: Optional[bool] = None,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.check_interval_seconds = check_interval_seconds
        self.db_path = db_path
        self._trading_mode_override = trading_mode
        self._trade_amount_override = trade_amount_usdt
        self._testnet_override = testnet

        profile = get_strategy_profile(
            timeframe,
            tp_override=take_profit_pct,
            sl_override=stop_loss_pct,
            horizon_override=horizon_candles,
            confidence_override=confidence_threshold,
        )
        if any(v is not None for v in (stop_loss_pct, take_profit_pct, horizon_candles, confidence_threshold)):
            logger.warning(
                f"⚠️ [OVERRIDE DE PERFIL] Parâmetros manuais aplicados sobre o perfil '{timeframe}': "
                f"TP={profile.tp_pct:.2%}, SL={profile.sl_pct:.2%}, Horizonte={profile.horizon_candles}, "
                f"Confiança={profile.confidence:.0%}"
            )

        self.stop_loss_pct = profile.sl_pct
        self.take_profit_pct = profile.tp_pct
        self.horizon_candles = profile.horizon_candles
        self.confidence_threshold = profile.confidence
        self.retrain_every_n_candles = (
            int(retrain_every_n_candles) if retrain_every_n_candles is not None else profile.retrain_every
        )

        # Estado em runtime (inicializado em `run()`)
        self.db = TradingDatabase(db_path=self.db_path)
        self.macro_store = macro_intelligence.MacroLearningStore(db_path=self.db_path)
        self.model_storage = ModelPersistence(models_dir=DEFAULT_MODELS_DIR)
        self.exchange: Any = None
        self.positions: Optional[PositionManager] = None
        self.final_mode = "paper"

        self.model: Optional[Any] = None
        self.model_version = 1
        self.current_model_acc = 0.0
        self.feature_cols: List[str] = []
        self.champion_metrics: Optional[Dict[str, Any]] = None
        self.observation_mode = False

        self.model_lock = threading.Lock()
        self.retrain_in_progress = False
        self.candles_since_retrain = 0
        self.last_evaluated_candle_ts = 0
        self.last_heartbeat_ts = 0.0
        self.calib: Dict[str, Any] = {}
        self.active_confidence = self.confidence_threshold
        self.last_macro: Dict[str, Any] = {}
        self._shutdown_requested = threading.Event()

    def _install_shutdown_handlers(self) -> None:
        """Captura sinais de parada/desligamento do Windows para salvar todo o aprendizado antes de sair."""
        def _handle_sig(signum, _frame):
            logger.info(f"🛑 Sinal de sistema ({signum}) recebido (parada/desligamento). Salvando checkpoint e encerrando...")
            self._shutdown_requested.set()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle_sig)
                except (ValueError, OSError):
                    pass

    def _resolve_runtime_config(self) -> Optional[Dict[str, Any]]:
        bin_cfg = binance_client.load_binance_config()
        final_mode = (self._trading_mode_override or bin_cfg["trading_mode"]).lower()
        final_amount = (
            self._trade_amount_override
            if self._trade_amount_override is not None
            else bin_cfg["trade_amount_usdt"]
        )
        final_testnet = (
            self._testnet_override if self._testnet_override is not None else bin_cfg["testnet"]
        )

        if final_mode == "live" and not LIVE_TRADING_ENABLED:
            logger.error(
                "⛔ [BLOQUEADO] O modo 'live' está desativado no código até que a estratégia seja validada "
                "em backtest com taxas e paper trading. Use 'paper' ou 'testnet'."
            )
            return None

        if final_mode in ("live", "testnet") and not bin_cfg["has_keys"]:
            logger.warning(
                f"⚠️ [AVISO DE SEGURANÇA] Modo '{final_mode}' selecionado, mas as chaves da Binance "
                f"não foram configuradas no .env! Alternando automaticamente para 'paper' (simulado)."
            )
            final_mode = "paper"

        try:
            final_testnet = binance_client.resolve_environment(final_mode, final_testnet)
        except binance_client.EnvironmentConfigError as e:
            logger.error(f"⛔ [CONFIGURAÇÃO INCOERENTE] {e}")
            return None

        return {
            "bin_cfg": bin_cfg,
            "mode": final_mode,
            "amount": final_amount,
            "testnet": final_testnet,
        }

    def _log_startup_banner(
        self,
        bin_cfg: Dict[str, Any],
        env_name: str,
        final_amount: float,
        risk_manager: RiskManager,
    ) -> None:
        mode_label = {
            "live": "🔴 LIVE TRADING (ORDENS REAIS NA CONTA SPOT)",
            "testnet": "🟠 TESTNET TRADING (ORDENS REAIS NO SANDBOX)",
            "paper": "🟢 PAPER TRADING (SIMULAÇÃO QUANTITATIVA SEGURA)",
        }.get(self.final_mode, "PAPER TRADING")

        logger.info("====================================================================")
        logger.info("🚀 INICIANDO QUANT TRADING BOT - MACHINE LEARNING CONTÍNUO")
        logger.info(f"   Par: {self.symbol} | Tempo Gráfico: {self.timeframe}")
        logger.info(f"   Ambiente de Conexão: {env_name}")
        logger.info(f"   Modo de Operação: {mode_label}")
        if bin_cfg["has_keys"]:
            logger.info(f"   Conta Binance: Conectada ({bin_cfg['api_key_masked']})")
        else:
            logger.info("   Conta Binance: Chaves não informadas (Operando em modo público simulado)")
        logger.info(f"   Alocação por Entrada: ${final_amount:,.2f} USDT")
        logger.info(f"   Retreinamento Automático: A cada {self.retrain_every_n_candles} novos candles")
        logger.info(f"   Confiança Base: {self.confidence_threshold * 100:.0f}% (com Auto-Calibração Ativa)")
        logger.info(
            f"   Gestão de Risco: Stop Loss {self.stop_loss_pct * 100:.1f}% | "
            f"Take Profit {self.take_profit_pct * 100:.1f}%"
        )
        logger.info(
            f"   Limites: Perda diária ${risk_manager.max_daily_loss_usdt:,.2f} | "
            f"{risk_manager.max_trades_per_day} trades/dia | "
            f"Máx. por ordem ${risk_manager.max_trade_amount_usdt:,.2f} | "
            f"OCO na exchange: {'Sim' if self.positions and self.positions.use_exchange_oco else 'Não'}"
        )
        logger.info("====================================================================")

    def _load_or_train_initial_model(self, recent_df: pd.DataFrame) -> None:
        _, current_feature_cols = build_features(recent_df.iloc[:40])
        saved_model_data = self.model_storage.load_latest_model(self.symbol, self.timeframe)
        self.model = None
        self.model_version = self.model_storage.next_version(self.symbol, self.timeframe)
        self.current_model_acc = 0.0
        self.feature_cols = current_feature_cols
        self.champion_metrics = None
        self.observation_mode = False

        if saved_model_data is not None and saved_model_data[3] == current_feature_cols:
            loaded_model, loaded_ver, loaded_acc, loaded_cols = saved_model_data
            loaded_classes = list(getattr(loaded_model, "classes_", []))
            if len(loaded_classes) >= 2:
                self.model, self.model_version, self.current_model_acc, self.feature_cols = (
                    loaded_model, loaded_ver, loaded_acc, loaded_cols
                )
                self.champion_metrics = getattr(
                    loaded_model, "validation_metrics_",
                    {"accuracy": self.current_model_acc, "precision_buy": self.current_model_acc},
                )
            else:
                logger.warning(
                    f"⚠️ Modelo salvo v{loaded_ver} é degenerado (classes={loaded_classes}). "
                    f"Arquivando versões antigas e tentando retreinar..."
                )
                self.model_storage.archive_incompatible_models(self.symbol, self.timeframe)
                self.model_version = self.model_storage.next_version(self.symbol, self.timeframe)
        elif saved_model_data is not None:
            logger.warning("⚠️ Schema de features mudou em relação ao modelo salvo. Arquivando modelos antigos.")
            self.model_storage.archive_incompatible_models(self.symbol, self.timeframe)
            self.model_version = self.model_storage.next_version(self.symbol, self.timeframe)

        if self.model is not None:
            return

        feat_df, self.feature_cols = build_features(recent_df)
        dataset = create_target(
            feat_df,
            mode="triple_barrier",
            take_profit_pct=self.take_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
            horizon_candles=self.horizon_candles,
        )
        try:
            self.model, self.current_model_acc = train_model(
                dataset,
                self.feature_cols,
                model_version=self.model_version,
                horizon_candles=self.horizon_candles,
                tp_pct=self.take_profit_pct,
                sl_pct=self.stop_loss_pct,
                confidence_threshold=self.confidence_threshold,
            )
            self.champion_metrics = getattr(
                self.model, "validation_metrics_",
                {"accuracy": self.current_model_acc, "precision_buy": self.current_model_acc},
            )
            self.model_storage.save_model(
                self.model,
                self.model_version,
                self.current_model_acc,
                self.feature_cols,
                self.symbol,
                self.timeframe,
                extra_meta={"validation": self.champion_metrics},
            )
        except InsufficientLabelsError as e:
            self.observation_mode = True
            logger.warning(
                f"⚠️ [MODO OBSERVAÇÃO] {e} O robô continuará coletando candles sem abrir posições "
                f"até haver amostras válidas das duas classes."
            )

    def _refresh_calibration(self) -> Dict[str, Any]:
        acc_stats = self.db.get_rolling_accuracy(self.symbol, self.timeframe, last_n=30)
        self.calib = DynamicCalibrator.get_calibration(
            rolling_accuracy=acc_stats["accuracy_pct"],
            evaluated_count=acc_stats["total"],
            base_threshold=self.confidence_threshold,
            buy_precision_pct=acc_stats.get("buy_precision_pct"),
            buy_evaluated_count=acc_stats.get("buy_total"),
        )
        self.active_confidence = self.calib["threshold"]
        return acc_stats

    def _publish_state(self) -> None:
        assert self.positions is not None
        with self.model_lock:
            ver = self.model_version
            acc = self.current_model_acc
        self.db.upsert_bot_state(
            pid=os.getpid(),
            symbol=self.symbol,
            timeframe=self.timeframe,
            trading_mode=self.final_mode,
            model_version=ver,
            model_accuracy=acc,
            base_confidence=self.confidence_threshold,
            active_confidence=self.active_confidence,
            calibration_mode=self.calib["mode"],
            calibration_badge=self.calib["badge"],
            calibration_desc=self.calib["description"],
            in_position=self.positions.in_position,
            entry_price=self.positions.position.entry_price if self.positions.position else 0.0,
        )

    def _background_retrain_worker(self, df_snapshot: pd.DataFrame, ver: int) -> None:
        try:
            logger.info(f"\n🔄 [THREAD RETREINO] Treinando desafiante v{ver} em segundo plano...")
            feat_retrain_df, f_cols = build_features(df_snapshot)
            retrain_dataset = create_target(
                feat_retrain_df,
                mode="triple_barrier",
                take_profit_pct=self.take_profit_pct,
                stop_loss_pct=self.stop_loss_pct,
                horizon_candles=self.horizon_candles,
            )
            challenger, new_acc = train_model(
                retrain_dataset,
                f_cols,
                model_version=ver,
                horizon_candles=self.horizon_candles,
                tp_pct=self.take_profit_pct,
                sl_pct=self.stop_loss_pct,
                confidence_threshold=self.confidence_threshold,
            )
            eval_slice = retrain_dataset.iloc[int(len(retrain_dataset) * 0.8):]
            with self.model_lock:
                cur_champ = self.model
                cur_champ_metrics = self.champion_metrics

            if cur_champ is not None and len(eval_slice) >= 20:
                champ_eval = evaluate_model_on_dataset(
                    cur_champ, eval_slice, f_cols,
                    tp_pct=self.take_profit_pct, sl_pct=self.stop_loss_pct,
                    confidence_threshold=self.confidence_threshold,
                )
                chal_eval = evaluate_model_on_dataset(
                    challenger, eval_slice, f_cols,
                    tp_pct=self.take_profit_pct, sl_pct=self.stop_loss_pct,
                    confidence_threshold=self.confidence_threshold,
                )
            else:
                champ_eval = cur_champ_metrics
                chal_eval = getattr(
                    challenger, "validation_metrics_",
                    {"accuracy": new_acc, "precision_buy": new_acc},
                )

            chal_metrics = getattr(challenger, "validation_metrics_", chal_eval)
            promoted = should_promote_challenger(chal_eval, champ_eval)
            if promoted:
                with self.model_lock:
                    self.model = challenger
                    self.model_version = ver
                    self.current_model_acc = new_acc
                    self.champion_metrics = chal_metrics
                    self.observation_mode = False
                self.model_storage.save_model(
                    challenger, ver, new_acc, f_cols, self.symbol, self.timeframe,
                    extra_meta={"validation": chal_metrics},
                )
                logger.info(f"🏆 [CAMPEÃO PROMOVIDO] Modelo v{ver} assumiu a operação e foi salvo em disco.")
            else:
                logger.info(
                    f"🛡️ [CAMPEÃO MANTIDO] Desafiante v{ver} (Prec.Compra {chal_metrics.get('precision_buy', 0)*100:.1f}%) "
                    f"não superou o campeão v{self.model_version} ({(cur_champ_metrics or {}).get('precision_buy', 0)*100:.1f}%)."
                )
            try:
                self.macro_store.record_learning_cycle(
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    model_version=ver,
                    model_type=str(chal_metrics.get("model_type", "ensemble")),
                    metrics=chal_metrics,
                    macro_score=float(self.last_macro.get("macro_score", 0.0)),
                    promoted=promoted,
                    notes="Retreino contínuo automático em segundo plano",
                )
            except Exception:
                pass
        except InsufficientLabelsError as e:
            logger.warning(f"⚠️ [RETREINO IGNORADO] {e}")
        except Exception:
            logger.exception("Erro inesperado no retreino em background:")
        finally:
            self.retrain_in_progress = False

    def _maybe_trigger_retrain(self) -> None:
        if self.candles_since_retrain < self.retrain_every_n_candles or self.retrain_in_progress:
            return
        retrain_raw = self.db.get_recent_candles(self.symbol, self.timeframe, limit=1500)
        retrain_df = get_latest_contiguous_segment(retrain_raw, self.timeframe)
        if len(retrain_df) < 200:
            logger.warning(
                f"Segmento contíguo ({len(retrain_df)} candles) ainda curto para retreino; aguardando mais dados."
            )
            return
        self.retrain_in_progress = True
        self.candles_since_retrain = 0
        with self.model_lock:
            next_ver = max(self.model_version + 1, self.model_storage.next_version(self.symbol, self.timeframe))
        threading.Thread(
            target=self._background_retrain_worker,
            args=(retrain_df, next_ver),
            daemon=True,
        ).start()

    def _run_inference(self, closed_ts: int, closed_close: float, current_price: float) -> None:
        assert self.positions is not None
        full_history_df = get_latest_contiguous_segment(
            self.db.get_recent_candles(self.symbol, self.timeframe, limit=100), self.timeframe
        )
        if len(full_history_df) >= 35:
            feat_full_df, _ = build_features(full_history_df)
            closed_features = feat_full_df.iloc[-1:][self.feature_cols]
        else:
            closed_features = pd.DataFrame()

        with self.model_lock:
            active_model = self.model
            active_ver = self.model_version
            is_obs = self.observation_mode

        if active_model is None or is_obs:
            logger.info("   👀 [MODO OBSERVAÇÃO] Sem modelo válido para inferência; aguardando dados para treino.")
            return
        if closed_features.empty or closed_features.isna().values.any():
            return

        pred, prob_down, prob_up = predict_signal(active_model, closed_features)
        pred_id = self.db.record_prediction(
            symbol=self.symbol,
            timeframe=self.timeframe,
            model_version=active_ver,
            candle_timestamp=closed_ts,
            prediction=pred,
            prob_up=prob_up,
            prob_down=prob_down,
            entry_price=closed_close,
            tp_pct=self.take_profit_pct,
            sl_pct=self.stop_loss_pct,
            horizon_candles=self.horizon_candles,
        )
        logger.info(
            f"   🤖 Previsão #{pred_id} (Modelo v{active_ver}): "
            f"{'ALTA (1)' if pred == 1 else 'BAIXA/NEUTRO (0)'} "
            f"[Prob. Alta: {prob_up:.1%} | Prob. Baixa: {prob_down:.1%}]"
        )

        # Coleta e persiste contexto macro (Dólar DXY, Juros EUA 10Y, Notícias, Inflação/Fed)
        macro_adj = 0.0
        try:
            macro = macro_intelligence.fetch_macro_snapshot()
            self.last_macro = macro
            self.macro_store.save_macro_snapshot(self.symbol, self.timeframe, closed_ts, macro)
            macro_adj = float(macro.get("confidence_adjustment", 0.0))
            logger.info(
                f"   🌎 [MACRO & NOTÍCIAS] {macro.get('badge', '')} {macro.get('regime')} "
                f"(Score {macro.get('macro_score', 0):+.1f} | DXY {macro.get('dxy_change_pct', 0):+.2f}% | "
                f"US10Y {macro.get('us10y_yield', 0):.2f}% | Notícias {macro.get('news_sentiment_score', 0):+.2f} | "
                f"Ajuste filtro: {macro_adj:+.0%})"
            )
        except Exception as e:
            logger.warning(f"   ⚠️ Contexto macro indisponível neste ciclo (usando apenas IA técnica): {e}")

        effective_confidence = min(0.85, max(0.55, self.active_confidence + macro_adj))

        if not self.positions.in_position and pred == 1 and prob_up >= effective_confidence:
            feat_dict = closed_features.iloc[0].to_dict()
            last_trade = None
            try:
                with self.db._get_connection() as conn:
                    row = conn.execute(
                        "SELECT reason, pnl_usd, exit_time FROM simulated_trades WHERE symbol = ? AND timeframe = ? ORDER BY id DESC LIMIT 1",
                        (self.symbol, self.timeframe),
                    ).fetchone()
                    last_trade = dict(row) if row else None
            except Exception:
                last_trade = None

            cooldown_sec = timeframe_to_seconds(self.timeframe) * 2
            safe_to_enter, guard_reasons = entry_guard.evaluate_entry_safety(
                features_row=feat_dict,
                macro_snapshot=self.last_macro,
                last_trade=last_trade,
                cooldown_seconds=cooldown_sec,
            )
            if safe_to_enter:
                logger.info(
                    f"   🛡️ [ENTRADA APROVADA] IA ({prob_up:.1%} >= {effective_confidence:.1%}) + Confluência Técnica/Macro OK."
                )
                self.positions.try_open(current_price, active_ver, prob_up)
            else:
                logger.info(f"   🛡️ [ENTRADA BLOQUEADA POR SEGURANÇA] {' | '.join(guard_reasons)}")
        elif not self.positions.in_position and pred == 1 and prob_up < effective_confidence:
            logger.info(
                f"   ✋ [SINAL FILTRADO] Probabilidade de alta ({prob_up:.1%}) "
                f"abaixo do filtro combinado IA+Macro ({effective_confidence:.1%})."
            )

    def _process_closed_candle(self, closed_candle: pd.Series, current_price: float) -> None:
        closed_ts = int(closed_candle["timestamp"].timestamp() * 1000)
        if closed_ts <= self.last_evaluated_candle_ts:
            return

        closed_close = float(closed_candle["close"])
        self.last_evaluated_candle_ts = closed_ts
        self.candles_since_retrain += 1
        closed_time_str = closed_candle["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"\n🔔 [NOVO CANDLE FECHADO] {closed_time_str} | Fechamento: ${closed_close:,.2f}")

        evaluations = self.db.evaluate_pending_predictions(self.symbol, self.timeframe, closed_ts, closed_close)
        for ev in evaluations:
            status = "✅ ACERTO" if ev["is_correct"] else "❌ ERRO"
            logger.info(
                f"   [FEEDBACK IA] Previsão #{ev['id']}: Sinal={ev['prediction']} "
                f"| Realizado={ev['actual_direction']} -> {status} "
                f"(Entrada: ${ev['entry_price']:,.2f} -> Fechou: ${ev['actual_close']:,.2f})"
            )

        acc_stats = self._refresh_calibration()
        if acc_stats["total"] > 0:
            logger.info(
                f"   📊 [ACURÁCIA VIVA] Últimas {acc_stats['total']} previsões: "
                f"{acc_stats['correct']} acertos ({acc_stats['accuracy_pct']:.1f}% de assertividade real)"
            )
        logger.info(
            f"   {self.calib['badge']} [AUTO-CALIBRAÇÃO] {self.calib['mode']} -> "
            f"Filtro Ativo: {self.active_confidence:.1%} ({self.calib['description']})"
        )

        self._maybe_trigger_retrain()
        self._run_inference(closed_ts, closed_close, current_price)

    def _emit_heartbeat(self, current_price: float) -> None:
        assert self.positions is not None
        now_mono = time.monotonic()
        if (now_mono - self.last_heartbeat_ts) < 60.0:
            return
        self.last_heartbeat_ts = now_mono

        try:
            tf_seconds = timeframe_to_seconds(self.timeframe)
            now_ms = int(time.time() * 1000)
            candle_close_ms = ((now_ms // (tf_seconds * 1000)) + 1) * (tf_seconds * 1000)
            remaining_sec = max(0, int((candle_close_ms - now_ms) / 1000))
        except Exception:
            remaining_sec = self.check_interval_seconds

        candles_left = max(0, self.retrain_every_n_candles - self.candles_since_retrain)
        with self.model_lock:
            ia_label = "OBSERVAÇÃO" if self.observation_mode else f"v{self.model_version}"
        logger.info(
            f"💓 [HEARTBEAT] {self.symbol}: ${current_price:,.2f} | "
            f"Fechamento: ~{remaining_sec:02d}s | "
            f"Retreino: {candles_left} candle(s) | "
            f"IA: {ia_label} [{self.calib['badge']} {self.calib['mode']}] | "
            f"Posição: {self.positions.status_label()}"
        )

    def _sleep_with_stop_check(self, seconds: int) -> bool:
        """Dorme `seconds` segundos checando `bot.stop` e sinal de desligamento a cada 1s. Retorna True se deve parar."""
        for _ in range(max(1, int(seconds))):
            if self._shutdown_requested.is_set() or consume_stop_signal():
                return True
            time.sleep(1)
        return False

    def _log_final_report(self) -> None:
        logger.info("\n" + "=" * 60)
        trade_stats = self.db.get_trade_summary(self.symbol)
        logger.info(
            f"Relatório Final de Trades: {trade_stats['total_trades']} operações "
            f"({trade_stats['wins']}V / {trade_stats['losses']}D | Taxa de Acerto: {trade_stats['win_rate']:.1f}%)"
        )
        logger.info(f"PnL Acumulado: {trade_stats['total_pnl_pct']:+.2f}% (${trade_stats['total_pnl_usd']:+.2f})")
        if self.positions and self.positions.in_position and self.positions.position:
            oco_suffix = (
                " e continua protegida pela OCO na exchange."
                if self.positions.position.oco_list_id
                else "."
            )
            logger.warning(
                f"⚠️ Robô encerrado COM POSIÇÃO ABERTA. Ela será restaurada no próximo início{oco_suffix}"
            )
        logger.info("=" * 60)

    def run(self) -> None:
        cfg = self._resolve_runtime_config()
        if cfg is None:
            return

        bin_cfg = cfg["bin_cfg"]
        self.final_mode = cfg["mode"]
        final_amount = cfg["amount"]
        final_testnet = cfg["testnet"]

        consume_stop_signal()
        self._install_shutdown_handlers()
        if not acquire_single_instance_lock(self.symbol, self.timeframe, self.final_mode):
            other_pid = get_running_bot_pid()
            logger.error(f"⛔ Já existe uma instância do robô em execução (PID {other_pid}). Abortando nova instância.")
            return

        exit_code = 0
        try:
            use_keys = bin_cfg["has_keys"] and self.final_mode in ("live", "testnet")
            self.exchange = binance_client.get_binance_exchange(
                api_key=bin_cfg["api_key"] if use_keys else None,
                api_secret=bin_cfg["api_secret"] if use_keys else None,
                testnet=final_testnet,
            )

            risk_manager = RiskManager(
                self.db,
                mode=self.final_mode,
                max_daily_loss_usdt=bin_cfg["max_daily_loss_usdt"],
                max_trades_per_day=bin_cfg["max_trades_per_day"],
                max_trade_amount_usdt=bin_cfg["max_trade_amount_usdt"],
            )
            self.positions = PositionManager(
                self.db,
                self.exchange,
                mode=self.final_mode,
                symbol=self.symbol,
                timeframe=self.timeframe,
                stop_loss_pct=self.stop_loss_pct,
                take_profit_pct=self.take_profit_pct,
                trade_amount_usdt=final_amount,
                use_exchange_oco=bin_cfg["use_exchange_oco"],
                risk=risk_manager,
                horizon_candles=self.horizon_candles,
            )

            env_name = "Binance Spot Testnet (Sandbox)" if final_testnet else "Binance Spot Oficial (Mercado Real)"
            self._log_startup_banner(bin_cfg, env_name, final_amount, risk_manager)

            logger.info(f"Sincronizando candles da {env_name}...")
            novos_inseridos = backfill_candles(
                self.db, self.exchange, symbol=self.symbol, timeframe=self.timeframe, min_candles=1000
            )
            recent_df = get_latest_contiguous_segment(
                self.db.get_recent_candles(self.symbol, self.timeframe, limit=1200), self.timeframe
            )
            if recent_df.empty:
                logger.error("Falha ao coletar dados da exchange. Verifique a conexão com a internet.")
                return
            logger.info(
                f"Sincronização concluída: {novos_inseridos} candles gravados/atualizados no SQLite "
                f"(segmento contíguo: {len(recent_df)} candles)."
            )

            self._load_or_train_initial_model(recent_df)
            self.last_evaluated_candle_ts = self.db.get_last_predicted_candle_timestamp(self.symbol, self.timeframe)
            self.positions.restore()
            self._refresh_calibration()
            self._publish_state()

            logger.info("\n=== Sistema operacional. Entrando no loop de monitoramento contínuo... ===")

            consecutive_errors = 0
            max_consecutive_errors = 10
            tf_ms = timeframe_to_ms(self.timeframe)

            while True:
                if self._shutdown_requested.is_set() or consume_stop_signal():
                    logger.info("🛑 Sinal de parada limpa recebido (bot.stop / desligamento). Salvando checkpoint e encerrando...")
                    break

                try:
                    live_df = fetch_live_ohlcv(self.exchange, symbol=self.symbol, timeframe=self.timeframe, limit=30)
                    if live_df.empty or len(live_df) < 2:
                        if self._sleep_with_stop_check(self.check_interval_seconds):
                            logger.info("🛑 Sinal de parada limpa recebido (bot.stop). Encerrando loop...")
                            break
                        continue

                    prev_last_ts = self.db.get_last_candle_timestamp(self.symbol, self.timeframe)
                    oldest_live_ts = int(live_df["timestamp"].iloc[0].timestamp() * 1000)
                    if prev_last_ts is not None and (oldest_live_ts - prev_last_ts) > tf_ms * 1.5:
                        logger.warning(
                            f"🔄 [LACUNA DETECTADA] Salto de {(oldest_live_ts - prev_last_ts) / 60000:.1f} min. "
                            f"Executando backfill automático..."
                        )
                        backfill_candles(self.db, self.exchange, symbol=self.symbol, timeframe=self.timeframe)

                    self.db.save_candles(self.symbol, self.timeframe, live_df, drop_last_open=True)
                    current_price = float(live_df["close"].iloc[-1])

                    self.positions.check_exits(
                        current_price,
                        candle_high=float(live_df["high"].iloc[-1]),
                        candle_low=float(live_df["low"].iloc[-1]),
                    )

                    self._process_closed_candle(live_df.iloc[-2], current_price)
                    self._publish_state()
                    self._emit_heartbeat(current_price)
                    consecutive_errors = 0

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    backoff = min(300, self.check_interval_seconds * (2 ** min(consecutive_errors - 1, 5)))
                    logger.exception(
                        f"⚠️ [ERRO NO LOOP ({consecutive_errors}/{max_consecutive_errors})] {exc} "
                        f"| Aguardando {backoff}s antes de tentar novamente."
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        logger.critical(
                            f"🛑 Limite de {max_consecutive_errors} erros consecutivos atingido. "
                            f"Encerrando com erro para permitir reinício pelo supervisor."
                        )
                        exit_code = 1
                        break
                    if self._sleep_with_stop_check(int(backoff)):
                        logger.info("🛑 Sinal de parada limpa recebido (bot.stop). Encerrando loop...")
                        break
                    continue

                if self._sleep_with_stop_check(self.check_interval_seconds):
                    logger.info("🛑 Sinal de parada limpa recebido (bot.stop). Encerrando loop...")
                    break

        except KeyboardInterrupt:
            logger.info("🛑 Execução interrompida pelo usuário (Ctrl+C).")
        finally:
            release_single_instance_lock()
            self._log_final_report()

        if exit_code != 0:
            sys.exit(exit_code)


def run_continuous_trading_bot(
    symbol: str = "BTC/USDT",
    timeframe: str = DEFAULT_TIMEFRAME,
    retrain_every_n_candles: Optional[int] = None,
    confidence_threshold: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    horizon_candles: Optional[int] = None,
    check_interval_seconds: int = 10,
    db_path: str = DEFAULT_DB_PATH,
    trading_mode: Optional[str] = None,
    trade_amount_usdt: Optional[float] = None,
    testnet: Optional[bool] = None,
) -> None:
    """Função de entrada compatível que instancia e executa `TradingBotRunner`."""
    runner = TradingBotRunner(
        symbol=symbol,
        timeframe=timeframe,
        retrain_every_n_candles=retrain_every_n_candles,
        confidence_threshold=confidence_threshold,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        horizon_candles=horizon_candles,
        check_interval_seconds=check_interval_seconds,
        db_path=db_path,
        trading_mode=trading_mode,
        trade_amount_usdt=trade_amount_usdt,
        testnet=testnet,
    )
    runner.run()


def main() -> None:
    setup_logging(DEFAULT_LOG_PATH)
    install_exception_hooks(os.path.join(DEFAULT_LOGS_DIR, "stderr.log"))

    parser = argparse.ArgumentParser(
        description="Quant Trading Bot com Machine Learning Contínuo em Tempo Real"
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default=DEFAULT_TIMEFRAME,
        choices=["1m", "3m", "5m", "15m", "30m", "1h"],
        help=f"Tempo gráfico dos candles (padrão: {DEFAULT_TIMEFRAME})",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTC/USDT",
        help="Par de criptomoeda negociado (padrão: BTC/USDT)",
    )
    parser.add_argument(
        "--retrain",
        type=int,
        default=None,
        help="Retreina a IA a cada N candles fechados (padrão: do perfil do timeframe)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Probabilidade mínima base para compra (padrão: do perfil do timeframe)",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=None,
        help="Percentual de Stop Loss (sobrescreve o perfil do timeframe)",
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=None,
        help="Percentual de Take Profit (sobrescreve o perfil do timeframe)",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=None,
        help="Horizonte em candles para Triple Barrier / Time Barrier (sobrescreve o perfil)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Intervalo de checagem em segundos (padrão: 10s)",
    )
    parser.add_argument(
        "--trading-mode",
        type=str,
        default=None,
        choices=["paper", "testnet", "live"],
        help="Modo de execução (paper: simulado, testnet: ordens de teste, live: ordens reais)",
    )
    parser.add_argument(
        "--trade-amount",
        type=float,
        default=None,
        help="Valor em USDT alocado para cada compra na Binance (padrão: do .env ou 50.0)",
    )

    args = parser.parse_args()

    run_continuous_trading_bot(
        symbol=args.symbol,
        timeframe=args.timeframe,
        retrain_every_n_candles=args.retrain,
        confidence_threshold=args.confidence,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        horizon_candles=args.horizon,
        check_interval_seconds=args.interval,
        trading_mode=args.trading_mode,
        trade_amount_usdt=args.trade_amount,
    )


if __name__ == "__main__":
    main()
