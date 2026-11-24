"""
Treinamento, Validação Walk-Forward e Inferência Segura (FyNex Quant AI)
========================================================================
Implementa estimadores (`rf`, `et`, `hgb`, `ensemble`), ponderação por recência
+ hard-examples, calibração Platt cronológica, validação walk-forward com purge
e critério quantitativo de promoção Campeão vs. Desafiante.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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

from core.logging_setup import logger
from core.ml.calibration import PlattCalibratedModel, optimize_confidence_threshold
from strategy_config import PAPER_FEE_PCT, PAPER_SLIPPAGE_PCT


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
    """Pesos exponenciais crescentes (candles mais recentes recebem maior peso), normalizados para média 1."""
    if n <= 1:
        return np.ones(max(1, n), dtype=float)
    floor = max(0.05, min(1.0, float(decay_floor)))
    raw = np.geomspace(floor, 1.0, num=n)
    return raw / float(np.mean(raw))


def _fit_with_weights(estimator: Any, X: Any, y: Any, weights: Optional[np.ndarray]) -> Any:
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
    """Validação cruzada temporal com purge (`TimeSeriesSplit(gap=horizon_candles)`)."""
    X = df[feature_cols]
    y = df["Target"]
    gap = max(0, int(horizon_candles))

    min_needed = (gap + 15) * 3
    if len(df) < max(40, min_needed) or y.nunique() < 2:
        return _empty_validation_metrics(include_folds=True)

    max_possible_splits = max(2, (len(df) - gap - 10) // max(25, gap + 15))
    effective_splits = max(2, min(max(2, n_splits), max_possible_splits))

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


def _extract_top_features(final_model: Any, feature_cols: List[str]) -> Tuple[str, List[Dict[str, Any]]]:
    raw_final = getattr(final_model, "base_estimator", final_model)
    imp_vec: Optional[np.ndarray] = None
    if hasattr(raw_final, "feature_importances_"):
        imp_vec = np.asarray(raw_final.feature_importances_, dtype=float)
    elif hasattr(raw_final, "named_estimators_"):
        parts = [
            np.asarray(est.feature_importances_, dtype=float)
            for name in ("rf", "et")
            if (est := raw_final.named_estimators_.get(name)) is not None and hasattr(est, "feature_importances_")
        ]
        if parts:
            imp_vec = np.mean(parts, axis=0)

    if imp_vec is None or len(imp_vec) != len(feature_cols):
        return "N/A", []

    importances = pd.Series(imp_vec, index=feature_cols).sort_values(ascending=False)
    top_str = ", ".join([f"{k}: {v:.1%}" for k, v in importances.head(3).items()])
    top_list = [
        {"feature": str(k), "importance": round(float(v), 4)}
        for k, v in importances.head(6).items()
    ]
    return top_str, top_list


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
    Treina/Retreina o classificador (`ensemble`, `hgb`, `et` ou `rf`) preservando a cronologia
    temporal com purge e calibração Platt cronológica alinhada à validação.
    """
    if df.empty or "Target" not in df.columns:
        raise InsufficientLabelsError("Dataset vazio ou sem coluna Target.")

    X = df[feature_cols]
    y = df["Target"]

    if y.nunique() < 2:
        raise InsufficientLabelsError(
            f"Dataset de treino possui apenas 1 classe ({list(y.unique())}) em {len(df)} amostras. "
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
        holdout_m = (
            evaluate_model_on_dataset(
                base_eval_model, df.iloc[split_index:], feature_cols,
                tp_pct=tp_pct, sl_pct=sl_pct, confidence_threshold=confidence_threshold,
            )
            if len(X_test) > 0
            else _empty_validation_metrics()
        )
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
    src = wf_metrics if use_wf else holdout_m
    accuracy = src["accuracy"]
    precision_buy = src["precision_buy"]
    brier_score = src["brier_score"]
    sim_pnl_pct = src["sim_pnl_pct"]
    buy_signals = src["buy_signals"]
    profit_factor = src.get("profit_factor", holdout_m["profit_factor"])
    expectancy_pct = src.get("expectancy_pct", holdout_m["expectancy_pct"])
    roc_auc = src.get("roc_auc", holdout_m["roc_auc"])
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

    top_feats, top_features_list = _extract_top_features(final_model, feature_cols)

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
