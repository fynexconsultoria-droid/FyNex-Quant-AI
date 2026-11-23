"""
Sistema de Alertas Operacionais (Fase 5.6)
==========================================
Envia notificações automáticas via Telegram (quando `TELEGRAM_BOT_TOKEN` e
`TELEGRAM_CHAT_ID` estão configurados no `.env`) para eventos críticos:
- Ordem de compra ou venda executada
- Falha ao executar venda (emergência)
- Limite de risco atingido ou Kill-Switch acionado
- Inicialização e parada do robô
Se as variáveis não estiverem preenchidas, registra apenas no log sem gerar erro.
"""

import os
import re
import json
import time
import logging
import threading
import urllib.request
from typing import Dict, Optional
from dotenv import dotenv_values

logger = logging.getLogger("QuantTradingBot")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

_TOKEN_SCRUB_RE = re.compile(r"bot\d{6,15}:[A-Za-z0-9_-]{10,}")
_ALERT_DEDUP_LOCK = threading.Lock()
_LAST_ALERT_TS: Dict[tuple[str, str], float] = {}
_RATE_LIMITED_LEVELS = {"CRITICAL", "RISK_LIMIT"}
_RATE_LIMIT_SECONDS = 60.0


def _scrub_token(err: object, token: str = "") -> str:
    msg = str(err or "")
    if token:
        msg = msg.replace(token, "***")
    return _TOKEN_SCRUB_RE.sub("bot***", msg)[:300]


def _load_telegram_credentials() -> tuple[str, str]:
    env_data = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    token = (env_data.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env_data.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def send_alert(
    message: str,
    level: str = "INFO",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: float = 4.0,
) -> bool:
    """
    Envia alerta para o Telegram se configurado. Aplica deduplicação/cooldown em alertas
    repetidos de emergência, trunca mensagens acima do limite do Telegram e nunca
    propaga exceções ou expõe o token em logs.
    """
    if token is None or chat_id is None:
        cfg_token, cfg_chat = _load_telegram_credentials()
        token = token if token is not None else cfg_token
        chat_id = chat_id if chat_id is not None else cfg_chat

    if not token or not chat_id:
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

    full_text = f"[FyNex IA • {level_u}]\n{message}"
    if len(full_text) > 4000:
        full_text = full_text[:3997] + "..."

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": full_text,
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
