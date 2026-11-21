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
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class StrategyProfile:
    timeframe: str
    tp_pct: float
    sl_pct: float
    horizon_candles: int
    confidence: float = 0.55
    retrain_every: int = 30
    use_atr_barriers: bool = False
    atr_tp_mult: float = 2.2
    atr_sl_mult: float = 1.4
    min_tp_pct: float = 0.0035
    max_tp_pct: float = 0.04
    min_sl_pct: float = 0.0025
    max_sl_pct: float = 0.025


STRATEGY_PROFILES: Dict[str, StrategyProfile] = {
    "1m": StrategyProfile(
        timeframe="1m",
        tp_pct=0.0045,
        sl_pct=0.0030,
        horizon_candles=30,
        confidence=0.55,
        retrain_every=30,
    ),
    "3m": StrategyProfile(
        timeframe="3m",
        tp_pct=0.0065,
        sl_pct=0.0040,
        horizon_candles=24,
        confidence=0.55,
        retrain_every=20,
    ),
    "5m": StrategyProfile(
        timeframe="5m",
        tp_pct=0.0080,
        sl_pct=0.0050,
        horizon_candles=24,
        confidence=0.55,
        retrain_every=15,
    ),
    "15m": StrategyProfile(
        timeframe="15m",
        tp_pct=0.0150,
        sl_pct=0.0090,
        horizon_candles=16,
        confidence=0.55,
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
        tp_pct=0.0280,
        sl_pct=0.0160,
        horizon_candles=16,
        confidence=0.55,
        retrain_every=8,
    ),
}

DEFAULT_TIMEFRAME = "5m"

# Taxa Spot padrão da Binance (0.1%/lado) e slippage estimado no paper trading/backtest
PAPER_FEE_PCT = 0.001
PAPER_SLIPPAGE_PCT = 0.0002


def get_strategy_profile(
    timeframe: str,
    tp_override: Optional[float] = None,
    sl_override: Optional[float] = None,
    horizon_override: Optional[int] = None,
    confidence_override: Optional[float] = None,
    use_atr_barriers: Optional[bool] = None,
) -> StrategyProfile:
    """Retorna o perfil do timeframe solicitado, aplicando overrides explícitos se houver."""
    base = STRATEGY_PROFILES.get(timeframe, STRATEGY_PROFILES[DEFAULT_TIMEFRAME])
    return StrategyProfile(
        timeframe=timeframe,
        tp_pct=float(tp_override) if tp_override is not None else base.tp_pct,
        sl_pct=float(sl_override) if sl_override is not None else base.sl_pct,
        horizon_candles=int(horizon_override) if horizon_override is not None else base.horizon_candles,
        confidence=float(confidence_override) if confidence_override is not None else base.confidence,
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
    Quando `profile.use_atr_barriers` está ativo e `atr_pct > 0`, escala as barreiras
    pelo ATR mantendo limites seguros acima das taxas de corretagem.
    """
    if profile.use_atr_barriers and atr_pct is not None and atr_pct > 0:
        tp = min(max(atr_pct * profile.atr_tp_mult, profile.min_tp_pct), profile.max_tp_pct)
        sl = min(max(atr_pct * profile.atr_sl_mult, profile.min_sl_pct), profile.max_sl_pct)
        return float(tp), float(sl)
    return float(profile.tp_pct), float(profile.sl_pct)
