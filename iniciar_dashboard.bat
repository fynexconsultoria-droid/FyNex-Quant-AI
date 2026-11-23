@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo =========================================================
echo       Iniciando FyNex IA - Quant Trading Dashboard
echo =========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

echo Abrindo o painel no seu navegador (http://127.0.0.1:8000)...
echo.

start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8000"
"%PY_EXE%" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
pause
