"""
Coleta e Integridade de Dados de Mercado (FyNex Quant AI)
=========================================================
Coleta candles OHLCV da Binance via CCXT, faz backfill paginado de lacunas
e extrai o segmento contíguo mais recente sem buracos temporais.
"""

import time
from typing import Optional

import ccxt
import numpy as np
import pandas as pd

from core.database import TradingDatabase, timeframe_to_ms
from core.logging_setup import logger


def fetch_live_ohlcv(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    limit: int = 1000,
    since: Optional[int] = None,
    max_retries: int = 3,
) -> pd.DataFrame:
    """Coleta cotações e candles diretamente da API pública da Binance com retry automático em falhas de rede."""
    for attempt in range(1, max(1, max_retries) + 1):
        try:
            raw_ohlcv = exchange.fetch_ohlcv(symbol=symbol, timeframe=timeframe, since=since, limit=limit)
            if not raw_ohlcv:
                return pd.DataFrame()

            df = pd.DataFrame(
                raw_ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)

            return df
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
            if attempt < max_retries:
                backoff = min(10.0, 1.5 * (2 ** (attempt - 1)))
                logger.warning(f"Rate limit na Binance (tentativa {attempt}/{max_retries}): {e}. Aguardando {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            logger.error(f"Rate limit persistente na Binance após {max_retries} tentativas: {e}")
            return pd.DataFrame()
        except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
            err_str = str(e).lower()
            if ("451" in err_str or "restricted location" in err_str) and attempt < max_retries:
                try:
                    import binance_client
                    binance_client._apply_mirror_to_exchange(exchange, binance_client.BINANCE_PUBLIC_FALLBACK_MIRROR)
                    logger.warning("🌎 Geo-block 451 na coleta OHLCV: alternando para data-api.binance.vision...")
                except Exception:
                    pass
                time.sleep(0.5)
                continue
            if attempt < max_retries:
                backoff = 0.5 * (2 ** (attempt - 1))
                logger.warning(f"Instabilidade na Binance (tentativa {attempt}/{max_retries}): {e}. Tentando em {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            logger.error(f"Erro de comunicação de rede após {max_retries} tentativas: {e}")
            return pd.DataFrame()
        except ccxt.ExchangeError as e:
            if "-1021" in str(e) and attempt < max_retries and hasattr(exchange, "load_time_difference"):
                try:
                    exchange.load_time_difference()
                except Exception:
                    pass
                logger.warning(f"Dessincronização de relógio (-1021) detectada. Ressincronizando (tentativa {attempt}/{max_retries})...")
                time.sleep(0.5)
                continue
            logger.error(f"Erro retornado pela exchange: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Erro inesperado na coleta de dados: {e}")
            return pd.DataFrame()
    return pd.DataFrame()


def backfill_candles(
    db: TradingDatabase,
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    min_candles: int = 1000,
    max_batches: int = 15,
) -> int:
    """
    Sincroniza candles com a exchange preenchendo eventuais lacunas desde o último candle
    salvo no banco (paginado com `since`), descartando o último candle em formação.
    """
    tf_ms = timeframe_to_ms(timeframe)
    now_ms = int(time.time() * 1000)
    last_ts = db.get_last_candle_timestamp(symbol, timeframe)

    if last_ts is None or (now_ms - last_ts) > (max_batches * 1000 * tf_ms):
        df = fetch_live_ohlcv(exchange, symbol=symbol, timeframe=timeframe, limit=min_candles)
        return db.save_candles(symbol, timeframe, df, drop_last_open=True)

    total_saved = 0
    cursor_since = last_ts
    for _ in range(max_batches):
        df = fetch_live_ohlcv(exchange, symbol=symbol, timeframe=timeframe, limit=1000, since=cursor_since)
        if df.empty:
            break
        total_saved += db.save_candles(symbol, timeframe, df, drop_last_open=True)
        last_fetched_ts = int(df["timestamp"].iloc[-1].timestamp() * 1000)
        if len(df) < 1000 or last_fetched_ts >= now_ms - tf_ms:
            break
        if last_fetched_ts <= cursor_since:
            break
        cursor_since = last_fetched_ts

    return total_saved


def get_latest_contiguous_segment(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Retorna apenas o segmento contíguo mais recente do DataFrame (sem buracos temporais),
    garantindo que indicadores e alvos nunca cruzem uma lacuna de tempo.
    """
    if df.empty or len(df) < 2:
        return df

    tf_ms = timeframe_to_ms(timeframe)
    if pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        diffs = df["timestamp"].diff().dt.total_seconds() * 1000.0
    else:
        diffs = df["timestamp"].astype("int64").diff()
    gap_mask = diffs > (tf_ms * 1.5)
    if not gap_mask.any():
        return df

    last_gap_pos = int(np.where(gap_mask.values)[0][-1])
    return df.iloc[last_gap_pos:].reset_index(drop=True)
