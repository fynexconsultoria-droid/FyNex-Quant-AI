"""
Proteção Dinâmica de Lucro Compartilhada (Breakeven + Trailing Profit)
======================================================================
Fonte única da verdade usada tanto pelo `PositionManager` (Paper/Testnet/Live)
quanto pelo `backtest.py` (Walk-Forward), garantindo 100% de paridade entre
simulação e execução real.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProfitProtectionDecision:
    should_update: bool
    new_sl: float
    progress: float


def compute_profit_protection_stop(
    *,
    entry_price: float,
    tp_price: float,
    current_sl: float,
    high_check: float,
    current_price: float,
    is_real_with_oco: bool = False,
    breakeven_trigger: float = 0.60,
    trailing_trigger: float = 0.80,
    breakeven_buffer_pct: float = 0.0025,
    trailing_lock_ratio: float = 0.50,
    price_clearance_pct: float = 0.0005,
    oco_clearance_pct: float = 0.0020,
) -> ProfitProtectionDecision:
    """
    Calcula se o Stop Loss deve subir para proteger lucro acumulado:
    - Aos 60% do alvo (`breakeven_trigger`): trava no breakeven (+0.25% cobrindo taxas).
    - Aos 80% do alvo (`trailing_trigger`): trava 50% do lucro atual.
    - Nunca aproxima o stop do preço atual a ponto de disparar stop fantasma ou erro -2010 na OCO.
    """
    tp_dist = tp_price - entry_price
    if tp_dist <= 0 or high_check <= entry_price or current_price <= 0:
        return ProfitProtectionDecision(False, current_sl, 0.0)

    progress = (high_check - entry_price) / tp_dist
    new_sl = current_sl
    be_sl = entry_price * (1.0 + breakeven_buffer_pct)

    if progress >= trailing_trigger:
        trail_sl = entry_price + (high_check - entry_price) * trailing_lock_ratio
        new_sl = max(new_sl, be_sl, trail_sl)
    elif progress >= breakeven_trigger:
        new_sl = max(new_sl, be_sl)

    if new_sl >= current_price * (1.0 - price_clearance_pct):
        return ProfitProtectionDecision(False, current_sl, progress)

    min_step = 1.0015 if is_real_with_oco else 1.0001
    if new_sl <= current_sl * min_step:
        return ProfitProtectionDecision(False, current_sl, progress)

    if is_real_with_oco and new_sl >= current_price * (1.0 - oco_clearance_pct):
        return ProfitProtectionDecision(False, current_sl, progress)

    return ProfitProtectionDecision(True, float(new_sl), float(progress))
