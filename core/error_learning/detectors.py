"""
Catálogo de Causas e Detecção de Fatores de Risco (FyNex Quant AI)
==================================================================
Extrai snapshots limpos de parâmetros e identifica causas-raiz objetivas
de falsos positivos ou operações perdedoras.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.infra.utils import finite_float

CAUSE_CATALOG: Dict[str, Dict[str, str]] = {
    "OVERBOUGHT_ENTRY": {
        "label": "Entrada esticada / sobrecompra",
        "advice": "Evitar comprar com RSI esticado (>64) ou colado na Banda Superior de Bollinger.",
    },
    "AGAINST_TREND": {
        "label": "Entrada contra a tendência",
        "advice": "Exigir alinhamento positivo de EMA12/26 antes de comprar.",
    },
    "CHOPPY_SIDEWAYS": {
        "label": "Mercado lateralizado (sem tendência)",
        "advice": "Evitar entradas com ADX fraco ou Bandas de Bollinger comprimidas.",
    },
    "LOW_VOLUME_BREAKOUT": {
        "label": "Volume fraco na entrada",
        "advice": "Exigir confirmação de volume acima da média para validar sinal de alta.",
    },
    "VOLATILITY_SPIKE": {
        "label": "Pico de volatilidade (ATR alto)",
        "advice": "Reduzir exposição ou aguardar estabilização quando o ATR% dispara.",
    },
    "NEGATIVE_MOMENTUM": {
        "label": "Momentum já enfraquecendo",
        "advice": "Não comprar quando histograma MACD e retorno de 5 candles já apontam queda.",
    },
    "MACRO_HEADWIND": {
        "label": "Cenário macro/notícias adverso",
        "advice": "Respeitar pressão contrária de Dólar (DXY), Juros EUA ou notícias negativas.",
    },
    "OVERCONFIDENT_MODEL": {
        "label": "Excesso de confiança do modelo",
        "advice": "Reforçar peso desses falsos positivos no próximo retreino e calibrar probabilidades.",
    },
    "SL_TOO_TIGHT": {
        "label": "Stop Loss apertado vs. ruído (ATR)",
        "advice": "Alargar ligeiramente o Stop Loss relativo ao ATR para não ser estopado pelo ruído.",
    },
    "UNCLASSIFIED_MARKET_NOISE": {
        "label": "Ruído normal de mercado / evento imprevisto",
        "advice": "Parâmetros dentro da normalidade; manter disciplina estatística e gestão de risco.",
    },
}

_MACRO_KEYS = (
    "macro_score",
    "dxy_change_pct",
    "us10y_change_pct",
    "sp500_change_pct",
    "fear_greed_value",
    "news_sentiment_score",
    "inflation_fed_bias",
    "war_geopolitics_mentions",
    "confidence_adjustment",
)


def clean_params_dict(
    features_row: Optional[Dict[str, Any]] = None,
    macro_snapshot: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extrai um snapshot compacto, limpo e 100% serializável em JSON de todos os parâmetros da decisão."""
    params: Dict[str, Any] = {}
    if isinstance(features_row, dict):
        for k, v in features_row.items():
            f = finite_float(v)
            if f is not None:
                params[str(k)] = round(f, 6)
    if isinstance(macro_snapshot, dict):
        for mk in _MACRO_KEYS:
            f = finite_float(macro_snapshot.get(mk))
            if f is not None:
                params[mk] = round(f, 4)
        if macro_snapshot.get("regime"):
            params["macro_regime"] = str(macro_snapshot["regime"])
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            if isinstance(v, (int, float)):
                f = finite_float(v)
                if f is not None:
                    params[str(k)] = round(f, 6)
            elif isinstance(v, str):
                params[str(k)] = v[:120]
    return params


def detect_risk_factors(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Inspeciona indicadores técnicos, contexto macro, calibração e Stop Loss buscando fatores de risco."""
    findings: List[Dict[str, Any]] = []
    if not isinstance(params, dict):
        return findings

    rsi = finite_float(params.get("rsi_14"))
    bb_pos = finite_float(params.get("bb_pos"))
    if (rsi is not None and rsi >= 64.0) or (bb_pos is not None and bb_pos >= 0.85):
        parts = []
        if rsi is not None and rsi >= 64.0:
            parts.append(f"RSI={rsi:.1f}")
        if bb_pos is not None and bb_pos >= 0.85:
            parts.append(f"%B={bb_pos:.2f}")
        findings.append({
            "code": "OVERBOUGHT_ENTRY",
            "label": CAUSE_CATALOG["OVERBOUGHT_ENTRY"]["label"],
            "detail": f"Entrada esticada ({', '.join(parts)})",
            "weight": 1.3 if (rsi is not None and rsi >= 67.0) else 1.0,
        })

    ema_ratio = finite_float(params.get("ema_trend_ratio"))
    ret_10 = finite_float(params.get("return_10"))
    if ret_10 is None:
        ret_10 = finite_float(params.get("roc_12"))
    if (ema_ratio is not None and ema_ratio <= -0.0015) or (
        ema_ratio is not None and ema_ratio < 0.0 and ret_10 is not None and ret_10 <= -0.006
    ):
        findings.append({
            "code": "AGAINST_TREND",
            "label": CAUSE_CATALOG["AGAINST_TREND"]["label"],
            "detail": f"EMA12/26={ema_ratio * 100:+.2f}%" + (f" | Ret10={ret_10 * 100:+.2f}%" if ret_10 is not None else ""),
            "weight": 1.4,
        })

    adx = finite_float(params.get("adx_14"))
    bb_w = finite_float(params.get("bb_width"))
    if (adx is not None and 0.0 <= adx < 0.18) or (bb_w is not None and 0.0 < bb_w < 0.009):
        parts = []
        if adx is not None and adx < 0.18:
            parts.append(f"ADX={adx * 100:.1f}")
        if bb_w is not None and bb_w < 0.009:
            parts.append(f"BBW={bb_w * 100:.2f}%")
        findings.append({
            "code": "CHOPPY_SIDEWAYS",
            "label": CAUSE_CATALOG["CHOPPY_SIDEWAYS"]["label"],
            "detail": f"Sem tendência clara ({', '.join(parts)})",
            "weight": 1.2 if (adx is not None and adx < 0.15) else 1.0,
        })

    vol_z = finite_float(params.get("volume_zscore_20"))
    vol_ratio = finite_float(params.get("vol_ratio"))
    if vol_z is not None and vol_z <= -0.50:
        findings.append({
            "code": "LOW_VOLUME_BREAKOUT",
            "label": CAUSE_CATALOG["LOW_VOLUME_BREAKOUT"]["label"],
            "detail": f"Volume abaixo da média (z-score={vol_z:+.2f})",
            "weight": 1.1,
        })
    elif vol_z is None and vol_ratio is not None and 0.0 <= vol_ratio < 0.80:
        findings.append({
            "code": "LOW_VOLUME_BREAKOUT",
            "label": CAUSE_CATALOG["LOW_VOLUME_BREAKOUT"]["label"],
            "detail": f"Volume abaixo da média ({vol_ratio:.2f}x)",
            "weight": 1.1,
        })

    atr_pct = finite_float(params.get("atr_pct"))
    if atr_pct is not None and atr_pct > 0.016:
        findings.append({
            "code": "VOLATILITY_SPIKE",
            "label": CAUSE_CATALOG["VOLATILITY_SPIKE"]["label"],
            "detail": f"ATR elevado ({atr_pct * 100:.2f}%)",
            "weight": 1.3,
        })

    macd_diff = finite_float(params.get("macd_diff"))
    if macd_diff is None:
        macd_diff = finite_float(params.get("macd_hist"))
    ret_5 = finite_float(params.get("return_5"))
    if ret_5 is None:
        ret_5 = finite_float(params.get("roc_5"))
    if macd_diff is not None and macd_diff < 0.0 and ret_5 is not None and ret_5 <= -0.002:
        findings.append({
            "code": "NEGATIVE_MOMENTUM",
            "label": CAUSE_CATALOG["NEGATIVE_MOMENTUM"]["label"],
            "detail": f"MACD_diff={macd_diff:.5f} e Ret5={ret_5 * 100:+.2f}%",
            "weight": 1.15,
        })

    macro_score = finite_float(params.get("macro_score"))
    dxy_chg = finite_float(params.get("dxy_change_pct"))
    news_sent = finite_float(params.get("news_sentiment_score"))
    if (
        (macro_score is not None and macro_score <= -15.0)
        or (dxy_chg is not None and dxy_chg >= 0.35)
        or (news_sent is not None and news_sent <= -0.25)
    ):
        parts = []
        if macro_score is not None and macro_score <= -15.0:
            parts.append(f"Macro={macro_score:+.1f}")
        if dxy_chg is not None and dxy_chg >= 0.35:
            parts.append(f"DXY={dxy_chg:+.2f}%")
        if news_sent is not None and news_sent <= -0.25:
            parts.append(f"Notícias={news_sent:+.2f}")
        findings.append({
            "code": "MACRO_HEADWIND",
            "label": CAUSE_CATALOG["MACRO_HEADWIND"]["label"],
            "detail": f"Pressão macro contrária ({', '.join(parts)})",
            "weight": 1.25,
        })

    sl_pct = finite_float(params.get("sl_pct"))
    if sl_pct is not None and atr_pct is not None and sl_pct > 0 and atr_pct > 0 and sl_pct < (atr_pct * 1.15):
        findings.append({
            "code": "SL_TOO_TIGHT",
            "label": CAUSE_CATALOG["SL_TOO_TIGHT"]["label"],
            "detail": f"SL ({sl_pct * 100:.2f}%) < 1.15×ATR ({atr_pct * 100:.2f}%)",
            "weight": 1.35,
        })

    prob_up = finite_float(params.get("prob_up"))
    if prob_up is not None and prob_up >= 0.68:
        findings.append({
            "code": "OVERCONFIDENT_MODEL",
            "label": CAUSE_CATALOG["OVERCONFIDENT_MODEL"]["label"],
            "detail": f"Alta confiança no falso positivo ({prob_up * 100:.1f}%)",
            "weight": 0.9,
        })

    findings.sort(key=lambda x: float(x.get("weight", 1.0)), reverse=True)
    return findings


def diagnose_error_parameters(
    params: Dict[str, Any],
    source: str = "TRADE_LOSS",
    outcome_reason: str = "STOP_LOSS",
    pnl_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Executa autópsia completa dos parâmetros gerando causa primária, severidade e lição."""
    causes = detect_risk_factors(params)
    if not causes:
        causes = [{
            "code": "UNCLASSIFIED_MARKET_NOISE",
            "label": CAUSE_CATALOG["UNCLASSIFIED_MARKET_NOISE"]["label"],
            "detail": "Indicadores técnicos e macro estavam dentro dos limites padrão.",
            "weight": 0.6,
        }]

    primary = causes[0]
    base_sev = 1.6 if source == "TRADE_LOSS" else 1.0
    if pnl_pct is not None and pnl_pct <= -1.2:
        base_sev += 0.5
    if len(causes) >= 3:
        base_sev += 0.4
    severity = round(min(3.0, max(0.8, base_sev)), 2)

    cause_summary = " + ".join(c["detail"] for c in causes[:3])
    advice = CAUSE_CATALOG.get(primary["code"], {}).get("advice", "")
    lesson_text = f"{cause_summary}. Lição: {advice}" if advice else cause_summary

    return {
        "primary_cause": primary["code"],
        "primary_label": primary["label"],
        "causes": causes,
        "severity": severity,
        "lesson_text": lesson_text,
        "outcome_reason": outcome_reason,
    }
