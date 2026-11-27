"""
Segurança HTTP do Dashboard (Host/DNS Rebinding, Origin, Basic Auth, CSP e CSRF)
================================================================================
Concentra toda a política de segurança da API FastAPI, com resolução transparente
de `ENV_PATH` e `_PUBLIC_HOSTS_REGISTERED` para compatibilidade total com testes.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from core.infra.env_store import read_env_file
from core.logging_setup import BASE_DIR

ENV_PATH = os.path.join(BASE_DIR, ".env")
CSRF_TOKEN = secrets.token_urlsafe(32)
_AUTH_ENV_CACHE_LOCK = threading.Lock()
_AUTH_ENV_CACHE: Tuple[str, int, int, Dict[str, Optional[str]]] = ("", -1, -1, {})

_SERVER_PORT = int(os.environ.get("PORT", "8000") or "8000")
ALLOWED_HOSTS = {
    "127.0.0.1",
    f"127.0.0.1:{_SERVER_PORT}",
    "127.0.0.1:8000",
    "localhost",
    f"localhost:{_SERVER_PORT}",
    "localhost:8000",
    "::1",
    f"[::1]:{_SERVER_PORT}",
    "[::1]:8000",
    "testserver",
}
ALLOWED_ORIGIN_TUPLES = {
    ("http", "127.0.0.1", _SERVER_PORT),
    ("http", "127.0.0.1", 8000),
    ("http", "127.0.0.1", None),
    ("http", "localhost", _SERVER_PORT),
    ("http", "localhost", 8000),
    ("http", "localhost", None),
    ("http", "::1", _SERVER_PORT),
    ("http", "::1", 8000),
    ("http", "::1", None),
    ("http", "testserver", None),
}
_PUBLIC_HOSTS_REGISTERED = False

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' wss://stream.binance.com:9443 wss://stream.binance.com:443 wss://stream.binance.com; "
    "form-action 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


def _register_cloud_hosts() -> None:
    """Registra domínios públicos configurados no Railway/Render ou via FYNEX_PUBLIC_HOSTS (apenas HTTPS)."""
    global _PUBLIC_HOSTS_REGISTERED
    raw_hosts = [
        os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""),
        os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""),
        *os.environ.get("FYNEX_PUBLIC_HOSTS", "").split(","),
    ]
    for item in raw_hosts:
        h = item.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        if not h:
            continue
        _PUBLIC_HOSTS_REGISTERED = True
        ALLOWED_HOSTS.add(h)
        host_only = _parse_host_header(h) or h.split(":")[0]
        ALLOWED_HOSTS.add(host_only)
        ALLOWED_ORIGIN_TUPLES.add(("https", host_only, None))
        ALLOWED_ORIGIN_TUPLES.add(("https", host_only, 443))


_register_cloud_hosts()


def _resolve_server_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _read_env_cached(env_path: str) -> Dict[str, Optional[str]]:
    """Lê `.env` com cache invalidado automaticamente por `(path, mtime_ns, size)`."""
    global _AUTH_ENV_CACHE
    if not env_path or not os.path.exists(env_path):
        return {}
    try:
        st = os.stat(env_path)
        sig = (env_path, st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _AUTH_ENV_CACHE_LOCK:
        if _AUTH_ENV_CACHE[:3] == sig:
            return _AUTH_ENV_CACHE[3]
    data = read_env_file(env_path)
    with _AUTH_ENV_CACHE_LOCK:
        _AUTH_ENV_CACHE = (sig[0], sig[1], sig[2], data)
    return data


def _get_auth_env(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is not None and val.strip():
        return val.strip()
    env_path = str(_resolve_server_attr("ENV_PATH", ENV_PATH))
    env_data = _read_env_cached(env_path)
    env_val = env_data.get(key)
    if env_val is not None and env_val.strip():
        return env_val.strip()
    return default


def _parse_host_header(host: str) -> str:
    """Extrai o hostname de um header Host suportando IPv4, domínios e IPv6, rejeitando userinfo/path."""
    if not host or any(ch in host for ch in (" ", "\t", "\r", "\n", "@", "/", "?", "#")):
        return ""
    try:
        parsed = urlparse(f"//{host}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            return ""
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def _is_loopback_host(hostname: str) -> bool:
    return hostname in ("127.0.0.1", "localhost", "::1", "testserver")


def _check_basic_auth(request: Request) -> bool:
    """
    Exige HTTP Basic Auth quando:
    - FYNEX_DASHBOARD_PASSWORD está configurada (env ou .env), OU
    - o dashboard está exposto em host público/não-loopback (fail-closed).
    """
    expected_pw = _get_auth_env("FYNEX_DASHBOARD_PASSWORD", "")
    bind_host = os.environ.get("HOST", "127.0.0.1").strip().lower()
    req_hostname = _parse_host_header((request.headers.get("host") or "").strip().lower())
    client_host = ((request.client.host if request.client else "") or "").strip().lower()
    public_reg = bool(_resolve_server_attr("_PUBLIC_HOSTS_REGISTERED", _PUBLIC_HOSTS_REGISTERED))
    is_non_local_client = bool(client_host) and client_host not in ("127.0.0.1", "::1", "localhost", "testclient")
    is_public_exposure = (
        public_reg
        or is_non_local_client
        or (bind_host not in ("127.0.0.1", "localhost", "::1", ""))
        or (req_hostname != "" and not _is_loopback_host(req_hostname))
    )
    if not expected_pw:
        return not is_public_exposure
    expected_user = _get_auth_env("FYNEX_DASHBOARD_USER", "admin") or "admin"
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
        user, _, pw = decoded.partition(":")
        user_ok = secrets.compare_digest(user, expected_user)
        pw_ok = secrets.compare_digest(pw, expected_pw)
        return user_ok and pw_ok
    except Exception:
        return False


def is_allowed_origin(origin: str) -> bool:
    """Valida exatamente scheme, host e porta do header Origin."""
    raw = origin.strip()
    if not raw or any(ch in raw for ch in (" ", "\t", "\r", "\n", "@")):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port) in ALLOWED_ORIGIN_TUPLES


def _apply_security_headers(response, *, is_api: bool = False, is_static: bool = False):
    response.headers["Content-Security-Policy"] = CSP_HEADER
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if is_api and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store, max-age=0"
    elif is_static and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


async def security_middleware(request: Request, call_next):
    if request.url.path == "/healthz":
        return _apply_security_headers(JSONResponse({"status": "ok"}))

    host = (request.headers.get("host") or "").lower().strip()
    host_no_port = _parse_host_header(host)
    if not host or (host not in ALLOWED_HOSTS and host_no_port not in ALLOWED_HOSTS):
        return _apply_security_headers(
            JSONResponse(
                status_code=403,
                content={"detail": f"Host '{host}' não autorizado (proteção contra DNS Rebinding)."},
            )
        )

    origin = (request.headers.get("origin") or "").strip()
    if origin and not is_allowed_origin(origin):
        return _apply_security_headers(
            JSONResponse(
                status_code=403,
                content={"detail": f"Origem '{origin}' bloqueada (mesma origem exigida)."},
            )
        )

    if not _check_basic_auth(request):
        return _apply_security_headers(
            JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="FyNex Quant AI"'},
                content={"detail": "Autenticação obrigatória para acessar o dashboard."},
            )
        )

    response = await call_next(request)
    return _apply_security_headers(
        response,
        is_api=request.url.path.startswith("/api/"),
        is_static=request.url.path.startswith("/static/"),
    )


def require_csrf(request: Request) -> None:
    expected = str(_resolve_server_attr("CSRF_TOKEN", CSRF_TOKEN))
    token = request.headers.get("X-CSRF-Token") or ""
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=403,
            detail="Token CSRF ausente ou inválido. Recarregue a página do dashboard.",
        )
