"""Routers FastAPI do Dashboard FyNex Quant AI organizados por domínio."""

from dashboard.routers.ai import router as ai_router
from dashboard.routers.alerts import router as alerts_router
from dashboard.routers.binance import router as binance_router
from dashboard.routers.bot import router as bot_router
from dashboard.routers.market import router as market_router
from dashboard.routers.status import router as status_router
from dashboard.routers.xtb import router as xtb_router

ALL_ROUTERS = (
    status_router,
    market_router,
    binance_router,
    xtb_router,
    ai_router,
    alerts_router,
    bot_router,
)

__all__ = [
    "ALL_ROUTERS",
    "status_router",
    "market_router",
    "binance_router",
    "xtb_router",
    "ai_router",
    "alerts_router",
    "bot_router",
]
