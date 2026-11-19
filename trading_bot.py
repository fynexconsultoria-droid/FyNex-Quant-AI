"""
Robô de Trading Quantitativo de Criptomoedas com Machine Learning Contínuo
==========================================================================
Autor: Engenheiro de Software Quantitativo Sênior
Tecnologias: Python 3.10+, CCXT, Pandas, NumPy, TA, Scikit-Learn, SQLite3, Joblib

Funcionalidades Avançadas:
1. Ingestão de dados em tempo real da Binance Produção (Spot pública, sem necessidade de chaves).
2. Persistência contínua de candles históricos e em tempo real em banco local SQLite (trading_data.db).
3. Engenharia de features técnicas avançadas (RSI, MACD, ATR normalizado, Médias Móveis, Momentum de Volume).
4. Treinamento e Retreinamento Contínuo em Janela Deslizante (Rolling Window Retraining com RandomForest).
5. Persistência de Modelos em Disco (pasta 'models/'): O robô nunca perde o aprendizado acumulado.
6. Auto-Calibração Dinâmica de Confiança: A IA ajusta automaticamente a exigência de probabilidade
   conforme sua acurácia real recente (Modo Defensivo vs. Regime Favorável).
7. Sistema de Feedback Vivo: Avaliação em tempo real de cada previsão (Acerto vs. Erro).
8. Gestão de Risco Quantitativa (Stop Loss 1.5% e Take Profit 3.0% com Paper Trading auditável).
9. Logging duplo simultâneo: Console colorido + arquivo persistente 'trading_bot.log'.
"""

import os
import sys
import time
import json
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Tuple, List, Optional, Dict, Any

import ccxt
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report

# Módulo de Conexão com a Binance (FyNex Quant AI)
import binance_client
from position_manager import PositionManager, RiskManager

# Trava de segurança: o modo 'live' (dinheiro real) permanece desativado até que a
# estratégia seja validada em backtest com taxas + paper trading (Plano de Ação, Fase 4).
LIVE_TRADING_ENABLED = False

# Biblioteca 'ta' (Technical Analysis Library in Python)
import ta
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator
from ta.volatility import AverageTrueRange

# Garante suporte completo a UTF-8 (emojis e acentos) no terminal Windows
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# -----------------------------------------------------------------------------
# Configuração de Logging Dual (Console + Arquivo 'trading_bot.log')
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("QuantTradingBot")


# -----------------------------------------------------------------------------
# 1. Camada de Persistência em Banco de Dados SQLite
# -----------------------------------------------------------------------------
class TradingDatabase:
    """
    Gerenciador de banco de dados SQLite para persistência contínua de candles,
    previsões geradas pelo modelo e histórico de trades simulados.
    """

    def __init__(self, db_path: str = "trading_data.db"):
        self.db_path = db_path
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Cria uma conexão com suporte a timeout e concorrência."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

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
                    is_correct INTEGER
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pred_eval 
                ON predictions(symbol, timeframe, candle_timestamp);
            """)

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

            # Migração dinâmica caso a tabela já existisse
            for col_sql in [
                "ALTER TABLE simulated_trades ADD COLUMN mode TEXT DEFAULT 'paper';",
                "ALTER TABLE simulated_trades ADD COLUMN order_id TEXT DEFAULT '';",
                "ALTER TABLE simulated_trades ADD COLUMN quantity REAL DEFAULT 0.0;"
            ]:
                try:
                    cursor.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

            conn.commit()
        logger.info(f"Banco de dados SQLite operacional: {self.db_path}")

    def save_candles(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """
        Salva candles no banco ignorando registros duplicados (INSERT OR IGNORE).
        Retorna a quantidade de novos registros inseridos.
        """
        if df.empty:
            return 0

        rows = []
        for _, row in df.iterrows():
            ts = int(row["timestamp"].timestamp() * 1000) if isinstance(row["timestamp"], pd.Timestamp) else int(row["timestamp"])
            dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            rows.append((
                symbol,
                timeframe,
                ts,
                dt_str,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"])
            ))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            initial_changes = conn.total_changes
            cursor.executemany("""
                INSERT OR IGNORE INTO ohlcv_candles 
                (symbol, timeframe, timestamp, datetime_utc, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            inserted = conn.total_changes - initial_changes

        return inserted

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

        # Inverte para ordem cronológica ascendente (mais antigo para mais recente)
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
        entry_price: float
    ) -> int:
        """Registra uma nova previsão feita pela IA."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO predictions (
                    symbol, timeframe, model_version, candle_timestamp,
                    predicted_at, prediction, prob_up, prob_down, entry_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, timeframe, model_version, candle_timestamp, now_str, prediction, prob_up, prob_down, entry_price))
            conn.commit()
            return cursor.lastrowid

    def evaluate_pending_predictions(
        self,
        symbol: str,
        timeframe: str,
        current_closed_ts: int,
        current_closed_close: float
    ) -> List[Dict[str, Any]]:
        """
        Avalia previsões anteriores pendentes de resultado quando um novo candle se fecha.
        Determina se a previsão de direção acertou (is_correct = 1) ou errou (0).
        """
        evaluated = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, candle_timestamp, prediction, prob_up, entry_price
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND actual_direction IS NULL AND candle_timestamp < ?
                ORDER BY candle_timestamp ASC
            """, (symbol, timeframe, current_closed_ts))
            rows = cursor.fetchall()

            for row in rows:
                pred_id = row["id"]
                pred = row["prediction"]
                entry_p = row["entry_price"]
                prob_up = row["prob_up"]

                # Direção real: 1 se fechou acima do preço de entrada, senão 0
                actual_dir = 1 if current_closed_close > entry_p else 0
                is_correct = 1 if pred == actual_dir else 0

                cursor.execute("""
                    UPDATE predictions
                    SET actual_close = ?, actual_direction = ?, is_correct = ?
                    WHERE id = ?
                """, (current_closed_close, actual_dir, is_correct, pred_id))

                evaluated.append({
                    "id": pred_id,
                    "prediction": pred,
                    "actual_direction": actual_dir,
                    "is_correct": bool(is_correct),
                    "prob_up": prob_up,
                    "entry_price": entry_p,
                    "actual_close": current_closed_close
                })

            conn.commit()

        return evaluated

    def get_rolling_accuracy(self, symbol: str, timeframe: str, last_n: int = 30) -> Dict[str, Any]:
        """Calcula a taxa de acerto real das últimas N previsões avaliadas."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT is_correct
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL
                ORDER BY id DESC
                LIMIT ?
            """, (symbol, timeframe, last_n))
            rows = cursor.fetchall()

        if not rows:
            return {"total": 0, "correct": 0, "accuracy_pct": 50.0}

        total = len(rows)
        correct = sum(1 for r in rows if r["is_correct"] == 1)
        acc = (correct / total) * 100.0

        return {"total": total, "correct": correct, "accuracy_pct": acc}

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
        quantity: float = 0.0
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
                mode, order_id, quantity
            ))
            conn.commit()

    def get_trade_summary(self, symbol: str) -> Dict[str, Any]:
        """Retorna o resumo consolidado de desempenho do Paper Trading."""
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
            "total_pnl_usd": pnl_usd
        }


# -----------------------------------------------------------------------------
# 2. Persistência de Modelos em Disco (Pasta 'models/')
# -----------------------------------------------------------------------------
class ModelPersistence:
    """
    Gerencia o salvamento e carregamento dos cérebros treinados em disco,
    garantindo que o aprendizado nunca seja perdido ao reiniciar o programa.
    """

    def __init__(self, models_dir: str = "models"):
        self.models_dir = models_dir
        os.makedirs(self.models_dir, exist_ok=True)
        self.meta_path = os.path.join(self.models_dir, "metadata.json")

    def save_model(
        self,
        model: RandomForestClassifier,
        version: int,
        accuracy: float,
        feature_cols: List[str],
        symbol: str,
        timeframe: str
    ) -> str:
        clean_symbol = symbol.replace("/", "_")
        version_filename = f"model_v{version}_{clean_symbol}_{timeframe}.joblib"
        version_path = os.path.join(self.models_dir, version_filename)
        latest_path = os.path.join(self.models_dir, f"latest_{clean_symbol}_{timeframe}.joblib")

        joblib.dump(model, version_path)
        joblib.dump(model, latest_path)

        metadata = {
            "version": version,
            "accuracy": float(accuracy),
            "feature_cols": feature_cols,
            "symbol": symbol,
            "timeframe": timeframe,
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "file": version_filename
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=4)

        logger.info(f"💾 [MODELO SALVO EM DISCO] Versão v{version} gravada em '{version_path}' (Acurácia Teste: {accuracy * 100:.2f}%)")
        return version_path

    def load_latest_model(
        self,
        symbol: str,
        timeframe: str
    ) -> Optional[Tuple[RandomForestClassifier, int, float, List[str]]]:
        clean_symbol = symbol.replace("/", "_")
        latest_path = os.path.join(self.models_dir, f"latest_{clean_symbol}_{timeframe}.joblib")

        if not os.path.exists(latest_path) or not os.path.exists(self.meta_path):
            return None

        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            if meta.get("symbol") != symbol or meta.get("timeframe") != timeframe:
                return None

            model = joblib.load(latest_path)
            version = int(meta.get("version", 1))
            acc = float(meta.get("accuracy", 0.5))
            features = meta.get("feature_cols", [])

            logger.info(f"🧠 [MODELO RECUPERADO DE DISCO] Carregado Modelo v{version} previamente treinado (Acurácia: {acc * 100:.2f}%)")
            return model, version, acc, features
        except Exception as e:
            logger.warning(f"Não foi possível restaurar modelo salvo: {e}. Um novo modelo será treinado.")
            return None


# -----------------------------------------------------------------------------
# 3. Auto-Calibração Dinâmica de Confiança (Adaptive Self-Tuning)
# -----------------------------------------------------------------------------
class DynamicCalibrator:
    """
    Ajusta dinamicamente o rigor de probabilidade e tolerância ao risco
    com base na performance real das previsões mais recentes.
    """

    @staticmethod
    def get_calibration(
        rolling_accuracy: float,
        evaluated_count: int,
        base_threshold: float = 0.55
    ) -> Dict[str, Any]:
        """
        Retorna o limiar ajustado e o regime de mercado diagnosticado.
        """
        if evaluated_count < 8:
            return {
                "threshold": base_threshold,
                "mode": "CALIBRAÇÃO INICIAL",
                "badge": "ℹ️",
                "description": f"Coletando amostras ({evaluated_count}/8). Régua padrão de {base_threshold:.0%}."
            }

        # Mercado com alto ruído (acurácia abaixo de 42%)
        if rolling_accuracy < 42.0:
            return {
                "threshold": 0.65,
                "mode": "PROTEÇÃO ATIVA",
                "badge": "🛡️",
                "description": f"Mercado errático (Acurácia {rolling_accuracy:.1f}%). Exigência elevada para 65% para evitar armadilhas."
            }
        # Mercado em consolidação / moderado (42% a 54%)
        elif rolling_accuracy < 54.0:
            return {
                "threshold": base_threshold,
                "mode": "REGIME NEUTRO",
                "badge": "⚖️",
                "description": f"Acurácia em {rolling_accuracy:.1f}%. Operando com confiança padrão de {base_threshold:.0%}."
            }
        # Mercado em tendência favorável (>= 54%)
        else:
            optimized_th = max(0.52, base_threshold - 0.02)
            return {
                "threshold": optimized_th,
                "mode": "REGIME FAVORÁVEL",
                "badge": "🚀",
                "description": f"Alta sincronia (Acurácia {rolling_accuracy:.1f}%). Confiança otimizada para {optimized_th:.0%}."
            }


# -----------------------------------------------------------------------------
# 4. Coleta de Dados em Tempo Real (Binance Produção)
# -----------------------------------------------------------------------------
def fetch_live_ohlcv(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    limit: int = 1000
) -> pd.DataFrame:
    """
    Coleta cotações e candles reais diretamente da API pública da Binance.
    """
    try:
        raw_ohlcv = exchange.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
        if not raw_ohlcv:
            return pd.DataFrame()

        df = pd.DataFrame(
            raw_ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        return df
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        logger.error(f"Erro de comunicação de rede: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Erro inesperado na coleta de dados: {e}")
        return pd.DataFrame()


# -----------------------------------------------------------------------------
# 5. Engenharia de Features Técnicas
# -----------------------------------------------------------------------------
def build_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Calcula indicadores estatísticos e de momento:
    - RSI 14
    - MACD Line, Signal e Histograma
    - ATR Normalizado (% do preço)
    - Relação de Médias Móveis (SMA 9 / SMA 21)
    - Momentum imediato de retorno
    - Dinâmica percentual de volume
    """
    df = df.copy()

    # 1. RSI (14)
    rsi_indicator = RSIIndicator(close=df["close"], window=14)
    df["rsi_14"] = rsi_indicator.rsi()

    # 2. MACD (12, 26, 9)
    macd_indicator = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_line"] = macd_indicator.macd()
    df["macd_signal"] = macd_indicator.macd_signal()
    df["macd_diff"] = macd_indicator.macd_diff()

    # 3. ATR Normalizado (Volatilidade Relativa ao Preço)
    atr_indicator = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14)
    df["atr_pct"] = atr_indicator.average_true_range() / df["close"]

    # 4. Médias Móveis Rápidas e Lentas
    sma_9 = SMAIndicator(close=df["close"], window=9).sma_indicator()
    sma_21 = SMAIndicator(close=df["close"], window=21).sma_indicator()
    df["sma_ratio"] = sma_9 / sma_21

    # 5. Momentum e Dinâmica de Volume
    df["return_1"] = df["close"].pct_change(1)
    df["volume_change"] = df["volume"].pct_change(1).clip(-2.0, 5.0)

    feature_cols = [
        "rsi_14",
        "macd_line",
        "macd_signal",
        "macd_diff",
        "atr_pct",
        "sma_ratio",
        "return_1",
        "volume_change"
    ]

    return df, feature_cols


# -----------------------------------------------------------------------------
# 6. Construção da Variável Alvo (Target)
# -----------------------------------------------------------------------------
def create_target(df: pd.DataFrame) -> pd.DataFrame:
    """
    Define a variável dependente binária:
    - 1 se o próximo candle fechar acima do atual.
    - 0 se fechar igual ou abaixo.
    """
    df = df.copy()
    df["Target"] = (df["close"].shift(-1) > df["close"]).astype(float)
    df_clean = df.dropna().copy()
    df_clean["Target"] = df_clean["Target"].astype(int)
    return df_clean


# -----------------------------------------------------------------------------
# 7. Treinamento e Retreinamento do Modelo (Rolling Retraining)
# -----------------------------------------------------------------------------
def train_model(
    df: pd.DataFrame,
    feature_cols: List[str],
    model_version: int = 1,
    train_split_ratio: float = 0.8
) -> Tuple[RandomForestClassifier, float]:
    """
    Treina/Retreina o RandomForestClassifier preservando rigorosamente
    a cronologia temporal (sem data leakage e sem shuffle).
    """
    split_index = int(len(df) * train_split_ratio)
    X = df[feature_cols]
    y = df["Target"]

    X_train, X_test = X.iloc[:split_index], X.iloc[split_index:]
    y_train, y_test = y.iloc[:split_index], y.iloc[split_index:]

    model = RandomForestClassifier(
        n_estimators=150,
        max_depth=5,
        min_samples_split=10,
        min_samples_leaf=5,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    logger.info(f"⚡ [RETREINO ML v{model_version}] Treino: {len(X_train)} | Teste: {len(X_test)} | Acurácia Teste: {accuracy * 100:.2f}%")

    importances = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    top_feats = ", ".join([f"{k}: {v:.1%}" for k, v in importances.head(3).items()])
    logger.info(f"   Top Features: {top_feats}")

    return model, accuracy


# -----------------------------------------------------------------------------
# 8. Loop de Execução e Aprendizado Contínuo com Auto-Calibração
# -----------------------------------------------------------------------------
def run_continuous_trading_bot(
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    retrain_every_n_candles: int = 5,
    confidence_threshold: float = 0.55,
    stop_loss_pct: float = 0.015,
    take_profit_pct: float = 0.03,
    check_interval_seconds: int = 10,
    db_path: str = "trading_data.db",
    trading_mode: Optional[str] = None,
    trade_amount_usdt: Optional[float] = None,
    testnet: Optional[bool] = None
) -> None:
    """
    Executa o robô em tempo real com todas as automações ativas:
    - Persistência no SQLite e pasta 'models/'
    - Retreino automático a cada N candles
    - Auto-calibração dinâmica do limiar de confiança
    - Conexão e Execução na Binance (Spot Real, Testnet ou Paper Trading)
    """
    db = TradingDatabase(db_path=db_path)
    model_storage = ModelPersistence(models_dir="models")

    # Carrega configurações da Binance do .env
    bin_cfg = binance_client.load_binance_config()
    final_mode = (trading_mode or bin_cfg["trading_mode"]).lower()
    final_amount = trade_amount_usdt if trade_amount_usdt is not None else bin_cfg["trade_amount_usdt"]
    final_testnet = testnet if testnet is not None else bin_cfg["testnet"]
    api_key = bin_cfg["api_key"]
    api_secret = bin_cfg["api_secret"]

    # Bloqueio de segurança: o modo live fica desativado até a validação da estratégia (Fase 4)
    if final_mode == "live" and not LIVE_TRADING_ENABLED:
        logger.error(
            "⛔ [BLOQUEADO] O modo 'live' está desativado no código até que a estratégia seja validada "
            "em backtest com taxas e paper trading. Use 'paper' ou 'testnet'."
        )
        return

    # Validação de segurança caso tenha selecionado Live ou Testnet sem chaves
    if final_mode in ["live", "testnet"] and not bin_cfg["has_keys"]:
        logger.warning(
            f"⚠️ [AVISO DE SEGURANÇA] Modo '{final_mode}' selecionado, mas as chaves da Binance "
            f"não foram configuradas no .env! Alternando automaticamente para 'paper' (simulado)."
        )
        final_mode = "paper"

    # O ambiente é derivado do modo (testnet => sandbox, live => produção, paper => dados reais)
    try:
        final_testnet = binance_client.resolve_environment(final_mode, final_testnet)
    except binance_client.EnvironmentConfigError as e:
        logger.error(f"⛔ [CONFIGURAÇÃO INCOERENTE] {e}")
        return

    use_keys = bin_cfg["has_keys"] and final_mode in ["live", "testnet"]
    exchange = binance_client.get_binance_exchange(
        api_key=api_key if use_keys else None,
        api_secret=api_secret if use_keys else None,
        testnet=final_testnet
    )

    risk_manager = RiskManager(
        db,
        mode=final_mode,
        max_daily_loss_usdt=bin_cfg["max_daily_loss_usdt"],
        max_trades_per_day=bin_cfg["max_trades_per_day"],
        max_trade_amount_usdt=bin_cfg["max_trade_amount_usdt"],
    )
    positions = PositionManager(
        db, exchange, mode=final_mode, symbol=symbol, timeframe=timeframe,
        stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
        trade_amount_usdt=final_amount, use_exchange_oco=bin_cfg["use_exchange_oco"],
        risk=risk_manager,
    )

    env_name = "Binance Spot Testnet (Sandbox)" if final_testnet else "Binance Spot Oficial (Mercado Real)"
    mode_label = {
        "live": "🔴 LIVE TRADING (ORDENS REAIS NA CONTA SPOT)",
        "testnet": "🟠 TESTNET TRADING (ORDENS REAIS NO SANDBOX)",
        "paper": "🟢 PAPER TRADING (SIMULAÇÃO QUANTITATIVA SEGURA)"
    }.get(final_mode, "PAPER TRADING")

    logger.info("====================================================================")
    logger.info(f"🚀 INICIANDO QUANT TRADING BOT - MACHINE LEARNING CONTÍNUO")
    logger.info(f"   Par: {symbol} | Tempo Gráfico: {timeframe}")
    logger.info(f"   Ambiente de Conexão: {env_name}")
    logger.info(f"   Modo de Operação: {mode_label}")
    if bin_cfg["has_keys"]:
        logger.info(f"   Conta Binance: Conectada ({bin_cfg['api_key_masked']})")
    else:
        logger.info("   Conta Binance: Chaves não informadas (Operando em modo público simulado)")
    logger.info(f"   Alocação por Entrada: ${final_amount:,.2f} USDT")
    logger.info(f"   Retreinamento Automático: A cada {retrain_every_n_candles} novos candles")
    logger.info(f"   Confiança Base: {confidence_threshold * 100:.0f}% (com Auto-Calibração Ativa)")
    logger.info(f"   Gestão de Risco: Stop Loss {stop_loss_pct * 100:.1f}% | Take Profit {take_profit_pct * 100:.1f}%")
    logger.info(
        f"   Limites: Perda diária ${risk_manager.max_daily_loss_usdt:,.2f} | "
        f"{risk_manager.max_trades_per_day} trades/dia | Máx. por ordem ${risk_manager.max_trade_amount_usdt:,.2f} | "
        f"OCO na exchange: {'Sim' if positions.use_exchange_oco else 'Não'}"
    )
    logger.info("====================================================================")

    # 1. Sincronização Inicial de Histórico
    logger.info(f"Sincronizando candles da {env_name}...")
    init_df = fetch_live_ohlcv(exchange, symbol=symbol, timeframe=timeframe, limit=1000)
    if init_df.empty:
        logger.error("Falha ao coletar dados da exchange. Verifique a conexão com a internet.")
        return

    novos_inseridos = db.save_candles(symbol, timeframe, init_df)
    logger.info(f"Sincronização concluída: {novos_inseridos} novos candles registrados no SQLite.")

    # 2. Carregamento ou Treinamento Inicial do Modelo
    saved_model_data = model_storage.load_latest_model(symbol, timeframe)
    if saved_model_data is not None:
        model, model_version, _, feature_cols = saved_model_data
    else:
        model_version = 1
        recent_df = db.get_recent_candles(symbol, timeframe, limit=1000)
        feat_df, feature_cols = build_features(recent_df)
        dataset = create_target(feat_df)
        model, initial_acc = train_model(dataset, feature_cols, model_version=model_version)
        model_storage.save_model(model, model_version, initial_acc, feature_cols, symbol, timeframe)

    # Variáveis de Estado
    candles_since_retrain = 0
    last_evaluated_candle_ts = 0

    # Restaura posição persistida (sobrevive a reinícios) e reconcilia com a conta
    positions.restore()

    # Calibração inicial
    calib = DynamicCalibrator.get_calibration(50.0, 0, base_threshold=confidence_threshold)
    active_confidence = calib["threshold"]

    logger.info("\n=== Sistema operacional. Entrando no loop de monitoramento contínuo... ===")

    try:
        while True:
            # 1. Coleta os candles mais recentes
            live_df = fetch_live_ohlcv(exchange, symbol=symbol, timeframe=timeframe, limit=30)
            if live_df.empty:
                time.sleep(check_interval_seconds)
                continue

            db.save_candles(symbol, timeframe, live_df)
            current_price = float(live_df["close"].iloc[-1])

            # -----------------------------------------------------------------
            # 2. Gestão de Risco em Tempo Real (OCO na exchange / Stop Loss / Take Profit)
            # -----------------------------------------------------------------
            positions.check_exits(current_price)

            # -----------------------------------------------------------------
            # 3. Detecção de Fechamento de Candle
            # -----------------------------------------------------------------
            closed_candle = live_df.iloc[-2]
            closed_ts = int(closed_candle["timestamp"].timestamp() * 1000)
            closed_close = float(closed_candle["close"])

            if closed_ts > last_evaluated_candle_ts:
                last_evaluated_candle_ts = closed_ts
                candles_since_retrain += 1
                closed_time_str = closed_candle["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

                logger.info(f"\n🔔 [NOVO CANDLE FECHADO] {closed_time_str} | Fechamento: ${closed_close:,.2f}")

                # 3.1. Feedback Loop: Avaliar previsões anteriores com o fechamento real
                evaluations = db.evaluate_pending_predictions(
                    symbol, timeframe, closed_ts, closed_close
                )
                for ev in evaluations:
                    status = "✅ ACERTO" if ev["is_correct"] else "❌ ERRO"
                    logger.info(
                        f"   [FEEDBACK IA] Previsão #{ev['id']}: Sinal={ev['prediction']} "
                        f"| Realizado={ev['actual_direction']} -> {status} (Entrada: ${ev['entry_price']:,.2f} -> Fechou: ${ev['actual_close']:,.2f})"
                    )

                # Métricas de Acurácia Viva
                acc_stats = db.get_rolling_accuracy(symbol, timeframe, last_n=30)
                if acc_stats["total"] > 0:
                    logger.info(
                        f"   📊 [ACURÁCIA VIVA] Últimas {acc_stats['total']} previsões: "
                        f"{acc_stats['correct']} acertos ({acc_stats['accuracy_pct']:.1f}% de assertividade real)"
                    )

                # 3.2. Auto-Calibração Dinâmica
                calib = DynamicCalibrator.get_calibration(
                    rolling_accuracy=acc_stats["accuracy_pct"],
                    evaluated_count=acc_stats["total"],
                    base_threshold=confidence_threshold
                )
                active_confidence = calib["threshold"]
                logger.info(f"   {calib['badge']} [AUTO-CALIBRAÇÃO] {calib['mode']} -> Filtro Ativo: {active_confidence:.1%} ({calib['description']})")

                # -------------------------------------------------------------
                # 3.3. Retreinamento Automático por Janela Deslizante
                # -------------------------------------------------------------
                if candles_since_retrain >= retrain_every_n_candles:
                    logger.info(f"\n🔄 Janela Deslizante: {candles_since_retrain} novos candles acumulados. Retreinando e salvando a IA...")
                    retrain_df = db.get_recent_candles(symbol, timeframe, limit=1200)
                    feat_retrain_df, _ = build_features(retrain_df)
                    retrain_dataset = create_target(feat_retrain_df)

                    model_version += 1
                    model, new_acc = train_model(
                        retrain_dataset,
                        feature_cols,
                        model_version=model_version
                    )
                    # Salva a nova versão em disco na pasta 'models/'
                    model_storage.save_model(model, model_version, new_acc, feature_cols, symbol, timeframe)
                    candles_since_retrain = 0

                # -------------------------------------------------------------
                # 3.4. Nova Inferência com a IA sobre o candle fechado
                # -------------------------------------------------------------
                full_history_df = db.get_recent_candles(symbol, timeframe, limit=100)
                feat_full_df, _ = build_features(full_history_df)
                closed_features = feat_full_df.iloc[-2:-1][feature_cols]

                if not closed_features.isna().values.any():
                    pred = int(model.predict(closed_features)[0])
                    probabilities = model.predict_proba(closed_features)[0]
                    prob_down, prob_up = float(probabilities[0]), float(probabilities[1])

                    pred_id = db.record_prediction(
                        symbol=symbol,
                        timeframe=timeframe,
                        model_version=model_version,
                        candle_timestamp=closed_ts,
                        prediction=pred,
                        prob_up=prob_up,
                        prob_down=prob_down,
                        entry_price=closed_close
                    )

                    logger.info(
                        f"   🤖 Previsão #{pred_id} (Modelo v{model_version}): "
                        f"{'ALTA (1)' if pred == 1 else 'BAIXA/NEUTRO (0)'} "
                        f"[Prob. Alta: {prob_up:.1%} | Prob. Baixa: {prob_down:.1%}]"
                    )

                    # Entrada no Trade (Real via Binance ou Simulado)
                    if not positions.in_position and pred == 1 and prob_up >= active_confidence:
                        positions.try_open(current_price, model_version, prob_up)
                    elif not positions.in_position and pred == 1 and prob_up < active_confidence:
                        logger.info(f"   ✋ [SINAL FILTRADO] Probabilidade de alta ({prob_up:.1%}) abaixo do filtro exigido pela calibração ({active_confidence:.1%}).")

            # Heartbeat informativo em tempo real
            try:
                tf_seconds = exchange.parse_timeframe(timeframe)
                now_ms = int(time.time() * 1000)
                candle_close_ms = ((now_ms // (tf_seconds * 1000)) + 1) * (tf_seconds * 1000)
                remaining_sec = max(0, int((candle_close_ms - now_ms) / 1000))
            except Exception:
                remaining_sec = check_interval_seconds

            candles_left = max(0, retrain_every_n_candles - candles_since_retrain)
            pos_info = positions.status_label()

            logger.info(
                f"⏳ [MONITORANDO] {symbol}: ${current_price:,.2f} | "
                f"Fechamento: ~{remaining_sec:02d}s | "
                f"Retreino: {candles_left} candle(s) | "
                f"IA: v{model_version} [{calib['badge']} {calib['mode']}] | Posição: {pos_info}"
            )

            time.sleep(check_interval_seconds)

    except KeyboardInterrupt:
        logger.info("\n" + "=" * 60)
        logger.info("🛑 Execução interrompida pelo usuário.")
        trade_stats = db.get_trade_summary(symbol)
        logger.info(
            f"Relatório Final de Trades: {trade_stats['total_trades']} operações "
            f"({trade_stats['wins']}V / {trade_stats['losses']}D | Taxa de Acerto: {trade_stats['win_rate']:.1f}%)"
        )
        logger.info(f"PnL Acumulado: {trade_stats['total_pnl_pct']:+.2f}% (${trade_stats['total_pnl_usd']:+.2f})")
        if positions.in_position:
            logger.warning("⚠️ Robô encerrado COM POSIÇÃO ABERTA. Ela será restaurada no próximo início"
                           + (" e continua protegida pela OCO na exchange." if positions.position.oco_list_id else "."))
        logger.info("=" * 60)


# -----------------------------------------------------------------------------
# Bloco Principal de Execução (__main__)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Quant Trading Bot com Machine Learning Contínuo em Tempo Real"
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default="1m",
        choices=["1m", "3m", "5m", "15m", "30m", "1h"],
        help="Tempo gráfico dos candles (padrão: 1m para aprendizado rápido a cada minuto)"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTC/USDT",
        help="Par de criptomoeda negociado (padrão: BTC/USDT)"
    )
    parser.add_argument(
        "--retrain",
        type=int,
        default=5,
        help="Retreina a IA a cada N candles fechados (padrão: 5)"
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.55,
        help="Probabilidade mínima base para compra (padrão: 0.55 = 55%%)"
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=0.015,
        help="Percentual de Stop Loss (padrão: 0.015 = 1.5%%)"
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=0.03,
        help="Percentual de Take Profit (padrão: 0.03 = 3.0%%)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Intervalo de checagem em segundos (padrão: 10s)"
    )
    parser.add_argument(
        "--trading-mode",
        type=str,
        default=None,
        choices=["paper", "testnet", "live"],
        help="Modo de execução (paper: simulado, testnet: ordens de teste, live: ordens reais)"
    )
    parser.add_argument(
        "--trade-amount",
        type=float,
        default=None,
        help="Valor em USDT alocado para cada compra na Binance (padrão: do .env ou 50.0)"
    )

    args = parser.parse_args()

    retrain_candles = args.retrain
    if args.timeframe in ["15m", "30m", "1h"] and retrain_candles == 5:
        retrain_candles = 12

    run_continuous_trading_bot(
        symbol=args.symbol,
        timeframe=args.timeframe,
        retrain_every_n_candles=retrain_candles,
        confidence_threshold=args.confidence,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        check_interval_seconds=args.interval,
        trading_mode=args.trading_mode,
        trade_amount_usdt=args.trade_amount
    )
