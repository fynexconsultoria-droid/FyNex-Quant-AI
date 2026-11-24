"""
Facade de compatibilidade para Gestão de Posição e Risco (FyNex Quant AI)
=========================================================================
A implementação modularizada reside em `core.trading`:
- `core.trading.models`: dataclass `Position`
- `core.trading.store`: `PositionStore` (SQLite)
- `core.trading.risk`: `RiskManager` (limites diários e kill-switch)
- `core.trading.profit_protection`: proteção dinâmica de lucro (Breakeven + Trailing)
- `core.trading.position_manager`: `PositionManager`
"""

from core.infra.utils import utc_now_str
from core.logging_setup import BASE_DIR
from core.trading import (
    KILL_SWITCH_PATH,
    STOP_FALLBACK_EXTRA_PCT,
    Position,
    PositionManager,
    PositionStore,
    RiskManager,
)
from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT

__all__ = [
    "BASE_DIR",
    "KILL_SWITCH_PATH",
    "STOP_FALLBACK_EXTRA_PCT",
    "PAPER_FEE_PCT",
    "PAPER_SLIPPAGE_PCT",
    "utc_now_str",
    "Position",
    "PositionStore",
    "RiskManager",
    "PositionManager",
]
