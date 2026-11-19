"""
Módulo de Integração com a API Binance (FyNex Quant AI)
======================================================
Responsável pelo gerenciamento de credenciais, conexão segura via CCXT,
consulta de saldos reais da carteira Spot, validação de chaves e execução de ordens
reais (Live) ou de teste (Testnet), incluindo proteção OCO do lado da exchange.
"""

import os
import re
import time
import logging
import tempfile
from typing import Dict, Any, Optional, List

import ccxt
from dotenv import dotenv_values

logger = logging.getLogger("BinanceClient")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

VALID_MODES = ("paper", "testnet", "live")
API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")

# Valores padrão dos limites de risco (podem ser sobrescritos no .env)
RISK_DEFAULTS = {
    "MAX_DAILY_LOSS_USDT": 20.0,
    "MAX_TRADES_PER_DAY": 10,
    "MAX_TRADE_AMOUNT_USDT": 200.0,
}


class EnvironmentConfigError(ValueError):
    """Combinação incoerente entre modo de operação e ambiente da Binance."""


# -----------------------------------------------------------------------------
# Configuração (.env)
# -----------------------------------------------------------------------------
def _get(env_data: Dict[str, Optional[str]], key: str, default: str = "") -> str:
    return (env_data.get(key) or os.environ.get(key) or default).strip()


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_binance_config() -> Dict[str, Any]:
    """
    Carrega as variáveis de configuração do arquivo .env com valores padrão seguros.
    """
    env_data: Dict[str, Optional[str]] = {}
    if os.path.exists(ENV_PATH):
        env_data = dotenv_values(ENV_PATH)

    api_key = _get(env_data, "BINANCE_API_KEY")
    api_secret = _get(env_data, "BINANCE_API_SECRET")
    testnet = _get(env_data, "BINANCE_TESTNET", "false").lower() in ["true", "1", "yes", "sim"]

    trading_mode = _get(env_data, "TRADING_MODE", "paper").lower()
    if trading_mode not in VALID_MODES:
        trading_mode = "paper"

    trade_amount_usdt = _to_float(_get(env_data, "TRADE_AMOUNT_USDT", "50"), 50.0)

    max_daily_loss = _to_float(_get(env_data, "MAX_DAILY_LOSS_USDT", str(RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"])),
                               RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"])
    max_trades_day = int(_to_float(_get(env_data, "MAX_TRADES_PER_DAY", str(RISK_DEFAULTS["MAX_TRADES_PER_DAY"])),
                                   RISK_DEFAULTS["MAX_TRADES_PER_DAY"]))
    max_trade_amount = _to_float(_get(env_data, "MAX_TRADE_AMOUNT_USDT", str(RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"])),
                                 RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"])
    use_oco = _get(env_data, "USE_EXCHANGE_OCO", "true").lower() in ["true", "1", "yes", "sim"]

    masked_key = ""
    if api_key:
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****"

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "api_key_masked": masked_key,
        "has_keys": bool(api_key and api_secret),
        "testnet": testnet,
        "trading_mode": trading_mode,
        "trade_amount_usdt": trade_amount_usdt,
        "max_daily_loss_usdt": max_daily_loss,
        "max_trades_per_day": max_trades_day,
        "max_trade_amount_usdt": max_trade_amount,
        "use_exchange_oco": use_oco,
        "env_path": ENV_PATH,
    }


def resolve_environment(trading_mode: str, keys_are_testnet: bool) -> bool:
    """
    Define de forma única e segura se a exchange deve operar no sandbox (testnet).

    - testnet -> SEMPRE sandbox. Exige que as chaves configuradas sejam de testnet.
    - live    -> SEMPRE produção. Exige que as chaves configuradas sejam de produção.
    - paper   -> dados públicos de PRODUÇÃO (preço real), nenhuma ordem é enviada.

    Lança EnvironmentConfigError quando a combinação é incoerente, evitando que
    o modo "testnet" envie ordens reais (ou que o "live" opere no sandbox).
    """
    mode = (trading_mode or "paper").lower()
    if mode not in VALID_MODES:
        raise EnvironmentConfigError(f"Modo de operação inválido: '{trading_mode}'.")
    if mode == "testnet":
        if not keys_are_testnet:
            raise EnvironmentConfigError(
                "Modo 'testnet' selecionado, mas BINANCE_TESTNET=false (chaves de PRODUÇÃO). "
                "Recusado para evitar ordens reais. Use chaves da testnet e BINANCE_TESTNET=true."
            )
        return True
    if mode == "live":
        if keys_are_testnet:
            raise EnvironmentConfigError(
                "Modo 'live' selecionado, mas BINANCE_TESTNET=true (chaves de TESTNET). "
                "Combinação incoerente recusada."
            )
        return False
    return False  # paper


def validate_api_credential(value: str, label: str) -> str:
    """Valida o formato de uma credencial (evita injeção de linhas no .env)."""
    value = (value or "").strip()
    if not API_KEY_PATTERN.match(value):
        raise ValueError(f"{label} inválida: use apenas letras e números (16 a 128 caracteres).")
    return value


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def save_binance_config(
    api_key: str,
    api_secret: str,
    testnet: bool,
    trading_mode: str,
    trade_amount_usdt: float
) -> bool:
    """
    Salva as configurações com segurança no arquivo .env (gravação atômica).
    Credenciais vazias (ou mascaradas) preservam as já existentes.
    O ambiente é derivado do modo: testnet -> sandbox, live -> produção.
    Os limites de risco existentes no .env são preservados.
    """
    current = load_binance_config()

    final_key = validate_api_credential(api_key, "API Key") if api_key and api_key.strip() else current["api_key"]
    if api_secret and api_secret.strip() and not api_secret.startswith(("*", "•")):
        final_secret = validate_api_credential(api_secret, "API Secret")
    else:
        final_secret = current["api_secret"]

    final_mode = trading_mode.lower() if trading_mode and trading_mode.lower() in VALID_MODES else "paper"
    if final_mode == "testnet":
        final_testnet = True
    elif final_mode == "live":
        final_testnet = False
    else:
        final_testnet = bool(testnet)

    amount = _to_float(str(trade_amount_usdt), 50.0)
    final_amount = min(max(10.0, amount), current["max_trade_amount_usdt"])

    content = f"""# ==============================================================================
# FyNex Quant AI - Configurações de Conexão com a Binance
# ==============================================================================

# Credenciais de API (Mantenha em sigilo absoluto! NUNCA habilite permissão de saque)
BINANCE_API_KEY={final_key}
BINANCE_API_SECRET={final_secret}

# Indica de qual ambiente são as chaves acima:
# true  -> Binance Spot Testnet (testnet.binance.vision - testes sem dinheiro real)
# false -> Binance Spot Produção (api.binance.com - conta real)
BINANCE_TESTNET={'true' if final_testnet else 'false'}

# Modo de Operação do Robô:
# paper   -> Simulação (dados reais, ordens virtuais sem risco)
# testnet -> Ordens na Spot Testnet (exige BINANCE_TESTNET=true)
# live    -> ORDENS REAIS com fundos reais (exige BINANCE_TESTNET=false)
TRADING_MODE={final_mode}

# Valor em USDT alocado para cada compra realizada pela IA
TRADE_AMOUNT_USDT={final_amount:.2f}

# Limites de Risco (o robô para de abrir posições ao atingi-los)
MAX_DAILY_LOSS_USDT={current['max_daily_loss_usdt']:.2f}
MAX_TRADES_PER_DAY={current['max_trades_per_day']}
MAX_TRADE_AMOUNT_USDT={current['max_trade_amount_usdt']:.2f}

# Proteção na exchange: envia ordem OCO (Take Profit + Stop-Limit) após cada compra
USE_EXCHANGE_OCO={'true' if current['use_exchange_oco'] else 'false'}
"""
    _atomic_write(ENV_PATH, content)
    return True


# -----------------------------------------------------------------------------
# Conexão
# -----------------------------------------------------------------------------
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
    testnet: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Testa a conexão autenticada com a Binance, recupera os saldos da carteira Spot
    e valida as permissões de trading.
    """
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
        raw_balance = exchange.fetch_balance()

        assets: List[Dict[str, Any]] = []
        for currency, data in raw_balance.get("total", {}).items():
            if data and float(data) > 0.000001:
                assets.append({
                    "asset": currency,
                    "free": float(raw_balance.get("free", {}).get(currency, 0.0) or 0.0),
                    "locked": float(raw_balance.get("used", {}).get(currency, 0.0) or 0.0),
                    "total": float(data)
                })

        priority = {"USDT": 0, "BTC": 1, "BNB": 2}
        assets.sort(key=lambda item: (priority.get(item["asset"], 3), -item["total"]))

        usdt_free = float(raw_balance.get("free", {}).get("USDT", 0.0) or 0.0)
        usdt_total = float(raw_balance.get("total", {}).get("USDT", 0.0) or 0.0)
        btc_total = float(raw_balance.get("total", {}).get("BTC", 0.0) or 0.0)

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
        return {"success": False, "error_code": "AUTH_ERROR",
                "message": "Erro de Autenticação Binance: API Key ou Secret inválida ou expirada.",
                "detail": str(e)}
    except ccxt.ExchangeNotAvailable as e:
        return {"success": False, "error_code": "EXCHANGE_UNAVAILABLE",
                "message": "Servidores da Binance temporariamente inacessíveis ou sob manutenção.",
                "detail": str(e)}
    except ccxt.NetworkError as e:
        return {"success": False, "error_code": "NETWORK_ERROR",
                "message": "Falha de rede ao conectar com a Binance. Verifique sua conexão com a internet.",
                "detail": str(e)}
    except Exception as e:
        error_msg = str(e)
        if "-2015" in error_msg:
            msg = "Chave de API inválida, IP não autorizado ou permissões insuficientes na Binance."
        elif "-1021" in error_msg:
            msg = "Dessincronização de horário com o servidor da Binance. Ajuste a hora do seu Windows."
        else:
            msg = f"Falha ao validar credenciais da Binance: {error_msg}"
        return {"success": False, "error_code": "GENERAL_ERROR", "message": msg, "detail": error_msg}


# -----------------------------------------------------------------------------
# Saldos e utilidades de ordens
# -----------------------------------------------------------------------------
def _base_asset(symbol: str) -> str:
    return symbol.split("/")[0]


def _quote_asset(symbol: str) -> str:
    return symbol.split("/")[1]


def get_free_balance(exchange: ccxt.binance, asset: str) -> float:
    balance = exchange.fetch_balance()
    return float(balance.get("free", {}).get(asset, 0.0) or 0.0)


def base_fee_from_order(order: Dict[str, Any], base: str) -> float:
    """Soma as taxas cobradas no ativo base (ex.: BTC) em uma ordem executada."""
    fees = order.get("fees") or []
    if not fees and order.get("fee"):
        fees = [order["fee"]]
    total = 0.0
    for fee in fees:
        if fee and fee.get("currency") == base and fee.get("cost") is not None:
            total += float(fee["cost"])
    return total


def net_filled_quantity(order: Dict[str, Any], base: str, fallback_qty: float) -> float:
    """Quantidade realmente recebida = executado bruto - taxas cobradas no ativo base."""
    filled = float(order.get("filled") or fallback_qty)
    return max(0.0, filled - base_fee_from_order(order, base))


# -----------------------------------------------------------------------------
# Execução de ordens a mercado
# -----------------------------------------------------------------------------
def execute_spot_buy(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    amount_usdt: float = 50.0
) -> Dict[str, Any]:
    """
    Executa uma ordem de COMPRA a Mercado na Binance Spot.
    Confere saldo USDT livre, precisão de lote e valor mínimo exigido pela exchange.
    Retorna a quantidade LÍQUIDA recebida (descontada a taxa cobrada no ativo base).
    """
    try:
        markets = exchange.load_markets()
        if symbol not in markets:
            return {"success": False, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        quote = _quote_asset(symbol)
        base = _base_asset(symbol)

        free_quote = get_free_balance(exchange, quote)
        if free_quote < amount_usdt:
            return {"success": False,
                    "message": f"Saldo {quote} insuficiente: livre {free_quote:.2f}, necessário {amount_usdt:.2f}."}

        ticker = exchange.fetch_ticker(symbol)
        current_price = float(ticker["last"])
        if current_price <= 0:
            return {"success": False, "message": "Preço atual de mercado inválido."}

        quantity = float(exchange.amount_to_precision(symbol, amount_usdt / current_price))

        min_cost = market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0
        calculated_cost = quantity * current_price
        if calculated_cost < min_cost:
            return {"success": False,
                    "message": f"Valor da ordem (${calculated_cost:.2f}) abaixo do mínimo da Binance (${min_cost:.2f})."}

        logger.info(f"🛒 Executando Ordem Spot de COMPRA: {quantity} {symbol} a ~${current_price:,.2f}")
        order = exchange.create_market_buy_order(symbol, quantity)

        gross_qty = float(order.get("filled") or quantity)
        net_qty = net_filled_quantity(order, base, quantity)
        cost = float(order.get("cost") or calculated_cost)
        avg_price = float(order.get("average") or (cost / gross_qty if gross_qty > 0 else current_price))

        return {
            "success": True,
            "order_id": str(order.get("id")),
            "symbol": symbol,
            "side": "BUY",
            "gross_quantity": gross_qty,
            "quantity": net_qty,
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
    Executa uma ordem de VENDA a Mercado na Binance Spot.
    Nunca tenta vender mais do que o saldo livre do ativo base (evita erro -2010).
    """
    try:
        markets = exchange.load_markets()
        if symbol not in markets:
            return {"success": False, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        base = _base_asset(symbol)

        free_base = get_free_balance(exchange, base)
        sell_qty = float(exchange.amount_to_precision(symbol, min(quantity, free_base)))

        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.00001) or 0.00001
        if sell_qty < min_amount:
            return {"success": False,
                    "message": f"Quantidade vendável ({sell_qty}) inferior ao lote mínimo ({min_amount}). "
                               f"Saldo livre de {base}: {free_base}."}

        ticker = exchange.fetch_ticker(symbol)
        current_price = float(ticker["last"])

        logger.info(f"💰 Executando Ordem Spot de VENDA: {sell_qty} {symbol} a ~${current_price:,.2f}")
        order = exchange.create_market_sell_order(symbol, sell_qty)

        filled_qty = float(order.get("filled") or sell_qty)
        cost = float(order.get("cost") or filled_qty * current_price)
        avg_price = float(order.get("average") or (cost / filled_qty if filled_qty > 0 else current_price))

        return {
            "success": True,
            "order_id": str(order.get("id")),
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


def execute_spot_sell_with_retry(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    attempts: int = 3,
    base_delay_seconds: float = 2.0
) -> Dict[str, Any]:
    """Tenta vender até `attempts` vezes com backoff exponencial."""
    last: Dict[str, Any] = {"success": False, "message": "Nenhuma tentativa executada."}
    for attempt in range(1, attempts + 1):
        last = execute_spot_sell(exchange, symbol, quantity)
        if last.get("success"):
            return last
        logger.warning(f"Tentativa de venda {attempt}/{attempts} falhou: {last.get('message')}")
        if attempt < attempts:
            time.sleep(base_delay_seconds * (2 ** (attempt - 1)))
    return last


# -----------------------------------------------------------------------------
# Proteção OCO (Take Profit + Stop-Limit) do lado da exchange
# -----------------------------------------------------------------------------
def place_protective_oco(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    take_profit_price: float,
    stop_price: float,
    stop_limit_offset_pct: float = 0.003
) -> Dict[str, Any]:
    """
    Envia uma OCO de VENDA: LIMIT_MAKER no take profit (acima) e STOP_LOSS_LIMIT
    no stop (abaixo). Assim a posição fica protegida mesmo com o robô desligado.
    """
    try:
        exchange.load_markets()
        market = exchange.market(symbol)
        base = _base_asset(symbol)
        free_base = get_free_balance(exchange, base)
        qty = exchange.amount_to_precision(symbol, min(quantity, free_base))
        stop_limit_price = stop_price * (1.0 - stop_limit_offset_pct)

        params = {
            "symbol": market["id"],
            "side": "SELL",
            "quantity": qty,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": exchange.price_to_precision(symbol, take_profit_price),
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": exchange.price_to_precision(symbol, stop_price),
            "belowPrice": exchange.price_to_precision(symbol, stop_limit_price),
            "belowTimeInForce": "GTC",
        }
        response = exchange.privatePostOrderListOco(params)
        list_id = str(response.get("orderListId"))
        logger.info(f"🛡️ OCO de proteção enviada (orderListId={list_id}) | TP {params['abovePrice']} | "
                    f"Stop {params['belowStopPrice']} / Limite {params['belowPrice']} | Qtd {qty}")
        return {"success": True, "order_list_id": list_id, "raw": response}
    except Exception as e:
        logger.error(f"Falha ao enviar OCO de proteção: {e}")
        return {"success": False, "message": str(e)}


def get_oco_status(exchange: ccxt.binance, symbol: str, order_list_id: str) -> Dict[str, Any]:
    """
    Consulta a OCO. Se ela foi executada, retorna o preço médio, quantidade
    e o tipo de saída (TAKE_PROFIT ou STOP_LOSS).
    """
    try:
        response = exchange.privateGetOrderList({"orderListId": order_list_id})
        list_status = response.get("listOrderStatus")  # EXECUTING | ALL_DONE | REJECT
        result: Dict[str, Any] = {"success": True, "list_status": list_status, "filled": False}
        if list_status != "ALL_DONE":
            return result

        for leg in response.get("orders", []):
            order = exchange.fetch_order(str(leg["orderId"]), symbol)
            if float(order.get("filled") or 0.0) > 0:
                info_type = str(order.get("info", {}).get("type", "")).upper()
                result.update({
                    "filled": True,
                    "order_id": str(order.get("id")),
                    "price": float(order.get("average") or order.get("price") or 0.0),
                    "quantity": float(order.get("filled") or 0.0),
                    "reason": "TAKE_PROFIT" if info_type == "LIMIT_MAKER" else "STOP_LOSS",
                })
                break
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


def cancel_oco(exchange: ccxt.binance, symbol: str, order_list_id: str) -> Dict[str, Any]:
    try:
        market = exchange.market(symbol)
        exchange.privateDeleteOrderList({"symbol": market["id"], "orderListId": order_list_id})
        logger.info(f"OCO {order_list_id} cancelada.")
        return {"success": True}
    except Exception as e:
        logger.warning(f"Falha ao cancelar OCO {order_list_id}: {e}")
        return {"success": False, "message": str(e)}
