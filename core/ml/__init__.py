"""
Pacote de Machine Learning, Engenharia de Features e Persistência (FyNex Quant AI)
==================================================================================
Re-exporta toda a interface pública do antigo `core/ml.py` (incluindo
`PlattCalibratedModel` para compatibilidade total com artefatos `.joblib` salvos).
"""

from core.ml.calibration import (
    DynamicCalibrator,
    PlattCalibratedModel,
    optimize_confidence_threshold,
)
from core.ml.features import FEATURE_COLUMNS, build_features
from core.ml.labeling import create_target
from core.ml.persistence import ModelPersistence
from core.ml.training import (
    InsufficientLabelsError,
    _base_et,
    _base_hgb,
    _base_rf,
    _build_estimator,
    _combine_sample_weights,
    _compute_recency_weights,
    _compute_validation_metrics,
    _empty_validation_metrics,
    _fit_calibrated_pipeline,
    _fit_with_weights,
    evaluate_model_on_dataset,
    evaluate_walk_forward,
    predict_prob_up,
    predict_signal,
    should_promote_challenger,
    train_model,
)

__all__ = [
    "FEATURE_COLUMNS",
    "ModelPersistence",
    "PlattCalibratedModel",
    "DynamicCalibrator",
    "InsufficientLabelsError",
    "build_features",
    "create_target",
    "optimize_confidence_threshold",
    "predict_prob_up",
    "predict_signal",
    "_base_rf",
    "_base_et",
    "_base_hgb",
    "_build_estimator",
    "_compute_recency_weights",
    "_combine_sample_weights",
    "_fit_with_weights",
    "_fit_calibrated_pipeline",
    "_compute_validation_metrics",
    "_empty_validation_metrics",
    "evaluate_model_on_dataset",
    "evaluate_walk_forward",
    "should_promote_challenger",
    "train_model",
]
