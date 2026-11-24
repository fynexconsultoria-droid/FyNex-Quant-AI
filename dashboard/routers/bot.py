"""Rotas de Controle do Processo do Robô (Start / Stop)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from dashboard.bot_controller import start_bot_process, stop_bot_process
from dashboard.schemas import SYMBOL_PATTERN, TIMEFRAME_PATTERN
from dashboard.security import require_csrf

router = APIRouter(tags=["bot"])


@router.post("/api/bot/start", dependencies=[Depends(require_csrf)])
def start_bot(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
    retrain: Optional[int] = Query(default=None, ge=1, le=500),
    trading_mode: Optional[str] = Query(default=None, pattern="^(paper|testnet|live)$"),
    trade_amount: Optional[float] = Query(default=None, ge=10.0, le=10000.0),
):
    return start_bot_process(
        symbol=symbol,
        timeframe=timeframe,
        retrain=retrain,
        trading_mode=trading_mode,
        trade_amount=trade_amount,
    )


@router.post("/api/bot/stop", dependencies=[Depends(require_csrf)])
def stop_bot():
    return stop_bot_process()
