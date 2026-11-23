"""
Guardião de Assertividade e Segurança de Entradas (FyNex Quant AI)
==================================================================
Filosofia conservadora (qualidade > quantidade):
Antes de qualquer compra, valida 5 travas objetivas de proteção de capital:
1. Bloqueio em Regime Macro adverso (RISK-OFF SEVERO: Dólar/Juros/Inflação/Notícias muito negativos)
2. Anti-Topo / Anti-Euforia (RSI 14 > 70 bloqueia compra esticada)
3. Filtro de Tendência (evita "pegar faca caindo" quando EMA12/EMA26 está muito negativa)
4. Filtro de Volatilidade Extrema (bloqueia entrada em flash crashes / picos de ATR%)
5. Cooldown pós-Stop Loss (aguarda resfriamento do mercado após uma perda)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _finite_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def evaluate_entry_safety(
    features_row: Dict[str, Any],
    macro_snapshot: Optional[Dict[str, Any]] = None,
    last_trade: Optional[Dict[str, Any]] = None,
    cooldown_seconds: int = 7200,
    max_rsi_entry: float = 68.0,
    min_ema_trend_ratio: float = -0.002,
    max_atr_pct: float = 0.025,
    max_bb_pos: float = 0.95,
    min_adx_for_chop: float = 0.14,
    min_bb_width_for_chop: float = 0.008,
    consecutive_losses: int = 0,
    max_consecutive_losses: int = 3,
    circuit_breaker_cooldown_seconds: int = 14400,
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, List[str]]:
    """
    Avalia se o contexto técnico, macro e de risco está seguro para abrir compra.
    Retorna `(aprovado, lista_de_motivos_de_bloqueio)`.
    """
    reasons: List[str] = []
    now_dt = now_utc or datetime.now(timezone.utc)

    # 1. Proteção Macro (Dólar, Juros EUA, Inflação/Fed, Petróleo, Guerras e Notícias)
    if macro_snapshot:
        regime = str(macro_snapshot.get("regime", "")).upper()
        macro_score = _finite_float(macro_snapshot.get("macro_score")) or 0.0
        if regime == "RISK-OFF SEVERO" or macro_score <= -45.0:
            reasons.append(
                f"Regime macro adverso ({regime}, score {macro_score:+.1f}) — compras suspensas para proteger capital."
            )

    # 2. Anti-Topo / Sobrecompra (RSI 14)
    rsi = _finite_float(features_row.get("rsi_14"))
    if rsi is not None and rsi > max_rsi_entry:
        reasons.append(f"RSI 14 em sobrecompra ({rsi:.1f} > {max_rsi_entry:.0f}) — risco de comprar no topo.")

    # 3. Anti-Esticamento nas Bandas de Bollinger (%B)
    bb_pos = _finite_float(features_row.get("bb_pos"))
    if bb_pos is not None and bb_pos > max_bb_pos:
        reasons.append(
            f"Preço esticado na Banda Superior de Bollinger (%B={bb_pos:.2f} > {max_bb_pos:.2f}) — aguardando recuo."
        )

    # 4. Filtro de Tendência de Baixa Forte (EMA 12/26)
    ema_ratio = _finite_float(features_row.get("ema_trend_ratio"))
    if ema_ratio is not None and ema_ratio < min_ema_trend_ratio:
        reasons.append(
            f"Tendência de baixa ativa (EMA12/26={ema_ratio*100:+.2f}% < {min_ema_trend_ratio*100:+.2f}%) — evitando faca caindo."
        )

    # 5. Filtro de Volatilidade Extrema (ATR %)
    atr_pct = _finite_float(features_row.get("atr_pct"))
    if atr_pct is not None and atr_pct > max_atr_pct:
        reasons.append(
            f"Volatilidade extrema (ATR={atr_pct*100:.2f}% > {max_atr_pct*100:.2f}%) — risco de violino no stop."
        )

    # 6. Filtro Anti-Lateralização Severa (mercado morto / chop: ADX baixo + Bollinger comprimido)
    adx = _finite_float(features_row.get("adx_14"))
    bb_w = _finite_float(features_row.get("bb_width"))
    if adx is not None and bb_w is not None:
        if adx < min_adx_for_chop and bb_w < min_bb_width_for_chop:
            reasons.append(
                f"Mercado muito lateralizado (ADX={adx*100:.1f} < {min_adx_for_chop*100:.0f} e "
                f"Bollinger={bb_w*100:.2f}%) — evitando ser estopado em ruído."
            )

    # Parse exit_time da última operação (usado no circuit breaker temporário e no cooldown)
    last_exit_elapsed: Optional[float] = None
    if last_trade:
        exit_time_str = str(last_trade.get("exit_time") or "").strip()
        if exit_time_str:
            try:
                exit_dt = datetime.strptime(exit_time_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                last_exit_elapsed = (now_dt - exit_dt).total_seconds()
            except Exception:
                last_exit_elapsed = None

    # 7. Proteção de Caixa contra Sequência de Perdas (Circuit Breaker com resfriamento automático — sem deadlock permanente)
    if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
        cb_still_active = (
            last_exit_elapsed is None
            or circuit_breaker_cooldown_seconds <= 0
            or (0 <= last_exit_elapsed < circuit_breaker_cooldown_seconds)
        )
        if cb_still_active:
            rem_note = ""
            if last_exit_elapsed is not None and circuit_breaker_cooldown_seconds > 0:
                rem_min = max(1, int((circuit_breaker_cooldown_seconds - last_exit_elapsed) / 60))
                rem_note = f" ({rem_min} min restantes de resfriamento)"
            reasons.append(
                f"Circuit breaker de caixa: {consecutive_losses} perdas seguidas — pausando entradas até estabilizar{rem_note}."
            )

    # 8. Cooldown após Stop Loss
    if last_trade and cooldown_seconds > 0 and last_exit_elapsed is not None:
        last_reason = str(last_trade.get("reason", "")).upper()
        last_pnl = _finite_float(last_trade.get("pnl_usd")) or 0.0
        if ("STOP" in last_reason or last_pnl < 0) and (0 <= last_exit_elapsed < cooldown_seconds):
            rem_min = max(1, int((cooldown_seconds - last_exit_elapsed) / 60))
            reasons.append(
                f"Cooldown pós-Stop Loss ativo ({rem_min} min restante(s) para estabilização do mercado)."
            )

    return len(reasons) == 0, reasons


def compute_safe_position_size(
    base_amount_usdt: float,
    features_row: Dict[str, Any],
    macro_snapshot: Optional[Dict[str, Any]] = None,
    consecutive_losses: int = 0,
    daily_pnl_usd: float = 0.0,
    max_daily_loss_usdt: float = 20.0,
    min_order_usdt: float = 10.0,
) -> Tuple[float, float, List[str]]:
    """
    Gerenciamento dinâmico de caixa (Position Sizing defensivo):
    Reduz automaticamente o tamanho da entrada em cenários de volatilidade fora do comum,
    lateralização, cautela macro ou após perdas recentes (anti-martingale).

    Retorna `(valor_usdt_final, fator_multiplicador, lista_de_ajustes)`.
    """
    base_f = _finite_float(base_amount_usdt)
    if base_f is None or base_f <= 0:
        base_f = max(10.0, _finite_float(min_order_usdt) or 10.0)
    min_ord = _finite_float(min_order_usdt) or 10.0
    daily_pnl_f = _finite_float(daily_pnl_usd) or 0.0
    max_loss_f = _finite_float(max_daily_loss_usdt) or 20.0

    mult = 1.0
    notes: List[str] = []

    # 1. Volatilidade elevada (ATR %)
    atr_f = _finite_float(features_row.get("atr_pct"))
    if atr_f is not None:
        if atr_f > 0.018:
            mult *= 0.45
            notes.append(f"Volatilidade muito alta (ATR {atr_f*100:.2f}% → 45%)")
        elif atr_f > 0.011:
            mult *= 0.65
            notes.append(f"Volatilidade elevada (ATR {atr_f*100:.2f}% → 65%)")

    # 2. Tendência fraca / Lateralização moderada (ADX < 18)
    adx = _finite_float(features_row.get("adx_14"))
    if adx is not None and 0 < adx < 0.18:
        mult *= 0.65
        notes.append(f"Mercado lateral/tendência fraca (ADX {adx*100:.1f} → 65%)")

    # 3. Cautela Macro (Dólar / Juros / Notícias)
    if macro_snapshot:
        regime = str(macro_snapshot.get("regime", "")).upper()
        if regime == "CAUTELA MACRO":
            mult *= 0.75
            notes.append("Cautela Macro (→ 75%)")

    # 4. Anti-Martingale após perdas recentes ou drawdown diário
    if consecutive_losses >= 2:
        mult *= 0.50
        notes.append(f"{consecutive_losses} perdas seguidas (→ 50%)")
    elif consecutive_losses == 1:
        mult *= 0.75
        notes.append("1 perda recente (→ 75%)")

    if max_loss_f > 0 and daily_pnl_f < -(max_loss_f * 0.50):
        mult *= 0.50
        notes.append(f"Drawdown diário > 50% (${daily_pnl_f:+.2f} → 50%)")

    mult = max(0.25, min(1.0, round(mult, 2)))
    final_amount = round(max(min(base_f, min_ord), base_f * mult), 2)
    return final_amount, mult, notes
