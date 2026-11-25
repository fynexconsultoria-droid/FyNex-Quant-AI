@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo =========================================================
echo   Reiniciando FyNex Quant AI (Servidor + Robo 24/7)
echo   Sem afetar dados, historico ou posicoes abertas
echo =========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" reiniciar_sistema.py --timeframe 1h
echo.
if %ERRORLEVEL% EQU 0 (
    echo [OK] Sistema reiniciado com seguranca! Painel: http://127.0.0.1:8000
) else (
    echo [AVISO] Verifique os logs em logs\dashboard.log e logs\trading_bot.log
)
pause
