"""
Motor de Auto-Análise de Erros & Aprendizado Adaptativo (FyNex Quant AI)
========================================================================
Quando a IA erra uma operação (prejuízo / Stop Loss / Time Barrier negativo) ou
erra uma previsão de compra (falso positivo), este módulo:
1. Recupera o snapshot completo de TODOS os parâmetros no momento da decisão
   (indicadores técnicos reais de `core.ml.build_features`, contexto macro,
   notícias, confiança, TP/SL, horário).
2. Executa uma autópsia multi-parâmetro identificando todas as causas-raiz
   objetivas do erro e persiste em SQLite com deduplicação por candle (sem
   contagem dupla entre `TRADE_LOSS` e `PREDICTION_MISS`).
3. Retroalimenta o sistema por 3 vias reais (sem vazamento temporal e sem deadlock):
   - Hard-Example Weighting aplicado apenas no refit final de produção (`refit_full=True`)
   - Travas/Penalidades adaptativas no `entry_guard` (expiram em 14 dias ou com vitórias posteriores)
   - Diagnóstico transparente no WhatsApp/Telegram, Chat da IA e Dashboard.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("QuantTradingBot")

_EL_INIT_LOCK = threading.Lock()
_INITIALIZED_EL_DBS: Dict[str, Tuple[int, int]] = {}

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


def _finite_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utc_now_str(now_utc: Optional[datetime] = None) -> str:
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean_params_dict(
    features_row: Optional[Dict[str, Any]] = None,
    macro_snapshot: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extrai um snapshot compacto, limpo e 100% serializável em JSON de todos os parâmetros da decisão."""
    params: Dict[str, Any] = {}
    if isinstance(features_row, dict):
        for k, v in features_row.items():
            f = _finite_float(v)
            if f is not None:
                params[str(k)] = round(f, 6)
    if isinstance(macro_snapshot, dict):
        for mk in (
            "macro_score",
            "dxy_change_pct",
            "us10y_change_pct",
            "sp500_change_pct",
            "fear_greed_value",
            "news_sentiment_score",
            "inflation_fed_bias",
            "war_geopolitics_mentions",
            "confidence_adjustment",
        ):
            f = _finite_float(macro_snapshot.get(mk))
            if f is not None:
                params[mk] = round(f, 4)
        if macro_snapshot.get("regime"):
            params["macro_regime"] = str(macro_snapshot["regime"])
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            if isinstance(v, (int, float)):
                f = _finite_float(v)
                if f is not None:
                    params[str(k)] = round(f, 6)
            elif isinstance(v, str):
                params[str(k)] = v[:120]
    return params


def detect_risk_factors(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Inspeciona todos os parâmetros técnicos (alinhados exatamente às colunas de
    `core.ml.build_features`: `rsi_14`, `bb_pos`, `bb_width`, `ema_trend_ratio`,
    `adx_14`, `volume_zscore_20`, `atr_pct`, `macd_diff`, `return_5`, `return_10`),
    além do contexto macro, calibração e Stop Loss.
    """
    findings: List[Dict[str, Any]] = []
    if not isinstance(params, dict):
        return findings

    rsi = _finite_float(params.get("rsi_14"))
    bb_pos = _finite_float(params.get("bb_pos"))
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

    ema_ratio = _finite_float(params.get("ema_trend_ratio"))
    ret_10 = _finite_float(params.get("return_10"))
    if ret_10 is None:
        ret_10 = _finite_float(params.get("roc_12"))
    if (ema_ratio is not None and ema_ratio <= -0.0015) or (
        ema_ratio is not None and ema_ratio < 0.0 and ret_10 is not None and ret_10 <= -0.006
    ):
        findings.append({
            "code": "AGAINST_TREND",
            "label": CAUSE_CATALOG["AGAINST_TREND"]["label"],
            "detail": f"EMA12/26={ema_ratio * 100:+.2f}%" + (f" | Ret10={ret_10 * 100:+.2f}%" if ret_10 is not None else ""),
            "weight": 1.4,
        })

    adx = _finite_float(params.get("adx_14"))
    bb_w = _finite_float(params.get("bb_width"))
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

    vol_z = _finite_float(params.get("volume_zscore_20"))
    vol_ratio = _finite_float(params.get("vol_ratio"))
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

    atr_pct = _finite_float(params.get("atr_pct"))
    if atr_pct is not None and atr_pct > 0.016:
        findings.append({
            "code": "VOLATILITY_SPIKE",
            "label": CAUSE_CATALOG["VOLATILITY_SPIKE"]["label"],
            "detail": f"ATR elevado ({atr_pct * 100:.2f}%)",
            "weight": 1.3,
        })

    macd_diff = _finite_float(params.get("macd_diff"))
    if macd_diff is None:
        macd_diff = _finite_float(params.get("macd_hist"))
    ret_5 = _finite_float(params.get("return_5"))
    if ret_5 is None:
        ret_5 = _finite_float(params.get("roc_5"))
    if macd_diff is not None and macd_diff < 0.0 and ret_5 is not None and ret_5 <= -0.002:
        findings.append({
            "code": "NEGATIVE_MOMENTUM",
            "label": CAUSE_CATALOG["NEGATIVE_MOMENTUM"]["label"],
            "detail": f"MACD_diff={macd_diff:.5f} e Ret5={ret_5 * 100:+.2f}%",
            "weight": 1.15,
        })

    macro_score = _finite_float(params.get("macro_score"))
    dxy_chg = _finite_float(params.get("dxy_change_pct"))
    news_sent = _finite_float(params.get("news_sentiment_score"))
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

    sl_pct = _finite_float(params.get("sl_pct"))
    if sl_pct is not None and atr_pct is not None and sl_pct > 0 and atr_pct > 0 and sl_pct < (atr_pct * 1.15):
        findings.append({
            "code": "SL_TOO_TIGHT",
            "label": CAUSE_CATALOG["SL_TOO_TIGHT"]["label"],
            "detail": f"SL ({sl_pct * 100:.2f}%) < 1.15×ATR ({atr_pct * 100:.2f}%)",
            "weight": 1.35,
        })

    prob_up = _finite_float(params.get("prob_up"))
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
    """
    Executa a autópsia completa dos parâmetros de um erro real e gera
    causa primária, lista de causas, severidade (1.0–3.0) e lição legível.
    """
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


class ErrorLearningStore:
    """
    Persistência SQLite e motor de consulta/aprendizado a partir de erros reais.
    Usa cache de inicialização por arquivo para evitar DDL/locks em leituras do dashboard.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_initialized()

    def _file_identity(self) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(self.db_path)
            return (int(st.st_dev), int(st.st_ino))
        except OSError:
            return None

    def _ensure_initialized(self) -> None:
        abs_path = os.path.abspath(self.db_path)
        ident = self._file_identity()
        with _EL_INIT_LOCK:
            if ident is not None and _INITIALIZED_EL_DBS.get(abs_path) == ident:
                return
            self._init_tables()
            new_ident = self._file_identity()
            if new_ident is not None:
                _INITIALIZED_EL_DBS[abs_path] = new_ident

    @contextmanager
    def _connect(self, write_immediate: bool = False) -> Iterator[sqlite3.Connection]:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None if write_immediate else "")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000;")
            if write_immediate:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            else:
                with conn:
                    yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_timestamp INTEGER,
                    prediction_id INTEGER,
                    order_id TEXT DEFAULT '',
                    model_version INTEGER DEFAULT 1,
                    prob_up REAL DEFAULT 0.0,
                    parameters_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dec_snap_lookup
                ON decision_snapshots(symbol, timeframe, candle_timestamp DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dec_snap_order
                ON decision_snapshots(symbol, timeframe, order_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS error_postmortems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_timestamp INTEGER,
                    model_version INTEGER DEFAULT 1,
                    prob_up REAL DEFAULT 0.0,
                    pnl_usd REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    outcome_reason TEXT DEFAULT '',
                    primary_cause TEXT NOT NULL,
                    causes_json TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    lesson_text TEXT NOT NULL,
                    severity REAL DEFAULT 1.0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_err_pm_sym_tf_date
                ON error_postmortems(symbol, timeframe, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_err_pm_sym_tf_ts
                ON error_postmortems(symbol, timeframe, candle_timestamp)
                """
            )

    def save_decision_snapshot(
        self,
        symbol: str,
        timeframe: str,
        features_row: Optional[Dict[str, Any]],
        macro_snapshot: Optional[Dict[str, Any]] = None,
        candle_timestamp: Optional[int] = None,
        prediction_id: Optional[int] = None,
        order_id: str = "",
        model_version: int = 1,
        prob_up: float = 0.0,
        extra: Optional[Dict[str, Any]] = None,
        now_utc: Optional[datetime] = None,
    ) -> int:
        params = _clean_params_dict(
            features_row=features_row,
            macro_snapshot=macro_snapshot,
            extra={**(extra or {}), "prob_up": prob_up, "model_version": model_version},
        )
        now_str = _utc_now_str(now_utc)
        ord_clean = str(order_id or "")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_snapshots (
                    created_at, symbol, timeframe, candle_timestamp, prediction_id,
                    order_id, model_version, prob_up, parameters_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_str,
                    symbol,
                    timeframe,
                    int(candle_timestamp) if candle_timestamp is not None else None,
                    int(prediction_id) if prediction_id is not None else None,
                    ord_clean,
                    int(model_version),
                    float(prob_up or 0.0),
                    json.dumps(params, ensure_ascii=False),
                ),
            )
            # Poda segmentada: snapshots de previsões (order_id='') são podados por (symbol, timeframe),
            # preservando snapshots de ordens reais/abertas (order_id != '') separadamente.
            if not ord_clean:
                conn.execute(
                    """
                    DELETE FROM decision_snapshots
                    WHERE symbol = ? AND timeframe = ? AND COALESCE(order_id, '') = ''
                      AND id NOT IN (
                        SELECT id FROM decision_snapshots
                        WHERE symbol = ? AND timeframe = ? AND COALESCE(order_id, '') = ''
                        ORDER BY id DESC LIMIT 1000
                      )
                    """,
                    (symbol, timeframe, symbol, timeframe),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM decision_snapshots
                    WHERE COALESCE(order_id, '') != ''
                      AND id NOT IN (
                        SELECT id FROM decision_snapshots
                        WHERE COALESCE(order_id, '') != ''
                        ORDER BY id DESC LIMIT 500
                      )
                    """
                )
            return int(cur.lastrowid or 0)

    def update_snapshot_order_id(
        self,
        symbol: str,
        timeframe: str,
        old_order_id: str,
        new_order_id: str,
    ) -> None:
        """Atualiza `order_id` de um snapshot PENDING após reconciliação com o ID real da exchange."""
        if not old_order_id or not new_order_id or old_order_id == new_order_id:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE decision_snapshots
                SET order_id = ?
                WHERE symbol = ? AND timeframe = ? AND order_id = ?
                """,
                (str(new_order_id), symbol, timeframe, str(old_order_id)),
            )

    def get_snapshot(
        self,
        symbol: str,
        timeframe: str,
        prediction_id: Optional[int] = None,
        order_id: Optional[str] = None,
        candle_timestamp: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = None
            if order_id:
                row = conn.execute(
                    """
                    SELECT * FROM decision_snapshots
                    WHERE symbol = ? AND timeframe = ? AND order_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol, timeframe, str(order_id)),
                ).fetchone()
            if row is None and prediction_id is not None:
                row = conn.execute(
                    """
                    SELECT * FROM decision_snapshots
                    WHERE symbol = ? AND timeframe = ? AND prediction_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol, timeframe, int(prediction_id)),
                ).fetchone()
            if row is None and candle_timestamp is not None:
                row = conn.execute(
                    """
                    SELECT * FROM decision_snapshots
                    WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol, timeframe, int(candle_timestamp)),
                ).fetchone()
            if row is None:
                return None
            d = dict(row)
            try:
                d["parameters"] = json.loads(d.get("parameters_json") or "{}")
            except Exception:
                d["parameters"] = {}
            return d

    def record_error_postmortem(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        params: Dict[str, Any],
        outcome_reason: str = "",
        candle_timestamp: Optional[int] = None,
        model_version: int = 1,
        prob_up: float = 0.0,
        pnl_usd: float = 0.0,
        pnl_pct: float = 0.0,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Executa autópsia dos parâmetros do erro e persiste no SQLite com deduplicação por
        `(symbol, timeframe, candle_timestamp)` para nunca contar 2x o mesmo erro
        (`TRADE_LOSS` tem prioridade sobre `PREDICTION_MISS` no mesmo candle).
        """
        src_u = str(source).upper()
        enriched = dict(params or {})
        if prob_up and "prob_up" not in enriched:
            enriched["prob_up"] = round(float(prob_up), 6)

        diag = diagnose_error_parameters(
            enriched,
            source=src_u,
            outcome_reason=outcome_reason,
            pnl_pct=pnl_pct if src_u == "TRADE_LOSS" else None,
        )
        now_str = _utc_now_str(now_utc)
        ts_val = int(candle_timestamp) if candle_timestamp is not None else None

        with self._connect(write_immediate=True) as conn:
            if ts_val is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM error_postmortems
                    WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol, timeframe, ts_val),
                ).fetchone()
                if existing is not None:
                    ex_src = str(existing["source"]).upper()
                    if ex_src == "TRADE_LOSS" and src_u == "PREDICTION_MISS":
                        # Já existe autópsia completa do trade perdido neste candle -> não duplica
                        try:
                            ex_causes = json.loads(existing["causes_json"] or "[]")
                        except Exception:
                            ex_causes = []
                        return {
                            "id": int(existing["id"]),
                            "created_at": existing["created_at"],
                            "source": ex_src,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "candle_timestamp": ts_val,
                            "primary_cause": existing["primary_cause"],
                            "primary_label": CAUSE_CATALOG.get(existing["primary_cause"], {}).get(
                                "label", existing["primary_cause"]
                            ),
                            "causes": ex_causes,
                            "severity": float(existing["severity"] or 1.0),
                            "lesson_text": existing["lesson_text"],
                            "parameters": enriched,
                            "deduplicated": True,
                        }
                    if ex_src == "PREDICTION_MISS" and src_u == "TRADE_LOSS":
                        # Promove a autópsia existente para TRADE_LOSS com dados financeiros reais
                        conn.execute(
                            """
                            UPDATE error_postmortems
                            SET created_at = ?, source = ?, model_version = ?, prob_up = ?,
                                pnl_usd = ?, pnl_pct = ?, outcome_reason = ?, primary_cause = ?,
                                causes_json = ?, parameters_json = ?, lesson_text = ?, severity = ?
                            WHERE id = ?
                            """,
                            (
                                now_str, src_u, int(model_version), float(prob_up or 0.0),
                                round(float(pnl_usd or 0.0), 4), round(float(pnl_pct or 0.0), 4),
                                str(outcome_reason or ""), diag["primary_cause"],
                                json.dumps(diag["causes"], ensure_ascii=False),
                                json.dumps(enriched, ensure_ascii=False),
                                diag["lesson_text"], float(diag["severity"]), int(existing["id"]),
                            ),
                        )
                        return {
                            "id": int(existing["id"]),
                            "created_at": now_str,
                            "source": src_u,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "candle_timestamp": ts_val,
                            "primary_cause": diag["primary_cause"],
                            "primary_label": diag["primary_label"],
                            "causes": diag["causes"],
                            "severity": diag["severity"],
                            "lesson_text": diag["lesson_text"],
                            "parameters": enriched,
                            "upgraded": True,
                        }

            cur = conn.execute(
                """
                INSERT INTO error_postmortems (
                    created_at, source, symbol, timeframe, candle_timestamp, model_version,
                    prob_up, pnl_usd, pnl_pct, outcome_reason, primary_cause,
                    causes_json, parameters_json, lesson_text, severity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_str,
                    src_u,
                    symbol,
                    timeframe,
                    ts_val,
                    int(model_version),
                    float(prob_up or 0.0),
                    round(float(pnl_usd or 0.0), 4),
                    round(float(pnl_pct or 0.0), 4),
                    str(outcome_reason or ""),
                    diag["primary_cause"],
                    json.dumps(diag["causes"], ensure_ascii=False),
                    json.dumps(enriched, ensure_ascii=False),
                    diag["lesson_text"],
                    float(diag["severity"]),
                ),
            )
            pm_id = int(cur.lastrowid or 0)

        return {
            "id": pm_id,
            "created_at": now_str,
            "source": src_u,
            "symbol": symbol,
            "timeframe": timeframe,
            "candle_timestamp": ts_val,
            "primary_cause": diag["primary_cause"],
            "primary_label": diag["primary_label"],
            "causes": diag["causes"],
            "severity": diag["severity"],
            "lesson_text": diag["lesson_text"],
            "parameters": enriched,
        }

    def _recent_recovery_streak(
        self,
        symbol: str,
        timeframe: str,
        since_utc_str: Optional[str] = None,
        now_utc_str: Optional[str] = None,
    ) -> int:
        """
        Conta vitórias seguidas recentes ocorridas ESTRITAMENTE APÓS o último erro analisado
        (evita que vitórias antigas suprimam bloqueios de erros novos).
        """
        try:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='simulated_trades'"
                ).fetchone()
                if not exists:
                    return 0
                clauses = ["symbol = ?", "timeframe = ?"]
                params: List[Any] = [symbol, timeframe]
                if since_utc_str:
                    clauses.append("COALESCE(exit_time, entry_time) > ?")
                    params.append(since_utc_str)
                if now_utc_str:
                    clauses.append("COALESCE(exit_time, entry_time) <= ?")
                    params.append(now_utc_str)
                rows = conn.execute(
                    f"""
                    SELECT pnl_usd FROM simulated_trades
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id DESC LIMIT 5
                    """,
                    tuple(params),
                ).fetchall()
            streak = 0
            for r in rows:
                if float(r["pnl_usd"] or 0.0) > 0:
                    streak += 1
                else:
                    break
            return streak
        except Exception:
            return 0

    def get_adaptive_learning_profile(
        self,
        symbol: str,
        timeframe: str,
        lookback_days: int = 14,
        now_utc: Optional[datetime] = None,
        max_candle_ts_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Consolida as autópsias recentes (janela de 14 dias, deduplicadas por candle) e produz
        o perfil de aprendizado adaptativo ativo para `entry_guard`, `trading_bot` e treino.
        """
        now_dt = now_utc or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_str = now_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cutoff = (now_dt - timedelta(days=max(1, int(lookback_days)))).strftime("%Y-%m-%d %H:%M:%S")

        clauses = ["symbol = ?", "timeframe = ?", "created_at >= ?", "created_at <= ?"]
        params: List[Any] = [symbol, timeframe, cutoff, now_str]
        if max_candle_ts_ms is not None:
            clauses.append("(candle_timestamp IS NULL OR candle_timestamp <= ?)")
            params.append(int(max_candle_ts_ms))

        with self._connect() as conn:
            raw_rows = conn.execute(
                f"""
                SELECT id, created_at, source, candle_timestamp, prob_up, pnl_usd, pnl_pct,
                       outcome_reason, primary_cause, causes_json, lesson_text, severity
                FROM error_postmortems
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT 120
                """,
                tuple(params),
            ).fetchall()

        # Deduplicação defensiva por candle_timestamp (preferindo TRADE_LOSS)
        dedup_rows: List[sqlite3.Row] = []
        by_ts: Dict[int, sqlite3.Row] = {}
        for r in raw_rows:
            ts = r["candle_timestamp"]
            if ts is None:
                dedup_rows.append(r)
                continue
            ts_int = int(ts)
            prev = by_ts.get(ts_int)
            if prev is None:
                by_ts[ts_int] = r
            elif str(prev["source"]).upper() != "TRADE_LOSS" and str(r["source"]).upper() == "TRADE_LOSS":
                by_ts[ts_int] = r
        dedup_rows.extend(by_ts.values())
        dedup_rows.sort(key=lambda r: int(r["id"]), reverse=True)

        latest_err_at = str(dedup_rows[0]["created_at"]) if dedup_rows else None
        recovery_wins = self._recent_recovery_streak(
            symbol, timeframe, since_utc_str=latest_err_at, now_utc_str=now_str
        )
        decay_factor = 0.5 if recovery_wins >= 3 else 1.0

        cause_counts: Dict[str, int] = {}
        cause_scores: Dict[str, float] = {}
        failed_timestamps: Dict[int, float] = {}

        for r in dedup_rows:
            src_mult = 1.5 if str(r["source"]).upper() == "TRADE_LOSS" else 1.0
            sev = float(r["severity"] or 1.0)
            ts = r["candle_timestamp"]
            if ts is not None:
                ts_int = int(ts)
                boost = 2.2 if str(r["source"]).upper() == "TRADE_LOSS" else 1.7
                failed_timestamps[ts_int] = max(failed_timestamps.get(ts_int, 1.0), boost)

            try:
                causes = json.loads(r["causes_json"] or "[]")
            except Exception:
                causes = []
            seen_codes = set()
            for c in causes:
                code = str(c.get("code") or "")
                if not code or code == "UNCLASSIFIED_MARKET_NOISE" or code in seen_codes:
                    continue
                seen_codes.add(code)
                cause_counts[code] = cause_counts.get(code, 0) + 1
                cause_scores[code] = round(
                    cause_scores.get(code, 0.0) + float(c.get("weight", 1.0)) * src_mult * (sev / 1.5) * decay_factor,
                    2,
                )

        # Regras ativas: qualquer causa que se repetiu >= 2 vezes nos últimos 14 dias
        recurring_causes: Dict[str, Dict[str, Any]] = {}
        active_rules: List[Dict[str, Any]] = []
        for code, cnt in sorted(cause_counts.items(), key=lambda kv: (kv[1], cause_scores.get(kv[0], 0.0)), reverse=True):
            if cnt < 2:
                continue
            score = cause_scores.get(code, float(cnt))
            action = "BLOQUEAR_REPETICAO" if (cnt >= 3 and decay_factor >= 1.0) else "APERTAR_FILTRO"
            if code == "SL_TOO_TIGHT":
                action = "ALARGAR_STOP_ATR"
            info = {
                "code": code,
                "label": CAUSE_CATALOG.get(code, {}).get("label", code),
                "count": cnt,
                "score": round(score, 2),
                "action": action,
                "advice": CAUSE_CATALOG.get(code, {}).get("advice", ""),
            }
            recurring_causes[code] = info
            active_rules.append(info)

        sl_tight_count = cause_counts.get("SL_TOO_TIGHT", 0)
        sl_atr_boost = 1.25 if sl_tight_count >= 3 else (1.15 if sl_tight_count >= 2 else 1.0)
        overconf_count = cause_counts.get("OVERCONFIDENT_MODEL", 0)
        base_overconf_penalty = 0.03 if overconf_count >= 3 else (0.015 if overconf_count >= 2 else 0.0)
        if decay_factor < 1.0:
            base_overconf_penalty = round(base_overconf_penalty * 0.5, 4)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "lookback_days": lookback_days,
            "total_errors_analyzed": len(dedup_rows),
            "recovery_win_streak": recovery_wins,
            "decay_factor": decay_factor,
            "cause_counts": cause_counts,
            "recurring_causes": recurring_causes,
            "active_rules": active_rules,
            "sl_atr_boost": sl_atr_boost,
            "base_confidence_penalty": round(base_overconf_penalty, 4),
            "failed_timestamps": failed_timestamps,
        }

    def evaluate_setup_against_lessons(
        self,
        symbol: str,
        timeframe: str,
        features_row: Dict[str, Any],
        macro_snapshot: Optional[Dict[str, Any]] = None,
        sl_pct: Optional[float] = None,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Compara o setup candidato atual com os erros recorrentes recentes.
        Retorna:
        - `approved` (bool)
        - `block_reasons` (List[str])
        - `confidence_penalty` (float, 0.0 a 0.08)
        - `size_multiplier` (float, 0.60 a 1.00)
        - `sl_atr_boost` (float, 1.00 a 1.25)
        - `matched_lessons` (List[str])
        """
        profile = self.get_adaptive_learning_profile(symbol, timeframe, now_utc=now_utc)
        recurring = profile["recurring_causes"]
        if not recurring and profile["base_confidence_penalty"] <= 0:
            return {
                "approved": True,
                "block_reasons": [],
                "confidence_penalty": 0.0,
                "size_multiplier": 1.0,
                "sl_atr_boost": 1.0,
                "matched_lessons": [],
                "profile": profile,
            }

        cur_params = _clean_params_dict(
            features_row=features_row,
            macro_snapshot=macro_snapshot,
            extra={"sl_pct": sl_pct} if sl_pct is not None else None,
        )
        cur_factors = {f["code"]: f for f in detect_risk_factors(cur_params)}

        block_reasons: List[str] = []
        matched_lessons: List[str] = []
        conf_penalty = float(profile["base_confidence_penalty"])
        size_mult = 1.0

        matched_recurring: List[Dict[str, Any]] = []
        for code, rule in recurring.items():
            if code in ("OVERCONFIDENT_MODEL", "SL_TOO_TIGHT"):
                continue
            if code in cur_factors:
                matched_recurring.append(rule)
                matched_lessons.append(
                    f"{rule['label']} (falhou {rule['count']}x nos últimos {profile['lookback_days']}d: {cur_factors[code]['detail']})"
                )
                if rule["action"] == "BLOQUEAR_REPETICAO":
                    block_reasons.append(
                        f"Lição aprendida pela IA: '{rule['label']}' causou {rule['count']} erros recentes "
                        f"({cur_factors[code]['detail']}) — bloqueando repetição do erro."
                    )
                else:
                    conf_penalty += 0.03
                    size_mult *= 0.80

        if len(matched_recurring) >= 2 and not block_reasons:
            labels = " + ".join(r["label"] for r in matched_recurring[:2])
            block_reasons.append(
                f"Lição aprendida pela IA: múltiplos fatores de erro recorrente simultâneos ({labels})."
            )

        return {
            "approved": len(block_reasons) == 0,
            "block_reasons": block_reasons,
            "confidence_penalty": round(min(0.08, conf_penalty), 4),
            "size_multiplier": round(max(0.60, min(1.0, size_mult)), 2),
            "sl_atr_boost": float(profile["sl_atr_boost"]),
            "matched_lessons": matched_lessons,
            "profile": profile,
        }

    def get_error_learning_report(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        limit: int = 12,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Retorna relatório estruturado completo para o Dashboard e Chat da IA."""
        profile = self.get_adaptive_learning_profile(symbol, timeframe, now_utc=now_utc)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, source, symbol, timeframe, model_version,
                       prob_up, pnl_usd, pnl_pct, outcome_reason, primary_cause,
                       causes_json, lesson_text, severity
                FROM error_postmortems
                WHERE symbol = ? AND timeframe = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (symbol, timeframe, max(1, min(int(limit), 50))),
            ).fetchall()

        recent_errors: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["causes"] = json.loads(d.pop("causes_json", "[]") or "[]")
            except Exception:
                d["causes"] = []
            d["primary_label"] = CAUSE_CATALOG.get(d["primary_cause"], {}).get("label", d["primary_cause"])
            recent_errors.append(d)

        top_causes = [
            {
                "code": code,
                "label": CAUSE_CATALOG.get(code, {}).get("label", code),
                "count": cnt,
                "advice": CAUSE_CATALOG.get(code, {}).get("advice", ""),
            }
            for code, cnt in sorted(profile["cause_counts"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        ]

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "total_errors_14d": profile["total_errors_analyzed"],
            "recovery_win_streak": profile["recovery_win_streak"],
            "sl_atr_boost": profile["sl_atr_boost"],
            "base_confidence_penalty_pct": round(profile["base_confidence_penalty"] * 100.0, 1),
            "active_rules": profile["active_rules"],
            "top_causes": top_causes,
            "recent_errors": recent_errors,
        }


def compute_hard_example_multipliers(
    df_train: pd.DataFrame,
    db_path: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    max_multiplier: float = 2.5,
) -> np.ndarray:
    """
    Calcula multiplicadores de peso (`>= 1.0`, teto `max_multiplier`) para o refit final do ML:
    - Reforça candles onde a IA errou de verdade (timestamp em `error_postmortems`)
    - Reforça moderadamente amostras negativas (`Target == 0`) do histórico que exibem
      as mesmas causas-raiz recorrentes recentes (alinhadas às colunas reais de `build_features`).
    IMPORTANTE: Aplicado apenas no `final_model` de produção (`refit_full=True`), preservando
    a validação walk-forward e o holdout OOF 100% livres de vazamento de postmortems futuros.
    """
    n = len(df_train)
    if n == 0:
        return np.ones(0, dtype=float)
    mult = np.ones(n, dtype=float)
    if not db_path or not symbol or not timeframe or not os.path.exists(db_path):
        return mult

    max_ts_ms: Optional[int] = None
    if "timestamp" in df_train.columns and len(df_train) > 0:
        try:
            last_ts = df_train["timestamp"].iloc[-1]
            max_ts_ms = int(last_ts.timestamp() * 1000) if hasattr(last_ts, "timestamp") else int(last_ts)
        except Exception:
            max_ts_ms = None

    try:
        store = ErrorLearningStore(db_path)
        profile = store.get_adaptive_learning_profile(
            symbol, timeframe, lookback_days=30, max_candle_ts_ms=max_ts_ms
        )
    except Exception:
        return mult

    failed_ts = profile.get("failed_timestamps") or {}
    recurring = profile.get("recurring_causes") or {}
    if not failed_ts and not recurring:
        return mult

    target_arr = df_train["Target"].to_numpy(dtype=int) if "Target" in df_train.columns else np.zeros(n, dtype=int)

    # 1. Reforço direto nos timestamps onde houve erro real (falso positivo com Target == 0)
    if failed_ts and "timestamp" in df_train.columns:
        ts_col = df_train["timestamp"]
        for i, ts_val in enumerate(ts_col):
            if target_arr[i] != 0:
                continue
            try:
                ts_ms = int(ts_val.timestamp() * 1000) if hasattr(ts_val, "timestamp") else int(ts_val)
            except Exception:
                continue
            boost = failed_ts.get(ts_ms)
            if boost is not None and boost > 1.0:
                mult[i] = max(mult[i], float(boost))

    # 2. Generalização: reforço suave em exemplos negativos históricos com o mesmo padrão de erro recorrente
    if recurring:
        neg_mask = (target_arr == 0)
        if "OVERBOUGHT_ENTRY" in recurring and "rsi_14" in df_train.columns:
            m = neg_mask & (pd.to_numeric(df_train["rsi_14"], errors="coerce").fillna(0.0).to_numpy() >= 64.0)
            mult[m] *= 1.30
        if "AGAINST_TREND" in recurring and "ema_trend_ratio" in df_train.columns:
            m = neg_mask & (pd.to_numeric(df_train["ema_trend_ratio"], errors="coerce").fillna(0.0).to_numpy() <= -0.0015)
            mult[m] *= 1.30
        if "CHOPPY_SIDEWAYS" in recurring and "adx_14" in df_train.columns:
            adx_v = pd.to_numeric(df_train["adx_14"], errors="coerce").fillna(1.0).to_numpy()
            m = neg_mask & (adx_v >= 0.0) & (adx_v < 0.18)
            mult[m] *= 1.25
        if "LOW_VOLUME_BREAKOUT" in recurring:
            if "volume_zscore_20" in df_train.columns:
                vz = pd.to_numeric(df_train["volume_zscore_20"], errors="coerce").fillna(0.0).to_numpy()
                m = neg_mask & (vz <= -0.50)
                mult[m] *= 1.25
            elif "vol_ratio" in df_train.columns:
                vol_v = pd.to_numeric(df_train["vol_ratio"], errors="coerce").fillna(1.0).to_numpy()
                m = neg_mask & (vol_v >= 0.0) & (vol_v < 0.80)
                mult[m] *= 1.25
        if "VOLATILITY_SPIKE" in recurring and "atr_pct" in df_train.columns:
            atr_v = pd.to_numeric(df_train["atr_pct"], errors="coerce").fillna(0.0).to_numpy()
            m = neg_mask & (atr_v > 0.016)
            mult[m] *= 1.30
        if "NEGATIVE_MOMENTUM" in recurring:
            macd_col = "macd_diff" if "macd_diff" in df_train.columns else ("macd_hist" if "macd_hist" in df_train.columns else None)
            ret_col = "return_5" if "return_5" in df_train.columns else ("roc_5" if "roc_5" in df_train.columns else None)
            if macd_col and ret_col:
                mh = pd.to_numeric(df_train[macd_col], errors="coerce").fillna(0.0).to_numpy()
                r5 = pd.to_numeric(df_train[ret_col], errors="coerce").fillna(0.0).to_numpy()
                m = neg_mask & (mh < 0.0) & (r5 <= -0.002)
                mult[m] *= 1.25

    mult = np.clip(mult, 1.0, max(1.0, float(max_multiplier)))
    mean_val = float(np.mean(mult))
    if mean_val > 0 and math.isfinite(mean_val):
        mult = mult / mean_val
    return mult
