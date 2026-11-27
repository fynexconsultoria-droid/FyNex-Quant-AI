"""
Gerenciador de Jobs de Treinamento da IA e Agendador Automático (1h) no Dashboard
=================================================================================
Mantém estado thread-safe de progresso em tempo real, executa o pipeline
de download/treino/seleção em background sem travar a API FastAPI e agenda
retreinamentos profundos automáticos (padrão: a cada 1 hora) com persistência
em SQLite e sincronização automática Demo ↔ Conta Real.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import train_ai
from core import macro_intelligence
from core.database import TradingDatabase
from core.logging_setup import DEFAULT_DB_PATH, DEFAULT_MODELS_DIR

logger = logging.getLogger("QuantTradingBot")

_AI_TRAIN_LOCK = threading.Lock()
_AI_TRAIN_STATE: Dict[str, Any] = {
    "running": False,
    "symbol": None,
    "timeframe": None,
    "progress_pct": 0,
    "stage": "Pronto para treinar",
    "error": None,
    "last_result": None,
    "trigger": "manual",
}

_SCHED_LOCK = threading.Lock()
_SCHED_STOP_EVENT = threading.Event()
_SCHED_THREAD: Optional[threading.Thread] = None
_SCHED_NEXT_RUN_TS: Optional[float] = None

DEFAULT_AUTO_TRAIN_ENABLED = True
DEFAULT_AUTO_TRAIN_INTERVAL_MINUTES = 60
DEFAULT_AUTO_TRAIN_SYMBOL = "BTC/USDT"
DEFAULT_AUTO_TRAIN_TIMEFRAME = "1h"
DEFAULT_AUTO_TRAIN_CANDLES = 3000
MIN_AUTO_TRAIN_INTERVAL_MINUTES = 60
MAX_AUTO_TRAIN_INTERVAL_MINUTES = 1440


def _server_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _parse_bool(raw: str, default: bool = True) -> bool:
    val = str(raw or "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "sim", "on")


def _parse_int(raw: str, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        v = default
    return max(min_v, min(max_v, v))


def _parse_utc_ts(ts_str: str) -> Optional[float]:
    raw = str(ts_str or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _fmt_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_schedule_config(db_path: Optional[str] = None) -> Dict[str, Any]:
    path = str(db_path or _server_attr("DB_PATH", DEFAULT_DB_PATH))
    db = TradingDatabase(db_path=path)
    enabled = _parse_bool(
        db.get_account_setting("ai_auto_train_enabled", "1" if DEFAULT_AUTO_TRAIN_ENABLED else "0"),
        default=DEFAULT_AUTO_TRAIN_ENABLED,
    )
    interval_min = _parse_int(
        db.get_account_setting("ai_auto_train_interval_minutes", str(DEFAULT_AUTO_TRAIN_INTERVAL_MINUTES)),
        default=DEFAULT_AUTO_TRAIN_INTERVAL_MINUTES,
        min_v=MIN_AUTO_TRAIN_INTERVAL_MINUTES,
        max_v=MAX_AUTO_TRAIN_INTERVAL_MINUTES,
    )
    symbol = (db.get_account_setting("ai_auto_train_symbol", DEFAULT_AUTO_TRAIN_SYMBOL) or DEFAULT_AUTO_TRAIN_SYMBOL).strip().upper()
    timeframe = (db.get_account_setting("ai_auto_train_timeframe", DEFAULT_AUTO_TRAIN_TIMEFRAME) or DEFAULT_AUTO_TRAIN_TIMEFRAME).strip().lower()
    if timeframe not in ("1h", "4h", "1d"):
        timeframe = DEFAULT_AUTO_TRAIN_TIMEFRAME
    candles = _parse_int(
        db.get_account_setting("ai_auto_train_candles", str(DEFAULT_AUTO_TRAIN_CANDLES)),
        default=DEFAULT_AUTO_TRAIN_CANDLES,
        min_v=300,
        max_v=15000,
    )
    last_run_at = db.get_account_setting("ai_auto_train_last_run_at", "").strip() or None
    last_summary = db.get_account_setting("ai_auto_train_last_summary", "").strip() or None
    total_runs = _parse_int(db.get_account_setting("ai_auto_train_total_runs", "0"), 0, 0, 1_000_000)
    return {
        "enabled": enabled,
        "interval_minutes": interval_min,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "last_run_at": last_run_at,
        "last_summary": last_summary,
        "total_runs": total_runs,
    }


def _compute_initial_next_run_ts(cfg: Dict[str, Any], now_ts: Optional[float] = None) -> float:
    now = float(now_ts if now_ts is not None else time.time())
    interval_sec = float(cfg["interval_minutes"]) * 60.0
    last_ts = _parse_utc_ts(cfg.get("last_run_at") or "")
    if last_ts is not None:
        candidate = last_ts + interval_sec
        if candidate > now + 15.0:
            return candidate
        # Se já passou do horário da última execução, aguarda 60s de aquecimento após boot
        return now + 60.0
    return now + interval_sec


def get_auto_train_schedule(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Retorna configuração persistente e contagem regressiva do auto-treino 1h."""
    global _SCHED_NEXT_RUN_TS
    cfg = _load_schedule_config(db_path=db_path)
    now = time.time()
    with _SCHED_LOCK:
        active = bool(_SCHED_THREAD is not None and _SCHED_THREAD.is_alive())
        if cfg["enabled"]:
            if _SCHED_NEXT_RUN_TS is None:
                _SCHED_NEXT_RUN_TS = _compute_initial_next_run_ts(cfg, now_ts=now)
            next_ts = _SCHED_NEXT_RUN_TS
            seconds_until = max(0, int(round(next_ts - now)))
        else:
            next_ts = None
            seconds_until = None
    return {
        **cfg,
        "scheduler_active": active,
        "next_run_at": _fmt_utc(next_ts),
        "seconds_until_next": seconds_until,
    }


def update_auto_train_schedule(
    *,
    enabled: Optional[bool] = None,
    interval_minutes: Optional[int] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    candles: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Atualiza e persiste a programação do Centro de Treinamento Automático."""
    global _SCHED_NEXT_RUN_TS
    path = str(db_path or _server_attr("DB_PATH", DEFAULT_DB_PATH))
    db = TradingDatabase(db_path=path)
    current = _load_schedule_config(db_path=path)

    new_enabled = current["enabled"] if enabled is None else bool(enabled)
    new_interval = (
        current["interval_minutes"]
        if interval_minutes is None
        else max(MIN_AUTO_TRAIN_INTERVAL_MINUTES, min(MAX_AUTO_TRAIN_INTERVAL_MINUTES, int(interval_minutes)))
    )
    new_symbol = (symbol or current["symbol"]).strip().upper()
    new_tf = (timeframe or current["timeframe"]).strip().lower()
    if new_tf not in ("1h", "4h", "1d"):
        new_tf = DEFAULT_AUTO_TRAIN_TIMEFRAME
    new_candles = (
        current["candles"]
        if candles is None
        else max(300, min(15000, int(candles)))
    )

    db.set_account_setting("ai_auto_train_enabled", "1" if new_enabled else "0")
    db.set_account_setting("ai_auto_train_interval_minutes", str(new_interval))
    db.set_account_setting("ai_auto_train_symbol", new_symbol)
    db.set_account_setting("ai_auto_train_timeframe", new_tf)
    db.set_account_setting("ai_auto_train_candles", str(new_candles))

    with _SCHED_LOCK:
        if new_enabled:
            # Recalcula próximo disparo com base no novo intervalo
            now = time.time()
            last_ts = _parse_utc_ts(current.get("last_run_at") or "")
            if last_ts is not None and (last_ts + new_interval * 60.0) > now + 15.0:
                _SCHED_NEXT_RUN_TS = last_ts + new_interval * 60.0
            else:
                _SCHED_NEXT_RUN_TS = now + new_interval * 60.0
        else:
            _SCHED_NEXT_RUN_TS = None

    return get_auto_train_schedule(db_path=path)


def _record_training_completion(summary_stage: str, trigger: str, db_path: str) -> None:
    global _SCHED_NEXT_RUN_TS
    try:
        db = TradingDatabase(db_path=db_path)
        now_ts = time.time()
        now_str = _fmt_utc(now_ts) or ""
        db.set_account_setting("ai_auto_train_last_run_at", now_str)
        db.set_account_setting("ai_auto_train_last_summary", str(summary_stage)[:260])
        if trigger == "auto":
            prev = _parse_int(db.get_account_setting("ai_auto_train_total_runs", "0"), 0, 0, 1_000_000)
            db.set_account_setting("ai_auto_train_total_runs", str(prev + 1))
        cfg = _load_schedule_config(db_path=db_path)
        with _SCHED_LOCK:
            if cfg["enabled"]:
                _SCHED_NEXT_RUN_TS = now_ts + float(cfg["interval_minutes"]) * 60.0
            else:
                _SCHED_NEXT_RUN_TS = None
    except Exception:
        pass


def _set_train_state(**updates: Any) -> None:
    with _AI_TRAIN_LOCK:
        _AI_TRAIN_STATE.update(updates)


def get_train_state() -> Dict[str, Any]:
    with _AI_TRAIN_LOCK:
        snap = dict(_AI_TRAIN_STATE)
    try:
        sched = get_auto_train_schedule()
        snap["schedule"] = sched
        if (
            not snap.get("running")
            and snap.get("last_result") is None
            and not snap.get("error")
            and snap.get("stage") == "Pronto para treinar"
            and sched.get("last_summary")
        ):
            snap["stage"] = sched["last_summary"]
    except Exception:
        pass
    return snap


def _run_ai_training_job(
    symbol: str,
    timeframe: str,
    candles: int,
    offline: bool,
    trigger: str = "manual",
) -> None:
    db_path = str(_server_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_server_attr("MODELS_DIR", DEFAULT_MODELS_DIR))
    trigger_tag = "Auto 1h" if trigger == "auto" else "Manual"
    try:
        db = TradingDatabase(db_path=db_path)
        if not offline:
            from core import forex_market
            src_label = "Yahoo Finance (Forex/XTB)" if forex_market.is_forex_symbol(symbol) else "Binance"
            _set_train_state(
                stage=f"[{trigger_tag}] Baixando histórico de {symbol} via {src_label} para o SQLite...",
                progress_pct=15,
            )
            train_ai.download_deep_history(db, symbol=symbol, timeframe=timeframe, target_candles=candles)
        else:
            _set_train_state(stage=f"[{trigger_tag}] Carregando candles salvos no banco SQLite...", progress_pct=20)

        df = db.get_recent_candles(symbol, timeframe, limit=max(candles, 2000))
        _set_train_state(stage=f"[{trigger_tag}] Preparando features e Triple Barrier...", progress_pct=30)

        summary = train_ai.train_and_select_best_model(
            df,
            symbol=symbol,
            timeframe=timeframe,
            models_dir=models_dir,
            save_winner=True,
            progress_cb=lambda msg, pct: _set_train_state(stage=msg, progress_pct=pct),
            db_path=db_path,
        )
        winner = summary.get("winner") or {}
        try:
            macro_snap = macro_intelligence.fetch_macro_snapshot()
            macro_score = float(macro_snap.get("macro_score", 0.0))
        except Exception:
            macro_score = 0.0
        try:
            macro_intelligence.MacroLearningStore(db_path).record_learning_cycle(
                symbol=symbol,
                timeframe=timeframe,
                model_version=int(summary.get("model_version", 1)),
                model_type=str(winner.get("model_type", "ensemble")),
                metrics=winner,
                macro_score=macro_score,
                promoted=True,
                notes=f"Treinamento profundo ({trigger_tag}) via dashboard ({candles} candles)",
            )
        except Exception:
            pass
        auto_promoted = False
        try:
            if db.is_auto_evolution_enabled():
                from core.ml import ModelPersistence

                ver_int = int(summary.get("model_version", 1))
                champ_info = ModelPersistence(models_dir=models_dir).promote_to_live(
                    symbol,
                    timeframe,
                    version=ver_int,
                    promoted_by="auto",
                    notes=f"Auto-evolução sincronizada ({trigger_tag} • {candles} candles)",
                )
                db.record_model_promotion(
                    symbol=symbol,
                    timeframe=timeframe,
                    model_version=ver_int,
                    model_type=str(champ_info.get("model_type", winner.get("model_type", "ensemble"))),
                    accuracy=float(champ_info.get("accuracy", winner.get("precision_buy", 0.0))),
                    precision_buy=float(winner.get("precision_buy", 0.0)),
                    profit_factor=float(winner.get("profit_factor", 0.0)),
                    promoted_by="auto",
                    notes=f"Auto-evolução sincronizada ({trigger_tag} • {candles} candles)",
                )
                auto_promoted = True
        except Exception:
            auto_promoted = False
        summary["auto_promoted_to_live"] = auto_promoted
        summary["trigger"] = trigger
        promo_suffix = " • Sincronizado automaticamente na Demo e Conta Real!" if auto_promoted else ""
        final_stage = (
            f"Concluído! Campeão Demo: {str(winner.get('model_type', '')).upper()} "
            f"(v{summary.get('model_version')}){promo_suffix}"
        )
        _set_train_state(
            running=False,
            progress_pct=100,
            stage=final_stage,
            error=None,
            last_result=summary,
            trigger=trigger,
        )
        _record_training_completion(final_stage, trigger=trigger, db_path=db_path)
    except Exception as e:
        _set_train_state(
            running=False,
            progress_pct=0,
            stage="Falha no treinamento",
            error=str(e),
            last_result=None,
        )
        # Reagenda próxima tentativa mesmo após erro temporário de rede
        try:
            cfg = _load_schedule_config(db_path=db_path)
            with _SCHED_LOCK:
                if cfg["enabled"]:
                    global _SCHED_NEXT_RUN_TS
                    _SCHED_NEXT_RUN_TS = time.time() + float(cfg["interval_minutes"]) * 60.0
        except Exception:
            pass


def start_training_job(
    symbol: str,
    timeframe: str,
    candles: int,
    offline: bool,
    trigger: str = "manual",
) -> Dict[str, Any]:
    with _AI_TRAIN_LOCK:
        if _AI_TRAIN_STATE["running"]:
            return {
                "status": "already_running",
                "message": "Já existe um treinamento da IA em andamento.",
            }
        _AI_TRAIN_STATE.update({
            "running": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "progress_pct": 5,
            "stage": "Iniciando treinamento automático..." if trigger == "auto" else "Iniciando treinamento...",
            "error": None,
            "last_result": None,
            "trigger": trigger,
        })

    threading.Thread(
        target=_run_ai_training_job,
        args=(symbol, timeframe, candles, offline, trigger),
        daemon=True,
    ).start()
    return {
        "status": "started",
        "trigger": trigger,
        "message": f"Treinamento da IA iniciado para {symbol} ({timeframe}, {candles} candles).",
    }


def _auto_train_scheduler_loop(poll_seconds: float = 10.0) -> None:
    global _SCHED_NEXT_RUN_TS
    logger.info("Agendador automático do Centro de Treinamento da IA iniciado (padrão: a cada 1h).")
    demo_watchdog_tick = 0
    while not _SCHED_STOP_EVENT.is_set():
        try:
            # Watchdog: mantém o robô Demo sempre treinando o mercado quando habilitado
            if demo_watchdog_tick % 3 == 0:
                try:
                    from dashboard.bot_controller import ensure_demo_training_bot_running

                    ensure_demo_training_bot_running()
                except Exception as wd_err:
                    logger.debug("Watchdog Demo ignorado: %s", wd_err)
            demo_watchdog_tick += 1

            cfg = _load_schedule_config()
            if cfg["enabled"]:
                now = time.time()
                should_trigger = False
                with _SCHED_LOCK:
                    if _SCHED_NEXT_RUN_TS is None:
                        _SCHED_NEXT_RUN_TS = _compute_initial_next_run_ts(cfg, now_ts=now)
                    if now >= _SCHED_NEXT_RUN_TS:
                        should_trigger = True
                        # Avança temporariamente o próximo horário para evitar disparos duplicados
                        _SCHED_NEXT_RUN_TS = now + float(cfg["interval_minutes"]) * 60.0

                if should_trigger:
                    with _AI_TRAIN_LOCK:
                        already_running = bool(_AI_TRAIN_STATE["running"])
                    if not already_running:
                        logger.info(
                            "⏰ Auto-treino agendado disparado: %s (%s, %s candles)",
                            cfg["symbol"],
                            cfg["timeframe"],
                            cfg["candles"],
                        )
                        start_training_job(
                            symbol=cfg["symbol"],
                            timeframe=cfg["timeframe"],
                            candles=cfg["candles"],
                            offline=False,
                            trigger="auto",
                        )
        except Exception as exc:
            logger.warning("Aviso no agendador de auto-treino da IA: %s", exc)
        _SCHED_STOP_EVENT.wait(poll_seconds)
    logger.info("Agendador automático do Centro de Treinamento da IA finalizado.")


def start_auto_train_scheduler(*, force: bool = False) -> bool:
    """Inicia a thread daemon do agendador horário (ignorado automaticamente durante pytest)."""
    global _SCHED_THREAD, _SCHED_NEXT_RUN_TS
    if not force and (
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("FYNEX_DISABLE_AUTO_TRAIN_SCHEDULER", "").strip() in ("1", "true", "yes")
    ):
        return False

    with _SCHED_LOCK:
        if _SCHED_THREAD is not None and _SCHED_THREAD.is_alive():
            return False
        _SCHED_STOP_EVENT.clear()
        try:
            cfg = _load_schedule_config()
            if cfg["enabled"] and _SCHED_NEXT_RUN_TS is None:
                _SCHED_NEXT_RUN_TS = _compute_initial_next_run_ts(cfg)
        except Exception:
            pass
        _SCHED_THREAD = threading.Thread(
            target=_auto_train_scheduler_loop,
            name="fynex-ai-auto-train-scheduler",
            daemon=True,
        )
        _SCHED_THREAD.start()
        return True


def stop_auto_train_scheduler() -> None:
    """Encerra a thread daemon do agendador de forma limpa no shutdown."""
    global _SCHED_THREAD
    _SCHED_STOP_EVENT.set()
    with _SCHED_LOCK:
        t = _SCHED_THREAD
        _SCHED_THREAD = None
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
