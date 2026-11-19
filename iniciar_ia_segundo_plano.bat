@echo off
chcp 65001 >nul
echo =========================================================
echo    Iniciando Quant Trading Bot em Segundo Plano (24/7)
echo =========================================================

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
    set "PYW_EXE=.venv\Scripts\pythonw.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
    set "PYW_EXE=venv\Scripts\pythonw.exe"
) else (
    set "PY_EXE=python"
    set "PYW_EXE=pythonw"
)

"%PY_EXE%" -c "import sys,trading_bot as tb; pid=tb.get_running_bot_pid(); (print(f'[AVISO] O robô já está rodando (PID {pid}).'), sys.exit(1)) if pid else sys.exit(0)"
if %ERRORLEVEL% NEQ 0 (
    pause
    exit /b 1
)

echo O robô continuará aprendendo e operando mesmo se você fechar esta janela.
echo Todos os registros são salvos em 'trading_bot.log' e 'trading_data.db'.
echo.

start "" "%PYW_EXE%" trading_bot.py

echo [OK] Robô iniciado com sucesso em segundo plano!
echo Para acompanhar o robô ao vivo, execute: ver_logs.bat
echo Para parar o robô a qualquer momento, execute: parar_ia.bat
echo.
pause
