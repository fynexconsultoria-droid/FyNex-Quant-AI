"""Rotas de Configuração e Disparo de Alertas (WhatsApp CallMeBot e Resumo Diário)."""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException

import alerts
from core.logging_setup import DEFAULT_DB_PATH
from dashboard.schemas import WhatsAppConfigPayload
from dashboard.security import require_csrf

router = APIRouter(tags=["alerts"])


def _db_path() -> str:
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, "DB_PATH"):
        return str(srv.DB_PATH)
    return DEFAULT_DB_PATH


@router.get("/api/alerts/whatsapp")
def get_whatsapp_alert_config():
    return alerts.get_whatsapp_config()


@router.post("/api/alerts/whatsapp", dependencies=[Depends(require_csrf)])
def save_whatsapp_alert_config(payload: WhatsAppConfigPayload):
    try:
        cfg = alerts.save_whatsapp_config(
            phone=payload.phone,
            apikey=payload.apikey,
            enabled=payload.enabled,
            daily_report_hour=payload.daily_report_hour,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Falha ao salvar configuração do WhatsApp.")

    test_result = None
    if payload.send_test and cfg.get("enabled"):
        test_result = alerts.send_whatsapp_message(
            "✅ FyNex Quant AI conectada ao seu WhatsApp via CallMeBot!\n"
            "Você receberá alertas de entradas, saídas (lucro/prejuízo), falhas e resumo diário às "
            f"{cfg['daily_report_hour']:02d}:00 (Brasília)."
        )
    return {"status": "saved", "whatsapp": cfg, "test_result": test_result}


@router.post("/api/alerts/whatsapp/daily-summary", dependencies=[Depends(require_csrf)])
def send_whatsapp_daily_summary_now():
    summary = alerts.build_daily_summary(db_path=_db_path())
    send_res = alerts.send_whatsapp_message(f"[FyNex IA • RESUMO DO DIA]\n{summary['message']}")
    return {"status": "sent" if send_res.get("sent") else "failed", "summary": summary, "send_result": send_res}
