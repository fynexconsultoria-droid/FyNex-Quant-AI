"""
Perfil Único de Estratégia por Timeframe (FyNex Quant AI - Fase 2)
==================================================================
Fonte única da verdade para parâmetros de alvo (Triple Barrier), gestão de risco
e horizonte temporal, compartilhada entre:
- trading_bot.py (treino, inferência, avaliação ao vivo e saída por tempo)
- backtest.py (simulação walk-forward)
- dashboard/server.py (exibição de status e calibração)
"""

from dataclasses import dataclass
import math
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class StrategyProfile:
    timeframe: str
    tp_pct: float
    sl_pct: float
    horizon_candles: int
    confidence: float = 0.60
    retrain_every: int = 30
    use_atr_barriers: bool = False
    atr_tp_mult: float = 2.2
    atr_sl_mult: float = 1.4
    min_tp_pct: float = 0.0075
    max_tp_pct: float = 0.04
    min_sl_pct: float = 0.0045
    max_sl_pct: float = 0.025


STRATEGY_PROFILES: Dict[str, StrategyProfile] = {
    "1m": StrategyProfile(
        timeframe="1m",
        tp_pct=0.0060,
        sl_pct=0.0035,
        horizon_candles=30,
        confidence=0.65,
        retrain_every=30,
    ),
    "3m": StrategyProfile(
        timeframe="3m",
        tp_pct=0.0075,
        sl_pct=0.0045,
        horizon_candles=24,
        confidence=0.62,
        retrain_every=20,
    ),
    "5m": StrategyProfile(
        timeframe="5m",
        tp_pct=0.0090,
        sl_pct=0.0050,
        horizon_candles=24,
        confidence=0.60,
        retrain_every=15,
    ),
    "15m": StrategyProfile(
        timeframe="15m",
        tp_pct=0.0105,
        sl_pct=0.0065,
        horizon_candles=18,
        confidence=0.60,
        retrain_every=12,
    ),
    "30m": StrategyProfile(
        timeframe="30m",
        tp_pct=0.0120,
        sl_pct=0.0080,
        horizon_candles=16,
        confidence=0.62,
        retrain_every=10,
    ),
    "1h": StrategyProfile(
        timeframe="1h",
        tp_pct=0.0180,
        sl_pct=0.0110,
        horizon_candles=18,
        confidence=0.65,
        retrain_every=6,
        use_atr_barriers=True,
        atr_tp_mult=2.0,
        atr_sl_mult=1.3,
        min_tp_pct=0.0080,
        max_tp_pct=0.0250,
        min_sl_pct=0.0050,
        max_sl_pct=0.0150,
    ),
    "4h": StrategyProfile(
        timeframe="4h",
        tp_pct=0.0300,
        sl_pct=0.0180,
        horizon_candles=12,
        confidence=0.65,
        retrain_every=4,
        use_atr_barriers=True,
        atr_tp_mult=2.0,
        atr_sl_mult=1.3,
        min_tp_pct=0.0120,
        max_tp_pct=0.0450,
        min_sl_pct=0.0080,
        max_sl_pct=0.0250,
    ),
    "1d": StrategyProfile(
        timeframe="1d",
        tp_pct=0.0500,
        sl_pct=0.0300,
        horizon_candles=10,
        confidence=0.65,
        retrain_every=3,
        use_atr_barriers=True,
        atr_tp_mult=2.0,
        atr_sl_mult=1.3,
        min_tp_pct=0.0200,
        max_tp_pct=0.0800,
        min_sl_pct=0.0120,
        max_sl_pct=0.0400,
    ),
}

DEFAULT_TIMEFRAME = "5m"

# Taxa Spot padrão da Binance (0.1%/lado) e slippage estimado no paper trading/backtest
PAPER_FEE_PCT = 0.001
PAPER_SLIPPAGE_PCT = 0.0002


def breakeven_win_rate(
    tp_pct: float,
    sl_pct: float,
    fee_pct: float = PAPER_FEE_PCT,
    slippage_pct: float = PAPER_SLIPPAGE_PCT,
) -> float:
    """Retorna a taxa de acerto mínima (0..1) necessária para empatar após taxas e slippage."""
    cost = 2.0 * (fee_pct + slippage_pct)
    denom = float(tp_pct) + float(sl_pct)
    if denom <= 0:
        return 1.0
    return min(1.0, max(0.0, (float(sl_pct) + cost) / denom))


def get_strategy_profile(
    timeframe: str,
    tp_override: Optional[float] = None,
    sl_override: Optional[float] = None,
    horizon_override: Optional[int] = None,
    confidence_override: Optional[float] = None,
    use_atr_barriers: Optional[bool] = None,
) -> StrategyProfile:
    """Retorna o perfil do timeframe solicitado, aplicando overrides explícitos validados."""
    if timeframe not in STRATEGY_PROFILES:
        base = STRATEGY_PROFILES[DEFAULT_TIMEFRAME]
    else:
        base = STRATEGY_PROFILES[timeframe]

    eff_tp = float(tp_override) if tp_override is not None else base.tp_pct
    eff_sl = float(sl_override) if sl_override is not None else base.sl_pct
    eff_horizon = int(horizon_override) if horizon_override is not None else base.horizon_candles
    eff_conf = float(confidence_override) if confidence_override is not None else base.confidence

    if not (math.isfinite(eff_tp) and eff_tp > 0):
        raise ValueError(f"tp_pct inválido: {eff_tp}")
    if not (math.isfinite(eff_sl) and eff_sl > 0):
        raise ValueError(f"sl_pct inválido: {eff_sl}")
    if eff_horizon < 1:
        raise ValueError(f"horizon_candles inválido: {eff_horizon}")
    if not (math.isfinite(eff_conf) and 0.0 < eff_conf < 1.0):
        raise ValueError(f"confidence inválido: {eff_conf}")

    return StrategyProfile(
        timeframe=timeframe,
        tp_pct=eff_tp,
        sl_pct=eff_sl,
        horizon_candles=eff_horizon,
        confidence=eff_conf,
        retrain_every=base.retrain_every,
        use_atr_barriers=bool(use_atr_barriers) if use_atr_barriers is not None else base.use_atr_barriers,
        atr_tp_mult=base.atr_tp_mult,
        atr_sl_mult=base.atr_sl_mult,
        min_tp_pct=base.min_tp_pct,
        max_tp_pct=base.max_tp_pct,
        min_sl_pct=base.min_sl_pct,
        max_sl_pct=base.max_sl_pct,
    )


def resolve_barriers(
    profile: StrategyProfile,
    atr_pct: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Resolve `(tp_pct, sl_pct)` para o perfil.
    Quando `profile.use_atr_barriers` está ativo e `atr_pct > 0` finito, escala as barreiras
    pelo ATR mantendo limites seguros acima das taxas de corretagem.
    """
    if profile.use_atr_barriers and atr_pct is not None and math.isfinite(float(atr_pct)) and float(atr_pct) > 0:
        atr_f = float(atr_pct)
        tp = min(max(atr_f * profile.atr_tp_mult, profile.min_tp_pct), profile.max_tp_pct)
        sl = min(max(atr_f * profile.atr_sl_mult, profile.min_sl_pct), profile.max_sl_pct)
        return float(tp), float(sl)
    return float(profile.tp_pct), float(profile.sl_pct)
