"""
Entrypoint de Produção na Nuvem (Railway / Render / Docker)
===========================================================
Executa simultaneamente no mesmo container:
1. O supervisor da IA (`supervisor.py --timeframe 1h`) em segundo plano (opcional via FYNEX_AUTOSTART_BOT=true)
2. O servidor Web/API do Dashboard (`dashboard/server.py`) em 0.0.0.0:$PORT
Captura SIGTERM do provedor de nuvem para encerrar o robô de forma limpa salvando checkpoint.
"""

import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SUPERVISOR_SCRIPT = os.path.join(BASE_DIR, "supervisor.py")
STOP_SIGNAL_PATH = os.path.join(BASE_DIR, "bot.stop")


def main() -> None:
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "models"), exist_ok=True)

    autostart = os.environ.get("FYNEX_AUTOSTART_BOT", "true").lower() in ("true", "1", "yes", "sim")
    bot_tf = os.environ.get("FYNEX_BOT_TIMEFRAME", "1h").strip() or "1h"
    bot_sym = os.environ.get("FYNEX_BOT_SYMBOL", "BTC/USDT").strip() or "BTC/USDT"

    bot_proc = None
    if autostart:
        if os.path.exists(STOP_SIGNAL_PATH):
            try:
                os.remove(STOP_SIGNAL_PATH)
            except OSError:
                pass
        cmd = [sys.executable, SUPERVISOR_SCRIPT, "--symbol", bot_sym, "--timeframe", bot_tf]
        print(f"[CLOUD] Iniciando IA supervisionada em segundo plano: {' '.join(cmd)}", flush=True)
        bot_proc = subprocess.Popen(cmd, cwd=BASE_DIR)

    def _graceful_shutdown(signum, _frame):
        print(f"[CLOUD] Sinal {signum} recebido. Solicitando parada limpa da IA...", flush=True)
        try:
            with open(STOP_SIGNAL_PATH, "w", encoding="utf-8") as f:
                f.write("stop\n")
        except OSError:
            pass
        if bot_proc is not None and bot_proc.poll() is None:
            try:
                bot_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                bot_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[CLOUD] Iniciando Dashboard FyNex PRO em http://{host}:{port}", flush=True)
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        app_dir=os.path.join(BASE_DIR, "dashboard"),
    )


if __name__ == "__main__":
    main()
