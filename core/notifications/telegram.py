"""
Canal Telegram Unificado (FyNex Quant AI)
=========================================
Fonte única de configuração, validação, mascaramento e envio de mensagens
via Telegram Bot API (usado tanto por `alerts` quanto pelo `xtb_copilot`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from core.infra.env_store import mask_secret, read_env_file, scrub_secrets, update_env_keys
from core.logging_setup import BASE_DIR

logger = logging.getLogger("QuantTradingBot")

DEFAULT_ENV_PATH = os.path.join(BASE_DIR, ".env")
TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,64}$")
TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d{5,20}$")


def _resolve_env_path(env_path: Optional[str] = None) -> str:
    if env_path:
        return env_path
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "ENV_PATH"):
        return str(al.ENV_PATH)
    return DEFAULT_ENV_PATH


def _urlopen():
    al = sys.modules.get("alerts")
    if al is not None and hasattr(al, "urllib"):
        return al.urllib.request.urlopen
    return urllib.request.urlopen


def load_telegram_credentials(
    env_path: Optional[str] = None,
    *,
    prefer_environ: bool = False,
) -> Tuple[str, str]:
    """Lê `(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)` do `.env` / variáveis de ambiente."""
    path = _resolve_env_path(env_path)
    env_data = read_env_file(path)
    if prefer_environ:
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or env_data.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.getenv("TELEGRAM_CHAT_ID") or env_data.get("TELEGRAM_CHAT_ID") or "").strip()
    else:
        token = (env_data.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (env_data.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def get_telegram_config(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Retorna status e configuração do Telegram com token mascarado."""
    token, chat_id = load_telegram_credentials(env_path=env_path, prefer_environ=True)
    return {
        "configured": bool(token and chat_id),
        "bot_token_masked": mask_secret(token, head=6, tail=4) if token else "",
        "chat_id": chat_id,
    }


def save_telegram_config(bot_token: str, chat_id: str, env_path: Optional[str] = None) -> Dict[str, Any]:
    """Valida e salva `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` atomicamente no `.env`."""
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()

    if any(ch in token for ch in ("\r", "\n")) or any(ch in cid for ch in ("\r", "\n")):
        raise ValueError("Credenciais do Telegram não podem conter quebras de linha.")

    updates: Dict[str, str] = {}
    if token and "..." not in token and not token.startswith(("*", "•")):
        if not TELEGRAM_TOKEN_RE.match(token):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN inválido. Use o formato oficial do BotFather (ex.: 123456789:AA...)."
            )
        updates["TELEGRAM_BOT_TOKEN"] = token

    if cid:
        if not TELEGRAM_CHAT_ID_RE.match(cid):
            raise ValueError("TELEGRAM_CHAT_ID inválido. Informe apenas o ID numérico do chat/grupo.")
        updates["TELEGRAM_CHAT_ID"] = cid

    path = _resolve_env_path(env_path)
    if updates:
        update_env_keys(updates, path, sync_environ=True)

    return get_telegram_config(env_path=path)


def send_telegram_raw(
    full_text: str,
    token: str,
    chat_id: str,
    timeout: float = 4.0,
) -> bool:
    """Envia mensagem texto simples via Telegram Bot API; nunca propaga exceções."""
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
        with _urlopen()(req, timeout=timeout) as resp:
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
        logger.debug(f"Alerta Telegram não enviado: {scrub_secrets(e, (token,))}")
        return False


def send_telegram_message(
    text: str,
    timeout: float = 8.0,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Envia mensagem via Telegram retornando `{"sent": bool, "error": str | None}`."""
    token, chat_id = load_telegram_credentials(env_path=env_path, prefer_environ=True)
    if not token or not chat_id:
        return {"sent": False, "error": "Telegram não configurado (defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)."}
    if not TELEGRAM_TOKEN_RE.match(token) or not TELEGRAM_CHAT_ID_RE.match(chat_id):
        return {"sent": False, "error": "Configuração de Telegram inválida."}

    safe_text = str(text or "")
    if len(safe_text) > 4000:
        safe_text = safe_text[:3997] + "..."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": safe_text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(65536)
            body = json.loads(raw_bytes.decode("utf-8", errors="ignore") or "{}")
            ok = bool(body.get("ok"))
            return {
                "sent": ok,
                "error": None if ok else scrub_secrets(body.get("description") or "Erro Telegram", (token,)),
            }
    except urllib.error.HTTPError as e:
        return {"sent": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"sent": False, "error": scrub_secrets(e, (token,))}


def send_telegram_approval_prompt(
    text: str,
    order_id: int,
    timeout: float = 8.0,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Envia pedido de aprovação de ordem na Conta Real com botões inline no Telegram."""
    token, chat_id = load_telegram_credentials(env_path=env_path, prefer_environ=True)
    if not token or not chat_id:
        return {"sent": False, "error": "Telegram não configurado."}
    if not TELEGRAM_TOKEN_RE.match(token) or not TELEGRAM_CHAT_ID_RE.match(chat_id):
        return {"sent": False, "error": "Configuração de Telegram inválida."}

    safe_text = str(text or "")
    if len(safe_text) > 4000:
        safe_text = safe_text[:3997] + "..."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": safe_text,
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": f"✅ Aprovar #{int(order_id)}", "callback_data": f"approve:{int(order_id)}"},
                    {"text": f"❌ Rejeitar #{int(order_id)}", "callback_data": f"reject:{int(order_id)}"},
                ]
            ]
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen()(req, timeout=timeout) as resp:
            raw_bytes = resp.read(65536)
            body = json.loads(raw_bytes.decode("utf-8", errors="ignore") or "{}")
            ok = bool(body.get("ok"))
            return {
                "sent": ok,
                "error": None if ok else scrub_secrets(body.get("description") or "Erro Telegram", (token,)),
            }
    except urllib.error.HTTPError as e:
        return {"sent": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"sent": False, "error": scrub_secrets(e, (token,))}


def fetch_telegram_updates(
    offset: int = 0,
    timeout: float = 5.0,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Busca atualizações pendentes (`getUpdates`) apenas quando Telegram está configurado."""
    token, chat_id = load_telegram_credentials(env_path=env_path, prefer_environ=True)
    if not token or not chat_id:
        return {"ok": False, "updates": [], "chat_id": "", "error": "Telegram não configurado."}
    if not TELEGRAM_TOKEN_RE.match(token) or not TELEGRAM_CHAT_ID_RE.match(chat_id):
        return {"ok": False, "updates": [], "chat_id": "", "error": "Configuração de Telegram inválida."}

    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=25"
    if offset > 0:
        url += f"&offset={int(offset)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with _urlopen()(req, timeout=timeout) as resp:
            raw_bytes = resp.read(262144)
            body = json.loads(raw_bytes.decode("utf-8", errors="ignore") or "{}")
            if not body.get("ok"):
                return {"ok": False, "updates": [], "chat_id": chat_id, "error": "Erro getUpdates Telegram."}
            return {"ok": True, "updates": list(body.get("result") or []), "chat_id": chat_id, "error": None}
    except Exception as e:
        return {"ok": False, "updates": [], "chat_id": chat_id, "error": scrub_secrets(e, (token,))}


def answer_telegram_callback(
    callback_query_id: str,
    text: str = "",
    timeout: float = 4.0,
    env_path: Optional[str] = None,
) -> bool:
    token, _ = load_telegram_credentials(env_path=env_path, prefer_environ=True)
    if not token or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = json.dumps({"callback_query_id": callback_query_id, "text": str(text or "")[:180]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _urlopen()(req, timeout=timeout):
            return True
    except Exception:
        return False
