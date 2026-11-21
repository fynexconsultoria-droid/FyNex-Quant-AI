@echo off
chcp 65001 >nul
echo =========================================================
echo       Acompanhamento ao Vivo dos Logs da IA
echo =========================================================
echo Pressione Ctrl + C para sair desta visualização.
echo (O robô continuará rodando normalmente em segundo plano).
echo.
powershell -Command "if (Test-Path logs\trading_bot.log) { Get-Content logs\trading_bot.log -Encoding UTF8 -Wait -Tail 35 } elseif (Test-Path trading_bot.log) { Get-Content trading_bot.log -Encoding UTF8 -Wait -Tail 35 } else { Write-Host 'Arquivo de log ainda não gerado. Inicie o robô primeiro.' }"
