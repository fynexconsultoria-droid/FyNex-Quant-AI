"""
Execução de Ordens Spot, Reconciliação e Proteção OCO Binance (FyNex Quant AI)
==============================================================================
Inclui preflight (mercado ativo e spread máximo), idempotência via `newClientOrderId`,
reconciliação em timeout de rede, compra/venda a mercado com retry seguro e gestão de OCO.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import ccxt

from core.exchange.config import load_binance_config
from core.exchange.connection import BALANCE_CACHE, _sleep, call_with_retry, check_binance_health
from core.exchange.market_data import fetch_cached_balance

logger = logging.getLogger("BinanceClient")


def _bc_fn(name: str, default):
    bc = sys.modules.get("binance_client")
    if bc is not None and hasattr(bc, name):
        return getattr(bc, name)
    return default


def _base_asset(symbol: str) -> str:
    return symbol.split("/")[0]


def _quote_asset(symbol: str) -> str:
    return symbol.split("/")[1]


def _is_testnet_exchange(exchange: Any) -> bool:
    try:
        urls = getattr(exchange, "urls", {}) or {}
        return "testnet" in str(urls.get("api", "")).lower()
    except Exception:
        return False


def generate_client_order_id(prefix: str = "fynex") -> str:
    """Gera um `newClientOrderId` único (<= 36 chars, padrão Binance) para idempotência."""
    clean_prefix = re.sub(r"[^A-Za-z0-9_-]", "", prefix)[:12] or "fynex"
    ts_part = hex(int(time.time() * 1000))[2:]
    rand_part = uuid.uuid4().hex[:8]
    return f"{clean_prefix}_{ts_part}_{rand_part}"[:36]


def get_free_balance(exchange: ccxt.binance, asset: str) -> float:
    balance = call_with_retry(
        lambda: exchange.fetch_balance(),
        exchange=exchange,
        testnet=_is_testnet_exchange(exchange),
        max_attempts=2,
    )
    return float(balance.get("free", {}).get(asset, 0.0) or 0.0)


def get_asset_balances(exchange: ccxt.binance, asset: str) -> Tuple[float, float, float]:
    """Retorna (free, locked, total) de um ativo na carteira Spot com retry."""
    balance = call_with_retry(
        lambda: exchange.fetch_balance(),
        exchange=exchange,
        testnet=_is_testnet_exchange(exchange),
        max_attempts=2,
    )
    free = float(balance.get("free", {}).get(asset, 0.0) or 0.0)
    locked = float(balance.get("used", {}).get(asset, 0.0) or 0.0)
    total = float(balance.get("total", {}).get(asset, free + locked) or 0.0)
    return free, locked, total


def _sum_order_fee(order: Dict[str, Any], currency: str) -> float:
    fees = order.get("fees") or []
    if not fees and order.get("fee"):
        fees = [order["fee"]]
    total = 0.0
    for fee in fees:
        if fee and fee.get("currency") == currency and fee.get("cost") is not None:
            total += float(fee["cost"])
    return total


def base_fee_from_order(order: Dict[str, Any], base: str) -> float:
    """Soma as taxas cobradas no ativo base (ex.: BTC) em uma ordem executada."""
    return _sum_order_fee(order, base)


def quote_fee_from_order(order: Dict[str, Any], quote: str) -> float:
    """Soma as taxas cobradas no ativo de cotação (ex.: USDT) em uma ordem executada."""
    return _sum_order_fee(order, quote)


def net_filled_quantity(order: Dict[str, Any], base: str, fallback_qty: float) -> float:
    """Quantidade realmente recebida = executado bruto - taxas cobradas no ativo base."""
    filled = float(order.get("filled") or fallback_qty)
    return max(0.0, filled - base_fee_from_order(order, base))


def reconcile_order_by_client_id(
    exchange: ccxt.binance,
    symbol: str,
    client_order_id: str,
    raise_on_network_error: bool = False,
) -> Optional[Dict[str, Any]]:
    """Consulta uma ordem na Binance pelo `origClientOrderId` após timeout/erro de rede."""
    try:
        markets = exchange.load_markets()
        market = markets.get(symbol)
        if not market:
            return None
        raw = call_with_retry(
            lambda: exchange.privateGetOrder({"symbol": market["id"], "origClientOrderId": client_order_id}),
            exchange=exchange,
            testnet=_is_testnet_exchange(exchange),
            max_attempts=2,
        )
        if not raw:
            return None
        executed_qty = float(raw.get("executedQty") or 0.0)
        cum_quote = float(raw.get("cummulativeQuoteQty") or 0.0)
        avg_price = (cum_quote / executed_qty) if executed_qty > 0 else float(raw.get("price") or 0.0)
        return {
            "id": str(raw.get("orderId")),
            "clientOrderId": raw.get("clientOrderId", client_order_id),
            "status": str(raw.get("status", "")).lower(),
            "filled": executed_qty,
            "cost": cum_quote,
            "average": avg_price,
            "fees": [],
            "info": raw,
        }
    except ccxt.OrderNotFound:
        return None
    except ccxt.ExchangeError as e:
        err_msg = str(e).lower()
        if "-2013" in err_msg or "order does not exist" in err_msg:
            return None
        if raise_on_network_error:
            raise
        return None
    except Exception:
        if raise_on_network_error:
            raise
        return None


def preflight_execution_check(
    exchange: ccxt.binance,
    symbol: str,
    max_spread_pct: float = 0.50,
) -> Dict[str, Any]:
    """Valida mercado ativo, preço válido e spread dentro do limite antes de ordem real."""
    testnet = _is_testnet_exchange(exchange)
    markets = call_with_retry(
        lambda: exchange.load_markets(),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    if symbol not in markets:
        return {"ok": False, "message": f"Par {symbol} não suportado pela exchange."}

    market = markets[symbol]
    if market.get("active") is False:
        return {"ok": False, "message": f"Par {symbol} está temporariamente suspenso/inativo na Binance."}

    ticker = call_with_retry(
        lambda: exchange.fetch_ticker(symbol),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    price = float(ticker.get("last") or ticker.get("close") or 0.0)
    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    if price <= 0:
        return {"ok": False, "message": "Preço atual de mercado inválido."}

    spread_pct = 0.0
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 0.0
        if max_spread_pct > 0 and spread_pct > max_spread_pct:
            return {
                "ok": False,
                "message": (
                    f"Spread anormal detectado em {symbol} ({spread_pct:.3f}% > limite {max_spread_pct:.2f}%). "
                    f"Ordem bloqueada por proteção contra slippage."
                ),
                "spread_pct": round(spread_pct, 4),
            }

    return {
        "ok": True,
        "market": market,
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_pct": round(spread_pct, 4),
    }


def check_execution_readiness(symbol: str = "BTC/USDT") -> Dict[str, Any]:
    """Diagnóstico de prontidão da conta Binance para entradas e saídas."""
    cfg = _bc_fn("load_binance_config", load_binance_config)()
    health = _bc_fn("check_binance_health", check_binance_health)(testnet=cfg["testnet"])
    checks: Dict[str, Any] = {
        "has_keys": cfg["has_keys"],
        "trading_mode": cfg["trading_mode"],
        "testnet": cfg["testnet"],
        "use_exchange_oco": cfg["use_exchange_oco"],
        "api_reachable": bool(health.get("ok")),
        "clock_synced": bool(health.get("clock_synced")),
        "latency_ms": health.get("latency_ms"),
        "clock_drift_ms": health.get("clock_drift_ms"),
        "active_mirror": health.get("active_mirror"),
        "can_trade": False,
        "withdraw_disabled": True,
        "usdt_free": 0.0,
        "ready_for_orders": False,
        "warnings": [],
    }

    if not cfg["has_keys"]:
        checks["warnings"].append("Nenhuma API Key configurada (operando apenas em simulação).")
        return checks

    conn = _bc_fn("fetch_cached_balance", fetch_cached_balance)(testnet=cfg["testnet"], ttl_seconds=15.0)
    if not conn.get("success"):
        checks["warnings"].append(conn.get("message") or "Falha ao autenticar na Binance.")
        return checks

    checks["can_trade"] = bool(conn.get("can_trade", False))
    checks["withdraw_disabled"] = not bool(conn.get("can_withdraw", False))
    checks["usdt_free"] = float(conn.get("usdt_free", 0.0))

    if not checks["can_trade"]:
        checks["warnings"].append("Permissão de Trading Spot desabilitada na chave da Binance.")
    if not checks["withdraw_disabled"]:
        checks["warnings"].append("ALERTA: Permissão de saque (Withdraw) está habilitada na API Key!")
    if not checks["clock_synced"]:
        checks["warnings"].append(f"Relógio dessincronizado ({checks['clock_drift_ms']} ms).")
    if checks["usdt_free"] < cfg["trade_amount_usdt"]:
        checks["warnings"].append(
            f"Saldo USDT livre (${checks['usdt_free']:.2f}) abaixo do valor por ordem (${cfg['trade_amount_usdt']:.2f})."
        )

    checks["ready_for_orders"] = (
        checks["api_reachable"]
        and checks["clock_synced"]
        and checks["can_trade"]
        and checks["usdt_free"] >= min(10.0, cfg["trade_amount_usdt"])
    )
    return checks


def execute_spot_buy(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    amount_usdt: float = 50.0,
    max_spread_pct: float = 0.50,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Executa ordem Spot de COMPRA a mercado com preflight, idempotência e reconciliação."""
    cid = client_order_id or generate_client_order_id("fynexb")
    t_start = time.perf_counter()
    try:
        pre = _bc_fn("preflight_execution_check", preflight_execution_check)(
            exchange, symbol, max_spread_pct=max_spread_pct
        )
        if not pre.get("ok"):
            return {"success": False, "message": pre.get("message", "Falha no preflight."), "client_order_id": cid}

        market = pre["market"]
        current_price = pre["price"]
        ref_ask = pre["ask"] if pre["ask"] > 0 else current_price
        spread_pct = pre["spread_pct"]
        quote = _quote_asset(symbol)
        base = _base_asset(symbol)

        free_quote = _bc_fn("get_free_balance", get_free_balance)(exchange, quote)
        if free_quote < amount_usdt:
            return {
                "success": False,
                "client_order_id": cid,
                "message": f"Saldo {quote} insuficiente: livre {free_quote:.2f}, necessário {amount_usdt:.2f}.",
            }

        quantity = float(exchange.amount_to_precision(symbol, amount_usdt / ref_ask))
        min_cost = market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0
        calculated_cost = quantity * ref_ask
        if calculated_cost < min_cost:
            return {
                "success": False,
                "client_order_id": cid,
                "message": f"Valor da ordem (${calculated_cost:.2f}) abaixo do mínimo da Binance (${min_cost:.2f}).",
            }

        logger.info(
            f"🛒 Executando Ordem Spot de COMPRA [{cid}]: {quantity} {symbol} "
            f"a ~${ref_ask:,.2f} (spread {spread_pct:.3f}%)"
        )

        order: Optional[Dict[str, Any]] = None
        try:
            order = exchange.create_market_buy_order(symbol, quantity, params={"newClientOrderId": cid})
        except (ccxt.NetworkError, ccxt.RequestTimeout) as net_err:
            logger.warning(f"⚠️ Timeout/rede na compra [{cid}]: {net_err}. Reconciliando na Binance...")
            order = _bc_fn("reconcile_order_by_client_id", reconcile_order_by_client_id)(exchange, symbol, cid)
            if order is None or float(order.get("filled") or 0.0) <= 0:
                return {
                    "success": False,
                    "client_order_id": cid,
                    "reconciled": False,
                    "ambiguous_network": order is None,
                    "message": f"Falha de rede na compra e ordem não confirmada na Binance: {net_err}",
                }
            logger.info(f"✅ Compra [{cid}] confirmada via reconciliação na Binance (order_id={order.get('id')}).")

        exec_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        gross_qty = float(order.get("filled") or quantity)
        net_qty = net_filled_quantity(order, base, quantity)
        cost = float(order.get("cost") or calculated_cost)
        avg_price = float(order.get("average") or (cost / gross_qty if gross_qty > 0 else ref_ask))
        slippage_pct = round(((avg_price - ref_ask) / ref_ask * 100.0), 4) if ref_ask > 0 else 0.0

        logger.info(
            f"✅ COMPRA confirmada [{cid}] | Preço médio: ${avg_price:,.2f} | "
            f"Slippage: {slippage_pct:+.3f}% | Latência: {exec_ms:.0f} ms"
        )
        BALANCE_CACHE.clear()

        return {
            "success": True,
            "order_id": str(order.get("id")),
            "client_order_id": cid,
            "symbol": symbol,
            "side": "BUY",
            "gross_quantity": gross_qty,
            "quantity": net_qty,
            "price": avg_price,
            "cost_usdt": cost,
            "slippage_pct": slippage_pct,
            "spread_pct": spread_pct,
            "execution_latency_ms": exec_ms,
            "raw_order": order,
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de compra na Binance [{cid}]: {e}")
        return {"success": False, "client_order_id": cid, "message": str(e)}


def execute_spot_sell(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    quantity: float = 0.0,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Executa ordem Spot de VENDA a mercado com verificação de saldo e reconciliação."""
    cid = client_order_id or generate_client_order_id("fynexs")
    t_start = time.perf_counter()
    try:
        testnet = _is_testnet_exchange(exchange)
        markets = call_with_retry(lambda: exchange.load_markets(), exchange=exchange, testnet=testnet, max_attempts=2)
        if symbol not in markets:
            return {"success": False, "client_order_id": cid, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        base = _base_asset(symbol)

        free_base, locked_base, _ = _bc_fn("get_asset_balances", get_asset_balances)(exchange, base)
        sell_qty = float(exchange.amount_to_precision(symbol, min(quantity, free_base)))

        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.00001) or 0.00001
        if sell_qty < min_amount:
            locked_note = f" (travado em ordens abertas: {locked_base})" if locked_base > 0 else ""
            return {
                "success": False,
                "client_order_id": cid,
                "message": (
                    f"Quantidade vendável ({sell_qty}) inferior ao lote mínimo ({min_amount}). "
                    f"Saldo livre de {base}: {free_base}{locked_note}."
                ),
            }

        ticker = call_with_retry(
            lambda: exchange.fetch_ticker(symbol), exchange=exchange, testnet=testnet, max_attempts=2
        )
        current_price = float(ticker.get("last") or ticker.get("close") or 0.0)
        ref_bid = float(ticker.get("bid") or current_price)

        logger.info(f"💰 Executando Ordem Spot de VENDA [{cid}]: {sell_qty} {symbol} a ~${ref_bid:,.2f}")

        order: Optional[Dict[str, Any]] = None
        try:
            order = exchange.create_market_sell_order(symbol, sell_qty, params={"newClientOrderId": cid})
        except (ccxt.NetworkError, ccxt.RequestTimeout) as net_err:
            logger.warning(f"⚠️ Timeout/rede na venda [{cid}]: {net_err}. Reconciliando na Binance...")
            order = _bc_fn("reconcile_order_by_client_id", reconcile_order_by_client_id)(exchange, symbol, cid)
            if order is None or float(order.get("filled") or 0.0) <= 0:
                return {
                    "success": False,
                    "client_order_id": cid,
                    "reconciled": False,
                    "message": f"Falha de rede na venda e ordem não encontrada na Binance: {net_err}",
                }
            logger.info(f"✅ Venda [{cid}] confirmada via reconciliação na Binance (order_id={order.get('id')}).")

        exec_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        filled_qty = float(order.get("filled") or sell_qty)
        cost = float(order.get("cost") or filled_qty * ref_bid)
        avg_price = float(order.get("average") or (cost / filled_qty if filled_qty > 0 else ref_bid))
        slippage_pct = round(((ref_bid - avg_price) / ref_bid * 100.0), 4) if ref_bid > 0 else 0.0

        logger.info(
            f"✅ VENDA confirmada [{cid}] | Preço médio: ${avg_price:,.2f} | "
            f"Slippage: {slippage_pct:+.3f}% | Latência: {exec_ms:.0f} ms"
        )
        BALANCE_CACHE.clear()

        return {
            "success": True,
            "order_id": str(order.get("id")),
            "client_order_id": cid,
            "symbol": symbol,
            "side": "SELL",
            "quantity": filled_qty,
            "price": avg_price,
            "proceeds_usdt": cost,
            "slippage_pct": slippage_pct,
            "execution_latency_ms": exec_ms,
            "raw_order": order,
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de venda na Binance [{cid}]: {e}")
        return {"success": False, "client_order_id": cid, "message": str(e)}


def execute_spot_sell_with_retry(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    attempts: int = 3,
    base_delay_seconds: float = 2.0,
) -> Dict[str, Any]:
    """Tenta vender com backoff exponencial reconciliando o `client_order_id` antes de cada nova tentativa."""
    last: Dict[str, Any] = {"success": False, "message": "Nenhuma tentativa executada."}
    prev_cid: Optional[str] = None
    reconcile_fn = _bc_fn("reconcile_order_by_client_id", reconcile_order_by_client_id)
    sell_fn = _bc_fn("execute_spot_sell", execute_spot_sell)

    for attempt in range(1, attempts + 1):
        if prev_cid is not None:
            reconciled = reconcile_fn(exchange, symbol, prev_cid)
            if reconciled and float(reconciled.get("filled") or 0.0) > 0:
                filled_qty = float(reconciled["filled"])
                avg_price = float(reconciled.get("average") or 0.0)
                logger.info(f"✅ Venda anterior [{prev_cid}] confirmada na reconciliação pré-retry!")
                return {
                    "success": True,
                    "order_id": str(reconciled.get("id")),
                    "client_order_id": prev_cid,
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": filled_qty,
                    "price": avg_price,
                    "proceeds_usdt": float(reconciled.get("cost") or filled_qty * avg_price),
                    "slippage_pct": 0.0,
                    "execution_latency_ms": 0.0,
                    "raw_order": reconciled,
                }
        last = sell_fn(exchange, symbol, quantity)
        prev_cid = last.get("client_order_id")
        if last.get("success"):
            return last
        logger.warning(f"Tentativa de venda {attempt}/{attempts} falhou: {last.get('message')}")
        if attempt < attempts:
            _sleep(base_delay_seconds * (2 ** (attempt - 1)))
    return last


def place_protective_oco(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    take_profit_price: float,
    stop_price: float,
    stop_limit_offset_pct: float = 0.003,
) -> Dict[str, Any]:
    """Envia ordem OCO de venda (LIMIT_MAKER no TP + STOP_LOSS_LIMIT no SL)."""
    try:
        testnet = _is_testnet_exchange(exchange)
        call_with_retry(lambda: exchange.load_markets(), exchange=exchange, testnet=testnet, max_attempts=2)
        market = exchange.market(symbol)
        base = _base_asset(symbol)
        free_base = _bc_fn("get_free_balance", get_free_balance)(exchange, base)
        qty = exchange.amount_to_precision(symbol, min(quantity, free_base))

        try:
            ticker = call_with_retry(
                lambda: exchange.fetch_ticker(symbol), exchange=exchange, testnet=testnet, max_attempts=2
            )
            cur = float(ticker.get("last") or 0.0)
            if cur > 0:
                if take_profit_price <= cur:
                    take_profit_price = cur * 1.002
                if stop_price >= cur:
                    stop_price = cur * 0.998
        except Exception:
            pass

        stop_limit_price = stop_price * (1.0 - stop_limit_offset_pct)
        params = {
            "symbol": market["id"],
            "listClientOrderId": generate_client_order_id("fynexoco"),
            "side": "SELL",
            "quantity": qty,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": exchange.price_to_precision(symbol, take_profit_price),
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": exchange.price_to_precision(symbol, stop_price),
            "belowPrice": exchange.price_to_precision(symbol, stop_limit_price),
            "belowTimeInForce": "GTC",
        }
        response = call_with_retry(
            lambda: exchange.privatePostOrderListOco(params),
            exchange=exchange,
            testnet=testnet,
            max_attempts=1,
            allow_mirror_failover=False,
        )
        list_id = str(response.get("orderListId"))
        logger.info(
            f"🛡️ OCO de proteção enviada (orderListId={list_id}) | TP {params['abovePrice']} | "
            f"Stop {params['belowStopPrice']} / Limite {params['belowPrice']} | Qtd {qty}"
        )
        return {"success": True, "order_list_id": list_id, "raw": response}
    except Exception as e:
        logger.error(f"Falha ao enviar OCO de proteção: {e}")
        return {"success": False, "message": str(e)}


def get_oco_status(exchange: ccxt.binance, symbol: str, order_list_id: str) -> Dict[str, Any]:
    """Consulta o status de uma OCO na Binance."""
    try:
        testnet = _is_testnet_exchange(exchange)
        response = call_with_retry(
            lambda: exchange.privateGetOrderList({"orderListId": order_list_id}),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
        )
        list_status = response.get("listOrderStatus")
        result: Dict[str, Any] = {"success": True, "list_status": list_status, "filled": False}
        if list_status != "ALL_DONE":
            return result

        for leg in response.get("orders", []):
            order = call_with_retry(
                lambda lid=str(leg["orderId"]): exchange.fetch_order(lid, symbol),
                exchange=exchange,
                testnet=testnet,
                max_attempts=2,
            )
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
    """Cancela uma ordem OCO aberta na Binance."""
    try:
        testnet = _is_testnet_exchange(exchange)
        call_with_retry(lambda: exchange.load_markets(), exchange=exchange, testnet=testnet, max_attempts=2)
        market = exchange.market(symbol)
        call_with_retry(
            lambda: exchange.privateDeleteOrderList({"symbol": market["id"], "orderListId": order_list_id}),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
            allow_mirror_failover=False,
        )
        logger.info(f"OCO {order_list_id} cancelada.")
        return {"success": True}
    except Exception as e:
        logger.warning(f"Falha ao cancelar OCO {order_list_id}: {e}")
        return {"success": False, "message": str(e)}
