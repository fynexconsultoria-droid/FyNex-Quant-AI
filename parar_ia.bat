@echo off
chcp 65001 >nul
echo =========================================================
echo             Parando a IA em Segundo Plano
echo =========================================================

powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*trading_bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Processo encerrado: PID ' + $_.ProcessId) }"

echo.
echo Verificação concluída.
pause
