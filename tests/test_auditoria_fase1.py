"""
Testes da Auditoria - Fase 1 (Estabilidade do robô: C1, A5, B5, B12).
"""

import os
import sys
import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import trading_bot as tb  # noqa: E402
import supervisor  # noqa: E402


def test_predict_prob_up_and_signal_handle_single_class_model():
    X = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]})
    y_all_zero = pd.Series([0, 0, 0])
    clf = RandomForestClassifier(n_estimators=5, random_state=42, n_jobs=1)
    clf.fit(X, y_all_zero)
    assert list(clf.classes_) == [0]

    # Não deve lançar IndexError
    prob_up = tb.predict_prob_up(clf, X.iloc[:1])
    pred, prob_down, p_up = tb.predict_signal(clf, X.iloc[:1])
    assert prob_up == 0.0
    assert pred == 0
    assert prob_down == 1.0
    assert p_up == 0.0


def test_train_model_rejects_single_class_dataset():
    df = pd.DataFrame({
        "f1": np.linspace(0, 1, 80),
        "f2": np.linspace(1, 2, 80),
        "Target": [0] * 80,
    })
    with pytest.raises(tb.InsufficientLabelsError):
        tb.train_model(df, ["f1", "f2"], model_version=1)


def test_install_exception_hooks_sets_hooks_and_redirects_none_stderr(tmp_path, monkeypatch):
    orig_sys_hook = sys.excepthook
    orig_thread_hook = tb.threading.excepthook
    orig_stderr = sys.stderr
    stderr_log = tmp_path / "logs" / "stderr.log"

    try:
        monkeypatch.setattr(sys, "stderr", None)
        tb.install_exception_hooks(str(stderr_log))
        assert sys.stderr is not None
        assert sys.excepthook is not orig_sys_hook
        assert tb.threading.excepthook is not orig_thread_hook
        sys.stderr.write("erro teste\n")
        sys.stderr.flush()
        sys.stderr.close()
        assert "erro teste" in stderr_log.read_text(encoding="utf-8")
    finally:
        sys.excepthook = orig_sys_hook
        tb.threading.excepthook = orig_thread_hook
        sys.stderr = orig_stderr


def test_supervisor_restarts_on_crash_and_stops_on_clean_exit(tmp_path, monkeypatch):
    codes = iter([1, 0])

    class DummyProc:
        def __init__(self, code):
            self._code = code

        def wait(self):
            return self._code

    calls = []

    def fake_popen(cmd, cwd=None):
        code = next(codes)
        calls.append(code)
        return DummyProc(code)

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)

    rc = supervisor.run_supervised(
        max_restarts=3,
        base_backoff_sec=0.01,
        stop_signal_path=str(tmp_path / "bot.stop"),
    )
    assert rc == 0
    assert calls == [1, 0]
