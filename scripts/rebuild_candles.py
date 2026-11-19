"""
Script de Saneamento do Banco de Dados (Fase 2.2)
=================================================
- Remove os candles de 1m que foram gravados pelo loop ao vivo enquanto ainda estavam
  abertos (após o lote inicial de 1000 candles fechados, i.e., id > 1000 em timeframe='1m').
- Remove todas as previsões feitas com dados corrompidos ou duplicadas.
- Mantém os 1000 candles do lote inicial de 1m (que estavam fechados) e os 1000 candles de 15m.
- Se houver conexão de rede com a Binance, faz o backfill completo para atualizar os candles.
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from trading_bot import TradingDatabase, backfill_candles  # noqa: E402
import binance_client  # noqa: E402


def clean_corrupted_1m_data(db: TradingDatabase) -> dict:
    with db._get_connection() as conn:
        cursor = conn.cursor()

        # Identifica os primeros 1000 candles de 1m (lote inicial fechado)
        rows = cursor.execute("""
            SELECT id, timestamp
            FROM ohlcv_candles
            WHERE symbol = 'BTC/USDT' AND timeframe = '1m'
            ORDER BY timestamp ASC
        """).fetchall()

        deleted_candles = 0
        if len(rows) > 1000:
            # Os candles após os primeiros 1000 foram inseridos pelo loop ao vivo com INSERT OR IGNORE parcial
            cutoff_ts = rows[999]["timestamp"]
            cursor.execute("""
                DELETE FROM ohlcv_candles
                WHERE symbol = 'BTC/USDT' AND timeframe = '1m' AND timestamp > ?
            """, (cutoff_ts,))
            deleted_candles = cursor.rowcount

        # Remove previsões feitas sobre candles corrompidos
        cursor.execute("DELETE FROM predictions;")
        deleted_predictions = cursor.rowcount

        conn.commit()

        rem_1m = cursor.execute(
            "SELECT COUNT(*) AS n FROM ohlcv_candles WHERE timeframe = '1m'"
        ).fetchone()["n"]
        rem_15m = cursor.execute(
            "SELECT COUNT(*) AS n FROM ohlcv_candles WHERE timeframe = '15m'"
        ).fetchone()["n"]

    return {
        "deleted_corrupted_1m_candles": deleted_candles,
        "deleted_predictions": deleted_predictions,
        "remaining_1m_candles": rem_1m,
        "remaining_15m_candles": rem_15m,
    }


if __name__ == "__main__":
    db_path = os.path.join(ROOT, "trading_data.db")
    db = TradingDatabase(db_path=db_path)
    summary = clean_corrupted_1m_data(db)
    print("Saneamento do SQLite concluído:", summary)

    # Tenta sincronizar com a Binance se houver acesso à rede
    try:
        exchange = binance_client.get_binance_exchange(testnet=False, timeout=5000)
        saved = backfill_candles(db, exchange, symbol="BTC/USDT", timeframe="1m", min_candles=1000)
        print(f"Backfill online (1m): {saved} candles atualizados.")
    except Exception as e:
        print(f"Backfill online não executado (sem rede no momento): {e}")
