# FyNex Quant AI

Robô de trading quantitativo para criptomoedas (Binance Spot) com Machine Learning contínuo (Triple Barrier + validação walk-forward com purge + calibração Platt), gestão de risco/posição com proteção OCO e dashboard web em tempo real.

## Estrutura do Projeto

```text
IA FyNex/
├── core/                         # Núcleo interno modularizado
│   ├── logging_setup.py          # Caminhos base, logging rotativo e hooks de exceção
│   ├── database.py               # Persistência SQLite (WAL): candles, previsões, trades e bot_state
│   ├── market_data.py            # Coleta OHLCV (Binance/CCXT), backfill paginado e segmentos contíguos
│   ├── ml.py                     # Features estacionárias, Triple Barrier, treino, calibração e modelos
│   └── process.py                # Lock atômico de instância única (bot.pid) e parada limpa (bot.stop)
├── dashboard/                    # Painel web em tempo real (FastAPI + UI)
│   ├── server.py                 # API REST segura (Host/Origin/CSP/CSRF)
│   └── static/                   # Frontend (index.html, app.js, style.css)
├── tests/                        # Suíte de testes automatizados (pytest)
├── trading_bot.py                # Orquestrador principal e CLI do robô
├── supervisor.py                 # Supervisor com reinício automático e backoff exponencial
├── position_manager.py           # Gestão de posições, risco diário, kill-switch e OCO
├── binance_client.py             # Cliente Binance (CCXT), credenciais (.env) e ordens Spot/OCO
├── strategy_config.py            # Perfis de estratégia por timeframe (TP, SL, horizonte, confiança)
├── backtest.py                   # Backtest walk-forward com taxas e slippage reais
├── alerts.py                     # Alertas operacionais via Telegram
├── iniciar_ia_segundo_plano.bat  # Inicia o robô supervisionado em segundo plano (Windows)
├── iniciar_dashboard.bat         # Inicia o dashboard em http://localhost:8000
├── parar_ia.bat                  # Solicita parada limpa do robô
├── ver_logs.bat                  # Acompanha os logs ao vivo
├── .env.example                  # Modelo de variáveis de ambiente
├── requirements.txt              # Dependências de produção
└── requirements-dev.txt          # Dependências de desenvolvimento e testes
```

## Instalação

```bash
pip install -r requirements.txt
# Para rodar os testes:
pip install -r requirements-dev.txt
```

Copie `.env.example` para `.env` e ajuste as variáveis desejadas (por padrão o robô opera em `TRADING_MODE=paper`).

## Uso Rápido

- **Treinar a IA com histórico longo (5.000 candles):** execute `treinar_ia.bat` ou `python train_ai.py --timeframe 5m --candles 5000`
- **Iniciar o robô em segundo plano:** execute `iniciar_ia_segundo_plano.bat` ou `python supervisor.py`
- **Iniciar o robô no terminal:** `python trading_bot.py --timeframe 5m --symbol BTC/USDT`
- **Abrir o dashboard:** execute `iniciar_dashboard.bat` ou `python -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000`
- **Acompanhar logs:** execute `ver_logs.bat`
- **Parar o robô com segurança:** execute `parar_ia.bat`
- **Executar backtest:** `python backtest.py`
- **Executar testes:** `pytest`
