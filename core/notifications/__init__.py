"""Pacote de Notificações Operacionais (Telegram, WhatsApp CallMeBot e Resumo Diário)."""

from core.notifications.daily_summary import (
    _LAST_ALERT_TS,
    _LAST_DAILY_SUMMARY_SENT_DATE,
    build_daily_summary,
    maybe_send_scheduled_daily_summary,
    send_alert,
)
from core.notifications.telegram import (
    TELEGRAM_CHAT_ID_RE,
    TELEGRAM_TOKEN_RE,
    get_telegram_config,
    load_telegram_credentials,
    save_telegram_config,
    send_telegram_message,
    send_telegram_raw,
)
from core.notifications.whatsapp import (
    CALLMEBOT_ACTIVATION_NUMBER,
    CALLMEBOT_ACTIVATION_TEXT,
    CALLMEBOT_API_URL,
    _WHATSAPP_LAST_SENT_MONO,
    _WHATSAPP_MIN_GAP_SEC,
    get_whatsapp_config,
    load_whatsapp_credentials,
    normalize_whatsapp_phone,
    save_whatsapp_config,
    send_whatsapp_message,
)

__all__ = [
    "CALLMEBOT_API_URL",
    "CALLMEBOT_ACTIVATION_NUMBER",
    "CALLMEBOT_ACTIVATION_TEXT",
    "TELEGRAM_TOKEN_RE",
    "TELEGRAM_CHAT_ID_RE",
    "load_telegram_credentials",
    "get_telegram_config",
    "save_telegram_config",
    "send_telegram_raw",
    "send_telegram_message",
    "normalize_whatsapp_phone",
    "load_whatsapp_credentials",
    "get_whatsapp_config",
    "save_whatsapp_config",
    "send_whatsapp_message",
    "send_alert",
    "build_daily_summary",
    "maybe_send_scheduled_daily_summary",
    "_LAST_ALERT_TS",
    "_LAST_DAILY_SUMMARY_SENT_DATE",
    "_WHATSAPP_LAST_SENT_MONO",
    "_WHATSAPP_MIN_GAP_SEC",
]
