"""
Testes da Fase 3 (Dashboard seguro e coerente: CSRF, Host/Origin, limites de query,
PID file + instância única, sinal de parada limpa, bot_state, filtro por timeframe
e metadata por par/timeframe).
"""

import os
import sys
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.ensemble import RandomForestClassifier

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import trading_bot as tb  # noqa: E402
from dashboard import server  # noqa: E402


@pytest.fixture
def client():
    return TestClient(server.app)


# ----------------------------------------- 3.1 Proteção CSRF e DNS Rebinding (C6)
def test_post_without_csrf_token_returns_403(client):
    r1 = client.post("/api/bot/stop")
    assert r1.status_code == 403

    r2 = client.post("/api/binance/save", json={"trading_mode": "paper"})
    assert r2.status_code == 403

    r3 = client.post("/api/binance/test")
    assert r3.status_code == 403


def test_post_with_valid_csrf_token_is_accepted(client, monkeypatch):
    monkeypatch.setattr(server, "is_bot_running", lambda: (False, None))
    r = client.post("/api/bot/stop", headers={"X-CSRF-Token": server.CSRF_TOKEN})
    assert r.status_code == 200
    assert r.json()["status"] == "not_running"


def test_external_origin_and_evil_host_are_blocked(client):
    r_origin = client.get("/api/status", headers={"Origin": "https://evil.example.com"})
    assert r_origin.status_code == 403

    r_host = client.get("/api/status", headers={"Host": "evil.example.com"})
    assert r_host.status_code == 403


def test_index_html_injects_csrf_meta_tag(client):
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="csrf-token" content="{server.CSRF_TOKEN}">' in r.text


# ----------------------------------------- 3.2 Limites de parâmetros (M12)
def test_excessive_limit_is_rejected_with_422(client):
    assert client.get("/api/candles?limit=5000").status_code == 422
    assert client.get("/api/predictions?limit=5000").status_code == 422
    assert client.get("/api/logs?lines=5000").status_code == 422


# ----------------------------------------- 3.3 PID file + Instância Única (M5, M6)
def test_single_instance_lock_blocks_second_bot(tmp_path, monkeypatch):
    pid_file = str(tmp_path / "bot.pid")
    # Simula que o PID atual é um processo válido do robô
    monkeypatch.setattr(tb, "is_pid_alive_bot", lambda pid: pid == os.getpid() or pid == 999999)

    assert tb.acquire_single_instance_lock("BTC/USDT", "1m", "paper", pid_path=pid_file) is True
    assert tb.get_running_bot_pid(pid_file) == os.getpid()

    # Simula outro processo (PID 999999) dono do lock
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write('{"pid": 999999}')
    assert tb.acquire_single_instance_lock("BTC/USDT", "1m", "paper", pid_path=pid_file) is False


def test_stale_pid_file_is_cleaned_automatically(tmp_path):
    pid_file = str(tmp_path / "bot.pid")
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write('{"pid": 99999999}')  # PID inexistente
    assert tb.get_running_bot_pid(pid_file) is None
    assert not os.path.exists(pid_file)


# ----------------------------------------- 3.4 Sinal de parada limpa (M7)
def test_stop_signal_lifecycle(tmp_path):
    stop_file = str(tmp_path / "bot.stop")
    assert tb.consume_stop_signal(stop_file) is False
    tb.request_bot_stop(stop_file)
    assert os.path.exists(stop_file)
    assert tb.consume_stop_signal(stop_file) is True
    assert not os.path.exists(stop_file)


# ----------------------------------------- 3.5 & 3.6 bot_state e filtro por timeframe (M9, A4)
def test_status_reads_bot_state_and_filters_by_timeframe(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dash.db")
    db = tb.TradingDatabase(db_path=db_path)

    df_1m = pd.DataFrame([{
        "timestamp": pd.to_datetime(1_700_000_000_000, unit="ms"),
        "open": 80000.0, "high": 80100.0, "low": 79900.0, "close": 80050.0, "volume": 2.0,
    }])
    df_15m = pd.DataFrame([{
        "timestamp": pd.to_datetime(1_700_000_000_000, unit="ms"),
        "open": 90000.0, "high": 90100.0, "low": 89900.0, "close": 90050.0, "volume": 20.0,
    }])
    db.save_candles("BTC/USDT", "1m", df_1m)
    db.save_candles("BTC/USDT", "15m", df_15m)

    db.upsert_bot_state(
        pid=1234, symbol="BTC/USDT", timeframe="1m", trading_mode="paper",
        model_version=7, model_accuracy=0.61, base_confidence=0.60, active_confidence=0.62,
        calibration_mode="REGIME NEUTRO", calibration_badge="⚖️",
        calibration_desc="Teste", in_position=False, entry_price=0.0,
    )

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "is_bot_running", lambda: (True, 1234))

    c = TestClient(server.app)
    s_1m = c.get("/api/status?timeframe=1m").json()
    assert s_1m["latest_price"] == pytest.approx(80050.0)
    assert s_1m["calibration"]["threshold"] == pytest.approx(62.0)

    s_15m = c.get("/api/status?timeframe=15m").json()
    assert s_15m["latest_price"] == pytest.approx(90050.0)


# ----------------------------------------- 3.7 Metadata por par/timeframe (A4)
def test_model_persistence_keeps_separate_metadata_per_timeframe(tmp_path):
    store = tb.ModelPersistence(models_dir=str(tmp_path / "models"), max_versions_kept=3)
    clf = RandomForestClassifier(n_estimators=5, random_state=42)
    clf.fit([[1, 2], [3, 4], [5, 6], [7, 8]], [0, 1, 0, 1])

    store.save_model(clf, version=3, accuracy=0.55, feature_cols=["a", "b"], symbol="BTC/USDT", timeframe="1m")
    store.save_model(clf, version=1, accuracy=0.62, feature_cols=["a", "b"], symbol="BTC/USDT", timeframe="15m")

    loaded_1m = store.load_latest_model("BTC/USDT", "1m")
    loaded_15m = store.load_latest_model("BTC/USDT", "15m")
    assert loaded_1m is not None and loaded_1m[1] == 3 and loaded_1m[2] == pytest.approx(0.55)
    assert loaded_15m is not None and loaded_15m[1] == 1 and loaded_15m[2] == pytest.approx(0.62)
