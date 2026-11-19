@echo off
chcp 65001 >nul
echo =========================================================
echo       Iniciando FyNex IA - Quant Trading Dashboard
echo =========================================================
echo.
echo Abrindo o painel no seu navegador (http://localhost:8000)...
echo.

start http://localhost:8000
python -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
pause
