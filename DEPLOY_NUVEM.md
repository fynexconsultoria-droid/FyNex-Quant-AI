# Deploy 24/7 na Nuvem (Railway ou Render) — FyNex Quant AI

Este projeto está 100% pronto para rodar 24/7 na nuvem (mesmo com seu PC desligado), executando o **Dashboard Web + IA Ultra-Segura (`1h`)** continuamente.

> **IMPORTANTE (Binance Geo-Block):** Nunca escolha servidores nos **Estados Unidos (US)**, pois a Binance bloqueia IPs americanos (`HTTP 451`). Escolha sempre **Europa (`Frankfurt` / `Amsterdam` / `EU West`)** ou **Ásia (`Singapore`)**.

---

## Passo 1 — Subir o código para um repositório privado no GitHub

1. Crie um repositório **Privado** no GitHub (ex.: `fynex-quant-ai`).
2. No terminal dentro da pasta do projeto:
   ```powershell
   git remote add origin https://github.com/SEU_USUARIO/fynex-quant-ai.git
   git push -u origin master
   ```
   *(Seu `.env` e senhas locais estão protegidos pelo `.gitignore` e NÃO serão enviados ao GitHub.)*

---

## Passo 2A — Deploy no Railway (Mais rápido)

1. Acesse [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → selecione `fynex-quant-ai`.
2. Em **Settings → Region**, selecione **Europe (EU West / Amsterdam)**.
3. Em **Variables**, adicione:
   - `FYNEX_DASHBOARD_USER` = `admin`
   - `FYNEX_DASHBOARD_PASSWORD` = *(crie uma senha forte para proteger o acesso ao seu dashboard!)*
   - `FYNEX_AUTOSTART_BOT` = `true`
   - `FYNEX_BOT_TIMEFRAME` = `1h`
   - `TRADING_MODE` = `paper` *(ou `testnet` se quiser testar com chaves Testnet)*
   - *(Opcional)* `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_TESTNET`
4. Em **Settings → Networking**, clique em **Generate Domain** — o sistema detecta `RAILWAY_PUBLIC_DOMAIN` automaticamente!
5. Acesse a URL gerada no seu celular ou PC com o usuário e senha que você definiu.

---

## Passo 2B — Deploy no Render (Já configurado em `render.yaml`)

1. Acesse [render.com](https://render.com) → **New + → Blueprint**.
2. Conecte seu repositório `fynex-quant-ai`.
3. O arquivo `render.yaml` já configura automaticamente:
   - Região **Frankfurt (Europa)** (sem bloqueio da Binance)
   - Docker com `start_cloud.py` (Dashboard + IA `1h`)
   - Senha segura gerada automaticamente (`FYNEX_DASHBOARD_PASSWORD` — veja o valor na aba **Environment** do Render).
4. Clique em **Apply** e aguarde o build finalizar (~2–3 min).
