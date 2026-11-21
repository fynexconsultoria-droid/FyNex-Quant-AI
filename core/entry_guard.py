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


def evaluate_entry_safety(
    features_row: Dict[str, Any],
    macro_snapshot: Optional[Dict[str, Any]] = None,
    last_trade: Optional[Dict[str, Any]] = None,
    cooldown_seconds: int = 3600,
    max_rsi_entry: float = 70.0,
    min_ema_trend_ratio: float = -0.003,
    max_atr_pct: float = 0.025,
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, List[str]]:
    """
    Avalia se o contexto técnico, macro e de risco está seguro para abrir compra.
    Retorna `(aprovado, lista_de_motivos_de_bloqueio)`.
    """
    reasons: List[str] = []

    # 1. Proteção Macro (Dólar, Juros EUA, Inflação/Fed e Notícias)
    if macro_snapshot:
        regime = str(macro_snapshot.get("regime", "")).upper()
        macro_score = float(macro_snapshot.get("macro_score", 0.0))
        if regime == "RISK-OFF SEVERO" or macro_score <= -50.0:
            reasons.append(
                f"Regime macro adverso ({regime}, score {macro_score:+.1f}) — compras suspensas para proteger capital."
            )

    # 2. Anti-Topo / Sobrecompra (RSI 14)
    rsi = features_row.get("rsi_14")
    if rsi is not None and float(rsi) > max_rsi_entry:
        reasons.append(f"RSI 14 em sobrecompra ({float(rsi):.1f} > {max_rsi_entry:.0f}) — risco de comprar no topo.")

    # 3. Filtro de Tendência de Baixa Forte (EMA 12/26)
    ema_ratio = features_row.get("ema_trend_ratio")
    if ema_ratio is not None and float(ema_ratio) < min_ema_trend_ratio:
        reasons.append(
            f"Tendência de baixa ativa (EMA12/26={float(ema_ratio)*100:+.2f}% < {min_ema_trend_ratio*100:+.2f}%) — evitando faca caindo."
        )

    # 4. Filtro de Volatilidade Extrema (ATR %)
    atr_pct = features_row.get("atr_pct")
    if atr_pct is not None and float(atr_pct) > max_atr_pct:
        reasons.append(
            f"Volatilidade extrema (ATR={float(atr_pct)*100:.2f}% > {max_atr_pct*100:.2f}%) — risco de violino no stop."
        )

    # 5. Cooldown após Stop Loss
    if last_trade and cooldown_seconds > 0:
        last_reason = str(last_trade.get("reason", "")).upper()
        last_pnl = float(last_trade.get("pnl_usd", 0.0) or 0.0)
        exit_time_str = str(last_trade.get("exit_time") or "").strip()
        if ("STOP" in last_reason or last_pnl < 0) and exit_time_str:
            try:
                exit_dt = datetime.strptime(exit_time_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                now_dt = now_utc or datetime.now(timezone.utc)
                elapsed = (now_dt - exit_dt).total_seconds()
                if 0 <= elapsed < cooldown_seconds:
                    rem_min = max(1, int((cooldown_seconds - elapsed) / 60))
                    reasons.append(
                        f"Cooldown pós-Stop Loss ativo ({rem_min} min restante(s) para estabilização do mercado)."
                    )
            except Exception:
                pass

    return len(reasons) == 0, reasons
