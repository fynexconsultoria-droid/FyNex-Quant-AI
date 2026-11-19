"""
Módulo de Integração com a API Binance (FyNex Quant AI)
======================================================
Responsável pelo gerenciamento de credenciais, conexão segura via CCXT,
consulta de saldos reais da carteira Spot, validação de chaves e execução de ordens
reais (Live) ou simuladas (Paper/Testnet).
"""

import os
import re
import logging
from typing import Dict, Any, Optional, Tuple, List
import ccxt
from dotenv import dotenv_values

logger = logging.getLogger("BinanceClient")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(BASE_DIR, ".env.example")


def load_binance_config() -> Dict[str, Any]:
    """
    Carrega as variáveis de configuração do arquivo .env com valores padrão seguros.
    """
    env_data = {}
    if os.path.exists(ENV_PATH):
        env_data = dotenv_values(ENV_PATH)

    api_key = (env_data.get("BINANCE_API_KEY") or os.environ.get("BINANCE_API_KEY") or "").strip()
    api_secret = (env_data.get("BINANCE_API_SECRET") or os.environ.get("BINANCE_API_SECRET") or "").strip()
    
    testnet_val = (env_data.get("BINANCE_TESTNET") or os.environ.get("BINANCE_TESTNET") or "false").strip().lower()
    testnet = testnet_val in ["true", "1", "yes", "sim"]

    trading_mode = (env_data.get("TRADING_MODE") or os.environ.get("TRADING_MODE") or "paper").strip().lower()
    if trading_mode not in ["paper", "testnet", "live"]:
        trading_mode = "paper"

    try:
        trade_amount_usdt = float(env_data.get("TRADE_AMOUNT_USDT") or os.environ.get("TRADE_AMOUNT_USDT") or 50.0)
    except ValueError:
        trade_amount_usdt = 50.0

    # Chave mascarada para segurança no frontend
    masked_key = ""
    if api_key:
        if len(api_key) > 8:
            masked_key = f"{api_key[:4]}...{api_key[-4:]}"
        else:
            masked_key = "****"

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "api_key_masked": masked_key,
        "has_keys": bool(api_key and api_secret),
        "testnet": testnet,
        "trading_mode": trading_mode,
        "trade_amount_usdt": trade_amount_usdt,
        "env_path": ENV_PATH
    }


def save_binance_config(
    api_key: str,
    api_secret: str,
    testnet: bool,
    trading_mode: str,
    trade_amount_usdt: float
) -> bool:
    """
    Salva as configurações com segurança no arquivo .env.
    Se a secret não for passada (ou for mantida como máscara), preserva a existente.
    """
    current = load_binance_config()
    final_key = api_key.strip() if api_key else current["api_key"]
    
    # Se a secret informada for asteriscos ou vazia, mantém a que já estava salva
    final_secret = api_secret.strip() if api_secret and not api_secret.startswith("*") else current["api_secret"]
    
    final_testnet = bool(testnet)
    final_mode = trading_mode.lower() if trading_mode in ["paper", "testnet", "live"] else "paper"
    final_amount = max(10.0, float(trade_amount_usdt))

    content = f"""# ==============================================================================
# FyNex Quant AI - Configurações de Conexão com a Binance
# ==============================================================================

# Credenciais de API (Mantenha em sigilo absoluto!)
BINANCE_API_KEY={final_key}
BINANCE_API_SECRET={final_secret}

# Ambiente:
# true  -> Binance Spot Testnet (testnet.binance.vision - testes sem dinheiro real)
# false -> Binance Spot Produção (api.binance.com - conta real)
BINANCE_TESTNET={'true' if final_testnet else 'false'}

# Modo de Operação do Robô:
# paper   -> Simulação Quantitativa (dados reais, mas ordens virtuais sem risco)
# testnet -> Execução de ordens na rede de testes da Binance (Spot Testnet)
# live    -> EXECUÇÃO DE ORDENS REAIS na conta Spot da Binance com fundos reais
TRADING_MODE={final_mode}

# Gestão de Risco: Valor em USDT alocado para cada compra realizada pela IA
TRADE_AMOUNT_USDT={final_amount:.2f}
"""

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    return True


def get_binance_exchange(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    testnet: bool = False,
    timeout: int = 15000
) -> ccxt.binance:
    """
    Instancia e configura o cliente ccxt.binance com tratamento de timezone e limites.
    """
    config: Dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": timeout,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
            "recvWindow": 10000
        }
    }

    if api_key and api_secret:
        config["apiKey"] = api_key.strip()
        config["secret"] = api_secret.strip()

    exchange = ccxt.binance(config)
    if testnet:
        exchange.set_sandbox_mode(True)

    return exchange


def test_binance_connection(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    testnet: bool = False
) -> Dict[str, Any]:
    """
    Testa a conexão autenticada com a Binance, recupera os saldos da carteira Spot
    e valida as permissões de trading.
    """
    # Se não foram fornecidas chaves nos argumentos, carrega do .env
    if not api_key or not api_secret:
        cfg = load_binance_config()
        api_key = api_key or cfg["api_key"]
        api_secret = api_secret or cfg["api_secret"]
        if testnet is None:
            testnet = cfg["testnet"]

    if not api_key or not api_secret:
        return {
            "success": False,
            "error_code": "NO_KEYS",
            "message": "Chave de API ou Chave Secreta não configuradas no sistema.",
            "instructions": "Preencha a API Key e API Secret geradas no painel da Binance."
        }

    try:
        exchange = get_binance_exchange(api_key=api_key, api_secret=api_secret, testnet=testnet)
        
        # 1. Testa sincronização e busca de saldo da conta Spot
        raw_balance = exchange.fetch_balance()
        
        # Filtra ativos com saldo positivo
        assets: List[Dict[str, Any]] = []
        total_usdt_estimate = 0.0

        for currency, data in raw_balance.get("total", {}).items():
            if data and float(data) > 0.000001:
                free = float(raw_balance.get("free", {}).get(currency, 0.0) or 0.0)
                used = float(raw_balance.get("used", {}).get(currency, 0.0) or 0.0)
                tot = float(data)
                
                assets.append({
                    "asset": currency,
                    "free": free,
                    "locked": used,
                    "total": tot
                })

        # Ordena: USDT primeiro, depois BTC, depois pelo maior saldo total
        def sort_priority(item):
            asset = item["asset"]
            if asset == "USDT":
                return (0, -item["total"])
            if asset == "BTC":
                return (1, -item["total"])
            if asset == "BNB":
                return (2, -item["total"])
            return (3, -item["total"])

        assets.sort(key=sort_priority)

        # Saldo USDT disponível
        usdt_free = float(raw_balance.get("free", {}).get("USDT", 0.0) or 0.0)
        usdt_total = float(raw_balance.get("total", {}).get("USDT", 0.0) or 0.0)
        btc_total = float(raw_balance.get("total", {}).get("BTC", 0.0) or 0.0)

        # Diagnóstico de permissões
        account_info = raw_balance.get("info", {})
        can_trade = account_info.get("canTrade", True)
        can_withdraw = account_info.get("canWithdraw", False)

        env_name = "Binance Spot Testnet (Sandbox)" if testnet else "Binance Spot Oficial (Produção Real)"

        return {
            "success": True,
            "environment": env_name,
            "testnet": testnet,
            "can_trade": can_trade,
            "can_withdraw": can_withdraw,
            "usdt_free": usdt_free,
            "usdt_total": usdt_total,
            "btc_total": btc_total,
            "assets": assets[:10],
            "message": f"Conexão com a {env_name} estabelecida com sucesso! Carteira sincronizada."
        }

    except ccxt.AuthenticationError as e:
        return {
            "success": False,
            "error_code": "AUTH_ERROR",
            "message": "Erro de Autenticação Binance: API Key ou Secret inválida ou expirada.",
            "detail": str(e)
        }
    except ccxt.ExchangeNotAvailable as e:
        return {
            "success": False,
            "error_code": "EXCHANGE_UNAVAILABLE",
            "message": "Servidores da Binance temporariamente inacessíveis ou sob manutenção.",
            "detail": str(e)
        }
    except ccxt.NetworkError as e:
        return {
            "success": False,
            "error_code": "NETWORK_ERROR",
            "message": "Falha de rede ao conectar com a Binance. Verifique sua conexão com a internet.",
            "detail": str(e)
        }
    except Exception as e:
        error_msg = str(e)
        if "-2015" in error_msg:
            msg = "Chave de API inválida, IP não autorizado ou permissões insuficientes na Binance."
        elif "-1021" in error_msg:
            msg = "Dessincronização de horário com o servidor da Binance. Ajuste a hora do seu Windows."
        else:
            msg = f"Falha ao validar credenciais da Binance: {error_msg}"

        return {
            "success": False,
            "error_code": "GENERAL_ERROR",
            "message": msg,
            "detail": error_msg
        }


def execute_spot_buy(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    amount_usdt: float = 50.0
) -> Dict[str, Any]:
    """
    Executa uma ordem real de COMPRA a Mercado (Market Buy) na Binance Spot.
    Calcula a precisão de lote e confere o valor mínimo exigido pela exchange.
    """
    try:
        markets = exchange.load_markets()
        if symbol not in markets:
            return {"success": False, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        ticker = exchange.fetch_ticker(symbol)
        current_price = float(ticker["last"])

        if current_price <= 0:
            return {"success": False, "message": "Preço atual de mercado inválido."}

        # Quantidade de moedas a comprar
        raw_quantity = amount_usdt / current_price
        
        # Ajusta precisão de acordo com as regras de filtro da Binance (lot size)
        quantity = float(exchange.amount_to_precision(symbol, raw_quantity))

        # Checa valor mínimo de ordem (cost filter)
        min_cost = market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0
        calculated_cost = quantity * current_price
        if calculated_cost < min_cost:
            return {
                "success": False,
                "message": f"Valor da ordem (${calculated_cost:.2f}) abaixo do mínimo da Binance (${min_cost:.2f} USDT)."
            }

        logger.info(f"🛒 Executando Ordem Spot de COMPRA: {quantity} {symbol} a ~${current_price:,.2f}")
        order = exchange.create_market_buy_order(symbol, quantity)

        # Processa resultado
        filled_qty = float(order.get("filled", quantity))
        cost = float(order.get("cost", calculated_cost))
        avg_price = float(order.get("average") or (cost / filled_qty if filled_qty > 0 else current_price))
        order_id = str(order.get("id"))

        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "side": "BUY",
            "quantity": filled_qty,
            "price": avg_price,
            "cost_usdt": cost,
            "raw_order": order
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de compra na Binance: {e}")
        return {"success": False, "message": str(e)}


def execute_spot_sell(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    quantity: float = 0.0
) -> Dict[str, Any]:
    """
    Executa uma ordem real de VENDA a Mercado (Market Sell) na Binance Spot
    (usado para fechar posições em Stop Loss ou Take Profit).
    """
    try:
        markets = exchange.load_markets()
        if symbol not in markets:
            return {"success": False, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        ticker = exchange.fetch_ticker(symbol)
        current_price = float(ticker["last"])

        # Ajusta precisão
        sell_qty = float(exchange.amount_to_precision(symbol, quantity))

        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.00001) or 0.00001
        if sell_qty < min_amount:
            return {
                "success": False,
                "message": f"Quantidade ({sell_qty}) inferior ao lote mínimo ({min_amount})."
            }

        logger.info(f"💰 Executando Ordem Spot de VENDA: {sell_qty} {symbol} a ~${current_price:,.2f}")
        order = exchange.create_market_sell_order(symbol, sell_qty)

        filled_qty = float(order.get("filled", sell_qty))
        cost = float(order.get("cost", filled_qty * current_price))
        avg_price = float(order.get("average") or (cost / filled_qty if filled_qty > 0 else current_price))
        order_id = str(order.get("id"))

        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "side": "SELL",
            "quantity": filled_qty,
            "price": avg_price,
            "proceeds_usdt": cost,
            "raw_order": order
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de venda na Binance: {e}")
        return {"success": False, "message": str(e)}
