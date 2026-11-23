"""
Machine Learning, Engenharia de Features e Persistência de Modelos (FyNex Quant AI)
===================================================================================
- ModelPersistence: salvamento/carregamento atômico de modelos em disco com versionamento.
- DynamicCalibrator: auto-calibração dinâmica do limiar de confiança.
- build_features / create_target: features estacionárias e rotulagem Triple Barrier.
- train_model / evaluate_walk_forward / should_promote_challenger: treino com purge,
  calibração Platt e promoção Campeão vs. Desafiante.
"""

import gc
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
    VotingClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from core.logging_setup import DEFAULT_MODELS_DIR, logger
from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT


# -----------------------------------------------------------------------------
# Persistência de Modelos em Disco
# -----------------------------------------------------------------------------
class ModelPersistence:
    """
    Gerencia o salvamento e carregamento dos modelos treinados em disco,
    mantendo metadados por (symbol, timeframe), gravação atômica (.tmp + os.replace)
    e poda controlada de versões antigas.
    """

    def __init__(self, models_dir: str = DEFAULT_MODELS_DIR, max_versions_kept: int = 5):
        self.models_dir = models_dir
        self.max_versions_kept = max_versions_kept
        os.makedirs(self.models_dir, exist_ok=True)
        self.meta_path = os.path.join(self.models_dir, "metadata.json")

    @staticmethod
    def _sanitize_token(value: str, label: str) -> str:
        clean = (value or "").strip().replace("/", "_")
        if not clean or not re.match(r"^[A-Za-z0-9_.-]{1,40}$", clean) or ".." in clean:
            raise ValueError(f"{label} inválido para nome de arquivo de modelo: {value!r}")
        return clean

    def _safe_model_path(self, filename: str) -> str:
        base_real = os.path.realpath(self.models_dir)
        target_real = os.path.realpath(os.path.join(base_real, filename))
        if os.path.commonpath([base_real, target_real]) != base_real:
            raise ValueError(f"Caminho fora do diretório de modelos rejeitado: {filename!r}")
        return target_real

    def get_meta_path(self, symbol: str, timeframe: str) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        return self._safe_model_path(f"metadata_{clean_symbol}_{clean_tf}.json")

    @staticmethod
    def _atomic_dump_joblib(obj: Any, target_path: str) -> None:
        directory = os.path.dirname(os.path.abspath(target_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".model_", suffix=".tmp")
        os.close(fd)
        try:
            joblib.dump(obj, tmp_path, compress=3)
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _atomic_dump_json(data: Dict[str, Any], target_path: str) -> None:
        directory = os.path.dirname(os.path.abspath(target_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".meta_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def feature_schema_hash(feature_cols: List[str]) -> str:
        raw = json.dumps(list(feature_cols), separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    def _list_versioned_files(self, clean_symbol: str, timeframe: str) -> List[Tuple[int, float, str]]:
        prefix = "model_v"
        suffix = f"_{clean_symbol}_{timeframe}.joblib"
        versioned: List[Tuple[int, float, str]] = []
        if not os.path.isdir(self.models_dir):
            return versioned
        for fname in os.listdir(self.models_dir):
            if fname.startswith(prefix) and fname.endswith(suffix):
                ver_str = fname[len(prefix):-len(suffix)]
                if ver_str.isdigit():
                    fpath = os.path.join(self.models_dir, fname)
                    try:
                        mtime = os.path.getmtime(fpath)
                    except OSError:
                        mtime = 0.0
                    versioned.append((int(ver_str), mtime, fpath))
        return versioned

    def next_version(self, symbol: str, timeframe: str) -> int:
        """Retorna a próxima versão monotonicamente crescente (nunca volta para v1)."""
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        disk_versions = [v for v, _, _ in self._list_versioned_files(clean_symbol, clean_tf)]
        meta = self.load_metadata(symbol, timeframe)
        meta_ver = int(meta.get("version", 0)) if meta else 0
        highest = max(disk_versions + [meta_ver, 0])
        return highest + 1

    def archive_incompatible_models(self, symbol: str, timeframe: str) -> int:
        """Move modelos antigos/incompatíveis do par/timeframe para `models/archive/`."""
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        archive_dir = self._safe_model_path("archive")
        os.makedirs(archive_dir, exist_ok=True)
        moved = 0
        for _, _, fpath in self._list_versioned_files(clean_symbol, clean_tf):
            dst = os.path.join(archive_dir, os.path.basename(fpath))
            try:
                shutil.move(fpath, dst)
                moved += 1
            except OSError:
                pass
        if moved > 0:
            logger.info(f"📦 [ARQUIVO] {moved} modelo(s) incompatível(is) movido(s) para '{archive_dir}'.")
        return moved

    def _prune_old_versions(
        self,
        clean_symbol: str,
        timeframe: str,
        active_version: Optional[int] = None,
    ) -> None:
        if self.max_versions_kept <= 0:
            return
        versioned = self._list_versioned_files(clean_symbol, timeframe)
        versioned.sort(key=lambda x: (x[1], x[0]), reverse=True)
        kept: List[Tuple[int, float, str]] = []
        to_remove: List[Tuple[int, float, str]] = []
        for item in versioned:
            ver, _, _ = item
            if active_version is not None and ver == active_version:
                kept.append(item)
            elif len(kept) < self.max_versions_kept:
                kept.append(item)
            else:
                to_remove.append(item)
        for _, _, old_path in to_remove:
            try:
                os.remove(old_path)
            except OSError:
                pass

    def save_model(
        self,
        model: RandomForestClassifier,
        version: int,
        accuracy: float,
        feature_cols: List[str],
        symbol: str,
        timeframe: str,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        version_filename = f"model_v{int(version)}_{clean_symbol}_{clean_tf}.joblib"
        version_path = self._safe_model_path(version_filename)
        latest_path = self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib")

        self._atomic_dump_joblib(model, version_path)
        self._atomic_dump_joblib(model, latest_path)
        self._prune_old_versions(clean_symbol, clean_tf, active_version=int(version))

        metadata: Dict[str, Any] = {
            "version": version,
            "accuracy": float(accuracy),
            "feature_cols": feature_cols,
            "feature_schema_hash": self.feature_schema_hash(feature_cols),
            "symbol": symbol,
            "timeframe": timeframe,
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "file": version_filename,
        }
        if extra_meta:
            metadata.update(extra_meta)

        pair_meta_path = self.get_meta_path(symbol, timeframe)
        self._atomic_dump_json(metadata, pair_meta_path)
        self._atomic_dump_json(metadata, self.meta_path)

        logger.info(f"💾 [MODELO SALVO EM DISCO] Versão v{version} gravada em '{version_path}' (Score: {accuracy * 100:.2f}%)")
        return version_path

    def load_metadata(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        pair_meta_path = self.get_meta_path(symbol, timeframe)
        candidates = [pair_meta_path, self.meta_path]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("symbol") == symbol and meta.get("timeframe") == timeframe:
                        return meta
                except Exception:
                    continue
        return None

    def load_latest_model(
        self,
        symbol: str,
        timeframe: str,
    ) -> Optional[Tuple[RandomForestClassifier, int, float, List[str]]]:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        latest_path = self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib")

        if not os.path.exists(latest_path):
            return None

        meta = self.load_metadata(symbol, timeframe)
        if not meta:
            return None

        try:
            referenced_file = meta.get("file")
            if referenced_file:
                try:
                    ref_path = self._safe_model_path(str(referenced_file))
                except ValueError:
                    logger.warning(f"Caminho inválido referenciado no metadata ('{referenced_file}'); ignorando.")
                    ref_path = ""
                if ref_path and not os.path.exists(ref_path):
                    logger.warning(f"Arquivo referenciado no metadata ('{referenced_file}') não existe; usando latest.")
            model = joblib.load(latest_path)
            version = int(meta.get("version", 1))
            acc = float(meta.get("accuracy", 0.5))
            features = meta.get("feature_cols", [])

            logger.info(f"🧠 [MODELO RECUPERADO DE DISCO] Carregado Modelo v{version} ({symbol} {timeframe}) (Acurácia: {acc * 100:.2f}%)")
            return model, version, acc, features
        except Exception as e:
            logger.warning(f"Não foi possível restaurar modelo salvo: {e}. Um novo modelo será treinado.")
            return None


class PlattCalibratedModel:
    """
    Calibrador Platt cronológico (100% serializável via joblib) que:
    1. Aprende o mapeamento sigmoide em previsões estritamente fora-da-amostra (sem vazamento).
    2. Mantém o estimador base treinado em 100% da janela mais recente com pesos de recência,
       eliminando a perda dos últimos 25%–33% candles que ocorria com `CalibratedClassifierCV(TimeSeriesSplit)`.
    """

    def __init__(self, base_estimator: Any, calibrator: Optional[LogisticRegression] = None):
        self.base_estimator = base_estimator
        self.calibrator = calibrator
        self.classes_ = np.asarray(getattr(base_estimator, "classes_", [0, 1]))

    @staticmethod
    def _to_logit(p: np.ndarray) -> np.ndarray:
        clipped = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
        return np.log(clipped / (1.0 - clipped)).reshape(-1, 1)

    def predict_proba(self, X: Any) -> np.ndarray:
        raw_probs = self.base_estimator.predict_proba(X)
        classes = list(self.classes_)
        if self.calibrator is None or 1 not in classes or raw_probs.shape[1] < 2:
            return raw_probs
        idx_up = classes.index(1)
        p_up_raw = raw_probs[:, idx_up]
        cal_probs = self.calibrator.predict_proba(self._to_logit(p_up_raw))
        cal_classes = list(self.calibrator.classes_)
        p_up_cal = cal_probs[:, cal_classes.index(1)] if 1 in cal_classes else p_up_raw
        p_up_cal = np.clip(p_up_cal, 1e-6, 1.0 - 1e-6)
        out = np.zeros((len(p_up_cal), len(classes)), dtype=float)
        out[:, idx_up] = p_up_cal
        other_idx = 1 - idx_up if len(classes) == 2 else 0
        out[:, other_idx] = 1.0 - p_up_cal
        return out

    def predict(self, X: Any) -> np.ndarray:
        probs = self.predict_proba(X)
        classes = list(self.classes_)
        if 1 not in classes:
            return np.zeros(len(probs), dtype=int)
        return (probs[:, classes.index(1)] >= 0.5).astype(int)


# -----------------------------------------------------------------------------
# Auto-Calibração Dinâmica de Confiança
# -----------------------------------------------------------------------------
class DynamicCalibrator:
    """
    Ajusta dinamicamente o rigor de probabilidade priorizando a PRECISÃO REAL
    dos sinais de compra (classe 1). Nunca afrouxa o limiar usando acurácia global
    em datasets desbalanceados antes de acumular amostra mínima de compras.
    """

    @staticmethod
    def get_calibration(
        rolling_accuracy: float,
        evaluated_count: int,
        base_threshold: float = 0.55,
        buy_precision_pct: Optional[float] = None,
        buy_evaluated_count: Optional[int] = None,
        min_samples: int = 12,
    ) -> Dict[str, Any]:
        # Se o chamador informou buy_evaluated_count, exige amostra suficiente de compras
        if buy_evaluated_count is not None:
            if buy_evaluated_count < min_samples or buy_precision_pct is None:
                return {
                    "threshold": base_threshold,
                    "mode": "CALIBRAÇÃO INICIAL",
                    "badge": "ℹ️",
                    "description": (
                        f"Coletando compras avaliadas ({buy_evaluated_count}/{min_samples}). "
                        f"Régua padrão de {base_threshold:.0%}."
                    ),
                }
            metric = float(buy_precision_pct)
            count = int(buy_evaluated_count)
        else:
            metric = float(rolling_accuracy)
            count = int(evaluated_count)
            if count < min_samples:
                return {
                    "threshold": base_threshold,
                    "mode": "CALIBRAÇÃO INICIAL",
                    "badge": "ℹ️",
                    "description": f"Coletando amostras ({count}/{min_samples}). Régua padrão de {base_threshold:.0%}.",
                }

        if metric < 45.0:
            return {
                "threshold": max(0.65, base_threshold + 0.08),
                "mode": "PROTEÇÃO ATIVA",
                "badge": "🛡️",
                "description": f"Mercado errático (Precisão {metric:.1f}%). Exigência elevada para evitar armadilhas.",
            }
        if metric < 55.0:
            return {
                "threshold": base_threshold,
                "mode": "REGIME NEUTRO",
                "badge": "⚖️",
                "description": f"Precisão em {metric:.1f}%. Operando com confiança padrão de {base_threshold:.0%}.",
            }

        # Só afrouxa levemente quando há precisão real de compras comprovada
        if buy_evaluated_count is None:
            return {
                "threshold": base_threshold,
                "mode": "REGIME FAVORÁVEL",
                "badge": "🚀",
                "description": f"Sincronia positiva ({metric:.1f}%). Mantendo filtro seguro de {base_threshold:.0%}.",
            }

        optimized_th = max(0.55, base_threshold - 0.02)
        return {
            "threshold": optimized_th,
            "mode": "REGIME FAVORÁVEL",
            "badge": "🚀",
            "description": f"Alta sincronia (Precisão {metric:.1f}%). Confiança otimizada para {optimized_th:.0%}.",
        }


# -----------------------------------------------------------------------------
# Engenharia de Features Técnicas (Estacionárias / Invariantes à Escala)
# -----------------------------------------------------------------------------
def build_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Calcula 22 indicadores estacionários (100% invariantes à escala de preço):
    - Momentum e tendência: RSI 14, MACD normalizado, razão SMA9/SMA21, razão EMA12/EMA26, retornos multi-horizonte
    - Volatilidade e regime: ATR %, Bollinger %B, Bollinger Width, ADX 14, volatilidade realizada
    - Volume e estrutura do candle: variação de volume, z-score de volume, corpo e pavios relativos
    - Sazonalidade intradiária cíclica (sin/cos da hora)
    """
    df = df.copy()
    close = df["close"].replace(0, np.nan)

    # 1. RSI (14)
    df["rsi_14"] = RSIIndicator(close=df["close"], window=14).rsi()

    # 2. MACD (12, 26, 9) normalizado pelo preço
    macd_indicator = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_line"] = macd_indicator.macd() / close
    df["macd_signal"] = macd_indicator.macd_signal() / close
    df["macd_diff"] = macd_indicator.macd_diff() / close

    # 3. ATR Normalizado (% do preço)
    atr_indicator = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14)
    df["atr_pct"] = atr_indicator.average_true_range() / close

    # 4. Médias Móveis (SMA 9/21 e EMA 12/26)
    sma_9 = SMAIndicator(close=df["close"], window=9).sma_indicator()
    sma_21 = SMAIndicator(close=df["close"], window=21).sma_indicator()
    df["sma_ratio"] = (sma_9 / sma_21) - 1.0

    ema_12 = EMAIndicator(close=df["close"], window=12).ema_indicator()
    ema_26 = EMAIndicator(close=df["close"], window=26).ema_indicator()
    df["ema_trend_ratio"] = (ema_12 / ema_26) - 1.0

    # 5. Bandas de Bollinger (posição relativa %B e largura normalizada)
    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low = bb.bollinger_lband()
    bb_mid = bb.bollinger_mavg().replace(0, np.nan)
    bb_span = (bb_high - bb_low).replace(0, np.nan)
    df["bb_pos"] = ((df["close"] - bb_low) / bb_span).clip(-0.5, 1.5)
    df["bb_width"] = (bb_high - bb_low) / bb_mid

    # 6. ADX 14 (força da tendência, normalizado em 0..1)
    if len(df) >= 28:
        adx_val = ADXIndicator(high=df["high"], low=df["low"], close=df["close"], window=14).adx()
        df["adx_14"] = (adx_val / 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    else:
        df["adx_14"] = 0.0

    # 7. Momentum em múltiplos horizontes, volatilidade e dinâmica de volume
    df["return_1"] = df["close"].pct_change(1)
    df["return_3"] = df["close"].pct_change(3)
    df["return_5"] = df["close"].pct_change(5)
    df["return_10"] = df["close"].pct_change(10)
    df["volatility_10"] = df["return_1"].rolling(10).std()
    df["volume_change"] = df["volume"].pct_change(1).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-2.0, 5.0)

    vol_mean = df["volume"].rolling(20).mean()
    vol_std = df["volume"].rolling(20).std().replace(0, np.nan)
    df["volume_zscore_20"] = ((df["volume"] - vol_mean) / vol_std).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-3.0, 5.0)

    # 8. Estrutura do candle (invariante à escala)
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    body_top = np.maximum(df["open"], df["close"])
    body_bot = np.minimum(df["open"], df["close"])
    df["candle_body_pct"] = ((df["close"] - df["open"]) / candle_range).fillna(0.0).clip(-1.0, 1.0)
    df["upper_wick_pct"] = ((df["high"] - body_top) / candle_range).fillna(0.0).clip(0.0, 1.0)
    df["lower_wick_pct"] = ((body_bot - df["low"]) / candle_range).fillna(0.0).clip(0.0, 1.0)

    # 9. Sazonalidade intradiária cíclica
    if "timestamp" in df.columns:
        ts_col = df["timestamp"]
        if pd.api.types.is_datetime64_any_dtype(ts_col):
            hours = ts_col.dt.hour + ts_col.dt.minute / 60.0
        elif pd.api.types.is_numeric_dtype(ts_col):
            ts_dt = pd.to_datetime(ts_col, unit="ms", errors="coerce")
            hours = (ts_dt.dt.hour + ts_dt.dt.minute / 60.0).fillna(0.0)
        else:
            hours = pd.Series(0.0, index=df.index)
    else:
        hours = pd.Series(0.0, index=df.index)
    df["hour_sin"] = np.sin(2.0 * np.pi * hours / 24.0)
    df["hour_cos"] = np.cos(2.0 * np.pi * hours / 24.0)

    feature_cols = [
        "rsi_14",
        "macd_line",
        "macd_signal",
        "macd_diff",
        "atr_pct",
        "sma_ratio",
        "ema_trend_ratio",
        "bb_pos",
        "bb_width",
        "adx_14",
        "return_1",
        "return_3",
        "return_5",
        "return_10",
        "volatility_10",
        "volume_change",
        "volume_zscore_20",
        "candle_body_pct",
        "upper_wick_pct",
        "lower_wick_pct",
        "hour_sin",
        "hour_cos",
    ]

    # Garante que nenhuma feature contenha +/-inf (que quebraria RandomForest/ExtraTrees)
    for col in feature_cols:
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    return df, feature_cols


# -----------------------------------------------------------------------------
# Variável Alvo (Triple Barrier / Custo-Consciente)
# -----------------------------------------------------------------------------
def create_target(
    df: pd.DataFrame,
    mode: str = "next_candle_cost",
    take_profit_pct: float = 0.004,
    stop_loss_pct: float = 0.003,
    horizon_candles: int = 15,
    min_net_return_pct: float = 0.0,
    use_atr_barriers: bool = False,
    atr_tp_mult: float = 2.0,
    atr_sl_mult: float = 1.3,
    min_tp_pct: float = 0.008,
    max_tp_pct: float = 0.025,
    min_sl_pct: float = 0.005,
    max_sl_pct: float = 0.015,
) -> pd.DataFrame:
    """
    Constrói a variável dependente binária (`Target`) sem look-ahead bias:
    - `triple_barrier`: Target=1 se TP for atingido antes do SL dentro de `horizon_candles`.
      Quando `use_atr_barriers=True` e `atr_pct` existe no DataFrame, escala TP/SL
      dinamicamente pela volatilidade (ATR) de cada candle (lucro razoável adaptado ao mercado).
    - `next_candle_cost`: Target=1 se o próximo fechamento superar `close * (1 + min_net_return_pct)`.
    """
    df = df.copy()

    if mode == "triple_barrier":
        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        atr_vals = df["atr_pct"].to_numpy(dtype=float) if (use_atr_barriers and "atr_pct" in df.columns) else None
        n = len(df)
        targets = np.full(n, np.nan, dtype=float)
        h = max(1, int(horizon_candles))

        for i in range(0, max(0, n - h)):
            entry = closes[i]
            if not (np.isfinite(entry) and entry > 0.0):
                continue
            fut_highs = highs[i + 1: i + h + 1]
            fut_lows = lows[i + 1: i + h + 1]
            if not (np.all(np.isfinite(fut_highs)) and np.all(np.isfinite(fut_lows))):
                continue
            if atr_vals is not None and np.isfinite(atr_vals[i]) and atr_vals[i] > 0:
                eff_tp = min(max(atr_vals[i] * atr_tp_mult, min_tp_pct), max_tp_pct)
                eff_sl = min(max(atr_vals[i] * atr_sl_mult, min_sl_pct), max_sl_pct)
            else:
                eff_tp = take_profit_pct
                eff_sl = stop_loss_pct
            tp_level = entry * (1.0 + eff_tp)
            sl_level = entry * (1.0 - eff_sl)
            outcome = 0.0
            for j in range(i + 1, i + h + 1):
                hit_sl = lows[j] <= sl_level
                hit_tp = highs[j] >= tp_level
                if hit_sl and hit_tp:
                    outcome = 0.0
                    break
                if hit_sl:
                    outcome = 0.0
                    break
                if hit_tp:
                    outcome = 1.0
                    break
            targets[i] = outcome

        df["Target"] = targets
    else:
        threshold = df["close"] * (1.0 + float(min_net_return_pct))
        df["Target"] = np.where(df["close"].shift(-1).isna(), np.nan, (df["close"].shift(-1) > threshold).astype(float))

    num_cols = df.select_dtypes(include=[np.number]).columns
    df[num_cols] = df[num_cols].replace([np.inf, -np.inf], np.nan)
    df_clean = df.dropna().copy()
    df_clean["Target"] = df_clean["Target"].astype(int)
    return df_clean


# -----------------------------------------------------------------------------
# Inferência Segura, Validação Walk-Forward e Treinamento
# -----------------------------------------------------------------------------
class InsufficientLabelsError(ValueError):
    """Lançada quando o dataset de treino não possui exemplos suficientes das duas classes."""


def predict_prob_up(model: Any, X: Any) -> float:
    """Retorna a probabilidade da classe 1 (ALTA) de forma segura usando `model.classes_`."""
    classes = list(getattr(model, "classes_", []))
    if 1 not in classes:
        return 0.0
    probs = model.predict_proba(X)[0]
    idx_up = classes.index(1)
    if idx_up >= len(probs):
        return 0.0
    return float(probs[idx_up])


def predict_signal(model: Any, X: Any) -> Tuple[int, float, float]:
    """Retorna `(pred, prob_down, prob_up)` em uma única passagem pelo modelo."""
    classes = list(getattr(model, "classes_", []))
    if 1 not in classes:
        return 0, 1.0, 0.0
    prob_up = predict_prob_up(model, X)
    prob_down = max(0.0, min(1.0, 1.0 - prob_up))
    pred = 1 if prob_up >= 0.5 else 0
    return pred, prob_down, prob_up


def _base_rf() -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=120,
        max_depth=5,
        min_samples_split=10,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=1,
    )


def _base_et() -> ExtraTreesClassifier:
    return ExtraTreesClassifier(
        n_estimators=140,
        max_depth=6,
        min_samples_split=10,
        min_samples_leaf=5,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=1,
    )


def _base_hgb() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=150,
        max_depth=4,
        learning_rate=0.05,
        min_samples_leaf=20,
        l2_regularization=1.0,
        class_weight="balanced",
        random_state=42,
    )


def _build_estimator(model_type: str = "ensemble") -> Any:
    if model_type == "rf":
        return _base_rf()
    if model_type == "et":
        return _base_et()
    if model_type == "hgb":
        return _base_hgb()
    return VotingClassifier(
        estimators=[("rf", _base_rf()), ("et", _base_et()), ("hgb", _base_hgb())],
        voting="soft",
        weights=[1, 1, 2],
    )


def _compute_recency_weights(n: int, decay_floor: float = 0.25) -> np.ndarray:
    """
    Gera pesos exponenciais crescentes (candles mais recentes recebem mais peso no treino),
    normalizados para média 1.0 para preservar a regularização dos estimadores.
    """
    if n <= 1:
        return np.ones(max(1, n), dtype=float)
    floor = max(0.05, min(1.0, float(decay_floor)))
    raw = np.geomspace(floor, 1.0, num=n)
    return raw / float(np.mean(raw))


def _fit_with_weights(estimator: Any, X: Any, y: Any, weights: Optional[np.ndarray]) -> Any:
    """
    Treina o estimador aplicando ponderação temporal (com fallback seguro).
    Evita passar `sample_weight` para `CalibratedClassifierCV(VotingClassifier)` onde o
    scikit-learn ignora pesos no estimador base e distorce a calibração Platt.
    """
    is_calibrated_voting = (
        isinstance(estimator, CalibratedClassifierCV)
        and isinstance(getattr(estimator, "estimator", None), VotingClassifier)
    )
    if weights is not None and len(weights) == len(y) and not is_calibrated_voting:
        try:
            estimator.fit(X, y, sample_weight=weights)
            return estimator
        except TypeError:
            pass
    estimator.fit(X, y)
    return estimator


def _combine_sample_weights(
    n: int,
    extra_sample_weights: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Combina ponderação temporal (recência) com reforço de exemplos difíceis (erros reais)."""
    w = _compute_recency_weights(n)
    if extra_sample_weights is not None and len(extra_sample_weights) == n:
        ext = np.asarray(extra_sample_weights, dtype=float)
        ext = np.where(np.isfinite(ext) & (ext > 0.0), ext, 1.0)
        w = w * ext
        mean_w = float(np.mean(w))
        if mean_w > 0 and np.isfinite(mean_w):
            w = w / mean_w
    return w


def _fit_calibrated_pipeline(
    X_fit: pd.DataFrame,
    y_fit: pd.Series,
    model_type: str = "ensemble",
    gap: int = 15,
    calibrate_probabilities: bool = True,
    extra_sample_weights: Optional[np.ndarray] = None,
) -> Any:
    """
    Pipeline único usado tanto na validação walk-forward quanto no modelo final:
    - Aplica pesos exponenciais de recência + reforço controlado de exemplos difíceis (erros reais)
      no estimador base treinado em 100% de `X_fit`.
    - Quando `calibrate_probabilities=True` e há amostra suficiente, ajusta um calibrador
      Platt 1D sobre previsões estritamente fora-da-amostra (split cronológico com purge `gap`),
      preservando paridade de escala entre validação e produção sem descartar os candles recentes.
    """
    w_full = _combine_sample_weights(len(y_fit), extra_sample_weights)
    base_full = _build_estimator(model_type)
    _fit_with_weights(base_full, X_fit, y_fit, w_full)

    min_class_count = int(y_fit.value_counts().min()) if y_fit.nunique() >= 2 else 0
    cal_split = int(len(X_fit) * 0.8)
    cal_train_end = cal_split - max(0, int(gap))

    if (
        calibrate_probabilities
        and min_class_count >= 10
        and len(X_fit) >= max(80, (gap + 15) * 3)
        and cal_train_end >= 30
        and (len(X_fit) - cal_split) >= 20
    ):
        y_pre = y_fit.iloc[:cal_train_end]
        y_cal = y_fit.iloc[cal_split:]
        if y_pre.nunique() >= 2 and y_cal.nunique() >= 2 and int(y_cal.value_counts().min()) >= 3:
            try:
                pre_extra = (
                    extra_sample_weights[:cal_train_end]
                    if (extra_sample_weights is not None and len(extra_sample_weights) == len(y_fit))
                    else None
                )
                pre_model = _build_estimator(model_type)
                _fit_with_weights(
                    pre_model,
                    X_fit.iloc[:cal_train_end],
                    y_pre,
                    _combine_sample_weights(len(y_pre), pre_extra),
                )
                classes = list(getattr(pre_model, "classes_", []))
                if 1 in classes:
                    p_cal_raw = pre_model.predict_proba(X_fit.iloc[cal_split:])[:, classes.index(1)]
                    platt = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
                    platt.fit(PlattCalibratedModel._to_logit(p_cal_raw), y_cal.to_numpy(dtype=int))
                    return PlattCalibratedModel(base_estimator=base_full, calibrator=platt)
            except Exception as exc:
                logger.warning(f"⚠️ Calibração Platt cronológica falhou ({exc}); usando estimador base.")
    return base_full


def optimize_confidence_threshold(
    y_true: np.ndarray,
    probs_up: np.ndarray,
    tp_pct: float = 0.008,
    sl_pct: float = 0.005,
    fee_pct: float = PAPER_FEE_PCT,
    slippage_pct: float = PAPER_SLIPPAGE_PCT,
    default_threshold: float = 0.55,
    min_signals: int = 5,
) -> Tuple[float, float]:
    """
    Encontra o limiar de probabilidade ótimo fora da amostra (OOF) maximizando
    o PnL simulado líquido de taxas e slippage.
    """
    y_arr = np.asarray(y_true, dtype=int)
    p_arr = np.asarray(probs_up, dtype=float)
    if len(y_arr) == 0:
        return float(default_threshold), 0.0

    roundtrip_cost = 2.0 * (fee_pct + slippage_pct)
    grid = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.65, 0.68)
    best_th = float(default_threshold)
    best_score = (-1e9, -1e9)
    best_pnl = 0.0

    for th in grid:
        mask = p_arr >= th
        n_sig = int(mask.sum())
        if n_sig < min_signals:
            continue
        hits = int((y_arr[mask] == 1).sum())
        misses = n_sig - hits
        pnl = (hits * (tp_pct - roundtrip_cost) - misses * (sl_pct + roundtrip_cost)) * 100.0
        prec = hits / n_sig
        score = (pnl, prec)
        if score > best_score:
            best_score = score
            best_th = float(th)
            best_pnl = float(pnl)

    return round(best_th, 2), round(best_pnl, 3)


def _compute_validation_metrics(
    y_true: np.ndarray,
    probs_up: np.ndarray,
    preds: np.ndarray,
    tp_pct: float = 0.008,
    sl_pct: float = 0.005,
    confidence_threshold: float = 0.55,
    fee_pct: float = PAPER_FEE_PCT,
    slippage_pct: float = PAPER_SLIPPAGE_PCT,
) -> Dict[str, float]:
    """Calcula métricas orientadas a lucro (precisão de compra, Profit Factor, Expectancy, ROC-AUC, Brier e PnL)."""
    y_arr = np.asarray(y_true, dtype=int)
    p_arr = np.asarray(probs_up, dtype=float)
    pred_arr = np.asarray(preds, dtype=int)

    acc = float(accuracy_score(y_arr, pred_arr)) if len(y_arr) > 0 else 0.5
    brier = float(brier_score_loss(y_arr, np.clip(p_arr, 0.0, 1.0))) if len(y_arr) > 0 else 0.25

    buy_mask = (pred_arr == 1) & (p_arr >= confidence_threshold)

    buy_signals = int(buy_mask.sum())
    if buy_signals > 0:
        buy_hits = int((y_arr[buy_mask] == 1).sum())
        buy_misses = buy_signals - buy_hits
        prec_buy = float(buy_hits / buy_signals)
        roundtrip_cost = 2.0 * (fee_pct + slippage_pct)
        net_win = (tp_pct - roundtrip_cost) * 100.0
        net_loss = (sl_pct + roundtrip_cost) * 100.0
        if net_win >= 0:
            gross_win = buy_hits * net_win
            gross_loss = buy_misses * net_loss
        else:
            gross_win = 0.0
            gross_loss = buy_misses * net_loss + buy_hits * abs(net_win)
        sim_pnl_pct = gross_win - gross_loss
        profit_factor = float(min(10.0, gross_win / gross_loss)) if gross_loss > 0 else (10.0 if gross_win > 0 else 0.0)
        expectancy_pct = float(sim_pnl_pct / buy_signals)
    else:
        prec_buy = 0.0
        sim_pnl_pct = 0.0
        profit_factor = 0.0
        expectancy_pct = 0.0

    ll = 0.693
    auc = 0.5
    if len(y_arr) > 0 and len(np.unique(y_arr)) == 2:
        p_clip = np.clip(p_arr, 1e-6, 1.0 - 1e-6)
        probs_2d = np.column_stack([1.0 - p_clip, p_clip])
        ll = float(log_loss(y_arr, probs_2d, labels=[0, 1]))
        try:
            auc = float(roc_auc_score(y_arr, p_clip))
        except Exception:
            auc = 0.5

    return {
        "accuracy": acc,
        "precision_buy": prec_buy,
        "buy_signals": buy_signals,
        "brier_score": brier,
        "sim_pnl_pct": float(sim_pnl_pct),
        "profit_factor": round(profit_factor, 3),
        "expectancy_pct": round(expectancy_pct, 4),
        "roc_auc": round(auc, 4),
        "log_loss": ll,
    }


def _empty_validation_metrics(include_folds: bool = False) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "accuracy": 0.5,
        "precision_buy": 0.0,
        "buy_signals": 0,
        "brier_score": 0.25,
        "sim_pnl_pct": 0.0,
        "profit_factor": 0.0,
        "expectancy_pct": 0.0,
        "roc_auc": 0.5,
        "log_loss": 0.693,
    }
    if include_folds:
        metrics["folds"] = 0
    return metrics


def evaluate_model_on_dataset(
    model: Any,
    df: pd.DataFrame,
    feature_cols: List[str],
    tp_pct: float = 0.008,
    sl_pct: float = 0.005,
    confidence_threshold: float = 0.55,
) -> Dict[str, float]:
    """Avalia um modelo já treinado em um dataset (usado para comparar campeão vs desafiante)."""
    if df.empty or "Target" not in df.columns:
        return _empty_validation_metrics()
    X = df[feature_cols]
    y = df["Target"].to_numpy(dtype=int)
    classes = list(getattr(model, "classes_", []))
    if 1 not in classes:
        probs_up = np.zeros(len(X), dtype=float)
        preds = np.zeros(len(X), dtype=int)
    else:
        idx_up = classes.index(1)
        probs_up = model.predict_proba(X)[:, idx_up]
        preds = model.predict(X)
    return _compute_validation_metrics(
        y, probs_up, preds,
        tp_pct=tp_pct, sl_pct=sl_pct, confidence_threshold=confidence_threshold,
    )


def evaluate_walk_forward(
    df: pd.DataFrame,
    feature_cols: List[str],
    n_splits: int = 4,
    horizon_candles: int = 15,
    tp_pct: float = 0.008,
    sl_pct: float = 0.005,
    confidence_threshold: float = 0.55,
    model_type: str = "ensemble",
    calibrate_probabilities: bool = True,
    extra_sample_weights: Optional[np.ndarray] = None,
) -> Dict[str, float]:
    """
    Validação cruzada temporal com purge (`TimeSeriesSplit(gap=horizon_candles)`),
    usando exatamente o mesmo pipeline calibrado implantado em produção e agregando
    métricas sobre as previsões OOF concatenadas (sem viés de média não ponderada por fold).
    """
    X = df[feature_cols]
    y = df["Target"]
    gap = max(0, int(horizon_candles))

    min_needed = (gap + 15) * 3
    if len(df) < max(40, min_needed) or y.nunique() < 2:
        return _empty_validation_metrics(include_folds=True)

    max_possible_splits = max(2, (len(df) - gap - 10) // max(25, gap + 15))
    effective_splits = min(max(2, n_splits), max_possible_splits)
    if effective_splits < 2:
        effective_splits = 2

    try:
        tscv = TimeSeriesSplit(n_splits=effective_splits, gap=gap)
        splits = list(tscv.split(X))
    except ValueError:
        return _empty_validation_metrics(include_folds=True)

    oof_y: List[np.ndarray] = []
    oof_p: List[np.ndarray] = []
    oof_preds: List[np.ndarray] = []
    valid_folds = 0

    for train_idx, test_idx in splits:
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr = y.iloc[train_idx]
        if y_tr.nunique() < 2 or len(X_te) == 0:
            continue
        fold_extra = (
            extra_sample_weights[train_idx]
            if (extra_sample_weights is not None and len(extra_sample_weights) == len(df))
            else None
        )
        clf = _fit_calibrated_pipeline(
            X_tr,
            y_tr,
            model_type=model_type,
            gap=gap,
            calibrate_probabilities=calibrate_probabilities,
            extra_sample_weights=fold_extra,
        )
        classes = list(getattr(clf, "classes_", []))
        if 1 in classes:
            probs_up = clf.predict_proba(X_te)[:, classes.index(1)]
            preds = (probs_up >= 0.5).astype(int)
        else:
            probs_up = np.zeros(len(X_te), dtype=float)
            preds = np.zeros(len(X_te), dtype=int)
        oof_y.append(y.iloc[test_idx].to_numpy(dtype=int))
        oof_p.append(probs_up)
        oof_preds.append(preds)
        valid_folds += 1

    if valid_folds == 0 or not oof_y:
        return _empty_validation_metrics(include_folds=True)

    all_y = np.concatenate(oof_y)
    all_p = np.concatenate(oof_p)
    all_preds = np.concatenate(oof_preds)

    agg = _compute_validation_metrics(
        all_y,
        all_p,
        all_preds,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        confidence_threshold=confidence_threshold,
    )
    opt_th, opt_pnl = optimize_confidence_threshold(
        all_y,
        all_p,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        default_threshold=confidence_threshold,
    )
    agg["optimal_threshold"] = opt_th
    agg["optimal_sim_pnl_pct"] = opt_pnl
    agg["folds"] = valid_folds
    return agg


def should_promote_challenger(
    challenger_metrics: Dict[str, float],
    champion_metrics: Optional[Dict[str, float]],
    min_precision_buy: float = 0.50,
    min_buy_signals: int = 20,
) -> bool:
    """Decide se o modelo desafiante deve substituir o campeão atual na mesma janela."""
    if not champion_metrics:
        return True

    chal_prec = float(challenger_metrics.get("precision_buy", 0.0))
    champ_prec = float(champion_metrics.get("precision_buy", 0.0))

    if "sim_pnl_pct" in challenger_metrics and "sim_pnl_pct" in champion_metrics:
        chal_pnl = float(challenger_metrics["sim_pnl_pct"])
        champ_pnl = float(champion_metrics["sim_pnl_pct"])
        chal_buys = int(challenger_metrics.get("buy_signals", 0))
        if chal_buys < min_buy_signals:
            return False
        if chal_prec < min_precision_buy:
            return False
        return chal_pnl > max(0.0, champ_pnl) and chal_prec >= (champ_prec - 0.01)

    chal_acc = float(challenger_metrics.get("accuracy", 0.5))
    champ_acc = float(champion_metrics.get("accuracy", 0.5))
    chal_score = 0.6 * chal_prec + 0.4 * chal_acc
    champ_score = 0.6 * champ_prec + 0.4 * champ_acc
    return chal_score >= (champ_score - 0.005) and (chal_prec >= min_precision_buy or chal_score >= champ_score)


def train_model(
    df: pd.DataFrame,
    feature_cols: List[str],
    model_version: int = 1,
    train_split_ratio: float = 0.8,
    calibrate_probabilities: bool = True,
    refit_full: bool = True,
    horizon_candles: int = 15,
    tp_pct: float = 0.008,
    sl_pct: float = 0.005,
    confidence_threshold: float = 0.55,
    min_pos_rate: float = 0.05,
    max_pos_rate: float = 0.95,
    check_balance: bool = True,
    model_type: str = "ensemble",
    run_walk_forward_eval: bool = True,
    extra_sample_weights: Optional[np.ndarray] = None,
) -> Tuple[Any, float]:
    """
    Treina/Retreina o classificador (`ensemble` = RF + ExtraTrees + HistGradientBoosting, `hgb`, `et` ou `rf`)
    preservando a cronologia temporal com purge e calibração Platt cronológica alinhada à validação.
    """
    if df.empty or "Target" not in df.columns:
        raise InsufficientLabelsError("Dataset vazio ou sem coluna Target.")

    X = df[feature_cols]
    y = df["Target"]

    if y.nunique() < 2:
        unique_vals = list(y.unique())
        raise InsufficientLabelsError(
            f"Dataset de treino possui apenas 1 classe ({unique_vals}) em {len(df)} amostras. "
            f"Treinamento abortado para evitar modelo degenerado."
        )

    pos_rate = float(y.mean())
    if check_balance and (pos_rate < min_pos_rate or pos_rate > max_pos_rate):
        raise InsufficientLabelsError(
            f"Taxa de rótulos positivos fora da faixa aceitável ({pos_rate:.1%}, exigido "
            f"{min_pos_rate:.0%}–{max_pos_rate:.0%}) em {len(df)} amostras. "
            f"Revise TP/SL/horizonte do timeframe."
        )

    gap = max(0, int(horizon_candles))
    split_index = max(1, int(len(df) * train_split_ratio))
    if gap > 0 and split_index <= gap + 15:
        raise InsufficientLabelsError(
            f"Dataset curto demais para manter purge gap={gap} ({len(df)} amostras)."
        )
    train_end = max(1, split_index - gap)

    X_train, X_test = X.iloc[:train_end], X.iloc[split_index:]
    y_train = y.iloc[:train_end]

    if y_train.nunique() < 2:
        raise InsufficientLabelsError(
            f"Split de treino possui apenas 1 classe ({list(y_train.unique())}). "
            f"Ajuste os parâmetros de alvo ou aguarde mais dados."
        )

    # Mantém a validação holdout e walk-forward OOF 100% livre de postmortems futuros;
    # aplica `extra_sample_weights` (hard-examples) somente no refit final de produção (`refit_full=True`).
    has_extra_w = extra_sample_weights is not None and len(extra_sample_weights) == len(df)
    if run_walk_forward_eval:
        base_eval_model = _fit_calibrated_pipeline(
            X_train,
            y_train,
            model_type=model_type,
            gap=gap,
            calibrate_probabilities=calibrate_probabilities,
            extra_sample_weights=None,
        )
        if len(X_test) > 0:
            holdout_m = evaluate_model_on_dataset(
                base_eval_model, df.iloc[split_index:], feature_cols,
                tp_pct=tp_pct, sl_pct=sl_pct, confidence_threshold=confidence_threshold,
            )
        else:
            holdout_m = _empty_validation_metrics()

        wf_metrics = evaluate_walk_forward(
            df, feature_cols, n_splits=4, horizon_candles=gap,
            tp_pct=tp_pct, sl_pct=sl_pct, confidence_threshold=confidence_threshold,
            model_type=model_type,
            calibrate_probabilities=calibrate_probabilities,
            extra_sample_weights=None,
        )
    else:
        holdout_m = _empty_validation_metrics()
        wf_metrics = _empty_validation_metrics(include_folds=True)

    use_wf = wf_metrics["folds"] > 0
    accuracy = wf_metrics["accuracy"] if use_wf else holdout_m["accuracy"]
    precision_buy = wf_metrics["precision_buy"] if use_wf else holdout_m["precision_buy"]
    brier_score = wf_metrics["brier_score"] if use_wf else holdout_m["brier_score"]
    sim_pnl_pct = wf_metrics["sim_pnl_pct"] if use_wf else holdout_m["sim_pnl_pct"]
    buy_signals = wf_metrics["buy_signals"] if use_wf else holdout_m["buy_signals"]
    profit_factor = wf_metrics.get("profit_factor", holdout_m["profit_factor"]) if use_wf else holdout_m["profit_factor"]
    expectancy_pct = wf_metrics.get("expectancy_pct", holdout_m["expectancy_pct"]) if use_wf else holdout_m["expectancy_pct"]
    roc_auc = wf_metrics.get("roc_auc", holdout_m["roc_auc"]) if use_wf else holdout_m["roc_auc"]
    optimal_threshold = wf_metrics.get("optimal_threshold", confidence_threshold)
    optimal_sim_pnl_pct = wf_metrics.get("optimal_sim_pnl_pct", sim_pnl_pct)

    X_fit = X if refit_full else X_train
    y_fit = y if refit_full else y_train
    fit_extra = extra_sample_weights if (refit_full and has_extra_w) else None

    final_model = _fit_calibrated_pipeline(
        X_fit,
        y_fit,
        model_type=model_type,
        gap=gap,
        calibrate_probabilities=calibrate_probabilities,
        extra_sample_weights=fit_extra,
    )

    raw_final = getattr(final_model, "base_estimator", final_model)
    top_features_list: List[Dict[str, Any]] = []
    top_feats = "N/A"
    imp_vec: Optional[np.ndarray] = None
    if hasattr(raw_final, "feature_importances_"):
        imp_vec = np.asarray(raw_final.feature_importances_, dtype=float)
    elif hasattr(raw_final, "named_estimators_"):
        parts = []
        for name in ("rf", "et"):
            est = raw_final.named_estimators_.get(name)
            if est is not None and hasattr(est, "feature_importances_"):
                parts.append(np.asarray(est.feature_importances_, dtype=float))
        if parts:
            imp_vec = np.mean(parts, axis=0)

    if imp_vec is not None and len(imp_vec) == len(feature_cols):
        importances = pd.Series(imp_vec, index=feature_cols).sort_values(ascending=False)
        top_feats = ", ".join([f"{k}: {v:.1%}" for k, v in importances.head(3).items()])
        top_features_list = [
            {"feature": str(k), "importance": round(float(v), 4)}
            for k, v in importances.head(6).items()
        ]

    final_model.validation_metrics_ = {
        "model_type": model_type,
        "accuracy": float(accuracy),
        "holdout_accuracy": float(holdout_m["accuracy"]),
        "precision_buy": float(precision_buy),
        "holdout_precision_buy": float(holdout_m["precision_buy"]),
        "buy_signals": int(buy_signals),
        "brier_score": float(brier_score),
        "sim_pnl_pct": float(sim_pnl_pct),
        "profit_factor": float(profit_factor),
        "expectancy_pct": float(expectancy_pct),
        "roc_auc": float(roc_auc),
        "optimal_threshold": float(optimal_threshold),
        "optimal_sim_pnl_pct": float(optimal_sim_pnl_pct),
        "top_features": top_features_list,
        "log_loss": float(wf_metrics.get("log_loss", 0.693)),
        "folds": int(wf_metrics.get("folds", 0)),
        "samples": int(len(df)),
        "positive_rate": float(pos_rate),
    }

    if run_walk_forward_eval:
        logger.info(
            f"⚡ [RETREINO ML v{model_version} | {model_type.upper()}] Amostras: {len(df)} (Pos: {pos_rate:.1%}) | "
            f"Prec.Compra: {precision_buy * 100:.1f}% ({buy_signals} sinais) | "
            f"PF: {profit_factor:.2f} | AUC: {roc_auc:.3f} | PnL: {sim_pnl_pct:+.2f}% | Th Ótimo: {optimal_threshold:.0%}"
        )
        logger.info(f"   Top Features: {top_feats}")

    return final_model, float(precision_buy)
