"""
Controlador de Processo do Robô e Cooldowns do Dashboard (FyNex Quant AI)
=========================================================================
Gerencia verificação de processo ativo (`bot.pid` + fallback `psutil` com cache curto),
inicialização supervisionada, parada limpa protegendo `order_in_flight == 1`
e controle de cooldown para refresh forçado.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

import psutil
from fastapi import HTTPException

from core import forex_market
from core.infra.sqlite import sqlite_readonly_optional
from core.logging_setup import BASE_DIR, DEFAULT_DB_PATH, PID_FILE_PATH, STOP_SIGNAL_PATH
from core.process import get_running_bot_pid, is_pid_alive_bot, request_bot_stop

BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")
SUPERVISOR_SCRIPT = os.path.join(BASE_DIR, "supervisor.py")

_BOT_STATUS_CACHE_LOCK = threading.Lock()
_BOT_STATUS_CACHE: Tuple[Optional[float], bool, Optional[int]] = (None, False, None)
_BOT_START_LOCK = threading.Lock()
_LAST_BOT_START_MONO: Optional[float] = None
_REFRESH_COOLDOWN_LOCK = threading.Lock()
_LAST_REFRESH_TS: Dict[str, float] = {}


def _server_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _get_time_mod():
    return _server_attr("time", time)


def allow_forced_refresh(key: str, cooldown_sec: float = 15.0) -> bool:
    now = _get_time_mod().monotonic()
    with _REFRESH_COOLDOWN_LOCK:
        last_ts = _LAST_REFRESH_TS.get(key)
        if last_ts is not None and (now - last_ts) < cooldown_sec:
            return False
        _LAST_REFRESH_TS[key] = now
        return True


def invalidate_bot_status_cache() -> None:
    global _BOT_STATUS_CACHE
    with _BOT_STATUS_CACHE_LOCK:
        _BOT_STATUS_CACHE = (None, False, None)


def is_bot_running(use_cache: bool = True) -> Tuple[bool, Optional[int]]:
    """Verifica se o robô está ativo usando `bot.pid` e fallback `psutil` (cache 2.5s)."""
    global _BOT_STATUS_CACHE
    now = _get_time_mod().monotonic()
    if use_cache:
        with _BOT_STATUS_CACHE_LOCK:
            ts, cached_running, cached_pid = _BOT_STATUS_CACHE
            if ts is not None and (now - ts) < 2.5:
                return cached_running, cached_pid

    pid = get_running_bot_pid(PID_FILE_PATH)
    if pid:
        with _BOT_STATUS_CACHE_LOCK:
            _BOT_STATUS_CACHE = (now, True, pid)
        return True, pid

    found_pid: Optional[int] = None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(
                os.path.basename(str(arg).strip('"').strip("'")).lower() in ("trading_bot.py", "supervisor.py")
                for arg in cmdline
            ):
                found_pid = proc.info["pid"]
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    running = found_pid is not None
    with _BOT_STATUS_CACHE_LOCK:
        _BOT_STATUS_CACHE = (now, running, found_pid)
    return running, found_pid


def _check_is_bot_running(*, use_cache: bool = True) -> Tuple[bool, Optional[int]]:
    fn = _server_attr("is_bot_running", is_bot_running)
    try:
        return fn(use_cache=use_cache)
    except TypeError:
        return fn()


def is_order_in_flight() -> bool:
    db_path = str(_server_attr("DB_PATH", DEFAULT_DB_PATH))
    with sqlite_readonly_optional(db_path) as conn:
        if conn is None:
            return False
        try:
            row = conn.execute("SELECT order_in_flight FROM bot_state WHERE id = 1").fetchone()
            return bool(row and row["order_in_flight"])
        except Exception:
            return False


def start_bot_process(
    *,
    symbol: str,
    timeframe: str,
    retrain: Optional[int] = None,
    trading_mode: Optional[str] = None,
    trade_amount: Optional[float] = None,
) -> Dict[str, Any]:
    global _LAST_BOT_START_MONO
    if forex_market.is_forex_symbol(symbol):
        raise HTTPException(
            status_code=400,
            detail=(
                f"O robô autônomo Binance Spot opera apenas pares cripto (ex.: BTC/USDT). "
                f"'{symbol}' é monitorado automaticamente pelo Copiloto XTB / Forex IA."
            ),
        )

    with _BOT_START_LOCK:
        invalidate_bot_status_cache()
        running, pid = _check_is_bot_running(use_cache=False)
        if running:
            return {"status": "already_running", "pid": pid, "message": "O robô já está ativo em segundo plano."}
        if _LAST_BOT_START_MONO is not None and (_get_time_mod().monotonic() - _LAST_BOT_START_MONO) < 4.0:
            return {"status": "starting", "pid": None, "message": "Inicialização do robô já em andamento."}

        try:
            if os.path.exists(STOP_SIGNAL_PATH):
                try:
                    os.remove(STOP_SIGNAL_PATH)
                except OSError:
                    pass

            python_exe = sys.executable
            if os.name == "nt":
                pyw_candidate = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
                if os.path.exists(pyw_candidate):
                    python_exe = pyw_candidate

            target_script = SUPERVISOR_SCRIPT if os.path.exists(SUPERVISOR_SCRIPT) else BOT_SCRIPT
            cmd = [python_exe, target_script, "--symbol", symbol, "--timeframe", timeframe]
            if retrain is not None:
                cmd.extend(["--retrain", str(retrain)])
            if trading_mode:
                cmd.extend(["--trading-mode", trading_mode])
            if trade_amount:
                cmd.extend(["--trade-amount", str(trade_amount)])

            popen_kwargs: Dict[str, Any] = {
                "cwd": BASE_DIR,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = 0x00000008 | 0x08000000
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
            # Evita processos zumbis (<defunct>) em sistemas POSIX recolhendo o exit code em daemon
            threading.Thread(target=proc.wait, daemon=True).start()
            _LAST_BOT_START_MONO = _get_time_mod().monotonic()
            invalidate_bot_status_cache()
            return {
                "status": "started",
                "pid": proc.pid,
                "message": f"Robô iniciado com sucesso (PID {proc.pid}) em {symbol} ({timeframe}).",
            }
        except Exception:
            raise HTTPException(status_code=500, detail="Erro interno ao iniciar o processo do robô.")


def stop_bot_process() -> Dict[str, Any]:
    """Sinaliza parada limpa via `bot.stop`, aguardando saída ordenada e protegendo `order_in_flight == 1`."""
    if not _BOT_START_LOCK.acquire(blocking=False):
        return {"status": "stopping", "message": "Operação de controle do robô já está em andamento."}
    try:
        invalidate_bot_status_cache()
        running, pid = _check_is_bot_running(use_cache=False)
        if not running or not pid:
            return {"status": "not_running", "message": "O robô não está em execução."}

        p: Optional[psutil.Process] = None
        orig_create_time: Optional[float] = None
        try:
            request_bot_stop(STOP_SIGNAL_PATH)
            p = psutil.Process(pid)
            orig_create_time = p.create_time()
            for _ in range(180):
                if not p.is_running():
                    invalidate_bot_status_cache()
                    return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado de forma limpa."}
                time.sleep(0.25)

            if is_order_in_flight():
                return {
                    "status": "order_in_flight",
                    "message": "Há uma ordem em andamento na exchange. A parada limpa foi solicitada e aguardará a conclusão.",
                }

            # Regrava bot.stop antes de forçar encerramento para evitar respawn pelo supervisor.py
            request_bot_stop(STOP_SIGNAL_PATH)
            if is_pid_alive_bot(pid, expected_create_time=orig_create_time):
                p.terminate()
                p.wait(timeout=3.0)
            invalidate_bot_status_cache()
            return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
        except psutil.NoSuchProcess:
            invalidate_bot_status_cache()
            return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
        except Exception:
            if is_order_in_flight():
                return {
                    "status": "order_in_flight",
                    "message": "Ordem em andamento; encerramento forçado abortado por segurança.",
                }
            try:
                request_bot_stop(STOP_SIGNAL_PATH)
                if p is not None and is_pid_alive_bot(pid, expected_create_time=orig_create_time):
                    p.kill()
                    try:
                        p.wait(timeout=2.0)
                    except Exception:
                        pass
                invalidate_bot_status_cache()
                return {"status": "killed", "message": f"Processo forçado a fechar (PID {pid})."}
            except Exception:
                raise HTTPException(status_code=500, detail="Falha ao parar o processo do robô.")
    finally:
        _BOT_START_LOCK.release()
