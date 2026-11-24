"""Modelo de dados de posição aberta (FyNex Quant AI)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    symbol: str
    timeframe: str
    mode: str
    model_version: int
    entry_time: str
    entry_price: float
    quantity: float
    order_id: str
    sl_price: float
    tp_price: float
    oco_list_id: str = ""
    base_balance_before: float = 0.0
    status: str = "OPEN"
