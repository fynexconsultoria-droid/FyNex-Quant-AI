"""Rotas de Inteligência Artificial (Performance, Chat, Evolução, Lições de Erro e Treino)."""

from __future__ import annotations

import re
import sys

from fastapi import APIRouter, Depends, HTTPException, Query

from core import ai_chat, error_learning, macro_intelligence
from core.logging_setup import DEFAULT_DB_PATH
from dashboard.schemas import (
    OPERATIONAL_TIMEFRAME_PATTERN,
    SYMBOL_PATTERN,
    TIMEFRAME_PATTERN,
    AiChatPayload,
    AiTrainSchedulePayload,
)
from dashboard.security import require_csrf
from dashboard.training_jobs import (
    get_auto_train_schedule,
    get_train_state,
    start_training_job,
    update_auto_train_schedule,
)

router = APIRouter(tags=["ai"])


def _db_path() -> str:
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, "DB_PATH"):
        return str(srv.DB_PATH)
    return DEFAULT_DB_PATH


@router.get("/api/ai/performance")
def get_ai_performance(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
):
    return ai_chat.get_performance_summary(db_path=_db_path(), symbol=symbol, timeframe=timeframe)


@router.post("/api/ai/chat", dependencies=[Depends(require_csrf)])
def post_ai_chat(payload: AiChatPayload):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Digite uma pergunta para a IA.")
    if len(msg) > 500:
        msg = msg[:500]
    return ai_chat.answer_ai_chat(
        message=msg,
        db_path=_db_path(),
        symbol=payload.symbol or "BTC/USDT",
        timeframe=payload.timeframe or "1h",
    )


@router.get("/api/ai/learning-history")
def get_ai_learning_history(limit: int = Query(default=15, ge=1, le=100)):
    store = macro_intelligence.MacroLearningStore(_db_path())
    return {"items": store.get_learning_history(limit=limit)}


@router.get("/api/ai/evolution")
def get_ai_evolution(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
):
    macro = macro_intelligence.fetch_macro_snapshot()
    evo = macro_intelligence.compute_ai_evolution_progress(
        db_path=_db_path(), symbol=symbol, timeframe=timeframe, macro_snapshot=macro
    )
    return {"evolution": evo, "macro": macro}


@router.get("/api/ai/error-lessons")
def get_ai_error_lessons(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
    limit: int = Query(default=10, ge=1, le=50),
):
    store = error_learning.ErrorLearningStore(_db_path())
    return store.get_error_learning_report(symbol=symbol, timeframe=timeframe, limit=limit)


@router.get("/api/ai/train/status")
def get_ai_train_status():
    return get_train_state()


@router.get("/api/ai/train/schedule")
def get_ai_train_schedule():
    return get_auto_train_schedule(db_path=_db_path())


@router.post("/api/ai/train/schedule", dependencies=[Depends(require_csrf)])
def save_ai_train_schedule(payload: AiTrainSchedulePayload):
    interval_min = int(payload.interval_minutes or 60)
    if interval_min < 60 or interval_min > 1440:
        raise HTTPException(
            status_code=422,
            detail="Intervalo do auto-treino deve estar entre 60 min (1h) e 1440 min (24h).",
        )
    if payload.symbol is not None and not re.match(SYMBOL_PATTERN, payload.symbol.strip().upper()):
        raise HTTPException(status_code=422, detail="Símbolo inválido para auto-treino.")
    if payload.timeframe is not None and not re.match(OPERATIONAL_TIMEFRAME_PATTERN, payload.timeframe.strip().lower()):
        raise HTTPException(
            status_code=422,
            detail="Timeframe operacional inválido para auto-treino (use 1h, 4h ou 1d).",
        )
    if payload.candles is not None and (payload.candles < 300 or payload.candles > 15000):
        raise HTTPException(status_code=422, detail="Quantidade de candles deve estar entre 300 e 15000.")

    sched = update_auto_train_schedule(
        enabled=bool(payload.enabled),
        interval_minutes=interval_min,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        candles=payload.candles,
        db_path=_db_path(),
    )
    return {
        "status": "updated",
        "message": (
            f"Auto-treino automático ativado: a cada {sched['interval_minutes'] // 60}h "
            f"({sched['symbol']} • {sched['timeframe']} • {sched['candles']} candles)."
            if sched["enabled"]
            else "Auto-treino automático pausado."
        ),
        "schedule": sched,
    }


@router.post("/api/ai/train", dependencies=[Depends(require_csrf)])
def start_ai_training(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=OPERATIONAL_TIMEFRAME_PATTERN),
    candles: int = Query(default=3000, ge=300, le=15000),
    offline: bool = Query(default=False),
):
    return start_training_job(symbol=symbol, timeframe=timeframe, candles=candles, offline=offline)
