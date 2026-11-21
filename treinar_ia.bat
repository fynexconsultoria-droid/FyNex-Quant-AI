@echo off
chcp 65001 >nul
echo =========================================================
echo       FyNex IA - Treinamento Histórico Profundo (5m)
echo =========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

echo Baixando 5000 candles de 5m da Binance e treinando o melhor modelo...
echo.
"%PY_EXE%" train_ai.py --timeframe 5m --candles 5000
echo.
pause
