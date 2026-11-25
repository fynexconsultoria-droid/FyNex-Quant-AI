"""
Reinício Seguro do Sistema FyNex Quant AI (Servidor Dashboard + Robô Supervisionado)
====================================================================================
Reinicia o servidor FastAPI/Uvicorn e o robô de operações (`supervisor.py` + `trading_bot.py`)
de forma 100% segura, SEM afetar dados no SQLite (`trading_data.db`), histórico de trades
nem posições/operações abertas:
  1. Aguarda qualquer ordem em andamento (`order_in_flight`) concluir antes de parar o robô.
  2. Envia sinal limpo `bot.stop` para encerramento gracioso do robô e do supervisor.
  3. Reinicia o servidor Dashboard na porta configurada (`127.0.0.1:8000`).
  4. Sobe o robô supervisionado novamente (`supervisor.py --timeframe 1h`) restaurando posições.
  5. Valida saúde HTTP (`/api/status`, `/api/finance/overview`), PID vivo do robô e integridade do DB.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Tuple

import psutil

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import trading_bot as tb  # noqa: E402
from core.logging_setup import DEFAULT_DB_PATH, STOP_SIGNAL_PATH  # noqa: E402


def _resolve_python_executables() -> Tuple[str, str]:
    for folder in (".venv", "venv"):
        py = os.path.join(BASE_DIR, folder, "Scripts", "python.exe")
        pyw = os.path.join(BASE_DIR, folder, "Scripts", "pythonw.exe")
        if os.path.exists(py):
            return py, (pyw if os.path.exists(pyw) else py)
    py_cur = sys.executable
    pyw_cur = os.path.join(os.path.dirname(py_cur), "pythonw.exe")
    return py_cur, (pyw_cur if os.path.exists(pyw_cur) else py_cur)


def _db_integrity_snapshot(db_path: str = DEFAULT_DB_PATH) -> Dict[str, int]:
    snap = {"open_positions": 0, "xtb_positions": 0, "simulated_trades": 0, "xtb_trades": 0}
    if not os.path.exists(db_path):
        return snap
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        cur = conn.cursor()
        for table in list(snap.keys()):
            exists = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                snap[table] = int(cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()
    return snap


def _find_matching_processes(patterns: Tuple[str, ...]) -> List[psutil.Process]:
    my_pid = os.getpid()
    found: List[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == my_pid:
                continue
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            if not cmd:
                continue
            if any(p in cmd for p in patterns):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def _find_port_listeners(port: int) -> List[psutil.Process]:
    my_pid = os.getpid()
    seen = set()
    found: List[psutil.Process] = []
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                if conn.pid != my_pid and conn.pid not in seen:
                    seen.add(conn.pid)
                    try:
                        found.append(psutil.Process(conn.pid))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
    except Exception:
        pass
    return found


def _stop_bot_safely(timeout_sec: float = 20.0) -> str:
    bot_procs = _find_matching_processes(("trading_bot.py", "supervisor.py"))
    if not bot_procs:
        if os.path.exists(STOP_SIGNAL_PATH):
            try:
                os.remove(STOP_SIGNAL_PATH)
            except OSError:
                pass
        return "Robô não estava em execução."

    # 1. Aguarda qualquer ordem em andamento antes de interromper
    if os.path.exists(DEFAULT_DB_PATH):
        db = tb.TradingDatabase(DEFAULT_DB_PATH)
        wait_flight = 0.0
        while db.is_order_in_flight() and wait_flight < 30.0:
            time.sleep(0.5)
            wait_flight += 0.5

    # 2. Envia sinal limpo bot.stop
    tb.request_bot_stop()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        alive = _find_matching_processes(("trading_bot.py", "supervisor.py"))
        if not alive:
            break
        time.sleep(0.4)

    # 3. Se algum processo ainda restou e NÃO há ordem em andamento, encerra graciosamente
    remaining = _find_matching_processes(("trading_bot.py", "supervisor.py"))
    in_flight = False
    if remaining and os.path.exists(DEFAULT_DB_PATH):
        try:
            in_flight = tb.TradingDatabase(DEFAULT_DB_PATH).is_order_in_flight()
        except Exception:
            in_flight = False

    if remaining and not in_flight:
        # Encerra supervisores primeiro para evitar respawn, depois trading_bot
        remaining.sort(key=lambda p: 0 if "supervisor.py" in " ".join(p.cmdline() or []).lower() else 1)
        for p in remaining:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, still_alive = psutil.wait_procs(remaining, timeout=6.0)
        for p in still_alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(still_alive, timeout=3.0)

    if os.path.exists(STOP_SIGNAL_PATH):
        try:
            os.remove(STOP_SIGNAL_PATH)
        except OSError:
            pass
    return f"Robô/supervisor encerrado com segurança ({len(bot_procs)} processo(s))."


def _stop_dashboard_safely(port: int = 8000) -> str:
    procs_by_pid: Dict[int, psutil.Process] = {}
    for p in _find_matching_processes(("dashboard.server", "dashboard/server.py", "dashboard\\server.py")):
        procs_by_pid[p.pid] = p
    for p in _find_port_listeners(port):
        procs_by_pid[p.pid] = p

    procs = list(procs_by_pid.values())
    if not procs:
        return f"Servidor dashboard não estava rodando na porta {port}."

    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(procs, timeout=5.0)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=3.0)
    return f"Servidor dashboard anterior encerrado ({len(procs)} processo(s))."


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def _start_dashboard(py_exe: str, host: str = "127.0.0.1", port: int = 8000) -> Tuple[int, bool]:
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    log_path = os.path.join(BASE_DIR, "logs", "dashboard.log")
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [py_exe, "-m", "uvicorn", "dashboard.server:app", "--host", host, "--port", str(port)],
            cwd=BASE_DIR,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_detached_flags(),
            close_fds=(os.name != "nt"),
        )
    finally:
        log_fh.close()

    url = f"http://{host}:{port}/api/status"
    healthy = False
    for _ in range(40):
        time.sleep(0.35)
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            continue
    return proc.pid, healthy


def _start_bot(pyw_exe: str, timeframe: str = "1h") -> Tuple[int, bool]:
    if os.path.exists(STOP_SIGNAL_PATH):
        try:
            os.remove(STOP_SIGNAL_PATH)
        except OSError:
            pass
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "LOKY_MAX_CPU_COUNT"):
        env.setdefault(var, "2")

    proc = subprocess.Popen(
        [pyw_exe, os.path.join(BASE_DIR, "supervisor.py"), "--timeframe", timeframe],
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=_detached_flags(),
        close_fds=(os.name != "nt"),
    )

    alive = False
    for _ in range(25):
        time.sleep(0.4)
        if _find_matching_processes(("trading_bot.py",)):
            alive = True
            break
    return proc.pid, alive


def main() -> int:
    parser = argparse.ArgumentParser(description="Reinicia servidor Dashboard e Robô FyNex sem perda de dados.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--only-server", action="store_true")
    parser.add_argument("--only-bot", action="store_true")
    args = parser.parse_args()

    py_exe, pyw_exe = _resolve_python_executables()
    snap_before = _db_integrity_snapshot()
    print(f"[1/5] Snapshot pré-reinício do DB: {snap_before}")

    if not args.only_server:
        print(f"[2/5] {_stop_bot_safely()}")
    if not args.only_bot:
        print(f"[3/5] {_stop_dashboard_safely(port=args.port)}")

    server_ok = True
    bot_ok = True
    if not args.only_bot:
        srv_pid, server_ok = _start_dashboard(py_exe, host=args.host, port=args.port)
        print(f"[4/5] Dashboard iniciado (PID={srv_pid}) -> http://{args.host}:{args.port} | Saudável: {server_ok}")

    if not args.only_server:
        sup_pid, bot_ok = _start_bot(pyw_exe, timeframe=args.timeframe)
        bot_pid = tb.get_running_bot_pid()
        print(f"[4/5] Robô supervisionado iniciado (Supervisor PID={sup_pid}, Bot PID={bot_pid}) | Ativo: {bot_ok}")

    snap_after = _db_integrity_snapshot()
    preserved = (
        snap_after["open_positions"] >= snap_before["open_positions"]
        and snap_after["xtb_positions"] >= snap_before["xtb_positions"]
        and snap_after["simulated_trades"] >= snap_before["simulated_trades"]
        and snap_after["xtb_trades"] >= snap_before["xtb_trades"]
    )
    print(f"[5/5] Snapshot pós-reinício do DB: {snap_after} | Dados preservados: {preserved}")

    if not server_ok or not bot_ok or not preserved:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
