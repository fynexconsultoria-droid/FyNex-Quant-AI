#!/usr/bin/env bash
set -euo pipefail

echo "================================================================"
echo "  FyNex Quant AI - Instalacao 24/7 na Oracle Cloud (Ubuntu)"
echo "================================================================"

# 1. Instala Docker e Docker Compose Plugin se ainda nao estiverem instalados
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/5] Instalando Docker..."
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
else
  echo "[1/5] Docker ja instalado."
fi

# 2. Abre porta 8000 no firewall interno da VM Ubuntu (Oracle usa iptables)
echo "[2/5] Liberando porta 8000 no firewall da VM..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT || true
if command -v netfilter-persistent >/dev/null 2>&1; then
  sudo netfilter-persistent save || true
fi

# 3. Prepara arquivos e pastas persistentes
echo "[3/5] Criando arquivos persistentes (trading_data.db, models/, logs/)..."
mkdir -p models logs
touch trading_data.db

PUBLIC_IP=$(curl -4 -s https://ifconfig.me || echo "")

if [ ! -f .env ] || ! grep -q "FYNEX_DASHBOARD_PASSWORD=" .env; then
  echo "[4/5] Configurando acesso seguro (.env)..."
  read -r -p "Usuario do Dashboard [admin]: " DASH_USER
  DASH_USER=${DASH_USER:-admin}
  read -r -s -p "Crie uma SENHA forte para o Dashboard: " DASH_PASS
  echo ""
  if [ -z "$DASH_PASS" ]; then
    echo "ERRO: A senha nao pode ficar vazia em servidor na nuvem."
    exit 1
  fi

  cat > .env <<EOF
# FyNex Quant AI - Oracle Cloud 24/7
TRADING_MODE=paper
BINANCE_TESTNET=false
BINANCE_API_KEY=
BINANCE_API_SECRET=
TRADE_AMOUNT_USDT=50.00
MAX_DAILY_LOSS_USDT=20.00
MAX_TRADES_PER_DAY=10
MAX_TRADE_AMOUNT_USDT=200.00
USE_EXCHANGE_OCO=true

# Seguranca do Dashboard na Nuvem
FYNEX_DASHBOARD_USER=${DASH_USER}
FYNEX_DASHBOARD_PASSWORD=${DASH_PASS}
FYNEX_PUBLIC_HOSTS=${PUBLIC_IP},${PUBLIC_IP}:8000
EOF
  chmod 600 .env
else
  echo "[4/5] Arquivo .env ja configurado."
fi

# 5. Constroi e sobe o container em segundo plano (24/7)
echo "[5/5] Construindo e iniciando FyNex Quant AI 24/7..."
sudo docker compose up -d --build

echo ""
echo "================================================================"
echo "  SUCESSO! FyNex Quant AI rodando 24/7 na Oracle Cloud!"
if [ -n "$PUBLIC_IP" ]; then
  echo "  Acesse no navegador: http://${PUBLIC_IP}:8000"
else
  echo "  Acesse no navegador: http://<IP_PUBLICO_DA_VM>:8000"
fi
echo "  Ver logs ao vivo:   sudo docker compose logs -f"
echo "  Atualizar codigo:   git pull && sudo docker compose up -d --build"
echo "================================================================"
