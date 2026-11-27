"""Schemas Pydantic e padrões de validação da API do Dashboard (FyNex Quant AI)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

SYMBOL_PATTERN = r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$"
TIMEFRAME_PATTERN = r"^(1m|3m|5m|15m|30m|1h|4h|1d)$"
OPERATIONAL_TIMEFRAME_PATTERN = r"^(1h|4h|1d)$"
ACCOUNT_PATTERN = r"^(demo|live)$"


class LiveOrderDecisionPayload(BaseModel):
    note: str = ""


class PromoteModelPayload(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    version: Optional[int] = None
    notes: str = ""


class AccountPolicyPayload(BaseModel):
    auto_promote_to_live: bool = True
    demo_always_on_training: Optional[bool] = None
    live_high_potential_only: Optional[bool] = None
    live_min_confidence_pct: Optional[int] = None
    live_min_potential_score: Optional[int] = None
    live_micro_compound_enabled: Optional[bool] = None
    live_compound_alloc_pct: Optional[int] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None


class XTBOrderPayload(BaseModel):
    symbol: str
    side: str
    lots: float
    leverage: int = 30
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None


class CopilotStatusPayload(BaseModel):
    status: str


class TelegramConfigPayload(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    send_test: bool = False


class WhatsAppConfigPayload(BaseModel):
    phone: str = ""
    apikey: str = ""
    enabled: bool = True
    daily_report_hour: int = 21
    send_test: bool = False


class AiChatPayload(BaseModel):
    message: str
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"


class AiTrainSchedulePayload(BaseModel):
    enabled: bool = True
    interval_minutes: int = 60
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    candles: Optional[int] = None


class BinanceConfigPayload(BaseModel):
    api_key: Optional[str] = ""
    api_secret: Optional[str] = ""
    testnet: bool = False
    trading_mode: str = "paper"
    trade_amount_usdt: float = 50.0


class BinanceTestPayload(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    testnet: Optional[bool] = None
