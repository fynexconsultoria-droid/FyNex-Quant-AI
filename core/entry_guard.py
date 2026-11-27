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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.infra.utils import finite_float as _finite_float, parse_utc_str


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
    learned_guard_result: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """
    Avalia se o contexto técnico, macro e de risco está seguro para abrir compra.
    Retorna `(aprovado, lista_de_motivos_de_bloqueio)`.
    """
    reasons: List[str] = []
    now_dt = now_utc or datetime.now(timezone.utc)

    # 1. Proteção Macro & Bolsas Globais (Dólar, Juros EUA, S&P 500, Nasdaq, VIX, Inflação/Fed e Notícias)
    if macro_snapshot:
        regime = str(macro_snapshot.get("regime", "")).upper()
        macro_score = _finite_float(macro_snapshot.get("macro_score")) or 0.0
        if regime == "RISK-OFF SEVERO" or macro_score <= -45.0:
            reasons.append(
                f"Regime macro adverso ({regime}, score {macro_score:+.1f}) — compras suspensas para proteger capital."
            )
        vix_val = _finite_float(macro_snapshot.get("vix_price")) or 0.0
        if vix_val >= 32.0:
            reasons.append(
                f"Pânico nas bolsas de valores (VIX={vix_val:.1f} >= 32) — compras bloqueadas por proteção."
            )
        sp_chg = _finite_float(macro_snapshot.get("sp500_change_pct")) or 0.0
        ndx_chg = _finite_float(macro_snapshot.get("nasdaq_change_pct")) or 0.0
        if sp_chg <= -2.5 or (sp_chg <= -1.8 and ndx_chg <= -2.2):
            reasons.append(
                f"Forte queda em Wall Street (S&P 500 {sp_chg:+.2f}%, Nasdaq {ndx_chg:+.2f}%) — aguardando estabilização."
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
        exit_dt = parse_utc_str(str(last_trade.get("exit_time") or ""))
        if exit_dt is not None:
            last_exit_elapsed = (now_dt - exit_dt).total_seconds()

    # 7. Proteção de Caixa contra Sequência de Perdas (Circuit Breaker com resfriamento automático — sem deadlock permanente)
    if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
        cb_still_active = (
            last_exit_elapsed is None
            or circuit_breaker_cooldown_seconds <= 0
            or (last_exit_elapsed < circuit_breaker_cooldown_seconds)
        )
        if cb_still_active:
            rem_note = ""
            if last_exit_elapsed is not None and circuit_breaker_cooldown_seconds > 0:
                rem_min = max(1, int((circuit_breaker_cooldown_seconds - max(0.0, last_exit_elapsed)) / 60))
                rem_note = f" ({rem_min} min restantes de resfriamento)"
            reasons.append(
                f"Circuit breaker de caixa: {consecutive_losses} perdas seguidas — pausando entradas até estabilizar{rem_note}."
            )

    # 8. Cooldown após Stop Loss (defensivo mesmo sob ajuste retroativo de relógio)
    if last_trade and cooldown_seconds > 0 and last_exit_elapsed is not None:
        last_reason = str(last_trade.get("reason", "")).upper()
        last_pnl = _finite_float(last_trade.get("pnl_usd")) or 0.0
        if ("STOP" in last_reason or last_pnl < 0) and (last_exit_elapsed < cooldown_seconds):
            rem_min = max(1, int((cooldown_seconds - max(0.0, last_exit_elapsed)) / 60))
            reasons.append(
                f"Cooldown pós-Stop Loss ativo ({rem_min} min restante(s) para estabilização do mercado)."
            )

    # 9. Travas adaptativas aprendidas automaticamente com erros recentes da IA
    if isinstance(learned_guard_result, dict):
        for blk in (learned_guard_result.get("block_reasons") or []):
            if blk:
                reasons.append(str(blk))

    return len(reasons) == 0, reasons


def compute_safe_position_size(
    base_amount_usdt: float,
    features_row: Dict[str, Any],
    macro_snapshot: Optional[Dict[str, Any]] = None,
    consecutive_losses: int = 0,
    daily_pnl_usd: float = 0.0,
    max_daily_loss_usdt: float = 20.0,
    min_order_usdt: float = 10.0,
    learned_size_multiplier: float = 1.0,
    learned_notes: Optional[List[str]] = None,
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
    min_ord_f = _finite_float(min_order_usdt)
    min_ord = min_ord_f if min_ord_f is not None else 10.0
    daily_pnl_val = _finite_float(daily_pnl_usd)
    daily_pnl_f = daily_pnl_val if daily_pnl_val is not None else 0.0
    max_loss_val = _finite_float(max_daily_loss_usdt)
    max_loss_f = max_loss_val if max_loss_val is not None else 20.0

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
    if adx is not None and 0.0 <= adx < 0.18:
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

    # 5. Redução defensiva aprendida com erros recorrentes recentes
    l_mult = _finite_float(learned_size_multiplier)
    if l_mult is not None and 0.0 < l_mult < 0.999:
        mult *= max(0.50, min(1.0, l_mult))
        if learned_notes:
            notes.append(f"Lição de erro ({learned_notes[0]} → {int(round(l_mult * 100))}%)")
        else:
            notes.append(f"Ajuste defensivo pós-erro (→ {int(round(l_mult * 100))}%)")

    mult = max(0.25, min(1.0, round(mult, 2)))
    final_amount = round(max(min(base_f, min_ord), base_f * mult), 2)
    return final_amount, mult, notes


def evaluate_live_high_potential(
    *,
    prob_up: float,
    effective_confidence: float,
    tp_pct: float,
    sl_pct: float,
    features_row: Dict[str, Any],
    macro_snapshot: Optional[Dict[str, Any]] = None,
    pre_order_intel: Optional[Dict[str, Any]] = None,
    champion_metrics: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.72,
    min_potential_score: float = 78.0,
    min_risk_reward: float = 1.5,
) -> Tuple[bool, float, List[str], str]:
    """
    Filtro Sniper de Alto Potencial exclusivo da CONTA REAL (Dinheiro Real).
    Enquanto a Conta Demo opera continuamente para treinar e acompanhar o mercado,
    a Conta Real só aprova propostas com alta convicção estatística, tendência clara,
    boa assimetria Risco/Retorno e cenário macro/bolsas favorável.

    Retorna `(aprovado, score_0_100, motivos_bloqueio, resumo_curto)`.
    """
    reasons: List[str] = []
    prob_f = _finite_float(prob_up) or 0.0
    req_conf = max(_finite_float(effective_confidence) or 0.55, float(min_confidence))
    tp_f = _finite_float(tp_pct) or 0.0
    sl_f = _finite_float(sl_pct) or 0.0
    rr = (tp_f / sl_f) if sl_f > 0 else 0.0

    # 1. Convicção estatística mínima para dinheiro real
    if prob_f < req_conf:
        reasons.append(
            f"Confiança IA ({prob_f*100:.1f}%) abaixo do mínimo Sniper da Conta Real ({req_conf*100:.0f}%)."
        )

    # 2. Assimetria Risco/Retorno favorável (TP/SL >= 1.5x)
    if rr < float(min_risk_reward):
        reasons.append(
            f"Relação Retorno/Risco baixa ({rr:.2f}x < {min_risk_reward:.2f}x) para dinheiro real."
        )

    # 3. Tendência e força direcional (evita entradas fracas ou contra-tendência)
    ema_ratio = _finite_float(features_row.get("ema_trend_ratio"))
    if ema_ratio is not None and ema_ratio < 0.0:
        reasons.append(
            f"EMA12/26 ainda negativa ({ema_ratio*100:+.2f}%) — Conta Real exige tendência alinhada a favor."
        )

    adx = _finite_float(features_row.get("adx_14"))
    if adx is not None and adx < 0.16:
        reasons.append(
            f"Força de tendência fraca (ADX={adx*100:.1f} < 16) — reservado para treino na Conta Demo."
        )

    # 4. Zona técnica saudável (evita comprar já esticado na Conta Real)
    rsi = _finite_float(features_row.get("rsi_14"))
    if rsi is not None and rsi > 64.0:
        reasons.append(f"RSI 14 elevado ({rsi:.1f} > 64) — sem margem segura para Conta Real.")

    bb_pos = _finite_float(features_row.get("bb_pos"))
    if bb_pos is not None and bb_pos > 0.85:
        reasons.append(f"Preço próximo do topo de Bollinger (%B={bb_pos:.2f} > 0.85) — aguardando melhor ponto.")

    # 5. Cenário Macro & Bolsas Globais (S&P 500, VIX, Notícias)
    macro_score = 0.0
    vix_val = 0.0
    if isinstance(macro_snapshot, dict) and macro_snapshot:
        macro_score = _finite_float(macro_snapshot.get("macro_score")) or 0.0
        vix_val = _finite_float(macro_snapshot.get("vix_price")) or 0.0
        if macro_score < -12.0:
            reasons.append(
                f"Cenário macro desfavorável ({macro_snapshot.get('regime', 'CAUTELA')}, score {macro_score:+.1f}) para Conta Real."
            )
        if vix_val >= 26.0:
            reasons.append(f"Volatilidade global elevada nas bolsas (VIX={vix_val:.1f} >= 26) — modo defensivo na Conta Real.")

    if isinstance(pre_order_intel, dict) and pre_order_intel:
        if not pre_order_intel.get("allow_live_proposal", True):
            reasons.append("Dossiê pré-ordem (Bolsas/Notícias) vetou entrada na Conta Real.")

    # Cálculo do Score Composto de Potencial (0 a 100)
    # A) Convicção IA (até 42 pts)
    conf_pts = max(0.0, min(42.0, ((prob_f - 0.50) / 0.35) * 42.0))
    # B) Risco/Retorno TP/SL (até 20 pts)
    rr_pts = max(0.0, min(20.0, ((rr - 1.0) / 1.2) * 20.0))
    # C) Tendência & Momentum técnico (até 18 pts)
    trend_pts = 9.0
    if ema_ratio is not None:
        trend_pts = 11.0 if ema_ratio >= 0.002 else (9.0 if ema_ratio >= 0.0 else 2.0)
    if adx is not None:
        trend_pts += 7.0 if adx >= 0.22 else (5.0 if adx >= 0.16 else 1.0)
    else:
        trend_pts += 5.0
    trend_pts = max(0.0, min(18.0, trend_pts))
    # D) Contexto Macro & Bolsas (até 12 pts)
    macro_pts = max(0.0, min(12.0, 7.5 + (macro_score / 40.0) * 4.5))
    if vix_val >= 26.0:
        macro_pts = min(macro_pts, 3.0)
    # E) Qualidade do Modelo Campeão (até 8 pts)
    champ_prec = _finite_float((champion_metrics or {}).get("precision_buy"))
    if champ_prec is None:
        champ_prec = _finite_float((champion_metrics or {}).get("accuracy"))
    if champ_prec is None:
        champ_pts = 6.0
    else:
        champ_pts = max(0.0, min(8.0, ((champ_prec - 0.50) / 0.30) * 8.0))

    total_score = round(min(100.0, conf_pts + rr_pts + trend_pts + macro_pts + champ_pts), 1)
    if total_score < float(min_potential_score):
        reasons.append(
            f"Score de potencial ({total_score:.0f}/100) abaixo do mínimo da Conta Real ({min_potential_score:.0f}/100)."
        )

    approved = len(reasons) == 0
    badge = f"🎯 Potencial {total_score:.0f}/100 (R/R {rr:.2f}x • IA {prob_f*100:.1f}%)"
    return approved, total_score, reasons, badge


def compute_micro_bankroll_amount(
    *,
    free_usdt_balance: Optional[float],
    fallback_base_amount: float = 10.0,
    alloc_pct: float = 0.40,
    min_order_usdt: float = 10.0,
    max_trade_amount_usdt: float = 100.0,
    safety_reserve_usdt: float = 1.0,
) -> Tuple[float, str]:
    """
    Gestão de Micro-Banca com Juros Compostos Automáticos (Conta Real Spot):
    - Ideal para começar com bancas pequenas (ex.: R$ 150 ≈ $26-$27 USDT).
    - Sempre respeita o mínimo operacional da Binance ($10 USDT) e reserva margem de segurança.
    - Conforme o saldo real em USDT cresce com os lucros, aumenta proporcionalmente o valor
      por operação (ex.: 40% do saldo livre), limitado por `max_trade_amount_usdt`.

    Retorna `(valor_usdt_final, descricao)`. Se saldo real for menor que `min_order_usdt`,
    retorna `(0.0, motivo)` para evitar enviar ordem que falharia por MIN_NOTIONAL.
    """
    min_ord = max(5.0, _finite_float(min_order_usdt) or 10.0)
    max_amt = max(min_ord, _finite_float(max_trade_amount_usdt) or 100.0)
    pct = max(0.15, min(0.75, _finite_float(alloc_pct) or 0.40))
    reserve = max(0.0, _finite_float(safety_reserve_usdt) or 0.0)
    bal = _finite_float(free_usdt_balance)

    if bal is None or bal <= 0.0:
        fb = round(max(min_ord, min(max_amt, _finite_float(fallback_base_amount) or min_ord)), 2)
        return fb, f"Base configurada (${fb:.2f} USDT)"

    if bal < min_ord:
        return 0.0, f"Saldo USDT livre (${bal:.2f}) abaixo do mínimo Binance (${min_ord:.2f})"

    spendable = max(min_ord, bal - reserve) if bal >= (min_ord + reserve) else bal
    target = max(min_ord, bal * pct)
    final_amt = round(min(max_amt, spendable, target), 2)
    return final_amt, f"Micro-Banca {pct*100:.0f}% de ${bal:.2f} → ${final_amt:.2f} USDT"
