"""
Controlador de Processo do Robô e Cooldowns do Dashboard (FyNex Quant AI)
=========================================================================
Gerencia verificação de processo ativo (`bot_demo.pid`, `bot_live.pid` e legado `bot.pid`),
execução paralela e isolada entre Conta Demo (Treinamento/Simulação) e Conta Real (Live),
parada limpa protegendo `order_in_flight == 1` e controle de cooldown para refresh forçado.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

import psutil
from fastapi import HTTPException

import binance_client
from core import forex_market
from core.infra.sqlite import sqlite_readonly_optional, table_exists
from core.logging_setup import (
    BASE_DIR,
    DEFAULT_DB_PATH,
    PID_FILE_PATH,
    STOP_SIGNAL_PATH,
    get_account_pid_path,
    get_account_stop_path,
    normalize_account,
)
from core.process import get_running_bot_pid, is_pid_alive_bot, request_bot_stop
from strategy_config import OPERATIONAL_TIMEFRAMES, is_operational_timeframe

BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")
SUPERVISOR_SCRIPT = os.path.join(BASE_DIR, "supervisor.py")

_BOT_STATUS_CACHE_LOCK = threading.Lock()
_BOT_STATUS_CACHE: Dict[str, Tuple[Optional[float], bool, Optional[int]]] = {}
_ACCOUNT_START_LOCKS: Dict[str, threading.Lock] = {
    "default": threading.Lock(),
    "demo": threading.Lock(),
    "live": threading.Lock(),
}
_LAST_BOT_START_MONO: Dict[str, float] = {}
_REFRESH_COOLDOWN_LOCK = threading.Lock()
_LAST_REFRESH_TS: Dict[str, float] = {}
_DEMO_MANUAL_PAUSE = False


def set_demo_manual_pause(paused: bool) -> None:
    """Controla se o operador pausou manualmente o robô Demo nesta sessão."""
    global _DEMO_MANUAL_PAUSE
    _DEMO_MANUAL_PAUSE = bool(paused)


def is_demo_manual_paused() -> bool:
    return bool(_DEMO_MANUAL_PAUSE)


def ensure_demo_training_bot_running(*, force: bool = False) -> Dict[str, Any]:
    """
    Garante que o robô da Conta Demo (Treinamento IA) permaneça ativo acompanhando o mercado
    sempre que a política `demo_always_on_training` estiver ligada e o operador não tiver pausado manualmente.
    """
    if not force and (
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("FYNEX_DISABLE_AUTO_TRAIN_SCHEDULER", "").strip() in ("1", "true", "yes")
    ):
        return {"status": "skipped_test"}
    if _DEMO_MANUAL_PAUSE and not force:
        return {"status": "manual_pause"}

    from core.database import TradingDatabase

    db_path = str(_server_attr("DB_PATH", DEFAULT_DB_PATH))
    db = TradingDatabase(db_path=db_path)
    if not db.is_demo_always_on_enabled():
        return {"status": "disabled"}

    running, pid = _check_is_bot_running(use_cache=False, account="demo")
    if running:
        return {"status": "already_running", "pid": pid}

    sym = (db.get_account_setting("ai_auto_train_symbol", "BTC/USDT") or "BTC/USDT").strip().upper()
    if forex_market.is_forex_symbol(sym):
        sym = "BTC/USDT"
    tf = (db.get_account_setting("ai_auto_train_timeframe", "1h") or "1h").strip().lower()
    if not is_operational_timeframe(tf):
        tf = "1h"

    return start_bot_process(
        symbol=sym,
        timeframe=tf,
        trading_mode="paper",
        account="demo",
    )


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


def invalidate_bot_status_cache(account: Optional[str] = None) -> None:
    with _BOT_STATUS_CACHE_LOCK:
        if account is None:
            _BOT_STATUS_CACHE.clear()
        else:
            acc = normalize_account(account)
            _BOT_STATUS_CACHE.pop(acc, None)
            _BOT_STATUS_CACHE.pop("any", None)


def _read_pid_mode(pid_path: str) -> Optional[str]:
    if not os.path.exists(pid_path):
        return None
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = str(data.get("trading_mode") or "").lower()
        return mode or None
    except Exception:
        return None


def _cmdline_account(cmdline: list) -> Optional[str]:
    for idx, arg in enumerate(cmdline):
        s = str(arg).strip('"').strip("'")
        if s == "--account" and idx + 1 < len(cmdline):
            val = str(cmdline[idx + 1]).strip('"').strip("'").lower()
            if val in ("demo", "live"):
                return val
        elif s.startswith("--account="):
            val = s.split("=", 1)[1].strip().lower()
            if val in ("demo", "live"):
                return val
    for idx, arg in enumerate(cmdline):
        s = str(arg).strip('"').strip("'")
        if s == "--trading-mode" and idx + 1 < len(cmdline):
            val = str(cmdline[idx + 1]).strip('"').strip("'").lower()
            return "live" if val == "live" else "demo"
        elif s.startswith("--trading-mode="):
            val = s.split("=", 1)[1].strip().lower()
            return "live" if val == "live" else "demo"
    return None


def is_bot_running(use_cache: bool = True, account: Optional[str] = None) -> Tuple[bool, Optional[int]]:
    """
    Verifica se o robô está ativo usando arquivos PID por conta (`bot_demo.pid`, `bot_live.pid`),
    fallback legado (`bot.pid`) e inspeção `psutil` com cache curto de 2.5s.
    """
    cache_key = normalize_account(account) if account else "any"
    now = _get_time_mod().monotonic()
    if use_cache:
        with _BOT_STATUS_CACHE_LOCK:
            entry = _BOT_STATUS_CACHE.get(cache_key)
            if entry is not None:
                ts, cached_running, cached_pid = entry
                if ts is not None and (now - ts) < 2.5:
                    return cached_running, cached_pid

    # 1. Verificação rápida via arquivos PID
    if account:
        acc = normalize_account(account)
        acc_pid_path = get_account_pid_path(acc)
        pid = get_running_bot_pid(acc_pid_path)
        if pid:
            with _BOT_STATUS_CACHE_LOCK:
                _BOT_STATUS_CACHE[cache_key] = (now, True, pid)
            return True, pid
        # Compatibilidade: legado bot.pid pode estar rodando paper/testnet (demo) ou live
        legacy_pid = get_running_bot_pid(PID_FILE_PATH)
        if legacy_pid:
            legacy_mode = _read_pid_mode(PID_FILE_PATH)
            legacy_acc = "live" if legacy_mode == "live" else "demo"
            if legacy_acc == acc:
                with _BOT_STATUS_CACHE_LOCK:
                    _BOT_STATUS_CACHE[cache_key] = (now, True, legacy_pid)
                return True, legacy_pid
    else:
        for path in (PID_FILE_PATH, get_account_pid_path("demo"), get_account_pid_path("live")):
            pid = get_running_bot_pid(path)
            if pid:
                with _BOT_STATUS_CACHE_LOCK:
                    _BOT_STATUS_CACHE[cache_key] = (now, True, pid)
                return True, pid

    # 2. Fallback via psutil
    found_pid: Optional[int] = None
    target_acc = normalize_account(account) if account else None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not any(
                os.path.basename(str(arg).strip('"').strip("'")).lower() in ("trading_bot.py", "supervisor.py")
                for arg in cmdline
            ):
                continue
            if target_acc is not None:
                proc_acc = _cmdline_account(cmdline) or "demo"
                if proc_acc != target_acc:
                    continue
            found_pid = proc.info["pid"]
            break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    running = found_pid is not None
    with _BOT_STATUS_CACHE_LOCK:
        _BOT_STATUS_CACHE[cache_key] = (now, running, found_pid)
    return running, found_pid


def _check_is_bot_running(*, use_cache: bool = True, account: Optional[str] = None) -> Tuple[bool, Optional[int]]:
    fn = _server_attr("is_bot_running", is_bot_running)
    try:
        return fn(use_cache=use_cache, account=account)
    except TypeError:
        try:
            return fn(use_cache=use_cache)
        except TypeError:
            return fn()


def get_all_accounts_bot_status(use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """Retorna o status simultâneo dos robôs da Conta Demo e da Conta Real."""
    demo_running, demo_pid = _check_is_bot_running(use_cache=use_cache, account="demo")
    live_running, live_pid = _check_is_bot_running(use_cache=use_cache, account="live")
    return {
        "demo": {"running": bool(demo_running), "pid": demo_pid},
        "live": {"running": bool(live_running), "pid": live_pid},
    }


def is_order_in_flight(account: Optional[str] = None) -> bool:
    db_path = str(_server_attr("DB_PATH", DEFAULT_DB_PATH))
    with sqlite_readonly_optional(db_path) as conn:
        if conn is None:
            return False
        try:
            if account in ("demo", "live") and table_exists(conn, "bot_account_state"):
                row = conn.execute(
                    "SELECT order_in_flight FROM bot_account_state WHERE account = ?",
                    (account,),
                ).fetchone()
                if row is not None:
                    return bool(row["order_in_flight"])
            if table_exists(conn, "bot_state"):
                row = conn.execute("SELECT order_in_flight FROM bot_state WHERE id = 1").fetchone()
                return bool(row and row["order_in_flight"])
            return False
        except Exception:
            return False


def _validate_live_account_start() -> None:
    """Valida todos os pré-requisitos de segurança antes de iniciar o robô na Conta Real (Dinheiro Real)."""
    import trading_bot

    bin_cfg = binance_client.load_binance_config()
    if not bin_cfg["has_keys"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "Conta Real bloqueada: configure sua API Key e API Secret de PRODUÇÃO da Binance "
                "no botão 'Conectar Binance' antes de iniciar o robô com dinheiro real."
            ),
        )
    if bin_cfg["testnet"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "Conta Real bloqueada: suas credenciais atuais estão marcadas como TESTNET (Sandbox). "
                "Selecione 'Produção (Live)' com chaves reais da Binance ou opere na Conta Demo."
            ),
        )
    if not getattr(trading_bot, "LIVE_TRADING_ENABLED", False):
        raise HTTPException(
            status_code=400,
            detail=(
                "Trava de segurança ativa: LIVE_TRADING_ENABLED = False em trading_bot.py. "
                "Valide o desempenho na Conta Demo, promova o Modelo Campeão e ative LIVE_TRADING_ENABLED = True "
                "quando estiver pronto para enviar ordens reais."
            ),
        )


def start_bot_process(
    *,
    symbol: str,
    timeframe: str,
    retrain: Optional[int] = None,
    trading_mode: Optional[str] = None,
    trade_amount: Optional[float] = None,
    account: Optional[str] = None,
) -> Dict[str, Any]:
    if not is_operational_timeframe(timeframe):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Timeframe '{timeframe}' bloqueado pela política operacional. "
                f"O sistema opera exclusivamente com gráficos de 1 hora ou superiores: {', '.join(OPERATIONAL_TIMEFRAMES)}."
            ),
        )
    if forex_market.is_forex_symbol(symbol):
        raise HTTPException(
            status_code=400,
            detail=(
                f"O robô autônomo Binance Spot opera apenas pares cripto (ex.: BTC/USDT). "
                f"'{symbol}' é monitorado automaticamente pelo Copiloto XTB / Forex IA."
            ),
        )

    eff_account = normalize_account(account) if account else None
    eff_mode = trading_mode
    if eff_account == "demo":
        eff_mode = eff_mode if eff_mode in ("paper", "testnet") else "paper"
        set_demo_manual_pause(False)
    elif eff_account == "live":
        eff_mode = "live"
        _validate_live_account_start()

    lock_key = eff_account or "default"
    start_lock = _ACCOUNT_START_LOCKS[lock_key]
    stop_path = get_account_stop_path(eff_account) if eff_account else STOP_SIGNAL_PATH

    with start_lock:
        invalidate_bot_status_cache(eff_account)
        running, pid = _check_is_bot_running(use_cache=False, account=eff_account)
        if running:
            acc_label = f" ({eff_account.upper()})" if eff_account else ""
            return {
                "status": "already_running",
                "pid": pid,
                "account": eff_account or "demo",
                "message": f"O robô{acc_label} já está ativo em segundo plano.",
            }
        last_mono = _LAST_BOT_START_MONO.get(lock_key)
        if last_mono is not None and (_get_time_mod().monotonic() - last_mono) < 4.0:
            return {
                "status": "starting",
                "pid": None,
                "account": eff_account or "demo",
                "message": "Inicialização do robô já em andamento.",
            }

        try:
            if os.path.exists(stop_path):
                try:
                    os.remove(stop_path)
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
            if eff_mode:
                cmd.extend(["--trading-mode", eff_mode])
            if trade_amount:
                cmd.extend(["--trade-amount", str(trade_amount)])
            if eff_account:
                cmd.extend(["--account", eff_account])

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
            threading.Thread(target=proc.wait, daemon=True).start()
            _LAST_BOT_START_MONO[lock_key] = _get_time_mod().monotonic()
            invalidate_bot_status_cache(eff_account)
            acc_tag = "CONTA REAL (LIVE)" if eff_account == "live" else ("CONTA DEMO (TREINO IA)" if eff_account == "demo" else "")
            prefix = f"Robô {acc_tag}" if acc_tag else "Robô"
            return {
                "status": "started",
                "pid": proc.pid,
                "account": eff_account or ("live" if eff_mode == "live" else "demo"),
                "trading_mode": eff_mode or "paper",
                "message": f"{prefix} iniciado com sucesso (PID {proc.pid}) em {symbol} ({timeframe}).",
            }
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="Erro interno ao iniciar o processo do robô.")


def stop_bot_process(account: Optional[str] = None) -> Dict[str, Any]:
    """Sinaliza parada limpa via `bot_{account}.stop` (ou legado `bot.stop`), protegendo `order_in_flight == 1`."""
    eff_account = normalize_account(account) if account else None
    if eff_account == "demo":
        set_demo_manual_pause(True)
    lock_key = eff_account or "default"
    start_lock = _ACCOUNT_START_LOCKS[lock_key]
    stop_path = get_account_stop_path(eff_account) if eff_account else STOP_SIGNAL_PATH

    if not start_lock.acquire(blocking=False):
        return {"status": "stopping", "account": eff_account, "message": "Operação de controle do robô já está em andamento."}
    try:
        invalidate_bot_status_cache(eff_account)
        running, pid = _check_is_bot_running(use_cache=False, account=eff_account)
        if not running or not pid:
            return {"status": "not_running", "account": eff_account, "message": "O robô não está em execução."}

        # Se o PID estiver no arquivo legado `bot.pid`, escreve também no STOP_SIGNAL_PATH legado
        stop_paths = [stop_path]
        if eff_account and get_running_bot_pid(PID_FILE_PATH) == pid and STOP_SIGNAL_PATH not in stop_paths:
            stop_paths.append(STOP_SIGNAL_PATH)

        p: Optional[psutil.Process] = None
        orig_create_time: Optional[float] = None
        try:
            for sp in stop_paths:
                request_bot_stop(sp)
            p = psutil.Process(pid)
            orig_create_time = p.create_time()
            for _ in range(180):
                if not p.is_running():
                    invalidate_bot_status_cache(eff_account)
                    return {"status": "stopped", "account": eff_account, "message": f"Robô (PID {pid}) encerrado de forma limpa."}
                time.sleep(0.25)

            if is_order_in_flight(account=eff_account):
                return {
                    "status": "order_in_flight",
                    "account": eff_account,
                    "message": "Há uma ordem em andamento na exchange. A parada limpa foi solicitada e aguardará a conclusão.",
                }

            for sp in stop_paths:
                request_bot_stop(sp)
            if is_pid_alive_bot(pid, expected_create_time=orig_create_time):
                p.terminate()
                p.wait(timeout=3.0)
            invalidate_bot_status_cache(eff_account)
            return {"status": "stopped", "account": eff_account, "message": f"Robô (PID {pid}) encerrado com sucesso."}
        except psutil.NoSuchProcess:
            invalidate_bot_status_cache(eff_account)
            return {"status": "stopped", "account": eff_account, "message": f"Robô (PID {pid}) encerrado com sucesso."}
        except Exception:
            if is_order_in_flight(account=eff_account):
                return {
                    "status": "order_in_flight",
                    "account": eff_account,
                    "message": "Ordem em andamento; encerramento forçado abortado por segurança.",
                }
            try:
                for sp in stop_paths:
                    request_bot_stop(sp)
                if p is not None and is_pid_alive_bot(pid, expected_create_time=orig_create_time):
                    p.kill()
                    try:
                        p.wait(timeout=2.0)
                    except Exception:
                        pass
                invalidate_bot_status_cache(eff_account)
                return {"status": "killed", "account": eff_account, "message": f"Processo forçado a fechar (PID {pid})."}
            except Exception:
                raise HTTPException(status_code=500, detail="Falha ao parar o processo do robô.")
    finally:
        start_lock.release()
