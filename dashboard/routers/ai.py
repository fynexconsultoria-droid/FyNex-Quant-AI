"""Rotas de Inteligência Artificial (Performance, Chat, Evolução, Lições de Erro e Treino)."""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException, Query

from core import ai_chat, error_learning, macro_intelligence
from core.logging_setup import DEFAULT_DB_PATH
from dashboard.schemas import SYMBOL_PATTERN, TIMEFRAME_PATTERN, AiChatPayload
from dashboard.security import require_csrf
from dashboard.training_jobs import get_train_state, start_training_job

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


@router.post("/api/ai/train", dependencies=[Depends(require_csrf)])
def start_ai_training(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
    candles: int = Query(default=3000, ge=300, le=15000),
    offline: bool = Query(default=False),
):
    return start_training_job(symbol=symbol, timeframe=timeframe, candles=candles, offline=offline)
