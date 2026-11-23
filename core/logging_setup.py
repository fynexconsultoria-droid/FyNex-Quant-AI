"""
Configuração de Caminhos Base, Logging Rotativo e Hooks de Exceção (FyNex Quant AI)
===================================================================================
Centraliza os caminhos absolutos do projeto, o logger principal (`QuantTradingBot`)
com rotação de arquivos (`RotatingFileHandler`) e os hooks globais de exceção.
"""

import os
import sys
import logging
import threading
from logging.handlers import RotatingFileHandler
from typing import Optional

# Raiz do projeto (um nível acima de `core/`)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "trading_data.db")
DEFAULT_MODELS_DIR = os.path.join(BASE_DIR, "models")
DEFAULT_LOGS_DIR = os.path.join(BASE_DIR, "logs")
DEFAULT_LOG_PATH = os.path.join(DEFAULT_LOGS_DIR, "trading_bot.log")
PID_FILE_PATH = os.path.join(BASE_DIR, "bot.pid")
STOP_SIGNAL_PATH = os.path.join(BASE_DIR, "bot.stop")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("QuantTradingBot")
logger.setLevel(logging.INFO)


class _WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler tolerante a PermissionError no Windows quando `ver_logs.bat` lê o log."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            if self.stream is None:
                self.stream = self._open()


def setup_logging(log_path: Optional[str] = None, console: bool = True) -> logging.Logger:
    """
    Configura logging dual (console + RotatingFileHandler 5 MB x 5) sob demanda,
    evitando abrir arquivos de log de produção no import (testes e dashboard).
    """
    target_log = log_path or DEFAULT_LOG_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target_log)), exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    if console and sys.stdout is not None and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    ):
        stream_h = logging.StreamHandler(sys.stdout)
        stream_h.setFormatter(formatter)
        logger.addHandler(stream_h)

    target_abs = os.path.abspath(target_log)
    has_same_file = any(
        isinstance(h, RotatingFileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == target_abs
        for h in logger.handlers
    )
    if not has_same_file:
        file_h = _WindowsSafeRotatingFileHandler(
            target_abs,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_h.setFormatter(formatter)
        logger.addHandler(file_h)

    return logger


def install_exception_hooks(stderr_log_path: Optional[str] = None) -> None:
    """
    Instala hooks globais para capturar exceções não tratadas (thread principal e secundárias)
    no logger. Quando executado via pythonw.exe (sem console), redireciona stderr para arquivo.
    """
    if stderr_log_path and sys.stderr is None:
        try:
            abs_stderr = os.path.abspath(stderr_log_path)
            os.makedirs(os.path.dirname(abs_stderr), exist_ok=True)
            # Limita crescimento ilimitado do stderr.log (trunca se ultrapassar 2 MB)
            if os.path.exists(abs_stderr) and os.path.getsize(abs_stderr) > 2 * 1024 * 1024:
                mode = "w"
            else:
                mode = "a"
            sys.stderr = open(abs_stderr, mode, encoding="utf-8", buffering=1)
        except OSError:
            pass

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Exceção não tratada no processo principal:", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            f"Exceção não tratada na thread '{getattr(args.thread, 'name', '?')}':",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
