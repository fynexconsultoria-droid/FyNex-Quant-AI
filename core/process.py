"""
Controle de Instância Única (PID File) e Sinalização de Parada Limpa (FyNex Quant AI)
=====================================================================================
Garante execução exclusiva do robô com lock atômico (`O_CREAT | O_EXCL`) e
desligamento cooperativo via arquivo `bot.stop`.
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from core.logging_setup import PID_FILE_PATH, STOP_SIGNAL_PATH


def is_pid_alive_bot(pid: int, expected_create_time: Optional[float] = None) -> bool:
    """
    Verifica se `pid` pertence a um processo Python ativo rodando `trading_bot.py`.
    Quando `expected_create_time` é fornecido, valida também contra reuso de PID no Windows.
    Em `AccessDenied` assume processo vivo por segurança para não apagar lock ativo.
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        if expected_create_time is not None:
            try:
                if abs(float(proc.create_time()) - float(expected_create_time)) > 2.0:
                    return False
            except psutil.AccessDenied:
                return True
        name = (proc.name() or "").lower()
        if "python" not in name:
            return False
        cmdline = proc.cmdline() or []
        return any(os.path.basename(str(arg).strip('"').strip("'")).lower() == "trading_bot.py" for arg in cmdline)
    except Exception as exc:
        if exc.__class__.__name__ == "AccessDenied":
            return True
        return False


def _resolve_is_pid_alive():
    """Permite monkeypatch transparente de `trading_bot.is_pid_alive_bot` nos testes."""
    tb_mod = sys.modules.get("trading_bot")
    if tb_mod is not None:
        return getattr(tb_mod, "is_pid_alive_bot", is_pid_alive_bot)
    return is_pid_alive_bot


def _call_is_pid_alive(pid: int, expected_create_time: Optional[float] = None) -> bool:
    fn = _resolve_is_pid_alive()
    if expected_create_time is not None:
        try:
            return bool(fn(pid, expected_create_time=expected_create_time))
        except TypeError:
            return bool(fn(pid))
    return bool(fn(pid))


def get_running_bot_pid(pid_path: str = PID_FILE_PATH) -> Optional[int]:
    """Lê o PID do arquivo `bot.pid`, confirma que o processo está vivo e limpa arquivos corrompidos/órfãos."""
    if not os.path.exists(pid_path):
        return None
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        create_time = data.get("create_time")
    except Exception:
        # Remove lock truncado/corrompido (com pequena folga de 2s para escrita concorrente recém-criada)
        try:
            import time as _time
            if (_time.time() - os.path.getmtime(pid_path)) > 2.0:
                os.remove(pid_path)
        except OSError:
            pass
        return None
    if _call_is_pid_alive(pid, expected_create_time=create_time):
        return pid
    try:
        os.remove(pid_path)
    except OSError:
        pass
    return None


def acquire_single_instance_lock(
    symbol: str,
    timeframe: str,
    trading_mode: str,
    pid_path: str = PID_FILE_PATH,
) -> bool:
    """
    Garante que apenas uma instância do robô rode por vez usando criação exclusiva
    (`O_CREAT | O_EXCL`) e `create_time` contra reuso de PID.
    """
    my_pid = os.getpid()
    my_create_time: Optional[float] = None
    try:
        import psutil
        my_create_time = float(psutil.Process(my_pid).create_time())
    except Exception:
        my_create_time = None

    payload: dict = {
        "pid": my_pid,
        "symbol": symbol,
        "timeframe": timeframe,
        "trading_mode": trading_mode,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if my_create_time is not None:
        payload["create_time"] = my_create_time
    payload_bytes = json.dumps(payload, indent=2).encode("utf-8")

    for _ in range(3):
        try:
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload_bytes)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            existing_pid = get_running_bot_pid(pid_path)
            if existing_pid == my_pid:
                return True
            if existing_pid and existing_pid != my_pid:
                return False
            continue
    return False


def release_single_instance_lock(pid_path: str = PID_FILE_PATH) -> None:
    if not os.path.exists(pid_path):
        return
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("pid", -1)) == os.getpid():
            os.remove(pid_path)
    except Exception:
        pass


def request_bot_stop(stop_path: str = STOP_SIGNAL_PATH) -> None:
    with open(stop_path, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def consume_stop_signal(stop_path: str = STOP_SIGNAL_PATH) -> bool:
    if os.path.exists(stop_path):
        try:
            os.remove(stop_path)
        except OSError:
            pass
        return True
    return False
