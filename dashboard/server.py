"""
Servidor Backend do Dashboard Web Quant Trading (FastAPI — FyNex Quant AI)
==========================================================================
Entrypoint enxuto que integra segurança HTTP, routers por domínio, controle
do robô e serviço de arquivos estáticos com injeção automática do token CSRF.

Módulos internos:
- `dashboard.security`: validação de Host/Origin, HTTP Basic Auth, CSP e CSRF
- `dashboard.schemas`: modelos Pydantic e padrões de validação
- `dashboard.bot_controller`: detecção/start/stop do processo do robô
- `dashboard.training_jobs`: treinamento profundo em background
- `dashboard.routers.*`: status, market, binance, xtb, ai, alerts e bot
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.infra.sqlite import connect_sqlite  # noqa: E402
from core.infra.utils import tail_lines as _tail_lines  # noqa: E402
from core.logging_setup import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_LOG_PATH,
    DEFAULT_MODELS_DIR,
    PID_FILE_PATH,
    STOP_SIGNAL_PATH,
)
from dashboard.bot_controller import (  # noqa: E402
    _LAST_REFRESH_TS,
    BOT_SCRIPT,
    SUPERVISOR_SCRIPT,
    allow_forced_refresh as _allow_forced_refresh,
    invalidate_bot_status_cache as _invalidate_bot_status_cache,
    is_bot_running,
    is_order_in_flight as _is_order_in_flight,
)
from dashboard.routers import ALL_ROUTERS  # noqa: E402
from dashboard.routers.ai import (  # noqa: E402
    get_ai_error_lessons,
    get_ai_evolution,
    get_ai_learning_history,
    get_ai_performance,
    get_ai_train_status,
    post_ai_chat,
    start_ai_training,
)
from dashboard.routers.alerts import (  # noqa: E402
    get_whatsapp_alert_config,
    save_whatsapp_alert_config,
    send_whatsapp_daily_summary_now,
)
from dashboard.routers.binance import (  # noqa: E402
    get_binance_balance,
    get_binance_config,
    get_binance_execution_ready,
    get_binance_health,
    save_binance_config_endpoint,
    test_binance_endpoint,
)
from dashboard.routers.bot import start_bot, stop_bot  # noqa: E402
from dashboard.routers.finance import export_finance_csv, get_finance_overview  # noqa: E402
from dashboard.routers.market import (  # noqa: E402
    get_forex_ai_status,
    get_live_macro_intelligence,
    get_live_market_depth,
    get_live_market_ticker,
    get_live_news,
    get_market_instruments,
    get_market_watchlist,
)
from dashboard.routers.status import (  # noqa: E402
    get_bot_status,
    get_candles,
    get_predictions,
    get_recent_logs,
    get_trades,
)
from dashboard.routers.xtb import (  # noqa: E402
    close_xtb_order,
    get_xtb_account,
    get_xtb_copilot_signals,
    get_xtb_copilot_telegram,
    open_xtb_order,
    save_xtb_copilot_telegram,
    trigger_xtb_copilot_scan,
    update_xtb_copilot_signal_status,
)
from dashboard.schemas import (  # noqa: E402
    SYMBOL_PATTERN,
    TIMEFRAME_PATTERN,
    AiChatPayload,
    BinanceConfigPayload,
    BinanceTestPayload,
    CopilotStatusPayload,
    TelegramConfigPayload,
    WhatsAppConfigPayload,
    XTBOrderPayload,
)
from dashboard.security import (  # noqa: E402
    ALLOWED_HOSTS,
    ALLOWED_ORIGIN_TUPLES,
    CSP_HEADER,
    CSRF_TOKEN,
    ENV_PATH,
    _PUBLIC_HOSTS_REGISTERED,
    _apply_security_headers,
    _check_basic_auth,
    _get_auth_env,
    _is_loopback_host,
    _parse_host_header,
    _register_cloud_hosts,
    is_allowed_origin,
    require_csrf,
    security_middleware,
)
from dashboard.training_jobs import _AI_TRAIN_LOCK, _AI_TRAIN_STATE  # noqa: E402

DB_PATH = DEFAULT_DB_PATH
MODELS_DIR = DEFAULT_MODELS_DIR
META_PATH = os.path.join(MODELS_DIR, "metadata.json")
LOG_PATH = DEFAULT_LOG_PATH
LEGACY_LOG_PATH = os.path.join(BASE_DIR, "trading_bot.log")

app = FastAPI(title="FyNex Quant AI Dashboard")
app.middleware("http")(security_middleware)

for _router in ALL_ROUTERS:
    app.include_router(_router)


def get_db_connection() -> sqlite3.Connection | None:
    """Retorna uma conexão SQLite para leitura rápida no dashboard."""
    if not os.path.exists(DB_PATH):
        return None
    return connect_sqlite(DB_PATH, timeout=15.0, readonly=False, configure_pragmas=False)


@contextmanager
def db_connection():
    """Context manager que garante o fechamento seguro da conexão SQLite."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            html = f.read()
        meta_tag = f'<meta name="csrf-token" content="{CSRF_TOKEN}">'
        if 'name="csrf-token"' not in html:
            html = html.replace("<head>", f"<head>\n  {meta_tag}", 1)
        return HTMLResponse(content=html, headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"message": "Dashboard frontend pronto. Abra static/index.html."})


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("dashboard.server:app", host=host, port=port, reload=False, app_dir=BASE_DIR)
