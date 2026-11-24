# FyNex Quant AI

Robô de trading quantitativo para criptomoedas (**Binance Spot**) e monitoramento multi-ativos (**Forex, Metais, Commodities e Índices / Copiloto XTB**) com Machine Learning contínuo (Triple Barrier + validação walk-forward com purge + calibração Platt + auto-análise de erros), gestão de posição/risco com proteção OCO e dashboard web em tempo real.

## Arquitetura Modular

```text
IA FyNex/
├── core/                                # Núcleo de domínio e infraestrutura modularizada
│   ├── infra/                           # Infraestrutura compartilhada
│   │   ├── sqlite.py                    # Conexões WAL, PRAGMAs, migrações idempotentes e SchemaRegistry
│   │   ├── env_store.py                 # Leitura/escrita atômica do .env sob lock global + scrubbing
│   │   ├── cache.py                     # TTLCache thread-safe (LRU) e CooldownTracker
│   │   └── utils.py                     # Conversões finitas, timestamps UTC, tail_lines e tokens seguros
│   ├── ml/                              # Pipeline de Machine Learning
│   │   ├── features.py                  # 22 indicadores estacionários invariantes à escala
│   │   ├── labeling.py                  # Rotulagem Triple Barrier (fixa ou ATR) sem look-ahead
│   │   ├── calibration.py               # PlattCalibratedModel, DynamicCalibrator e limiar ótimo OOF
│   │   ├── persistence.py               # ModelPersistence atômico com schema hash e poda de versões
│   │   └── training.py                  # RF/ET/HGB/Ensemble, walk-forward com purge e promoção
│   ├── trading/                         # Gestão de posições, risco e proteção de lucro
│   │   ├── models.py                    # Dataclass Position
│   │   ├── store.py                     # PositionStore (SQLite)
│   │   ├── risk.py                      # RiskManager (limites diários e kill-switch STOP)
│   │   ├── profit_protection.py         # Breakeven + Trailing compartilhado entre live e backtest
│   │   └── position_manager.py          # Orquestração Paper/Testnet/Live, reconciliação e OCO
│   ├── exchange/                        # Cliente Binance Spot (CCXT)
│   │   ├── config.py                    # Credenciais .env e resolve_environment (paper/testnet/live)
│   │   ├── connection.py                # Pool CCXT, failover de mirrors Spot, telemetria e health check
│   │   ├── market_data.py               # Ticker, order book, candles ao vivo e saldo em cache
│   │   └── execution.py                 # Preflight, idempotência, reconciliação, ordens Spot e OCO
│   ├── notifications/                   # Alertas operacionais unificados
│   │   ├── telegram.py                  # Canal Telegram único (alertas + Copiloto XTB)
│   │   ├── whatsapp.py                  # Canal WhatsApp CallMeBot com normalização E.164 e rate limit
│   │   └── daily_summary.py             # Despacho unificado, deduplicação e resumo diário agendado
│   ├── markets/                         # Catálogo multi-ativos, feed Forex/Commodities e mesa XTB
│   │   ├── catalog.py                   # MARKET_INSTRUMENTS (Cripto, Forex, Metais, Energia, Índices)
│   │   ├── yahoo_feed.py                # Cotações, candles e watchlist paralela com TTLCache
│   │   └── xtb_desk.py                  # Simulador da mesa XTB (XTBDeskStore)
│   ├── error_learning/                  # Motor de auto-análise de erros e aprendizado adaptativo
│   │   ├── detectors.py                 # Catálogo de causas-raiz e autópsia multi-parâmetro
│   │   ├── store.py                     # ErrorLearningStore (snapshots, post-mortems e travas)
│   │   └── weighting.py                 # Hard-Example Weighting aplicado apenas no refit final
│   ├── database.py                      # TradingDatabase (candles, previsões, trades e bot_state)
│   ├── market_data.py                   # Coleta OHLCV, backfill paginado e segmentos contíguos
│   ├── entry_guard.py                   # Guardião conservador de entradas e position sizing defensivo
│   ├── macro_intelligence.py            # DXY, US10Y, S&P500, Fear & Greed, Radar Global e evolução IA
│   ├── news_feed.py                     # Agregador RSS com classificação de categoria e sentimento
│   ├── forex_ai_trader.py               # Scanner autônomo Forex/Commodities com confluência técnica+macro
│   ├── xtb_copilot.py                   # Sinais prontos para xStation 5 (XTBCopilotStore)
│   ├── ai_chat.py                       # Assistente conversacional local (métricas 24h / 7d / 30d)
│   ├── logging_setup.py                 # Caminhos base, RotatingFileHandler Windows-safe e hooks
│   └── process.py                       # Lock atômico de instância única (bot.pid) e parada (bot.stop)
├── dashboard/                           # Painel Web em Tempo Real (FastAPI + ES Modules)
│   ├── server.py                        # Entrypoint enxuto FastAPI + injeção de CSRF
│   ├── security.py                      # Host/DNS Rebinding, Origin, Basic Auth fail-closed, CSP e CSRF
│   ├── schemas.py                       # Payloads Pydantic e padrões de validação
│   ├── bot_controller.py                # Controle do processo do robô com proteção order_in_flight
│   ├── training_jobs.py                 # Worker em background para treinamento via dashboard
│   ├── routers/                         # Endpoints por domínio (status, market, binance, xtb, ai, alerts, bot)
│   └── static/                          # Frontend modular (sem bundler, 100% compatível com CSP)
│       ├── index.html                   # Layout principal (carrega /static/js/main.js)
│       ├── style.css                    # Manifesto CSS importando os módulos de css/
│       ├── css/                         # 7 folhas de estilo por área
│       └── js/                          # 12 ES Modules (state, utils, chart, stream, terminal, etc.)
├── tests/                               # Suíte de testes automatizados (pytest)
├── trading_bot.py                       # Orquestrador principal e CLI do robô
├── train_ai.py                          # Treinamento histórico profundo e seleção de arquitetura
├── backtest.py                          # Backtest walk-forward com custos reais e paridade live
├── supervisor.py                        # Supervisor de processo com backoff exponencial
├── strategy_config.py                   # Perfis de estratégia por timeframe (TP, SL, ATR, horizonte)
├── binance_client.py                    # Facade de compatibilidade -> core.exchange
├── position_manager.py                  # Facade de compatibilidade -> core.trading
├── alerts.py                            # Facade de compatibilidade -> core.notifications
├── iniciar_ia_segundo_plano.bat         # Inicia o robô supervisionado em segundo plano (Windows)
├── iniciar_dashboard.bat                # Inicia o dashboard em http://localhost:8000
├── parar_ia.bat                         # Solicita parada limpa do robô
├── treinar_ia.bat                       # Executa treinamento histórico profundo
├── ver_logs.bat                         # Acompanha os logs ao vivo
├── .env.example                         # Modelo de variáveis de ambiente
├── requirements.txt                     # Dependências de produção
└── requirements-dev.txt                 # Dependências de desenvolvimento e testes
```

## Instalação

```bash
pip install -r requirements.txt
# Para rodar os testes:
pip install -r requirements-dev.txt
```

Copie `.env.example` para `.env` e ajuste as variáveis desejadas (por padrão o robô opera em `TRADING_MODE=paper`, com a trava `LIVE_TRADING_ENABLED = False` ativa por segurança).

## Uso Rápido

- **Treinar a IA com histórico longo (5.000 candles):** execute `treinar_ia.bat` ou `python train_ai.py --timeframe 5m --candles 5000`
- **Iniciar o robô em segundo plano:** execute `iniciar_ia_segundo_plano.bat` ou `python supervisor.py`
- **Iniciar o robô no terminal:** `python trading_bot.py --timeframe 5m --symbol BTC/USDT`
- **Abrir o dashboard:** execute `iniciar_dashboard.bat` ou `python -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000`
- **Acompanhar logs:** execute `ver_logs.bat`
- **Parar o robô com segurança:** execute `parar_ia.bat`
- **Executar backtest:** `python backtest.py`
- **Executar testes:** `pytest`
