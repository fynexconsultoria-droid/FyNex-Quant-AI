"""
Gerenciamento Atômico de `.env` e Higienização de Segredos (FyNex Quant AI)
===========================================================================
Centraliza leitura, escrita atômica (.tmp + fsync + os.replace) sob lock global
e mascaramento/scrubbing de credenciais (Binance, Telegram e CallMeBot),
evitando corridas entre módulos que atualizam o `.env`.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from typing import Dict, List, Optional, Sequence

from dotenv import dotenv_values

ENV_WRITE_LOCK = threading.Lock()

_KEY_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_TELEGRAM_TOKEN_SCRUB_RE = re.compile(r"bot\d{6,15}:[A-Za-z0-9_-]{10,}")
_APIKEY_SCRUB_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


def atomic_write_text(path: str, content: str, prefix: str = ".env.") -> None:
    """Grava conteúdo texto de forma atômica via arquivo temporário no mesmo diretório."""
    abs_path = os.path.abspath(path)
    directory = os.path.dirname(abs_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, abs_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def read_env_file(env_path: str) -> Dict[str, Optional[str]]:
    """Lê um arquivo `.env` de forma segura retornando `{}` se ausente ou ilegível."""
    if env_path and os.path.exists(env_path):
        try:
            return dict(dotenv_values(env_path))
        except Exception:
            return {}
    return {}


def get_env_value(
    env_data: Dict[str, Optional[str]],
    key: str,
    default: str = "",
    *,
    prefer_environ: bool = False,
) -> str:
    """Resolve uma variável combinando `.env` e `os.environ` com prioridade explícita."""
    if prefer_environ:
        val = os.environ.get(key)
        if val is not None and val.strip():
            return val.strip()
        env_val = env_data.get(key)
        if env_val is not None and env_val.strip():
            return env_val.strip()
        return default
    env_val = env_data.get(key)
    if env_val is not None and env_val.strip():
        return env_val.strip()
    val = os.environ.get(key)
    if val is not None and val.strip():
        return val.strip()
    return default


def update_env_keys(
    updates: Dict[str, str],
    env_path: str,
    *,
    sync_environ: bool = False,
    default_template: Optional[str] = None,
) -> None:
    """
    Atualiza chaves específicas em `env_path` de forma atômica sob lock global,
    preservando comentários e demais variáveis existentes.
    Se o arquivo não existir e `default_template` for fornecido, grava o template.
    """
    with ENV_WRITE_LOCK:
        if not os.path.exists(env_path) and default_template is not None:
            content = default_template
        else:
            lines: List[str] = []
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()

            seen: set[str] = set()
            new_lines: List[str] = []
            for line in lines:
                m = _KEY_LINE_RE.match(line)
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

            content = "\n".join(new_lines) + "\n"

        atomic_write_text(env_path, content)
        if sync_environ:
            for k, v in updates.items():
                os.environ[k] = v


def scrub_secrets(
    err: object,
    secrets_to_hide: Sequence[str] = (),
    *,
    max_len: int = 300,
) -> str:
    """Remove tokens de bot Telegram, apikeys de URLs e segredos literais de mensagens de erro."""
    msg = str(err or "")
    for secret in secrets_to_hide:
        if secret:
            msg = msg.replace(secret, "***")
    msg = _TELEGRAM_TOKEN_SCRUB_RE.sub("bot***", msg)
    msg = _APIKEY_SCRUB_RE.sub(r"\1***", msg)
    return msg[:max_len]


def mask_secret(value: str, head: int = 4, tail: int = 4) -> str:
    """Mascara uma chave/token exibindo apenas pontas (ou `****` se curta)."""
    if not value:
        return ""
    if len(value) > (head + tail):
        return f"{value[:head]}...{value[-tail:]}"
    return "****"
