"""
Persistência SQLite e Motor Adaptativo de Lições de Erro (FyNex Quant AI)
=========================================================================
Gerencia snapshots de decisão, autópsias deduplicadas por candle e geração
de travas/penalidades adaptativas para o `entry_guard` e Dashboard.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from core.error_learning.detectors import (
    CAUSE_CATALOG,
    clean_params_dict,
    detect_risk_factors,
    diagnose_error_parameters,
)
from core.infra.sqlite import SCHEMA_REGISTRY, table_exists
from core.infra.utils import utc_now_str


class ErrorLearningStore:
    """Persistência SQLite e motor de consulta/aprendizado a partir de erros reais."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        SCHEMA_REGISTRY.ensure_initialized("error_learning", self.db_path, self._init_tables)

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
        params = clean_params_dict(
            features_row=features_row,
            macro_snapshot=macro_snapshot,
            extra={**(extra or {}), "prob_up": prob_up, "model_version": model_version},
        )
        now_str = utc_now_str(now_utc)
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
        now_str = utc_now_str(now_utc)
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
                    now_str, src_u, symbol, timeframe, ts_val,
                    int(model_version), float(prob_up or 0.0),
                    round(float(pnl_usd or 0.0), 4), round(float(pnl_pct or 0.0), 4),
                    str(outcome_reason or ""), diag["primary_cause"],
                    json.dumps(diag["causes"], ensure_ascii=False),
                    json.dumps(enriched, ensure_ascii=False),
                    diag["lesson_text"], float(diag["severity"]),
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
        try:
            with self._connect() as conn:
                if not table_exists(conn, "simulated_trades"):
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

        cur_params = clean_params_dict(
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
