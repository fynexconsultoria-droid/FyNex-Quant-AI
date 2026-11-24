"""
Calibração de Probabilidades e Limiar Dinâmico de Confiança (FyNex Quant AI)
============================================================================
- PlattCalibratedModel: calibração Platt cronológica (100% serializável via joblib)
- DynamicCalibrator: ajuste adaptativo do filtro de confiança pela precisão viva
- optimize_confidence_threshold: otimização OOF de limiar maximizando PnL líquido
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
from sklearn.linear_model import LogisticRegression

from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT


class PlattCalibratedModel:
    """
    Calibrador Platt cronológico (100% serializável via joblib) que:
    1. Aprende o mapeamento sigmoide em previsões estritamente fora-da-amostra (sem vazamento).
    2. Mantém o estimador base treinado em 100% da janela mais recente com pesos de recência.
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
