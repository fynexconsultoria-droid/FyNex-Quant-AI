@echo off
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

"%PY_EXE%" -c "import time,psutil,trading_bot as tb; pid=tb.get_running_bot_pid(); print('Robô não está em execução.' if not pid else f'Enviando sinal de parada limpa ao PID {pid}...'); tb.request_bot_stop() if pid else None; [time.sleep(0.5) for _ in range(60) if pid and tb.is_pid_alive_bot(pid)]; db=tb.TradingDatabase(); in_flight=db.is_order_in_flight() if pid else False; pid2=tb.get_running_bot_pid(); print('Ordem em andamento; aguardando encerramento seguro sem forçar.' if (pid2 and in_flight) else ('Robô encerrado de forma limpa.' if not pid2 else (psutil.Process(pid2).terminate(), f'Processo encerrado: PID {pid2}')[1]))"

echo.
echo Verificação concluída.
pause
