"""
Utilitários Numéricos, Temporais e de Sistema Compartilhados (FyNex Quant AI)
=============================================================================
Fonte única para conversões numéricas finitas, timestamps UTC, leitura rápida
de cauda de logs e sanitização de tokens usados em nomes de arquivos.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def finite_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Converte `value` em float finito; retorna `default` para None, NaN, inf ou inválidos."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def to_float(value: Any, default: float) -> float:
    """Converte `value` em float finito garantindo sempre um retorno `float`."""
    res = finite_float(value, default=default)
    return float(default) if res is None else res


def utc_now_str(now_utc: Optional[datetime] = None) -> str:
    """Retorna timestamp UTC formatado como `YYYY-MM-DD HH:MM:SS`."""
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_str(value: Optional[str]) -> Optional[datetime]:
    """Faz parse seguro de string `YYYY-MM-DD HH:MM:SS` para datetime aware em UTC."""
    raw = (value or "").strip()
    if len(raw) < 19:
        return None
    try:
        return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sanitize_token(value: str, label: str = "token") -> str:
    """Sanitiza um identificador (ex.: symbol/timeframe) para uso seguro em nomes de arquivo."""
    clean = (value or "").strip().replace("/", "_")
    if not clean or not _SAFE_TOKEN_RE.match(clean) or ".." in clean:
        raise ValueError(f"{label} inválido para nome de arquivo: {value!r}")
    return clean


def tail_lines(file_path: str, max_lines: int = 50, max_bytes: int = 65536) -> List[str]:
    """Lê apenas os últimos `max_bytes` de um arquivo e retorna até `max_lines` linhas não vazias."""
    with open(file_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes), os.SEEK_SET)
        chunk = f.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    return lines[-max_lines:]
