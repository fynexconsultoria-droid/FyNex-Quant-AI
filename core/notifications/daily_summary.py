"""
Despacho Unificado de Alertas e Resumo Diário (FyNex Quant AI)
==============================================================
Combina Telegram + WhatsApp CallMeBot com deduplicação de alertas repetidos,
consolidação diária de trades (Cripto + Forex/XTB) e controle persistente em SQLite.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core.infra.sqlite import SCHEMA_REGISTRY, sqlite_connection, sqlite_readonly_optional
from core.logging_setup import DEFAULT_DB_PATH
from core.notifications.telegram import load_telegram_credentials, send_telegram_raw
from core.notifications.whatsapp import (
    load_whatsapp_credentials,
    normalize_whatsapp_phone,
    send_whatsapp_message,
)

_DAILY_SUMMARY_LOCK = threading.Lock()
_LAST_DAILY_SUMMARY_SENT_DATE: Dict[str, str] = {}
_ALERT_DEDUP_LOCK = threading.Lock()
_LAST_ALERT_TS: Dict[Tuple[str, str], float] = {}
_RATE_LIMITED_LEVELS = {"CRITICAL", "RISK_LIMIT", "TRADE_FAIL"}
_RATE_LIMIT_SECONDS = 60.0

_LEVEL_LABELS = {
    "LIVE_APPROVAL": "APROVAÇÃO CONTA REAL",
    "LIVE_EXECUTED": "ORDEM REAL EXECUTADA",
    "TRADE_ENTRY": "NOVA ENTRADA",
    "TRADE_EXIT": "OPERAÇÃO ENCERRADA",
    "TRADE_FAIL": "FALHA NA OPERAÇÃO",
    "DAILY_SUMMARY": "RESUMO DO DIA",
    "RISK_LIMIT": "LIMITE DE RISCO",
    "CRITICAL": "ALERTA CRÍTICO",
}


def _get_time_mod():
    al = sys.modules.get("alerts")
    return getattr(al, "time", time) if al is not None else time


def _resolve_load_telegram_creds():
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "_load_telegram_credentials"):
        return al._load_telegram_credentials
    return load_telegram_credentials


def _resolve_send_alert():
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "send_alert"):
        return al.send_alert
    return send_alert


def _get_brt_tz() -> Any:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        return timezone(timedelta(hours=-3))


def send_alert(
    message: str,
    level: str = "INFO",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: float = 4.0,
    whatsapp_phone: Optional[str] = None,
    whatsapp_apikey: Optional[str] = None,
    send_whatsapp: bool = True,
) -> bool:
    """Envia alerta para os canais configurados (Telegram e/ou WhatsApp) com deduplicação."""
    if token is None or chat_id is None:
        loader = _resolve_load_telegram_creds()
        cfg_token, cfg_chat = loader()
        token = token if token is not None else cfg_token
        chat_id = chat_id if chat_id is not None else cfg_chat

    wa_phone, wa_key, wa_enabled, _ = load_whatsapp_credentials()
    if whatsapp_phone is not None:
        try:
            wa_phone = normalize_whatsapp_phone(whatsapp_phone)
        except ValueError:
            wa_phone = ""
    if whatsapp_apikey is not None:
        wa_key = whatsapp_apikey.strip()
        wa_enabled = True

    tg_ready = bool(token and chat_id)
    wa_ready = bool(send_whatsapp and wa_enabled and wa_phone and wa_key)
    if not tg_ready and not wa_ready:
        return False

    level_u = (level or "INFO").upper().strip()
    if level_u in _RATE_LIMITED_LEVELS:
        dedup_key = (level_u, str(message or "")[:80])
        now_mono = _get_time_mod().monotonic()
        with _ALERT_DEDUP_LOCK:
            last_ts = _LAST_ALERT_TS.get(dedup_key)
            if last_ts is not None and (now_mono - last_ts) < _RATE_LIMIT_SECONDS:
                return False
            _LAST_ALERT_TS[dedup_key] = now_mono
            if len(_LAST_ALERT_TS) > 128:
                cutoff = now_mono - _RATE_LIMIT_SECONDS * 2
                for k in [k for k, v in _LAST_ALERT_TS.items() if v < cutoff]:
                    _LAST_ALERT_TS.pop(k, None)

    header_label = _LEVEL_LABELS.get(level_u, level_u)
    full_text = f"[FyNex IA • {header_label}]\n{message}"

    tg_ok = send_telegram_raw(full_text, token, chat_id, timeout=timeout) if tg_ready else False
    wa_ok = False
    if wa_ready:
        wa_res = send_whatsapp_message(
            full_text,
            phone=wa_phone,
            apikey=wa_key,
            timeout=max(timeout, 6.0),
        )
        wa_ok = bool(wa_res.get("sent"))

    return bool(tg_ok or wa_ok)


def build_daily_summary(
    db_path: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Consolida operações fechadas hoje (horário de Brasília) em Cripto e Forex/XTB."""
    path = db_path or DEFAULT_DB_PATH
    brt_tz = _get_brt_tz()
    now_dt_utc = now_utc or datetime.now(timezone.utc)
    if now_dt_utc.tzinfo is None:
        now_dt_utc = now_dt_utc.replace(tzinfo=timezone.utc)
    now_brt = now_dt_utc.astimezone(brt_tz)
    date_key = now_brt.strftime("%Y-%m-%d")
    date_label = now_brt.strftime("%d/%m/%Y")

    day_start_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_brt = day_start_brt + timedelta(days=1)
    start_utc_str = day_start_brt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc_str = day_end_brt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    crypto_trades: List[Dict[str, Any]] = []
    xtb_trades: List[Dict[str, Any]] = []
    open_crypto = 0
    open_xtb = 0

    with sqlite_readonly_optional(path, timeout=10.0) as conn:
        if conn is not None:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "simulated_trades" in tables:
                rows = conn.execute(
                    """
                    SELECT symbol, mode, reason, pnl_usd, pnl_pct, entry_price, exit_price, exit_time
                    FROM simulated_trades
                    WHERE COALESCE(exit_time, entry_time) >= ? AND COALESCE(exit_time, entry_time) < ?
                    ORDER BY id DESC
                    """,
                    (start_utc_str, end_utc_str),
                ).fetchall()
                crypto_trades = [dict(r) for r in rows]
            if "xtb_trades" in tables:
                rows = conn.execute(
                    """
                    SELECT symbol, side, reason, pnl_usd, pnl_pct, entry_price, exit_price, closed_at
                    FROM xtb_trades
                    WHERE closed_at >= ? AND closed_at < ?
                    ORDER BY id DESC
                    """,
                    (start_utc_str, end_utc_str),
                ).fetchall()
                xtb_trades = [dict(r) for r in rows]
            if "open_positions" in tables:
                row = conn.execute("SELECT COUNT(*) AS n FROM open_positions").fetchone()
                open_crypto = int(row["n"] or 0) if row else 0
            if "xtb_positions" in tables:
                row = conn.execute("SELECT COUNT(*) AS n FROM xtb_positions").fetchone()
                open_xtb = int(row["n"] or 0) if row else 0

    def _stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(items)
        wins = sum(1 for t in items if float(t.get("pnl_usd") or 0.0) > 0)
        losses = sum(1 for t in items if float(t.get("pnl_usd") or 0.0) < 0)
        pnl_usd = round(sum(float(t.get("pnl_usd") or 0.0) for t in items), 2)
        wr = round((wins / total * 100.0), 1) if total > 0 else 0.0
        return {"total": total, "wins": wins, "losses": losses, "pnl_usd": pnl_usd, "win_rate": wr}

    c_st = _stats(crypto_trades)
    x_st = _stats(xtb_trades)
    total_ops = c_st["total"] + x_st["total"]
    total_wins = c_st["wins"] + x_st["wins"]
    total_losses = c_st["losses"] + x_st["losses"]
    total_pnl = round(c_st["pnl_usd"] + x_st["pnl_usd"], 2)
    total_wr = round((total_wins / total_ops * 100.0), 1) if total_ops > 0 else 0.0

    if total_ops == 0:
        verdict = "⚪ SEM OPERAÇÕES ENCERRADAS HOJE"
    elif total_pnl > 0:
        verdict = f"✅ DIA NO LUCRO (${total_pnl:+.2f})"
    elif total_pnl < 0:
        verdict = f"🔻 DIA NO PREJUÍZO (${total_pnl:+.2f})"
    else:
        verdict = "⚖️ DIA NO ZERO A ZERO ($0.00)"

    lines = [
        f"📊 RESUMO DO DIA — {date_label} (Brasília)",
        f"{verdict}",
        "",
        f"• Total: {total_ops} op. ({total_wins} lucro / {total_losses} prejuízo | Acerto {total_wr:.1f}%)",
        f"• Cripto (Binance): {c_st['total']} op. | {c_st['wins']}V/{c_st['losses']}D | PnL ${c_st['pnl_usd']:+.2f}",
        f"• Forex/XTB: {x_st['total']} op. | {x_st['wins']}V/{x_st['losses']}D | PnL ${x_st['pnl_usd']:+.2f}",
        f"• Posições abertas agora: {open_crypto} Cripto | {open_xtb} Forex/XTB",
    ]

    combined: List[str] = []
    for t in crypto_trades[:3]:
        p = float(t.get("pnl_usd") or 0.0)
        icon = "✅" if p > 0 else "❌"
        combined.append(f"  {icon} {t['symbol']} ({t.get('reason', '-')}) ${p:+.2f}")
    for t in xtb_trades[:3]:
        p = float(t.get("pnl_usd") or 0.0)
        icon = "✅" if p > 0 else "❌"
        combined.append(f"  {icon} {t['symbol']} {t.get('side', '')} ({t.get('reason', '-')}) ${p:+.2f}")
    if combined:
        lines.append("")
        lines.append("Últimas operações de hoje:")
        lines.extend(combined[:5])

    return {
        "date": date_key,
        "date_label": date_label,
        "total_ops": total_ops,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": total_wr,
        "total_pnl_usd": total_pnl,
        "crypto": c_st,
        "xtb": x_st,
        "open_crypto": open_crypto,
        "open_xtb": open_xtb,
        "message": "\n".join(lines),
    }


def _init_dispatch_table(path: str) -> None:
    with sqlite_connection(path, timeout=10.0) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_dispatch_state (
                key TEXT PRIMARY KEY,
                last_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def maybe_send_scheduled_daily_summary(
    db_path: Optional[str] = None,
    now_utc: Optional[datetime] = None,
    env_path: Optional[str] = None,
) -> bool:
    """Dispara o resumo diário uma única vez por dia após `WHATSAPP_DAILY_REPORT_HOUR` (Brasília)."""
    wa_phone, wa_key, wa_enabled, daily_hour = load_whatsapp_credentials(env_path=env_path)
    wa_active = bool(wa_phone and wa_key and wa_enabled)
    loader = _resolve_load_telegram_creds()
    try:
        tg_token, tg_chat = loader(env_path=env_path)
    except TypeError:
        tg_token, tg_chat = loader()
    if not wa_active and not (tg_token and tg_chat):
        return False

    brt_tz = _get_brt_tz()
    now_dt_utc = now_utc or datetime.now(timezone.utc)
    if now_dt_utc.tzinfo is None:
        now_dt_utc = now_dt_utc.replace(tzinfo=timezone.utc)
    now_brt = now_dt_utc.astimezone(brt_tz)
    if now_brt.hour < int(daily_hour):
        return False

    today_key = now_brt.strftime("%Y-%m-%d")
    path = os.path.abspath(db_path or DEFAULT_DB_PATH)

    with _DAILY_SUMMARY_LOCK:
        if _LAST_DAILY_SUMMARY_SENT_DATE.get(path) == today_key:
            return False

        try:
            SCHEMA_REGISTRY.ensure_initialized("alert_dispatch", path, lambda: _init_dispatch_table(path))
            with sqlite_connection(path, timeout=10.0) as conn:
                row = conn.execute(
                    "SELECT last_value FROM alert_dispatch_state WHERE key = 'daily_summary_date'"
                ).fetchone()
                if row and row[0] == today_key:
                    _LAST_DAILY_SUMMARY_SENT_DATE[path] = today_key
                    return False
        except Exception:
            return False

        summary = build_daily_summary(db_path=path, now_utc=now_dt_utc)
        sent = _resolve_send_alert()(
            summary["message"],
            level="DAILY_SUMMARY",
            token=tg_token,
            chat_id=tg_chat,
            whatsapp_phone=wa_phone if wa_active else None,
            whatsapp_apikey=wa_key if wa_active else None,
            send_whatsapp=wa_active,
        )
        if sent:
            _LAST_DAILY_SUMMARY_SENT_DATE[path] = today_key
            try:
                with sqlite_connection(path, timeout=10.0) as conn:
                    conn.execute(
                        """
                        INSERT INTO alert_dispatch_state (key, last_value, updated_at)
                        VALUES ('daily_summary_date', ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            last_value = excluded.last_value,
                            updated_at = excluded.updated_at
                        """,
                        (today_key, now_dt_utc.strftime("%Y-%m-%d %H:%M:%S")),
                    )
            except Exception:
                pass
        return sent
