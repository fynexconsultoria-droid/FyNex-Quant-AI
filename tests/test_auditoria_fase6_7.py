"""
Testes da Auditoria - Fases 6 e 7 (Segurança do Dashboard: B1, B2, B3, M8; Qualidade: B7, B9).
Nota: testa o middleware ASGI diretamente (sem socket local) para rodar de forma rápida
e determinística em qualquer ambiente.
"""

import os
import sys
import math
import json
from starlette.requests import Request
from starlette.responses import JSONResponse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from dashboard import server  # noqa: E402
import trading_bot as tb  # noqa: E402
from backtest import run_walk_forward_backtest  # noqa: E402
import numpy as np
import pandas as pd


def _run_coro_sync(coro):
    """Executa uma corrotina simples sem criar ProactorEventLoop (evita socketpair em sandbox)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("Corrotina suspendeu inesperadamente")


def _call_security_middleware(headers: list[tuple[bytes, bytes]]):
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/api/status",
        "headers": headers,
        "query_string": b"",
    }
    req = Request(scope)

    async def dummy_next(_request):
        return JSONResponse({"ok": True})

    return _run_coro_sync(server.security_middleware(req, dummy_next))


def test_origin_localhost_evil_subdomain_is_blocked():
    assert server.is_allowed_origin("http://localhost:8000") is True
    assert server.is_allowed_origin("http://127.0.0.1:8000") is True
    assert server.is_allowed_origin("http://localhost.evil.com") is False
    assert server.is_allowed_origin("https://localhost:8000") is False

    resp = _call_security_middleware(
        [(b"host", b"127.0.0.1:8000"), (b"origin", b"http://localhost.evil.com")]
    )
    assert resp.status_code == 403


def test_empty_host_header_is_blocked():
    resp = _call_security_middleware([])
    assert resp.status_code == 403


def test_csp_and_security_headers_are_present():
    resp = _call_security_middleware([(b"host", b"127.0.0.1:8000")])
    assert resp.status_code == 200
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert "content-security-policy" in headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"


def test_backtest_profit_factor_is_json_safe_and_finite():
    rng = np.random.default_rng(7)
    n = 260
    base_ts = 1_700_000_000_000
    rets = rng.normal(0.0002, 0.003, size=n)
    closes = 50000.0 * np.cumprod(1.0 + rets)
    df = pd.DataFrame({
        "timestamp": pd.to_datetime([base_ts + i * 300_000 for i in range(n)], unit="ms"),
        "open": closes,
        "high": closes * 1.004,
        "low": closes * 0.996,
        "close": closes,
        "volume": rng.uniform(2.0, 10.0, size=n),
    })
    res = run_walk_forward_backtest(
        df, timeframe="5m", stop_loss_pct=0.003, take_profit_pct=0.005,
        horizon_candles=10, train_window=120, retrain_step=60,
    )
    assert math.isfinite(res["profit_factor"])
    assert math.isfinite(res["sharpe_ratio"])
    # Deve ser serializável em JSON estrito sem NaN/Infinity
    json.dumps({k: v for k, v in res.items() if k != "trades"}, allow_nan=False)


def test_atomic_lock_handles_stale_file(tmp_path, monkeypatch):
    pid_file = str(tmp_path / "bot.pid")
    (tmp_path / "bot.pid").write_text('{"pid": 99999999}', encoding="utf-8")
    monkeypatch.setattr(tb, "is_pid_alive_bot", lambda pid: pid == os.getpid())
    assert tb.acquire_single_instance_lock("BTC/USDT", "5m", "paper", pid_path=pid_file) is True
    assert tb.get_running_bot_pid(pid_file) == os.getpid()
