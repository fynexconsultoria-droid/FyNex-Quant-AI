"""Pacote de Auto-Análise de Erros e Aprendizado Adaptativo (FyNex Quant AI)."""

from core.error_learning.detectors import (
    CAUSE_CATALOG,
    clean_params_dict as _clean_params_dict,
    detect_risk_factors,
    diagnose_error_parameters,
)
from core.error_learning.store import ErrorLearningStore
from core.error_learning.weighting import compute_hard_example_multipliers
from core.infra.utils import finite_float as _finite_float, utc_now_str as _utc_now_str

__all__ = [
    "CAUSE_CATALOG",
    "ErrorLearningStore",
    "detect_risk_factors",
    "diagnose_error_parameters",
    "compute_hard_example_multipliers",
    "_clean_params_dict",
    "_finite_float",
    "_utc_now_str",
]
