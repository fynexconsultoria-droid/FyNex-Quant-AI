"""Rotas de Configuração, Saúde, Saldo e Teste de Conexão Binance."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import binance_client
from dashboard.bot_controller import _check_is_bot_running, allow_forced_refresh
from dashboard.schemas import SYMBOL_PATTERN, BinanceConfigPayload, BinanceTestPayload
from dashboard.security import require_csrf

router = APIRouter(tags=["binance"])


@router.get("/api/binance/health")
def get_binance_health(
    testnet: bool = Query(default=False),
    refresh: bool = Query(default=False),
):
    if refresh and allow_forced_refresh("binance_health", cooldown_sec=3.0):
        return binance_client.check_binance_health(testnet=testnet, ttl_seconds=0.0)
    return binance_client.check_binance_health(testnet=testnet)


@router.get("/api/binance/balance")
def get_binance_balance():
    return binance_client.fetch_cached_balance()


@router.get("/api/binance/execution-ready")
def get_binance_execution_ready(symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN)):
    return binance_client.check_execution_readiness(symbol=symbol)


@router.get("/api/binance/config")
def get_binance_config():
    cfg = binance_client.load_binance_config()
    return {
        "has_keys": cfg["has_keys"],
        "api_key_masked": cfg["api_key_masked"],
        "testnet": cfg["testnet"],
        "trading_mode": cfg["trading_mode"],
        "trade_amount_usdt": cfg["trade_amount_usdt"],
    }


@router.post("/api/binance/save", dependencies=[Depends(require_csrf)])
def save_binance_config_endpoint(payload: BinanceConfigPayload):
    try:
        binance_client.save_binance_config(
            api_key=payload.api_key or "",
            api_secret=payload.api_secret or "",
            testnet=payload.testnet,
            trading_mode=payload.trading_mode,
            trade_amount_usdt=payload.trade_amount_usdt,
        )
        saved = binance_client.load_binance_config()
        test_res = binance_client.test_binance_connection(testnet=saved["testnet"])
        running, _ = _check_is_bot_running()
        message = "Configurações salvas no arquivo .env com sucesso!"
        if running:
            message += " O robô está em execução: reinicie-o para aplicar as novas configurações."
        return {
            "status": "saved",
            "message": message,
            "restart_required": running,
            "test_result": test_res,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao salvar configurações da Binance: {str(e)}")


@router.post("/api/binance/test", dependencies=[Depends(require_csrf)])
def test_binance_endpoint(payload: Optional[BinanceTestPayload] = None):
    key = payload.api_key if payload else None
    secret = payload.api_secret if payload else None
    testnet = payload.testnet if payload else None
    return binance_client.test_binance_connection(api_key=key, api_secret=secret, testnet=testnet)
