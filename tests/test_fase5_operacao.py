"""
Testes da Fase 5 (Operação e Qualidade: poda de modelos antigos, leitura rápida do fim do log,
RotatingFileHandler e sistema de alertas).
"""

import os
import sys
from logging.handlers import RotatingFileHandler
from sklearn.ensemble import RandomForestClassifier

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import trading_bot as tb  # noqa: E402
import alerts  # noqa: E402
from dashboard import server  # noqa: E402


def test_model_persistence_prunes_old_versions_and_keeps_latest_n(tmp_path):
    store = tb.ModelPersistence(models_dir=str(tmp_path / "models"), max_versions_kept=3)
    clf = RandomForestClassifier(n_estimators=5, random_state=42, n_jobs=1)
    clf.fit([[1, 2], [3, 4], [5, 6], [7, 8]], [0, 1, 0, 1])

    for v in range(1, 7):
        store.save_model(clf, version=v, accuracy=0.55, feature_cols=["a", "b"], symbol="BTC/USDT", timeframe="1m")

    files = [
        f for f in os.listdir(store.models_dir)
        if f.startswith("model_v") and f.endswith("_BTC_USDT_1m.joblib")
    ]
    assert sorted(files) == [
        "model_v4_BTC_USDT_1m.joblib",
        "model_v5_BTC_USDT_1m.joblib",
        "model_v6_BTC_USDT_1m.joblib",
    ]


def test_tail_lines_reads_only_end_of_log(tmp_path):
    log_file = tmp_path / "big.log"
    lines = [f"linha-{i}" for i in range(200)]
    log_file.write_text("\n".join(lines), encoding="utf-8")

    tail = server._tail_lines(str(log_file), max_lines=5)
    assert tail == ["linha-195", "linha-196", "linha-197", "linha-198", "linha-199"]


def test_rotating_file_handler_is_configured():
    handlers = tb.logger.handlers + tb.logging.getLogger().handlers
    assert any(isinstance(h, RotatingFileHandler) for h in handlers)


def test_send_alert_without_credentials_returns_false_silently():
    assert alerts.send_alert("Teste", token="", chat_id="") is False
