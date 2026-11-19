@echo off
chcp 65001 >nul
echo =========================================================
echo    Iniciando Quant Trading Bot em Segundo Plano (24/7)
echo =========================================================
echo O robô continuará aprendendo e operando mesmo se você fechar esta janela.
echo Todos os registros são salvos em 'trading_bot.log' e 'trading_data.db'.
echo.

start "" pythonw trading_bot.py

echo [OK] Robô iniciado com sucesso em segundo plano!
echo Para acompanhar o robô ao vivo, execute: ver_logs.bat
echo Para parar o robô a qualquer momento, execute: parar_ia.bat
echo.
pause
