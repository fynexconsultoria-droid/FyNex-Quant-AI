@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo =========================================================
echo             Parando a IA em Segundo Plano
echo =========================================================

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" -c "import os,time,psutil,trading_bot as tb; pid=tb.get_running_bot_pid(); tb.request_bot_stop(); print('Robô não está em execução (sinal bot.stop gravado para encerrar eventual supervisor).' if not pid else f'Enviando sinal de parada limpa ao PID {pid}...'); [time.sleep(0.5) for _ in range(60) if pid and tb.is_pid_alive_bot(pid)]; in_flight=(tb.TradingDatabase().is_order_in_flight() if (pid and os.path.exists(tb.DEFAULT_DB_PATH)) else False); pid2=tb.get_running_bot_pid(); print('Ordem em andamento; aguardando encerramento seguro sem forçar.' if (pid2 and in_flight) else ('Robô encerrado de forma limpa.' if not pid2 else (psutil.Process(pid2).terminate() if tb.is_pid_alive_bot(pid2) else None, f'Processo encerrado: PID {pid2}')[1]))"

echo.
echo Verificação concluída.
pause
