"""
Configuração e Validação de Credenciais Binance (FyNex Quant AI)
================================================================
Gerencia carregamento, validação, coerência de ambiente (paper/testnet/live)
e gravação atômica de credenciais Binance no `.env` usando `core.infra.env_store`.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, Optional

from core.infra.env_store import get_env_value, mask_secret, read_env_file, update_env_keys
from core.infra.utils import to_float
from core.logging_setup import BASE_DIR

ENV_PATH = os.path.join(BASE_DIR, ".env")
VALID_MODES = ("paper", "testnet", "live")
API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")

RISK_DEFAULTS: Dict[str, Any] = {
    "MAX_DAILY_LOSS_USDT": 20.0,
    "MAX_TRADES_PER_DAY": 10,
    "MAX_TRADE_AMOUNT_USDT": 200.0,
}


class EnvironmentConfigError(ValueError):
    """Combinação incoerente entre modo de operação e ambiente da Binance."""


def _resolve_env_path(env_path: Optional[str] = None) -> str:
    if env_path:
        return env_path
    bc = sys.modules.get("binance_client")
    if bc is not None and hasattr(bc, "ENV_PATH"):
        return str(bc.ENV_PATH)
    return ENV_PATH


def validate_api_credential(value: str, label: str) -> str:
    """Valida o formato de uma credencial (evita injeção de linhas no .env)."""
    value = (value or "").strip()
    if not API_KEY_PATTERN.match(value):
        raise ValueError(f"{label} inválida: use apenas letras e números (16 a 128 caracteres).")
    return value


def resolve_environment(trading_mode: str, keys_are_testnet: bool) -> bool:
    """
    Define de forma única e segura se a exchange deve operar no sandbox (testnet):
    - testnet -> SEMPRE sandbox (exige BINANCE_TESTNET=true).
    - live    -> SEMPRE produção (exige BINANCE_TESTNET=false).
    - paper   -> dados públicos de PRODUÇÃO, nenhuma ordem real enviada.
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
    return False


def load_binance_config(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Carrega as variáveis de configuração Binance do `.env` com valores padrão seguros."""
    path = _resolve_env_path(env_path)
    env_data = read_env_file(path)

    api_key = get_env_value(env_data, "BINANCE_API_KEY")
    api_secret = get_env_value(env_data, "BINANCE_API_SECRET")
    testnet = get_env_value(env_data, "BINANCE_TESTNET", "false").lower() in ("true", "1", "yes", "sim")

    trading_mode = get_env_value(env_data, "TRADING_MODE", "paper").lower()
    if trading_mode not in VALID_MODES:
        trading_mode = "paper"

    trade_amount_usdt = to_float(get_env_value(env_data, "TRADE_AMOUNT_USDT", "50"), 50.0)
    max_daily_loss = to_float(
        get_env_value(env_data, "MAX_DAILY_LOSS_USDT", str(RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"])),
        RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"],
    )
    max_trades_day = int(
        to_float(
            get_env_value(env_data, "MAX_TRADES_PER_DAY", str(RISK_DEFAULTS["MAX_TRADES_PER_DAY"])),
            RISK_DEFAULTS["MAX_TRADES_PER_DAY"],
        )
    )
    max_trade_amount = to_float(
        get_env_value(env_data, "MAX_TRADE_AMOUNT_USDT", str(RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"])),
        RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"],
    )
    use_oco = get_env_value(env_data, "USE_EXCHANGE_OCO", "true").lower() in ("true", "1", "yes", "sim")

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "api_key_masked": mask_secret(api_key),
        "has_keys": bool(api_key and api_secret),
        "testnet": testnet,
        "trading_mode": trading_mode,
        "trade_amount_usdt": trade_amount_usdt,
        "max_daily_loss_usdt": max_daily_loss,
        "max_trades_per_day": max_trades_day,
        "max_trade_amount_usdt": max_trade_amount,
        "use_exchange_oco": use_oco,
        "env_path": path,
    }


def save_binance_config(
    api_key: str,
    api_secret: str,
    testnet: bool,
    trading_mode: str,
    trade_amount_usdt: float,
    env_path: Optional[str] = None,
) -> bool:
    """Salva configurações Binance no `.env` de forma atômica, preservando limites de risco."""
    from core.exchange.connection import clear_exchange_cache

    path = _resolve_env_path(env_path)
    current = load_binance_config(env_path=path)

    if api_key and api_key.strip() and "..." not in api_key and not api_key.strip().startswith(("*", "•")):
        final_key = validate_api_credential(api_key, "API Key")
    else:
        final_key = current["api_key"]
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

    amount = to_float(str(trade_amount_usdt), 50.0)
    final_amount = min(max(10.0, amount), current["max_trade_amount_usdt"])

    managed_values = {
        "BINANCE_API_KEY": final_key,
        "BINANCE_API_SECRET": final_secret,
        "BINANCE_TESTNET": "true" if final_testnet else "false",
        "TRADING_MODE": final_mode,
        "TRADE_AMOUNT_USDT": f"{final_amount:.2f}",
        "MAX_DAILY_LOSS_USDT": f"{current['max_daily_loss_usdt']:.2f}",
        "MAX_TRADES_PER_DAY": str(current["max_trades_per_day"]),
        "MAX_TRADE_AMOUNT_USDT": f"{current['max_trade_amount_usdt']:.2f}",
        "USE_EXCHANGE_OCO": "true" if current["use_exchange_oco"] else "false",
    }

    default_template = f"""# ==============================================================================
# FyNex Quant AI - Configurações de Conexão com a Binance
# ==============================================================================

# Credenciais de API (Mantenha em sigilo absoluto! NUNCA habilite permissão de saque)
BINANCE_API_KEY={managed_values['BINANCE_API_KEY']}
BINANCE_API_SECRET={managed_values['BINANCE_API_SECRET']}

# Ambiente das chaves acima (true = Testnet, false = Produção)
BINANCE_TESTNET={managed_values['BINANCE_TESTNET']}

# Modo de Operação do Robô (paper | testnet | live)
TRADING_MODE={managed_values['TRADING_MODE']}

# Valor em USDT alocado para cada compra realizada pela IA
TRADE_AMOUNT_USDT={managed_values['TRADE_AMOUNT_USDT']}

# Limites de Risco (o robô para de abrir posições ao atingi-los)
MAX_DAILY_LOSS_USDT={managed_values['MAX_DAILY_LOSS_USDT']}
MAX_TRADES_PER_DAY={managed_values['MAX_TRADES_PER_DAY']}
MAX_TRADE_AMOUNT_USDT={managed_values['MAX_TRADE_AMOUNT_USDT']}

# Proteção na exchange: envia ordem OCO (Take Profit + Stop-Limit) após cada compra
USE_EXCHANGE_OCO={managed_values['USE_EXCHANGE_OCO']}
"""
    update_env_keys(managed_values, path, default_template=default_template)
    clear_exchange_cache()
    return True
