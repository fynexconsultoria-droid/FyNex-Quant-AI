"""
Supervisor de Processo (FyNex Quant AI - Fase 1)
================================================
Executa `trading_bot.py` em loop supervisionado:
- Se o robô encerrar com código 0 (parada limpa via `bot.stop` ou Ctrl+C), o supervisor encerra.
- Se o robô encerrar com código != 0 (crash), reinicia automaticamente com backoff exponencial.
"""

import os
import sys
import time
import logging
import subprocess
from typing import List, Optional

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")
STOP_SIGNAL_PATH = os.path.join(BASE_DIR, "bot.stop")

logger = logging.getLogger("BotSupervisor")


def run_supervised(
    bot_args: Optional[List[str]] = None,
    max_restarts: int = 0,
    base_backoff_sec: float = 5.0,
    max_backoff_sec: float = 300.0,
    python_exe: Optional[str] = None,
    bot_script: str = BOT_SCRIPT,
    stop_signal_path: str = STOP_SIGNAL_PATH,
) -> int:
    """
    Executa o robô e o reinicia em caso de falha (exit_code != 0).
    `max_restarts=0` significa reiniciar indefinidamente.
    Retorna o último código de saída.
    """
    exe = python_exe or sys.executable
    cmd = [exe, bot_script] + list(bot_args or [])
    restarts = 0
    last_code = 0

    while True:
        start_time = time.monotonic()
        logger.info(f"Iniciando robô supervisionado: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, cwd=BASE_DIR)
        last_code = proc.wait()
        elapsed = time.monotonic() - start_time

        if last_code == 0 or os.path.exists(stop_signal_path):
            logger.info(f"Robô encerrado normalmente (exit_code={last_code}). Encerrando supervisor.")
            return last_code

        # Se rodou saudável por mais de 5 minutos, reseta o backoff
        if elapsed >= 300.0:
            restarts = 0

        restarts += 1
        if max_restarts > 0 and restarts > max_restarts:
            logger.critical(f"Limite de reinícios ({max_restarts}) atingido. Encerrando supervisor.")
            return last_code

        delay = min(max_backoff_sec, base_backoff_sec * (2 ** min(restarts - 1, 6)))
        logger.error(
            f"Robô encerrou com erro (exit_code={last_code}) após {elapsed:.1f}s. "
            f"Reiniciando em {delay:.1f}s (tentativa #{restarts})..."
        )
        time.sleep(delay)


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(BASE_DIR, "logs", "supervisor.log"), encoding="utf-8"),
        ],
    )
    sys.exit(run_supervised(sys.argv[1:]))
