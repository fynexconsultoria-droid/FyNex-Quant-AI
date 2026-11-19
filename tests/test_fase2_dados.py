"""
Testes da Fase 2 (Integridade de dados: candles fechados, UPSERT, backfill, lacunas,
avaliação exata em t+1, prevenção de previsões duplicadas e modo WAL).
"""

import os
import sys
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from trading_bot import (  # noqa: E402
    TradingDatabase,
    get_latest_contiguous_segment,
    backfill_candles,
)


@pytest.fixture
def db(tmp_path):
    return TradingDatabase(db_path=str(tmp_path / "test_fase2.db"))


def make_candles_df(timestamps_ms, closes=None, volumes=None):
    n = len(timestamps_ms)
    closes = closes or [80000.0 + i * 10 for i in range(n)]
    volumes = volumes or [3.5 for _ in range(n)]
    rows = []
    for ts, c, v in zip(timestamps_ms, closes, volumes):
        rows.append({
            "timestamp": pd.to_datetime(ts, unit="ms"),
            "open": c - 5.0,
            "high": c + 10.0,
            "low": c - 10.0,
            "close": c,
            "volume": v,
        })
    return pd.DataFrame(rows)


# ----------------------------------------- 2.1 Candles fechados + UPSERT (C1)
def test_drop_last_open_discards_in_progress_candle(db):
    base = 1_700_000_000_000
    df = make_candles_df(
        [base, base + 60_000, base + 120_000],
        closes=[80000.0, 80100.0, 80200.0],
        volumes=[4.0, 5.0, 0.1],  # o último é parcial (0.1 BTC)
    )
    db.save_candles("BTC/USDT", "1m", df, drop_last_open=True)
    saved = db.get_recent_candles("BTC/USDT", "1m", limit=10)
    assert len(saved) == 2
    assert list(saved["close"]) == [80000.0, 80100.0]


def test_upsert_updates_partial_candle_when_closed_version_arrives(db):
    ts = 1_700_000_000_000
    partial = make_candles_df([ts], closes=[80010.0], volumes=[0.2])
    db.save_candles("BTC/USDT", "1m", partial, drop_last_open=False)

    final = make_candles_df([ts], closes=[80095.0], volumes=[6.4])
    db.save_candles("BTC/USDT", "1m", final, drop_last_open=False)

    saved = db.get_recent_candles("BTC/USDT", "1m", limit=5)
    assert len(saved) == 1
    assert saved.iloc[0]["close"] == pytest.approx(80095.0)
    assert saved.iloc[0]["volume"] == pytest.approx(6.4)


# ----------------------------------------- 2.3 Detecção de lacunas e Backfill
def test_get_latest_contiguous_segment_cuts_at_time_gap():
    base = 1_700_000_000_000
    # 3 candles antes do buraco, buraco de 1h, e 4 candles contínuos depois
    ts_list = [
        base, base + 60_000, base + 120_000,
        base + 3_600_000, base + 3_660_000, base + 3_720_000, base + 3_780_000,
    ]
    df = make_candles_df(ts_list)
    seg = get_latest_contiguous_segment(df, "1m")
    assert len(seg) == 4
    first_ts = int(seg["timestamp"].iloc[0].timestamp() * 1000)
    assert first_ts == base + 3_600_000


def test_backfill_paginates_from_last_timestamp(db, monkeypatch):
    base = 1_700_000_000_000
    initial = make_candles_df([base, base + 60_000])
    db.save_candles("BTC/USDT", "1m", initial, drop_last_open=False)

    class DummyExchange:
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
            # Na Binance, `since` é inclusivo: retorna a partir de `since` + os novos + o aberto
            start = since or base
            return [
                [start, 80000, 80010, 79990, 80005, 3.0],
                [start + 60_000, 80005, 80020, 80000, 80015, 4.0],
                [start + 120_000, 80015, 80025, 80010, 80020, 0.2],  # em formação
            ]

    monkeypatch.setattr("time.time", lambda: (base + 180_000) / 1000.0)
    backfill_candles(db, DummyExchange(), symbol="BTC/USDT", timeframe="1m")
    saved = db.get_recent_candles("BTC/USDT", "1m", limit=10)
    # Deve ter 3 candles fechados: base, base+60k, base+120k (base+180k descartado)
    assert len(saved) == 3


# ----------------------------------------- 2.4 Avaliação exata em t+1 (A3)
def test_evaluate_pending_predictions_uses_exact_t_plus_1_after_pause(db):
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000  # candle t+1 real (subiu para 80100 -> previsão ALTA acertou)
    t_after_1h = t0 + 3_600_000  # 1h depois o preço caiu para 79000

    # O candle t+1 está salvo no banco
    db.save_candles(
        "BTC/USDT", "1m",
        make_candles_df([t0, t1], closes=[80000.0, 80100.0]),
        drop_last_open=False,
    )

    # Previsão feita em t0 prevendo ALTA (1) a partir de 80000.0
    db.record_prediction("BTC/USDT", "1m", 1, t0, prediction=1, prob_up=0.6, prob_down=0.4, entry_price=80000.0)

    # O robô ficou pausado e voltou em t_after_1h quando o preço é 79000.0
    evals = db.evaluate_pending_predictions("BTC/USDT", "1m", t_after_1h, current_closed_close=79000.0)
    assert len(evals) == 1
    assert evals[0]["actual_close"] == pytest.approx(80100.0)
    assert evals[0]["is_correct"] is True


def test_evaluate_skips_prediction_when_t_plus_1_candle_is_missing(db):
    t0 = 1_700_000_000_000
    t_later = t0 + 300_000  # 5 minutos depois, sem o candle t0+60_000 no banco
    db.record_prediction("BTC/USDT", "1m", 1, t0, prediction=1, prob_up=0.6, prob_down=0.4, entry_price=80000.0)
    evals = db.evaluate_pending_predictions("BTC/USDT", "1m", t_later, current_closed_close=79000.0)
    assert evals == []


# ----------------------------------------- 2.5 Sem duplicatas (A3)
def test_record_prediction_is_idempotent_for_same_candle(db):
    ts = 1_700_000_000_000
    id1 = db.record_prediction("BTC/USDT", "1m", 1, ts, 1, 0.6, 0.4, 80000.0)
    id2 = db.record_prediction("BTC/USDT", "1m", 2, ts, 0, 0.3, 0.7, 80000.0)
    assert id1 == id2
    with db._get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert count == 1
    assert db.get_last_predicted_candle_timestamp("BTC/USDT", "1m") == ts


# ----------------------------------------- 2.6 Modo WAL (M4)
def test_database_runs_in_wal_mode(db):
    with db._get_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert str(mode).lower() == "wal"
