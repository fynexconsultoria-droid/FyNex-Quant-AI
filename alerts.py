"""
Sistema de Alertas Operacionais (Telegram + WhatsApp CallMeBot)
===============================================================
Envia notificações automáticas via Telegram e/ou WhatsApp (CallMeBot) quando
configurados no `.env` para eventos operacionais do FyNex Quant AI:
- Entradas executadas (Cripto Spot e Forex/XTB)
- Saídas encerradas com veredito claro (LUCRO vs PREJUÍZO) e PnL líquido
- Falhas na execução de ordens, timeouts ou limites de risco / Kill-Switch
- Resumo diário automático dos resultados do dia (Cripto + Forex/XTB)

Regras de segurança:
- Nunca propaga exceções para o loop do robô
- Nunca expõe tokens ou API keys em logs ou retornos de erro
- Valida entradas antes de gravar no `.env` (bloqueia injeção de quebras de linha)
- Deduplica alertas repetidos de erro/risco para evitar spam
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

logger = logging.getLogger("QuantTradingBot")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "trading_data.db")

CALLMEBOT_API_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_ACTIVATION_NUMBER = "+34 644 71 98 39"
CALLMEBOT_ACTIVATION_TEXT = "I allow callmebot to send me messages"

_TOKEN_SCRUB_RE = re.compile(r"bot\d{6,15}:[A-Za-z0-9_-]{10,}")
_APIKEY_SCRUB_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)
_PHONE_VALID_RE = re.compile(r"^\+[1-9]\d{9,14}$")
_APIKEY_VALID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

_ENV_WRITE_LOCK = threading.Lock()
_DAILY_SUMMARY_LOCK = threading.Lock()
_LAST_DAILY_SUMMARY_SENT_DATE: Dict[str, str] = {}
_INITIALIZED_DISPATCH_DBS: set[str] = set()
_ALERT_DEDUP_LOCK = threading.Lock()
_LAST_ALERT_TS: Dict[Tuple[str, str], float] = {}
_RATE_LIMITED_LEVELS = {"CRITICAL", "RISK_LIMIT", "TRADE_FAIL"}
_RATE_LIMIT_SECONDS = 60.0

_WHATSAPP_SEND_LOCK = threading.Lock()
_WHATSAPP_LAST_SENT_MONO: Optional[float] = None
_WHATSAPP_MIN_GAP_SEC = 1.2


def _scrub_token(err: object, token: str = "") -> str:
    msg = str(err or "")
    if token:
        msg = msg.replace(token, "***")
    msg = _TOKEN_SCRUB_RE.sub("bot***", msg)
    msg = _APIKEY_SCRUB_RE.sub(r"\1***", msg)
    return msg[:300]


def _read_env_file(env_path: Optional[str] = None) -> Dict[str, Optional[str]]:
    path = env_path or ENV_PATH
    if os.path.exists(path):
        try:
            return dotenv_values(path)
        except Exception:
            return {}
    return {}


def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _update_env_keys(updates: Dict[str, str], env_path: Optional[str] = None) -> None:
    """Atualiza chaves específicas no `.env` de forma atômica preservando o restante."""
    path = env_path or ENV_PATH
    with _ENV_WRITE_LOCK:
        lines: List[str] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

        seen = set()
        new_lines: List[str] = []
        key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
        for line in lines:
            m = key_re.match(line)
            if m and m.group(1) in updates:
                k = m.group(1)
                if k not in seen:
                    new_lines.append(f"{k}={updates[k]}")
                    seen.add(k)
            else:
                new_lines.append(line)

        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f"{k}={v}")

        _atomic_write_text(path, "\n".join(new_lines) + "\n")
        for k, v in updates.items():
            os.environ[k] = v


# -----------------------------------------------------------------------------
# Credenciais Telegram
# -----------------------------------------------------------------------------
def _load_telegram_credentials(env_path: Optional[str] = None) -> Tuple[str, str]:
    env_data = _read_env_file(env_path)
    token = (env_data.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env_data.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


# -----------------------------------------------------------------------------
# Credenciais & Configuração WhatsApp (CallMeBot)
# -----------------------------------------------------------------------------
def normalize_whatsapp_phone(phone: str) -> str:
    """
    Normaliza telefone para formato internacional E.164 exigido pelo CallMeBot (+5562986290913).
    Aceita números brasileiros com DDD (10 ou 11 dígitos, com ou sem 0 inicial) adicionando +55 automaticamente.
    """
    raw = (phone or "").strip()
    if any(ch in raw for ch in ("\r", "\n")):
        raise ValueError("Número de WhatsApp não pode conter quebras de linha.")
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        raise ValueError(
            "Número de WhatsApp inválido. Informe apenas dígitos com DDD ou código do país (ex.: 62986290913)."
        )
    if raw.startswith("+"):
        normalized = f"+{digits}"
    else:
        # Remove zero de discagem nacional se o usuário digitou 062986290913
        if digits.startswith("0") and len(digits) in (11, 12):
            digits = digits[1:]
        if len(digits) in (10, 11):
            normalized = f"+55{digits}"
        elif digits.startswith("55") and len(digits) in (12, 13):
            normalized = f"+{digits}"
        else:
            normalized = f"+{digits}"

    if not _PHONE_VALID_RE.match(normalized):
        raise ValueError(
            "Número de WhatsApp inválido. Informe com DDD ou código do país (ex.: 62986290913 ou +5562986290913)."
        )
    return normalized


def _load_whatsapp_credentials(env_path: Optional[str] = None) -> Tuple[str, str, bool, int]:
    env_data = _read_env_file(env_path)
    raw_phone = (env_data.get("WHATSAPP_PHONE") or os.environ.get("WHATSAPP_PHONE") or "").strip()
    apikey = (env_data.get("WHATSAPP_CALLMEBOT_APIKEY") or os.environ.get("WHATSAPP_CALLMEBOT_APIKEY") or "").strip()
    enabled_raw = (
        env_data.get("WHATSAPP_ENABLED")
        if env_data.get("WHATSAPP_ENABLED") is not None
        else os.environ.get("WHATSAPP_ENABLED", "true")
    )
    enabled = str(enabled_raw or "true").strip().lower() not in ("0", "false", "no", "off")

    hour_raw = (
        env_data.get("WHATSAPP_DAILY_REPORT_HOUR")
        if env_data.get("WHATSAPP_DAILY_REPORT_HOUR") is not None
        else os.environ.get("WHATSAPP_DAILY_REPORT_HOUR", "21")
    )
    try:
        daily_hour = max(0, min(23, int(str(hour_raw).strip())))
    except Exception:
        daily_hour = 21

    try:
        phone = normalize_whatsapp_phone(raw_phone) if raw_phone else ""
    except ValueError:
        phone = ""

    return phone, apikey, enabled, daily_hour


def get_whatsapp_config(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Retorna configuração atual do WhatsApp CallMeBot com a API Key mascarada."""
    phone, apikey, enabled, daily_hour = _load_whatsapp_credentials(env_path=env_path)
    masked = ""
    if apikey:
        masked = f"{apikey[:2]}***{apikey[-2:]}" if len(apikey) > 5 else "****"
    return {
        "provider": "callmebot",
        "configured": bool(phone and apikey),
        "enabled": bool(enabled),
        "active": bool(phone and apikey and enabled),
        "phone": phone,
        "apikey_masked": masked,
        "daily_report_hour": daily_hour,
        "activation_number": CALLMEBOT_ACTIVATION_NUMBER,
        "activation_text": CALLMEBOT_ACTIVATION_TEXT,
    }


def save_whatsapp_config(
    phone: str = "",
    apikey: str = "",
    enabled: bool = True,
    daily_report_hour: int = 21,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Valida e salva credenciais do WhatsApp CallMeBot no `.env` de forma atômica."""
    phone_clean = (phone or "").strip()
    key_clean = (apikey or "").strip()

    if any(ch in phone_clean for ch in ("\r", "\n")) or any(ch in key_clean for ch in ("\r", "\n")):
        raise ValueError("Configurações do WhatsApp não podem conter quebras de linha.")

    updates: Dict[str, str] = {}
    if phone_clean:
        updates["WHATSAPP_PHONE"] = normalize_whatsapp_phone(phone_clean)

    if key_clean and "***" not in key_clean and not key_clean.startswith(("*", "•")):
        if not _APIKEY_VALID_RE.match(key_clean):
            raise ValueError(
                "API Key do CallMeBot inválida. Informe exatamente o código recebido no WhatsApp (apenas letras/números)."
            )
        updates["WHATSAPP_CALLMEBOT_APIKEY"] = key_clean

    try:
        hour_int = int(daily_report_hour)
    except Exception:
        raise ValueError("Horário do resumo diário deve ser um número inteiro entre 0 e 23.")
    if not (0 <= hour_int <= 23):
        raise ValueError("Horário do resumo diário deve estar entre 0 e 23 (horário de Brasília).")

    updates["WHATSAPP_ENABLED"] = "true" if enabled else "false"
    updates["WHATSAPP_DAILY_REPORT_HOUR"] = str(hour_int)

    _update_env_keys(updates, env_path=env_path)
    return get_whatsapp_config(env_path=env_path)


def send_whatsapp_message(
    message: str,
    phone: Optional[str] = None,
    apikey: Optional[str] = None,
    timeout: float = 8.0,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia uma mensagem no WhatsApp usando a API gratuita do CallMeBot.
    Retorna `{"sent": bool, "error": str | None}` e nunca lança exceção.
    """
    cfg_phone, cfg_key, cfg_enabled, _ = _load_whatsapp_credentials(env_path=env_path)
    explicit_creds = (phone is not None or apikey is not None)
    try:
        target_phone = normalize_whatsapp_phone(phone) if phone is not None else cfg_phone
    except ValueError as e:
        return {"sent": False, "error": str(e)}
    target_key = (apikey if apikey is not None else cfg_key).strip()

    if not explicit_creds and not cfg_enabled:
        return {"sent": False, "error": "Alertas do WhatsApp estão desativados."}
    if not target_phone or not target_key:
        return {"sent": False, "error": "WhatsApp CallMeBot não configurado (informe telefone e API Key)."}

    text = str(message or "").strip()
    if not text:
        return {"sent": False, "error": "Mensagem vazia."}
    # CallMeBot funciona melhor abaixo de ~1.500 caracteres (URL GET)
    if len(text) > 1400:
        text = text[:1397] + "..."

    params = urllib.parse.urlencode({
        "phone": target_phone,
        "text": text,
        "apikey": target_key,
    })
    url = f"{CALLMEBOT_API_URL}?{params}"

    global _WHATSAPP_LAST_SENT_MONO
    with _WHATSAPP_SEND_LOCK:
        now_mono = time.monotonic()
        if _WHATSAPP_LAST_SENT_MONO is not None:
            gap = now_mono - _WHATSAPP_LAST_SENT_MONO
            if gap < _WHATSAPP_MIN_GAP_SEC:
                time.sleep(min(_WHATSAPP_MIN_GAP_SEC - gap, 1.5))

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "FyNex-Quant-AI/2.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read(65536).decode("utf-8", errors="ignore")
                _WHATSAPP_LAST_SENT_MONO = time.monotonic()
                if not (200 <= status < 300):
                    return {"sent": False, "error": f"HTTP {status} do CallMeBot."}
                low = raw.lower()
                if any(bad in low for bad in ("apikey is invalid", "invalid apikey", "not authorized", "error:", "too many")):
                    clean_err = re.sub(r"<[^>]+>", " ", raw)
                    clean_err = re.sub(r"\s+", " ", clean_err).strip()
                    return {"sent": False, "error": _scrub_token(clean_err, target_key)}
                return {"sent": True, "error": None}
        except urllib.error.HTTPError as e:
            return {"sent": False, "error": f"HTTP {e.code}: {_scrub_token(e.reason, target_key)}"}
        except Exception as e:
            err_msg = _scrub_token(e, target_key)
            logger.debug(f"Alerta WhatsApp CallMeBot não enviado: {err_msg}")
            return {"sent": False, "error": err_msg}


# -----------------------------------------------------------------------------
# Despacho Unificado (Telegram + WhatsApp CallMeBot)
# -----------------------------------------------------------------------------
def _send_telegram_internal(
    full_text: str,
    token: str,
    chat_id: str,
    timeout: float = 4.0,
) -> bool:
    if not token or not chat_id:
        return False
    text = full_text if len(full_text) <= 4000 else (full_text[:3997] + "...")
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                return False
            raw = resp.read(65536)
            if not raw:
                return True
            try:
                body = json.loads(raw.decode("utf-8", errors="ignore"))
                return bool(body.get("ok", True))
            except Exception:
                return True
    except Exception as e:
        logger.debug(f"Alerta Telegram não enviado: {_scrub_token(e, token)}")
        return False


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
    """
    Envia alerta para os canais configurados (Telegram e/ou WhatsApp CallMeBot).
    Aplica deduplicação/cooldown em alertas repetidos de falha/emergência, trunca
    mensagens longas e nunca propaga exceções ou expõe credenciais em logs.
    """
    if token is None or chat_id is None:
        cfg_token, cfg_chat = _load_telegram_credentials()
        token = token if token is not None else cfg_token
        chat_id = chat_id if chat_id is not None else cfg_chat

    wa_phone, wa_key, wa_enabled, _ = _load_whatsapp_credentials()
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
        now_mono = time.monotonic()
        with _ALERT_DEDUP_LOCK:
            last_ts = _LAST_ALERT_TS.get(dedup_key)
            if last_ts is not None and (now_mono - last_ts) < _RATE_LIMIT_SECONDS:
                return False
            _LAST_ALERT_TS[dedup_key] = now_mono
            if len(_LAST_ALERT_TS) > 128:
                cutoff = now_mono - _RATE_LIMIT_SECONDS * 2
                for k in [k for k, v in _LAST_ALERT_TS.items() if v < cutoff]:
                    _LAST_ALERT_TS.pop(k, None)

    level_labels = {
        "TRADE_ENTRY": "NOVA ENTRADA",
        "TRADE_EXIT": "OPERAÇÃO ENCERRADA",
        "TRADE_FAIL": "FALHA NA OPERAÇÃO",
        "DAILY_SUMMARY": "RESUMO DO DIA",
        "RISK_LIMIT": "LIMITE DE RISCO",
        "CRITICAL": "ALERTA CRÍTICO",
    }
    header_label = level_labels.get(level_u, level_u)
    full_text = f"[FyNex IA • {header_label}]\n{message}"

    tg_ok = _send_telegram_internal(full_text, token, chat_id, timeout=timeout) if tg_ready else False
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


# -----------------------------------------------------------------------------
# Resumo Diário de Resultados (Cripto + Forex/XTB)
# -----------------------------------------------------------------------------
def _get_brt_tz() -> Any:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        return timezone(timedelta(hours=-3))


def build_daily_summary(
    db_path: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Consolida todas as operações fechadas hoje (horário de Brasília) em Cripto (Binance)
    e Forex/XTB, além das posições abertas, e monta a mensagem pronta para WhatsApp/Telegram.
    """
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

    if os.path.exists(path):
        conn = sqlite3.connect(path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
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
        finally:
            conn.close()

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

    # Lista resumida das últimas 4 operações do dia
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


def maybe_send_scheduled_daily_summary(
    db_path: Optional[str] = None,
    now_utc: Optional[datetime] = None,
    env_path: Optional[str] = None,
) -> bool:
    """
    Dispara o resumo diário automaticamente uma única vez por dia assim que o horário
    de Brasília atingir `WHATSAPP_DAILY_REPORT_HOUR` (padrão 21h).
    Persiste o controle em `alert_dispatch_state` no SQLite (com lock e fallback em memória)
    para resistir a reinícios e nunca duplicar o envio em caso de lock temporário.
    """
    wa_phone, wa_key, wa_enabled, daily_hour = _load_whatsapp_credentials(env_path=env_path)
    wa_active = bool(wa_phone and wa_key and wa_enabled)
    try:
        tg_token, tg_chat = _load_telegram_credentials(env_path=env_path)
    except TypeError:
        tg_token, tg_chat = _load_telegram_credentials()
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
            conn = sqlite3.connect(path, timeout=10.0)
            try:
                if path not in _INITIALIZED_DISPATCH_DBS:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS alert_dispatch_state (
                            key TEXT PRIMARY KEY,
                            last_value TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    conn.commit()
                    _INITIALIZED_DISPATCH_DBS.add(path)
                row = conn.execute(
                    "SELECT last_value FROM alert_dispatch_state WHERE key = 'daily_summary_date'"
                ).fetchone()
                if row and row[0] == today_key:
                    _LAST_DAILY_SUMMARY_SENT_DATE[path] = today_key
                    return False
            finally:
                conn.close()
        except Exception:
            return False

        summary = build_daily_summary(db_path=path, now_utc=now_dt_utc)
        sent = send_alert(
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
                conn = sqlite3.connect(path, timeout=10.0)
                try:
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
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
        return sent
