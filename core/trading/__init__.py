"""Pacote de gestão de posições, risco e proteção dinâmica de lucro (FyNex Quant AI)."""

from core.trading.models import Position
from core.trading.position_manager import STOP_FALLBACK_EXTRA_PCT, PositionManager
from core.trading.profit_protection import ProfitProtectionDecision, compute_profit_protection_stop
from core.trading.risk import KILL_SWITCH_PATH, RiskManager
from core.trading.store import PositionStore

__all__ = [
    "KILL_SWITCH_PATH",
    "STOP_FALLBACK_EXTRA_PCT",
    "Position",
    "PositionStore",
    "RiskManager",
    "PositionManager",
    "ProfitProtectionDecision",
    "compute_profit_protection_stop",
]
