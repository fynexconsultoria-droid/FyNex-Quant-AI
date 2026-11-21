"""
Testes da Auditoria - Fase 3 (Versionamento de modelos: A3).
"""

import os
import sys
import json
from sklearn.ensemble import RandomForestClassifier

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import trading_bot as tb  # noqa: E402


def _dummy_rf():
    clf = RandomForestClassifier(n_estimators=5, random_state=42, n_jobs=1)
    clf.fit([[1, 2], [3, 4], [5, 6], [7, 8]], [0, 1, 0, 1])
    return clf


def test_versioning_never_deletes_active_version_and_advances_monotonically(tmp_path):
    store = tb.ModelPersistence(models_dir=str(tmp_path / "models"), max_versions_kept=3)
    clf = _dummy_rf()

    # Simula modelos legados v11..v20 já existentes em disco
    for v in range(11, 21):
        (tmp_path / "models" / f"model_v{v}_BTC_USDT_1m.joblib").write_bytes(b"legacy")

    # next_version deve avançar para 21 (nunca voltar para 1)
    assert store.next_version("BTC/USDT", "1m") == 21

    # Mesmo salvando explicitamente v1 (pior caso), a versão ativa v1 NÃO pode ser apagada!
    path_v1 = store.save_model(clf, version=1, accuracy=0.60, feature_cols=["a", "b"], symbol="BTC/USDT", timeframe="1m")
    assert os.path.exists(path_v1)
    meta = store.load_metadata("BTC/USDT", "1m")
    assert meta is not None
    assert meta["file"] == "model_v1_BTC_USDT_1m.joblib"
    assert "feature_schema_hash" in meta
    assert os.path.exists(os.path.join(store.models_dir, meta["file"]))


def test_archive_incompatible_models_moves_files_to_archive(tmp_path):
    store = tb.ModelPersistence(models_dir=str(tmp_path / "models"), max_versions_kept=10)
    for v in (11, 12, 13):
        (tmp_path / "models" / f"model_v{v}_BTC_USDT_1m.joblib").write_bytes(b"old")

    moved = store.archive_incompatible_models("BTC/USDT", "1m")
    assert moved == 3
    archive_files = sorted(os.listdir(tmp_path / "models" / "archive"))
    assert archive_files == [
        "model_v11_BTC_USDT_1m.joblib",
        "model_v12_BTC_USDT_1m.joblib",
        "model_v13_BTC_USDT_1m.joblib",
    ]
    assert store.next_version("BTC/USDT", "1m") == 1


def test_saving_15_versions_keeps_10_newest_including_active(tmp_path):
    store = tb.ModelPersistence(models_dir=str(tmp_path / "models"), max_versions_kept=10)
    clf = _dummy_rf()
    for _ in range(15):
        v = store.next_version("BTC/USDT", "5m")
        store.save_model(clf, version=v, accuracy=0.55, feature_cols=["a", "b"], symbol="BTC/USDT", timeframe="5m")

    files = [f for f in os.listdir(store.models_dir) if f.startswith("model_v") and f.endswith("_BTC_USDT_5m.joblib")]
    assert len(files) == 10
    meta = store.load_metadata("BTC/USDT", "5m")
    assert meta["version"] == 15
    assert os.path.exists(os.path.join(store.models_dir, meta["file"]))
