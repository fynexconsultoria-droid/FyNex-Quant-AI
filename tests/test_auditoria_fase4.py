"""
Testes da Auditoria - Fase 4 (Operação, configuração e logs: M1, M2, M3, M4, B4).
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import binance_client  # noqa: E402
import trading_bot as tb  # noqa: E402


def test_save_binance_config_preserves_telegram_and_comments(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Meu comentário customizado\n"
        "BINANCE_API_KEY=\n"
        "BINANCE_API_SECRET=\n"
        "TRADING_MODE=paper\n"
        "TELEGRAM_BOT_TOKEN=123456:ABC-DEF\n"
        "TELEGRAM_CHAT_ID=987654321\n"
        "CUSTOM_VAR=keep_me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(binance_client, "ENV_PATH", str(env_file))
    for var in ["BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET", "TRADING_MODE"]:
        monkeypatch.delenv(var, raising=False)

    binance_client.save_binance_config("A" * 20, "B" * 20, True, "testnet", 75.0)
    text = env_file.read_text(encoding="utf-8")
    assert "# Meu comentário customizado" in text
    assert "TELEGRAM_BOT_TOKEN=123456:ABC-DEF" in text
    assert "TELEGRAM_CHAT_ID=987654321" in text
    assert "CUSTOM_VAR=keep_me" in text
    assert "TRADING_MODE=testnet" in text
    assert "TRADE_AMOUNT_USDT=75.00" in text


def test_importing_trading_bot_does_not_attach_production_file_handler():
    prod_log = os.path.abspath(os.path.join(ROOT, "trading_bot.log"))
    for h in tb.logger.handlers:
        if isinstance(h, RotatingFileHandler):
            assert os.path.abspath(getattr(h, "baseFilename", "")) != prod_log


def test_default_paths_are_absolute():
    assert os.path.isabs(tb.DEFAULT_DB_PATH)
    assert os.path.isabs(tb.DEFAULT_MODELS_DIR)
    assert os.path.isabs(tb.DEFAULT_LOG_PATH)
    assert os.path.isabs(tb.PID_FILE_PATH)
