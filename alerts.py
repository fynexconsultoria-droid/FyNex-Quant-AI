"""
Facade de compatibilidade para Alertas Operacionais (FyNex Quant AI)
====================================================================
A implementação modularizada reside em `core.notifications`:
- `core.notifications.telegram`: canal Telegram unificado
- `core.notifications.whatsapp`: canal WhatsApp CallMeBot
- `core.notifications.daily_summary`: despacho unificado + resumo diário
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

from core.infra.env_store import read_env_file, scrub_secrets, update_env_keys
from core.logging_setup import BASE_DIR, DEFAULT_DB_PATH
from core.notifications.daily_summary import (
    _ALERT_DEDUP_LOCK,
    _DAILY_SUMMARY_LOCK,
    _LAST_ALERT_TS,
    _LAST_DAILY_SUMMARY_SENT_DATE,
    _RATE_LIMIT_SECONDS,
    _RATE_LIMITED_LEVELS,
    build_daily_summary,
    maybe_send_scheduled_daily_summary,
    send_alert,
)
from core.notifications.telegram import (
    get_telegram_config,
    load_telegram_credentials as _load_telegram_credentials,
    save_telegram_config,
    send_telegram_message,
    send_telegram_raw as _send_telegram_internal,
)
from core.notifications.whatsapp import (
    CALLMEBOT_ACTIVATION_NUMBER,
    CALLMEBOT_ACTIVATION_TEXT,
    CALLMEBOT_API_URL,
    _WHATSAPP_LAST_SENT_MONO,
    _WHATSAPP_MIN_GAP_SEC,
    _WHATSAPP_SEND_LOCK,
    get_whatsapp_config,
    load_whatsapp_credentials as _load_whatsapp_credentials,
    normalize_whatsapp_phone,
    save_whatsapp_config,
    send_whatsapp_message,
)

ENV_PATH = os.path.join(BASE_DIR, ".env")


def _scrub_token(err: object, token: str = "") -> str:
    return scrub_secrets(err, (token,) if token else ())


def _read_env_file(env_path: Optional[str] = None) -> Dict[str, Optional[str]]:
    return read_env_file(env_path or ENV_PATH)


def _update_env_keys(updates: Dict[str, str], env_path: Optional[str] = None) -> None:
    update_env_keys(updates, env_path or ENV_PATH, sync_environ=True)


__all__ = [
    "BASE_DIR",
    "ENV_PATH",
    "DEFAULT_DB_PATH",
    "CALLMEBOT_API_URL",
    "CALLMEBOT_ACTIVATION_NUMBER",
    "CALLMEBOT_ACTIVATION_TEXT",
    "normalize_whatsapp_phone",
    "get_whatsapp_config",
    "save_whatsapp_config",
    "send_whatsapp_message",
    "get_telegram_config",
    "save_telegram_config",
    "send_telegram_message",
    "send_alert",
    "build_daily_summary",
    "maybe_send_scheduled_daily_summary",
    "_load_telegram_credentials",
    "_load_whatsapp_credentials",
    "_send_telegram_internal",
    "_scrub_token",
    "_read_env_file",
    "_update_env_keys",
    "_WHATSAPP_LAST_SENT_MONO",
    "_WHATSAPP_MIN_GAP_SEC",
    "_WHATSAPP_SEND_LOCK",
    "_ALERT_DEDUP_LOCK",
    "_DAILY_SUMMARY_LOCK",
    "_LAST_ALERT_TS",
    "_LAST_DAILY_SUMMARY_SENT_DATE",
    "_RATE_LIMITED_LEVELS",
    "_RATE_LIMIT_SECONDS",
    "time",
    "urllib",
]
