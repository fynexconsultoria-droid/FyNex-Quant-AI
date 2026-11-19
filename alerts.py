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
import json
import logging
import urllib.request
from typing import Optional
from dotenv import dotenv_values

logger = logging.getLogger("QuantTradingBot")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")


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
    timeout: float = 4.0
) -> bool:
    """
    Envia alerta para o Telegram se configurado. Nunca propaga exceção para não
    interromper o loop de trading.
    """
    if token is None or chat_id is None:
        cfg_token, cfg_chat = _load_telegram_credentials()
        token = token if token is not None else cfg_token
        chat_id = chat_id if chat_id is not None else cfg_chat

    if not token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": f"[FyNex IA • {level}]\n{message}",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.debug(f"Alerta Telegram não enviado: {e}")
        return False
