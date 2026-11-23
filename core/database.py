"""
Camada de Persistência SQLite (FyNex Quant AI)
==============================================
Gerencia candles OHLCV, previsões com feedback vivo (Triple Barrier e t+1),
histórico de operações (Paper / Testnet / Live) e estado em tempo real do robô.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from core.logging_setup import DEFAULT_DB_PATH, logger

TIMEFRAME_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    if timeframe in TIMEFRAME_MS:
        return TIMEFRAME_MS[timeframe]
    unit = timeframe[-1]
    val = int(timeframe[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return val * mult


def timeframe_to_seconds(timeframe: str) -> int:
    return timeframe_to_ms(timeframe) // 1000


class TradingDatabase:
    """
    Gerenciador de banco de dados SQLite para persistência contínua de candles,
    previsões geradas pelo modelo e histórico de trades simulados ou reais.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_database()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Cria uma conexão SQLite (WAL) e garante fechamento imediato ao sair do bloco `with`."""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA cache_size=-4000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA wal_autocheckpoint=1000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Cria as tabelas e índices necessários caso não existam."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabela de candles históricos e em tempo real
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv_candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    datetime_utc TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    UNIQUE(symbol, timeframe, timestamp)
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_lookup
                ON ohlcv_candles(symbol, timeframe, timestamp);
            """)

            # 2. Tabela de previsões do modelo e feedback de acerto/erro
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    candle_timestamp INTEGER NOT NULL,
                    predicted_at TEXT NOT NULL,
                    prediction INTEGER NOT NULL,
                    prob_up REAL NOT NULL,
                    prob_down REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    actual_close REAL,
                    actual_direction INTEGER,
                    is_correct INTEGER,
                    UNIQUE(symbol, timeframe, candle_timestamp)
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pred_eval
                ON predictions(symbol, timeframe, candle_timestamp);
            """)

            # Migração: remove duplicatas antigas e garante índice UNIQUE em bancos existentes
            cursor.execute("""
                DELETE FROM predictions
                WHERE id NOT IN (
                    SELECT MAX(id) FROM predictions GROUP BY symbol, timeframe, candle_timestamp
                );
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_candle
                ON predictions(symbol, timeframe, candle_timestamp);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pred_resolved
                ON predictions(symbol, timeframe, is_correct, id DESC);
            """)

            # Colunas para avaliação Triple Barrier ao vivo
            for col_sql in [
                "ALTER TABLE predictions ADD COLUMN tp_pct REAL;",
                "ALTER TABLE predictions ADD COLUMN sl_pct REAL;",
                "ALTER TABLE predictions ADD COLUMN horizon_candles INTEGER;",
                "ALTER TABLE predictions ADD COLUMN outcome TEXT;",
                "ALTER TABLE predictions ADD COLUMN resolved_at TEXT;",
            ]:
                try:
                    cursor.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

            # 3. Tabela de operações executadas (Paper Trading ou Live Trading)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS simulated_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_price REAL NOT NULL,
                    reason TEXT NOT NULL,
                    pnl_pct REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    mode TEXT DEFAULT 'paper',
                    order_id TEXT DEFAULT '',
                    quantity REAL DEFAULT 0.0
                );
            """)

            for col_sql in [
                "ALTER TABLE simulated_trades ADD COLUMN mode TEXT DEFAULT 'paper';",
                "ALTER TABLE simulated_trades ADD COLUMN order_id TEXT DEFAULT '';",
                "ALTER TABLE simulated_trades ADD COLUMN quantity REAL DEFAULT 0.0;",
            ]:
                try:
                    cursor.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_sym_tf
                ON simulated_trades(symbol, timeframe);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_mode
                ON simulated_trades(mode);
            """)

            # 4. Tabela de estado vivo do robô (lida pelo dashboard em tempo real)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    pid INTEGER,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    model_accuracy REAL NOT NULL,
                    base_confidence REAL NOT NULL,
                    active_confidence REAL NOT NULL,
                    calibration_mode TEXT NOT NULL,
                    calibration_badge TEXT NOT NULL,
                    calibration_desc TEXT NOT NULL,
                    in_position INTEGER NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0.0,
                    order_in_flight INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)

            try:
                cursor.execute("ALTER TABLE bot_state ADD COLUMN order_in_flight INTEGER NOT NULL DEFAULT 0;")
            except sqlite3.OperationalError:
                pass

            conn.commit()
        logger.info(f"Banco de dados SQLite operacional (WAL): {self.db_path}")

    def set_order_in_flight(self, active: bool) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE bot_state SET order_in_flight = ? WHERE id = 1",
                (1 if active else 0,),
            )
            conn.commit()

    def is_order_in_flight(self) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT order_in_flight FROM bot_state WHERE id = 1").fetchone()
        return bool(row and row["order_in_flight"])

    def upsert_bot_state(
        self,
        pid: int,
        symbol: str,
        timeframe: str,
        trading_mode: str,
        model_version: int,
        model_accuracy: float,
        base_confidence: float,
        active_confidence: float,
        calibration_mode: str,
        calibration_badge: str,
        calibration_desc: str,
        in_position: bool,
        entry_price: float = 0.0,
    ) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO bot_state (
                    id, pid, symbol, timeframe, trading_mode, model_version, model_accuracy,
                    base_confidence, active_confidence, calibration_mode, calibration_badge,
                    calibration_desc, in_position, entry_price, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pid = excluded.pid,
                    symbol = excluded.symbol,
                    timeframe = excluded.timeframe,
                    trading_mode = excluded.trading_mode,
                    model_version = excluded.model_version,
                    model_accuracy = excluded.model_accuracy,
                    base_confidence = excluded.base_confidence,
                    active_confidence = excluded.active_confidence,
                    calibration_mode = excluded.calibration_mode,
                    calibration_badge = excluded.calibration_badge,
                    calibration_desc = excluded.calibration_desc,
                    in_position = excluded.in_position,
                    entry_price = excluded.entry_price,
                    updated_at = excluded.updated_at
            """, (
                pid, symbol, timeframe, trading_mode, model_version, float(model_accuracy),
                float(base_confidence), float(active_confidence), calibration_mode,
                calibration_badge, calibration_desc, 1 if in_position else 0,
                float(entry_price), now_str,
            ))
            conn.commit()

    def get_bot_state(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        return dict(row) if row else None

    def save_candles(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        drop_last_open: bool = False,
    ) -> int:
        """
        Salva ou atualiza candles no banco via UPSERT (ON CONFLICT DO UPDATE).
        Se drop_last_open=True, descarta a última linha do DataFrame (candle ainda em formação).
        """
        if df.empty:
            return 0

        work_df = df.iloc[:-1] if (drop_last_open and len(df) > 1) else df
        if work_df.empty:
            return 0

        rows = []
        for ts_raw, o, h, l, c, v in zip(
            work_df["timestamp"],
            work_df["open"],
            work_df["high"],
            work_df["low"],
            work_df["close"],
            work_df["volume"],
        ):
            ts = int(ts_raw.timestamp() * 1000) if isinstance(ts_raw, pd.Timestamp) else int(ts_raw)
            dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            rows.append((symbol, timeframe, ts, dt_str, float(o), float(h), float(l), float(c), float(v)))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            initial_changes = conn.total_changes
            cursor.executemany("""
                INSERT INTO ohlcv_candles
                (symbol, timeframe, timestamp, datetime_utc, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, timestamp) DO UPDATE SET
                    datetime_utc = excluded.datetime_utc,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
            """, rows)
            conn.commit()
            changed = conn.total_changes - initial_changes

        return changed

    def get_last_candle_timestamp(self, symbol: str, timeframe: str) -> Optional[int]:
        """Retorna o maior timestamp (ms) armazenado para o par/timeframe, ou None."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(timestamp) AS max_ts FROM ohlcv_candles WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe),
            ).fetchone()
        return int(row["max_ts"]) if row and row["max_ts"] is not None else None

    def get_last_predicted_candle_timestamp(self, symbol: str, timeframe: str) -> int:
        """Retorna o último candle_timestamp para o qual já houve previsão registrada."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(candle_timestamp) AS max_ts FROM predictions WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe),
            ).fetchone()
        return int(row["max_ts"]) if row and row["max_ts"] is not None else 0

    def get_recent_candles(self, symbol: str, timeframe: str, limit: int = 1500) -> pd.DataFrame:
        """Recupera os últimos N candles ordenados cronologicamente."""
        with self._get_connection() as conn:
            query = """
                SELECT timestamp, datetime_utc, open, high, low, close, volume
                FROM ohlcv_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            df = pd.read_sql_query(query, conn, params=(symbol, timeframe, limit))

        if df.empty:
            return pd.DataFrame()

        df = df.iloc[::-1].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        return df

    def record_prediction(
        self,
        symbol: str,
        timeframe: str,
        model_version: int,
        candle_timestamp: int,
        prediction: int,
        prob_up: float,
        prob_down: float,
        entry_price: float,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        horizon_candles: Optional[int] = None,
    ) -> Optional[int]:
        """
        Registra uma nova previsão feita pela IA (idempotente por symbol, timeframe, candle_timestamp).
        """
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO predictions (
                    symbol, timeframe, model_version, candle_timestamp,
                    predicted_at, prediction, prob_up, prob_down, entry_price,
                    tp_pct, sl_pct, horizon_candles
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, timeframe, model_version, candle_timestamp, now_str,
                prediction, prob_up, prob_down, entry_price,
                tp_pct, sl_pct, horizon_candles,
            ))
            conn.commit()
            if cursor.lastrowid and cursor.rowcount > 0:
                return cursor.lastrowid
            row = conn.execute(
                "SELECT id FROM predictions WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?",
                (symbol, timeframe, candle_timestamp),
            ).fetchone()
            return int(row["id"]) if row else None

    def evaluate_pending_predictions(
        self,
        symbol: str,
        timeframe: str,
        current_closed_ts: int,
        current_closed_close: float,
    ) -> List[Dict[str, Any]]:
        """
        Avalia previsões pendentes via Triple Barrier (quando os parâmetros estão salvos)
        ou pelo fechamento exato do candle t + tf_ms (previsões legadas).
        """
        tf_ms = timeframe_to_ms(timeframe)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        evaluated = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, candle_timestamp, prediction, prob_up, entry_price,
                       tp_pct, sl_pct, horizon_candles
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND actual_direction IS NULL AND candle_timestamp < ?
                ORDER BY candle_timestamp ASC
            """, (symbol, timeframe, current_closed_ts))
            rows = cursor.fetchall()

            for row in rows:
                pred_id = row["id"]
                pred_ts = int(row["candle_timestamp"])
                pred = row["prediction"]
                entry_p = float(row["entry_price"])
                prob_up = float(row["prob_up"])
                tp_pct = row["tp_pct"]
                sl_pct = row["sl_pct"]
                horizon = row["horizon_candles"]

                if tp_pct is not None and sl_pct is not None and horizon is not None and int(horizon) > 0:
                    horizon_int = int(horizon)
                    end_ts = pred_ts + horizon_int * tf_ms
                    c_rows = cursor.execute("""
                        SELECT timestamp, high, low, close
                        FROM ohlcv_candles
                        WHERE symbol = ? AND timeframe = ? AND timestamp > ? AND timestamp <= ?
                        ORDER BY timestamp ASC
                    """, (symbol, timeframe, pred_ts, end_ts)).fetchall()

                    if not c_rows:
                        continue

                    tp_level = entry_p * (1.0 + float(tp_pct))
                    sl_level = entry_p * (1.0 - float(sl_pct))
                    outcome: Optional[str] = None
                    target_close: Optional[float] = None
                    actual_dir: Optional[int] = None

                    for cr in c_rows:
                        hit_sl = float(cr["low"]) <= sl_level
                        hit_tp = float(cr["high"]) >= tp_level
                        if hit_sl and hit_tp:
                            outcome = "SL"
                            actual_dir = 0
                            target_close = sl_level
                            break
                        if hit_sl:
                            outcome = "SL"
                            actual_dir = 0
                            target_close = sl_level
                            break
                        if hit_tp:
                            outcome = "TP"
                            actual_dir = 1
                            target_close = tp_level
                            break

                    if outcome is None:
                        last_ts = int(c_rows[-1]["timestamp"])
                        if last_ts >= end_ts or current_closed_ts >= end_ts:
                            outcome = "TIME"
                            actual_dir = 0
                            target_close = float(c_rows[-1]["close"])
                        else:
                            continue

                    is_correct = 1 if pred == actual_dir else 0
                    cursor.execute("""
                        UPDATE predictions
                        SET actual_close = ?, actual_direction = ?, is_correct = ?,
                            outcome = ?, resolved_at = ?
                        WHERE id = ?
                    """, (target_close, actual_dir, is_correct, outcome, now_str, pred_id))

                    evaluated.append({
                        "id": pred_id,
                        "prediction": pred,
                        "actual_direction": actual_dir,
                        "is_correct": bool(is_correct),
                        "outcome": outcome,
                        "prob_up": prob_up,
                        "entry_price": entry_p,
                        "actual_close": target_close,
                    })
                else:
                    target_ts = pred_ts + tf_ms
                    if target_ts == current_closed_ts:
                        target_close = float(current_closed_close)
                    else:
                        c_row = cursor.execute(
                            "SELECT close FROM ohlcv_candles WHERE symbol = ? AND timeframe = ? AND timestamp = ?",
                            (symbol, timeframe, target_ts),
                        ).fetchone()
                        target_close = float(c_row["close"]) if c_row else None

                    if target_close is None:
                        continue

                    actual_dir = 1 if target_close > entry_p else 0
                    is_correct = 1 if pred == actual_dir else 0

                    cursor.execute("""
                        UPDATE predictions
                        SET actual_close = ?, actual_direction = ?, is_correct = ?,
                            outcome = ?, resolved_at = ?
                        WHERE id = ?
                    """, (target_close, actual_dir, is_correct, "NEXT_CANDLE", now_str, pred_id))

                    evaluated.append({
                        "id": pred_id,
                        "prediction": pred,
                        "actual_direction": actual_dir,
                        "is_correct": bool(is_correct),
                        "outcome": "NEXT_CANDLE",
                        "prob_up": prob_up,
                        "entry_price": entry_p,
                        "actual_close": target_close,
                    })

            conn.commit()

        return evaluated

    def get_rolling_accuracy(self, symbol: str, timeframe: str, last_n: int = 30) -> Dict[str, Any]:
        """
        Calcula a taxa de acerto real das últimas N previsões avaliadas e a precisão
        específica dos sinais de COMPRA (prediction = 1).
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT prediction, is_correct
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL
                ORDER BY id DESC
                LIMIT ?
            """, (symbol, timeframe, last_n))
            rows = cursor.fetchall()

            cursor.execute("""
                SELECT is_correct
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL AND prediction = 1
                ORDER BY id DESC
                LIMIT ?
            """, (symbol, timeframe, last_n))
            buy_rows = cursor.fetchall()

        if not rows:
            return {
                "total": 0,
                "correct": 0,
                "accuracy_pct": 50.0,
                "buy_total": 0,
                "buy_correct": 0,
                "buy_precision_pct": 50.0,
            }

        total = len(rows)
        correct = sum(1 for r in rows if r["is_correct"] == 1)
        acc = (correct / total) * 100.0

        buy_total = len(buy_rows)
        buy_correct = sum(1 for r in buy_rows if r["is_correct"] == 1)
        buy_prec = (buy_correct / buy_total * 100.0) if buy_total > 0 else acc

        return {
            "total": total,
            "correct": correct,
            "accuracy_pct": acc,
            "buy_total": buy_total,
            "buy_correct": buy_correct,
            "buy_precision_pct": buy_prec,
        }

    def record_trade(
        self,
        symbol: str,
        timeframe: str,
        model_version: int,
        entry_time: str,
        entry_price: float,
        exit_time: str,
        exit_price: float,
        reason: str,
        pnl_pct: float,
        pnl_usd: float,
        mode: str = "paper",
        order_id: str = "",
        quantity: float = 0.0,
    ) -> None:
        """Registra uma operação finalizada (Paper Trading ou Live Trading Binance)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO simulated_trades (
                    symbol, timeframe, model_version, entry_time, entry_price,
                    exit_time, exit_price, reason, pnl_pct, pnl_usd,
                    mode, order_id, quantity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, timeframe, model_version, entry_time, entry_price,
                exit_time, exit_price, reason, pnl_pct, pnl_usd,
                mode, order_id, quantity,
            ))
            conn.commit()

    def get_trade_summary(self, symbol: str) -> Dict[str, Any]:
        """Retorna o resumo consolidado de desempenho das operações registradas."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as total_trades,
                       SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                       SUM(pnl_pct) as total_pnl_pct,
                       SUM(pnl_usd) as total_pnl_usd
                FROM simulated_trades
                WHERE symbol = ?
            """, (symbol,))
            row = cursor.fetchone()

        total = row["total_trades"] if row and row["total_trades"] else 0
        wins = row["wins"] if row and row["wins"] else 0
        losses = row["losses"] if row and row["losses"] else 0
        pnl_pct = row["total_pnl_pct"] if row and row["total_pnl_pct"] else 0.0
        pnl_usd = row["total_pnl_usd"] if row and row["total_pnl_usd"] else 0.0
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl_pct": pnl_pct,
            "total_pnl_usd": pnl_usd,
        }
