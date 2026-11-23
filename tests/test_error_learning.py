"""
Testes do Motor de Auto-Análise de Erros & Aprendizado Adaptativo (FyNex Quant AI)
==================================================================================
Valida:
1. Diagnóstico multi-parâmetro de erros (RSI esticado, volume fraco, contra-tendência,
   mercado lateral, macro adverso, SL apertado vs ATR, excesso de confiança)
2. Persistência de snapshots de decisão e autópsias em SQLite
3. Adaptação automática no `entry_guard` (penalidade de confiança, redução de mão,
   bloqueio de repetição após recorrência, alargamento de SL/ATR e decaimento após vitórias)
4. Hard-example weighting no treino de ML (peso reforçado apenas no treino)
5. Fluxo integrado: PositionManager (prejuízo -> autópsia -> lição no alerta WhatsApp/Telegram),
   AI Chat (`intent == "error_lessons"`) e endpoint `/api/ai/error-lessons`.
"""

import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alerts  # noqa: E402
import position_manager as pm  # noqa: E402
import trading_bot as tb  # noqa: E402
from core import ai_chat, entry_guard, error_learning, ml  # noqa: E402
from dashboard import server  # noqa: E402


def test_diagnose_error_parameters_identifies_all_root_causes():
    # Usa EXATAMENTE os nomes reais de features produzidos por `core.ml.build_features`
    bad_params = {
        "rsi_14": 68.5,
        "bb_pos": 0.91,
        "ema_trend_ratio": -0.003,
        "adx_14": 0.14,
        "bb_width": 0.007,
        "volume_zscore_20": -0.85,
        "atr_pct": 0.012,
        "sl_pct": 0.008,  # < 1.15 * 0.012 -> SL_TOO_TIGHT
        "macd_diff": -0.004,
        "return_5": -0.005,
        "return_10": -0.012,
        "macro_score": -25.0,
        "prob_up": 0.74,
    }
    diag = error_learning.diagnose_error_parameters(bad_params, source="TRADE_LOSS", outcome_reason="STOP_LOSS", pnl_pct=-1.5)
    codes = {c["code"] for c in diag["causes"]}
    assert "OVERBOUGHT_ENTRY" in codes
    assert "AGAINST_TREND" in codes
    assert "CHOPPY_SIDEWAYS" in codes
    assert "LOW_VOLUME_BREAKOUT" in codes
    assert "NEGATIVE_MOMENTUM" in codes
    assert "MACRO_HEADWIND" in codes
    assert "SL_TOO_TIGHT" in codes
    assert "OVERCONFIDENT_MODEL" in codes
    assert diag["severity"] >= 2.0
    assert "Lição:" in diag["lesson_text"]

    # Validação de contrato contra `core.ml.build_features` real (evita drift de nomes de coluna)
    n = 60
    ts = pd.date_range("2026-11-01", periods=n, freq="h")
    close = np.linspace(100.0, 95.0, n)
    vol = np.full(n, 1000.0)
    vol[-1] = 100.0  # queda brusca de volume -> volume_zscore_20 negativo
    ohlcv = pd.DataFrame({
        "timestamp": ts,
        "open": close + 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": vol,
    })
    feat_df, feat_cols = ml.build_features(ohlcv)
    assert {"volume_zscore_20", "macd_diff", "return_5", "return_10"}.issubset(set(feat_cols))
    real_row = feat_df.iloc[-1][feat_cols].to_dict()
    real_codes = {c["code"] for c in error_learning.detect_risk_factors(real_row)}
    assert "LOW_VOLUME_BREAKOUT" in real_codes
    assert "AGAINST_TREND" in real_codes

    # Setup limpo não deve inventar causa falsa
    clean_params = {
        "rsi_14": 52.0, "bb_pos": 0.55, "ema_trend_ratio": 0.004, "adx_14": 0.28,
        "bb_width": 0.02, "volume_zscore_20": 0.6, "atr_pct": 0.006, "sl_pct": 0.011,
        "macd_diff": 0.002, "return_5": 0.004, "return_10": 0.008, "macro_score": 10.0, "prob_up": 0.63,
    }
    diag_clean = error_learning.diagnose_error_parameters(clean_params)
    assert diag_clean["primary_cause"] == "UNCLASSIFIED_MARKET_NOISE"


def test_adaptive_learning_penalizes_blocks_and_widens_sl(tmp_path):
    db_path = str(tmp_path / "adapt.db")
    store = error_learning.ErrorLearningStore(db_path)
    now = datetime(2026, 11, 23, 15, 0, 0, tzinfo=timezone.utc)

    # 2 erros com LOW_VOLUME_BREAKOUT e SL_TOO_TIGHT -> aperta filtro, reduz mão e sugere alargar SL
    for i in range(2):
        store.record_error_postmortem(
            source="TRADE_LOSS",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"vol_ratio": 0.60, "atr_pct": 0.010, "sl_pct": 0.008, "rsi_14": 54.0},
            outcome_reason="STOP_LOSS",
            candle_timestamp=1_700_000_000_000 + i * 3_600_000,
            pnl_usd=-5.0,
            pnl_pct=-1.0,
            now_utc=now,
        )

    res_2 = store.evaluate_setup_against_lessons(
        "BTC/USDT", "1h",
        features_row={"vol_ratio": 0.65, "atr_pct": 0.010, "rsi_14": 53.0},
        sl_pct=0.009,
        now_utc=now,
    )
    assert res_2["approved"] is True
    assert res_2["confidence_penalty"] >= 0.03
    assert res_2["size_multiplier"] < 1.0
    assert res_2["sl_atr_boost"] >= 1.15

    # 3o erro com LOW_VOLUME_BREAKOUT -> agora BLOQUEIA repetição do mesmo erro no entry_guard!
    store.record_error_postmortem(
        source="TRADE_LOSS",
        symbol="BTC/USDT",
        timeframe="1h",
        params={"vol_ratio": 0.55, "atr_pct": 0.010, "sl_pct": 0.008, "rsi_14": 54.0},
        outcome_reason="STOP_LOSS",
        candle_timestamp=1_700_007_200_000,
        pnl_usd=-6.0,
        pnl_pct=-1.2,
        now_utc=now,
    )

    res_3 = store.evaluate_setup_against_lessons(
        "BTC/USDT", "1h",
        features_row={"vol_ratio": 0.62, "atr_pct": 0.010, "rsi_14": 53.0},
        sl_pct=0.009,
        now_utc=now,
    )
    assert res_3["approved"] is False
    assert any("Lição aprendida" in r for r in res_3["block_reasons"])

    ok, guard_reasons = entry_guard.evaluate_entry_safety(
        features_row={"rsi_14": 53.0, "bb_pos": 0.5, "ema_trend_ratio": 0.003, "atr_pct": 0.008, "adx_14": 0.25, "bb_width": 0.02},
        learned_guard_result=res_3,
        now_utc=now,
    )
    assert ok is False
    assert any("Lição aprendida" in r for r in guard_reasons)

    # Setup com volume forte NÃO é bloqueado pela lição de volume fraco!
    res_clean = store.evaluate_setup_against_lessons(
        "BTC/USDT", "1h",
        features_row={"vol_ratio": 1.45, "atr_pct": 0.008, "rsi_14": 53.0},
        sl_pct=0.012,
        now_utc=now,
    )
    assert res_clean["approved"] is True


def test_hard_example_multipliers_boost_failed_timestamps_and_patterns(tmp_path):
    db_path = str(tmp_path / "weights.db")
    store = error_learning.ErrorLearningStore(db_path)

    base_ts = 1_700_000_000_000
    rows = []
    for i in range(40):
        rows.append({
            "timestamp": pd.to_datetime(base_ts + i * 3_600_000, unit="ms"),
            "rsi_14": 70.0 if i in (5, 10, 15) else 50.0,
            "vol_ratio": 0.55 if i in (5, 10) else 1.2,
            "Target": 0 if i % 2 == 0 else 1,
        })
    df = pd.DataFrame(rows)

    # Registra 2 erros recorrentes em OVERBOUGHT_ENTRY (incluindo timestamp exato da linha 10, que tem Target=0)
    ts_10 = base_ts + 10 * 3_600_000
    store.record_error_postmortem(
        source="TRADE_LOSS", symbol="BTC/USDT", timeframe="1h",
        params={"rsi_14": 70.0, "vol_ratio": 0.55}, candle_timestamp=ts_10,
    )
    store.record_error_postmortem(
        source="PREDICTION_MISS", symbol="BTC/USDT", timeframe="1h",
        params={"rsi_14": 68.0, "vol_ratio": 1.1}, candle_timestamp=base_ts + 999 * 3_600_000,
    )

    mult = error_learning.compute_hard_example_multipliers(df, db_path=db_path, symbol="BTC/USDT", timeframe="1h")
    assert len(mult) == len(df)
    assert np.isclose(np.mean(mult), 1.0, atol=1e-6)
    # Linha 10 (erro real exato + padrão sobrecompra + Target=0) tem peso claramente maior que linha normal 2
    assert mult[10] > mult[2] * 1.5


def test_position_manager_loss_triggers_postmortem_whatsapp_chat_and_api(tmp_path, monkeypatch):
    db_path = str(tmp_path / "e2e_err.db")
    db = tb.TradingDatabase(db_path=db_path)
    sent_alerts = []
    monkeypatch.setattr(alerts, "send_alert", lambda msg, level="INFO", **k: (sent_alerts.append((level, msg)) or True))

    mgr = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=100.0, use_exchange_oco=False,
    )
    assert mgr.try_open(
        100_000.0, 1, 0.75,
        entry_context={
            "features": {"rsi_14": 67.5, "vol_ratio": 0.60, "atr_pct": 0.010, "adx_14": 0.25},
            "macro": {"macro_score": -20.0, "regime": "CAUTELA MACRO"},
            "candle_timestamp": 1_700_000_000_000,
        },
    ) is True

    # Fecha no STOP_LOSS -> deve rodar autópsia e incluir "Auto-análise:" no alerta de saída
    assert mgr.close("STOP_LOSS", 98_500.0) is True
    exit_alerts = [msg for lvl, msg in sent_alerts if lvl == "TRADE_EXIT"]
    assert len(exit_alerts) == 1
    assert "PREJUÍZO" in exit_alerts[0]
    assert "Auto-análise:" in exit_alerts[0]

    # Chat da IA deve responder sobre o erro analisado
    chat_ans = ai_chat.answer_ai_chat("Onde você errou e o que aprendeu?", db_path=db_path, symbol="BTC/USDT", timeframe="1h")
    assert chat_ans["intent"] == "error_lessons"
    assert "Erros analisados" in chat_ans["reply"]

    # Endpoint do dashboard deve retornar o relatório completo
    monkeypatch.setattr(server, "DB_PATH", db_path)
    api_rep = server.get_ai_error_lessons(symbol="BTC/USDT", timeframe="1h", limit=5)
    assert api_rep["total_errors_14d"] == 1
    assert len(api_rep["recent_errors"]) == 1
    assert api_rep["recent_errors"][0]["source"] == "TRADE_LOSS"

    # Pergunta de PnL diário com "prejuízo" NÃO pode ser sequestrada por error_lessons
    pnl_chat = ai_chat.answer_ai_chat("Tive lucro ou prejuízo hoje?", db_path=db_path, symbol="BTC/USDT", timeframe="1h")
    assert pnl_chat["intent"] == "report_24h"


def test_dedup_trade_loss_and_prediction_miss_and_oco_before_alert(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dedup_oco.db")
    store = error_learning.ErrorLearningStore(db_path)
    now = datetime(2026, 11, 23, 18, 0, 0, tzinfo=timezone.utc)
    ts = 1_700_000_000_000

    # Mesmo candle com TRADE_LOSS e depois PREDICTION_MISS -> conta apenas 1 erro!
    store.record_error_postmortem(
        source="TRADE_LOSS", symbol="BTC/USDT", timeframe="1h",
        params={"volume_zscore_20": -1.2}, candle_timestamp=ts, pnl_usd=-5.0, now_utc=now,
    )
    dup = store.record_error_postmortem(
        source="PREDICTION_MISS", symbol="BTC/USDT", timeframe="1h",
        params={"volume_zscore_20": -1.2}, candle_timestamp=ts, now_utc=now,
    )
    assert dup.get("deduplicated") is True
    prof = store.get_adaptive_learning_profile("BTC/USDT", "1h", now_utc=now)
    assert prof["total_errors_analyzed"] == 1
    assert prof["cause_counts"].get("LOW_VOLUME_BREAKOUT") == 1

    # Em modo real/testnet, OCO deve ser colocada ANTES de alerts.send_alert
    import binance_client as bc
    call_order = []
    db = tb.TradingDatabase(db_path=db_path)
    mgr = pm.PositionManager(
        db, object(), mode="testnet", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0, use_exchange_oco=True,
    )
    monkeypatch.setattr(bc, "get_asset_balances", lambda *a, **k: (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        bc, "execute_spot_buy",
        lambda *a, **k: {"success": True, "price": 90000.0, "quantity": 0.001, "order_id": "ORD123"},
    )
    monkeypatch.setattr(mgr, "_place_oco", lambda: call_order.append("OCO"))
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: (call_order.append("ALERT") or True))

    assert mgr.try_open(90000.0, 1, 0.72) is True
    assert call_order == ["OCO", "ALERT"]
