"""
Canal WhatsApp CallMeBot (FyNex Quant AI)
=========================================
Normalização de número E.164 (com suporte automático ao DDD brasileiro +55),
configuração atômica no `.env` e envio com rate-limiting e scrubbing de apikey.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from core.infra.env_store import read_env_file, scrub_secrets, update_env_keys
from core.logging_setup import BASE_DIR

logger = logging.getLogger("QuantTradingBot")

DEFAULT_ENV_PATH = os.path.join(BASE_DIR, ".env")
CALLMEBOT_API_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_ACTIVATION_NUMBER = "+34 644 71 98 39"
CALLMEBOT_ACTIVATION_TEXT = "I allow callmebot to send me messages"

_PHONE_VALID_RE = re.compile(r"^\+[1-9]\d{9,14}$")
_APIKEY_VALID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

_WHATSAPP_SEND_LOCK = threading.Lock()
_WHATSAPP_LAST_SENT_MONO: Optional[float] = None
_WHATSAPP_MIN_GAP_SEC = 1.2


def _resolve_env_path(env_path: Optional[str] = None) -> str:
    if env_path:
        return env_path
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "ENV_PATH"):
        return str(al.ENV_PATH)
    return DEFAULT_ENV_PATH


def _get_time_mod():
    al = sys.modules.get("alerts")
    return getattr(al, "time", time) if al is not None else time


def _get_urlopen():
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "urllib"):
        return al.urllib.request.urlopen
    return urllib.request.urlopen


def _get_last_sent() -> Optional[float]:
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "_WHATSAPP_LAST_SENT_MONO"):
        return al._WHATSAPP_LAST_SENT_MONO
    return _WHATSAPP_LAST_SENT_MONO


def _set_last_sent(val: Optional[float]) -> None:
    global _WHATSAPP_LAST_SENT_MONO
    _WHATSAPP_LAST_SENT_MONO = val
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "_WHATSAPP_LAST_SENT_MONO"):
        al._WHATSAPP_LAST_SENT_MONO = val


def normalize_whatsapp_phone(phone: str) -> str:
    """Normaliza telefone para formato internacional E.164 exigido pelo CallMeBot."""
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


def load_whatsapp_credentials(env_path: Optional[str] = None) -> Tuple[str, str, bool, int]:
    path = _resolve_env_path(env_path)
    env_data = read_env_file(path)
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
    phone, apikey, enabled, daily_hour = load_whatsapp_credentials(env_path=env_path)
    masked = (f"{apikey[:2]}***{apikey[-2:]}" if len(apikey) > 5 else "****") if apikey else ""
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

    path = _resolve_env_path(env_path)
    update_env_keys(updates, path, sync_environ=True)
    return get_whatsapp_config(env_path=path)


def send_whatsapp_message(
    message: str,
    phone: Optional[str] = None,
    apikey: Optional[str] = None,
    timeout: float = 8.0,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Envia mensagem no WhatsApp via CallMeBot; nunca lança exceção."""
    cfg_phone, cfg_key, cfg_enabled, _ = load_whatsapp_credentials(env_path=env_path)
    explicit_creds = phone is not None or apikey is not None
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
    if len(text) > 1400:
        text = text[:1397] + "..."

    params = urllib.parse.urlencode({"phone": target_phone, "text": text, "apikey": target_key})
    url = f"{CALLMEBOT_API_URL}?{params}"

    tmod = _get_time_mod()
    with _WHATSAPP_SEND_LOCK:
        now_mono = tmod.monotonic()
        last_sent = _get_last_sent()
        if last_sent is not None:
            gap = now_mono - last_sent
            if gap < _WHATSAPP_MIN_GAP_SEC:
                tmod.sleep(min(_WHATSAPP_MIN_GAP_SEC - gap, 1.5))

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "FyNex-Quant-AI/2.0"},
                method="GET",
            )
            with _get_urlopen()(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read(65536).decode("utf-8", errors="ignore")
                _set_last_sent(tmod.monotonic())
                if not (200 <= status < 300):
                    return {"sent": False, "error": f"HTTP {status} do CallMeBot."}
                low = raw.lower()
                if any(bad in low for bad in ("apikey is invalid", "invalid apikey", "not authorized", "error:", "too many")):
                    clean_err = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
                    return {"sent": False, "error": scrub_secrets(clean_err, (target_key,))}
                return {"sent": True, "error": None}
        except urllib.error.HTTPError as e:
            return {"sent": False, "error": f"HTTP {e.code}: {scrub_secrets(e.reason, (target_key,))}"}
        except Exception as e:
            err_msg = scrub_secrets(e, (target_key,))
            logger.debug(f"Alerta WhatsApp CallMeBot não enviado: {err_msg}")
            return {"sent": False, "error": err_msg}
