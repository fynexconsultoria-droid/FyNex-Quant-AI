"""Rotas da Mesa XTB, Copiloto XTB (xStation 5) e Telegram do Copiloto."""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException, Query

import alerts
from core import forex_ai_trader, forex_market, macro_intelligence, xtb_copilot
from core.logging_setup import DEFAULT_DB_PATH
from dashboard.schemas import CopilotStatusPayload, TelegramConfigPayload, XTBOrderPayload
from dashboard.security import require_csrf

router = APIRouter(tags=["xtb"])
_FOREX_TRADER_CACHE: dict[str, forex_ai_trader.ForexAITrader] = {}


def _get_forex_trader() -> forex_ai_trader.ForexAITrader:
    path = _db_path()
    trader = _FOREX_TRADER_CACHE.get(path)
    if trader is None:
        trader = forex_ai_trader.ForexAITrader(db_path=path)
        _FOREX_TRADER_CACHE[path] = trader
    return trader


def _db_path() -> str:
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, "DB_PATH"):
        return str(srv.DB_PATH)
    return DEFAULT_DB_PATH


def _get_xtb_store() -> forex_market.XTBDeskStore:
    return forex_market.XTBDeskStore(db_path=_db_path())


def _get_xtb_copilot() -> xtb_copilot.XTBCopilotStore:
    return xtb_copilot.XTBCopilotStore(db_path=_db_path())


@router.get("/api/xtb/account")
def get_xtb_account():
    return _get_xtb_store().get_account_snapshot()


@router.post("/api/xtb/order", dependencies=[Depends(require_csrf)])
def open_xtb_order(payload: XTBOrderPayload):
    store = _get_xtb_store()
    try:
        pos = store.open_position(
            symbol=payload.symbol,
            side=payload.side,
            lots=payload.lots,
            leverage=payload.leverage,
            sl_price=payload.sl_price,
            tp_price=payload.tp_price,
        )
        return {
            "status": "opened",
            "position": pos,
            "message": f"Ordem {pos['side']} {pos['lots']} lote(s) em {pos['symbol']} aberta a {pos['entry_price']}.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=503, detail="Falha interna ao executar ordem na mesa XTB.")


@router.post("/api/xtb/close/{position_id}", dependencies=[Depends(require_csrf)])
def close_xtb_order(position_id: int):
    store = _get_xtb_store()
    try:
        closed = store.close_position(position_id=position_id, reason="MANUAL")
        return {
            "status": "closed",
            "trade": closed,
            "message": f"Posição #{position_id} ({closed['symbol']}) fechada com PnL ${closed['pnl_usd']:+.2f}.",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=503, detail="Falha interna ao fechar posição na mesa XTB.")


@router.get("/api/xtb/copilot/signals")
def get_xtb_copilot_signals(limit: int = Query(default=25, ge=1, le=100)):
    is_open, msg = forex_ai_trader.is_forex_market_open()
    signals = _get_xtb_copilot().list_signals(limit=limit)
    new_count = sum(1 for s in signals if s.get("status") == "NEW")
    return {
        "mode": "HYBRID",
        "binance_mode": "AUTO_CRYPTO",
        "xtb_mode": "COPILOT_XSTATION5",
        "market_open": is_open,
        "status_message": msg,
        "monitored_pairs": list(forex_ai_trader.FOREX_AI_PAIRS),
        "new_count": new_count,
        "telegram": xtb_copilot.get_telegram_config(),
        "whatsapp": alerts.get_whatsapp_config(),
        "signals": signals,
    }


@router.post("/api/xtb/copilot/scan", dependencies=[Depends(require_csrf)])
def trigger_xtb_copilot_scan():
    try:
        macro_snap = macro_intelligence.fetch_macro_snapshot()
    except Exception:
        macro_snap = None
    trader = _get_forex_trader()
    report = trader.run_cycle(macro_snapshot=macro_snap)
    signals = _get_xtb_copilot().list_signals(limit=25)
    return {
        "status": "scanned",
        "market_open": report.get("market_open", False),
        "status_message": report.get("status_message", ""),
        "min_score": float(trader.min_score),
        "last_action": report.get("last_action"),
        "opportunities": report.get("opportunities", []),
        "signals": signals,
    }


@router.post("/api/xtb/copilot/signals/{signal_id}/status", dependencies=[Depends(require_csrf)])
def update_xtb_copilot_signal_status(signal_id: int, payload: CopilotStatusPayload):
    try:
        updated = _get_xtb_copilot().update_status(signal_id=signal_id, status=payload.status)
        return {"status": "updated", "signal": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/xtb/copilot/telegram")
def get_xtb_copilot_telegram():
    return xtb_copilot.get_telegram_config()


@router.post("/api/xtb/copilot/telegram", dependencies=[Depends(require_csrf)])
def save_xtb_copilot_telegram(payload: TelegramConfigPayload):
    try:
        cfg = xtb_copilot.save_telegram_config(bot_token=payload.bot_token, chat_id=payload.chat_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Falha ao salvar configuração do Telegram.")
    test_result = None
    if payload.send_test:
        test_result = xtb_copilot.send_telegram_message(
            "✅ FyNex Quant AI — Copiloto XTB conectado com sucesso! "
            "Você receberá aqui os alertas de Forex, Ouro, Prata, Petróleo e Índices prontos para a xStation 5."
        )
    return {"status": "saved", "telegram": cfg, "test_result": test_result}
