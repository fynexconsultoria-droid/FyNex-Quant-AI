"""
Configuração global de testes (Fase 4 - M2):
- Impede envio de alertas reais via Telegram durante pytest.
- Garante que nenhum teste escreva no `trading_bot.log` de produção.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alerts  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_alerts_and_logs(monkeypatch):
    # Desliga credenciais do Telegram para todos os testes (nenhuma requisição externa)
    monkeypatch.setattr(alerts, "_load_telegram_credentials", lambda: ("", ""))
    yield
    # Remove eventuais file handlers temporários adicionados por testes
    bot_logger = logging.getLogger("QuantTradingBot")
    for h in list(bot_logger.handlers):
        if isinstance(h, RotatingFileHandler):
            bot_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
