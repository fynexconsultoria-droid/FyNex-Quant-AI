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

"%PY_EXE%" -c "import os,time,trading_bot as tb; pid=tb.get_running_bot_pid(); print('Robô não está em execução.' if not pid else f'Enviando sinal de parada limpa ao PID {pid}...'); tb.request_bot_stop() if pid else None"
timeout /t 3 /nobreak >nul
"%PY_EXE%" -c "import psutil,trading_bot as tb; pid=tb.get_running_bot_pid(); (psutil.Process(pid).terminate(), print(f'Processo encerrado: PID {pid}')) if pid else print('Robô encerrado de forma limpa.')"

echo.
echo Verificação concluída.
pause
