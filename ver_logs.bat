@echo off
chcp 65001 >nul
echo =========================================================
echo       Acompanhamento ao Vivo dos Logs da IA
echo =========================================================
echo Pressione Ctrl + C para sair desta visualização.
echo (O robô continuará rodando normalmente em segundo plano).
echo.
powershell -Command "if (Test-Path trading_bot.log) { Get-Content trading_bot.log -Wait -Tail 35 } else { Write-Host 'Arquivo trading_bot.log ainda não gerado. Inicie o robô primeiro.' }"
