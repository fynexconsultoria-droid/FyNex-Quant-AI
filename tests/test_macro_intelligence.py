"""
Testes da Inteligência Macroeconômica (Dólar DXY, Juros EUA 10Y, Inflação/Fed,
Notícias e Fear & Greed) e Persistência Contínua de Aprendizado no SQLite.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from core import macro_intelligence  # noqa: E402
from dashboard import server  # noqa: E402


def test_analyze_news_and_inflation_detects_fed_and_inflation_bias():
    items = [
        {"title": "Fed sinaliza corte de juros com queda da inflação", "summary": "CPI desacelera", "sentiment": "positivo"},
        {"title": "Bitcoin sobe forte após alívio no FOMC", "summary": "Mercado otimista", "sentiment": "positivo"},
    ]
    res = macro_intelligence.analyze_news_and_inflation(items)
    assert res["news_total"] == 2
    assert res["news_sentiment_score"] == pytest.approx(1.0)
    assert res["inflation_fed_mentions"] >= 1
    assert res["inflation_fed_bias"] > 0


def test_compute_macro_decision_risk_on_vs_risk_off():
    risk_on = macro_intelligence.compute_macro_decision({
        "dxy_change_pct": -0.60,
        "us10y_change_pct": -2.0,
        "sp500_change_pct": 1.1,
        "news_sentiment_score": 0.6,
        "inflation_fed_mentions": 2,
        "inflation_fed_bias": 0.5,
        "fear_greed_value": 58,
    })
    assert risk_on["macro_score"] >= 20.0
    assert risk_on["regime"] == "RISK-ON FAVORÁVEL"
    assert risk_on["confidence_adjustment"] < 0

    risk_off = macro_intelligence.compute_macro_decision({
        "dxy_change_pct": 0.90,
        "us10y_change_pct": 3.5,
        "sp500_change_pct": -1.5,
        "news_sentiment_score": -0.7,
        "inflation_fed_mentions": 3,
        "inflation_fed_bias": -0.8,
        "fear_greed_value": 18,
    })
    assert risk_off["macro_score"] <= -50.0
    assert risk_off["regime"] == "RISK-OFF SEVERO"
    assert risk_off["confidence_adjustment"] >= 0.08


def test_macro_learning_store_persists_snapshots_and_learning_cycles(tmp_path):
    db_path = str(tmp_path / "macro_learn.db")
    store = macro_intelligence.MacroLearningStore(db_path=db_path)

    store.save_macro_snapshot("BTC/USDT", "30m", 1_700_000_000_000, {
        "dxy_price": 104.2, "dxy_change_pct": -0.35,
        "us10y_yield": 4.25, "us10y_change_pct": -1.1,
        "sp500_change_pct": 0.8, "fear_greed_value": 60,
        "news_sentiment_score": 0.4, "inflation_fed_bias": 0.3,
        "macro_score": 32.5, "regime": "RISK-ON FAVORÁVEL",
    })
    snaps = store.get_recent_macro_snapshots(limit=5)
    assert len(snaps) == 1
    assert snaps[0]["timeframe"] == "30m"
    assert snaps[0]["dxy_price"] == pytest.approx(104.2)

    store.record_learning_cycle(
        symbol="BTC/USDT",
        timeframe="30m",
        model_version=3,
        model_type="ensemble",
        metrics={"samples": 2400, "precision_buy": 0.61, "profit_factor": 1.45, "sim_pnl_pct": 4.2, "optimal_threshold": 0.58},
        macro_score=32.5,
        promoted=True,
        notes="Ciclo de teste 30m",
    )
    hist = store.get_learning_history(limit=5)
    assert len(hist) == 1
    assert hist[0]["model_version"] == 3
    assert hist[0]["promoted"] == 1


def test_macro_and_learning_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "macro_api.db"))
    monkeypatch.setattr(
        macro_intelligence,
        "fetch_macro_snapshot",
        lambda: {
            "dxy_price": 103.5, "dxy_change_pct": -0.2, "us10y_yield": 4.15,
            "us10y_change_pct": -0.5, "sp500_price": 5950.0, "sp500_change_pct": 0.6,
            "fear_greed_value": 62, "fear_greed_label": "Greed",
            "news_sentiment_score": 0.35, "inflation_fed_bias": 0.25, "inflation_fed_mentions": 2,
            "macro_score": 28.0, "regime": "RISK-ON FAVORÁVEL", "badge": "🟢", "confidence_adjustment": -0.02,
            "description": "Favorável",
        },
    )

    client = TestClient(server.app)
    r1 = client.get("/api/macro/live")
    assert r1.status_code == 200
    assert r1.json()["regime"] == "RISK-ON FAVORÁVEL"

    r2 = client.get("/api/ai/learning-history")
    assert r2.status_code == 200
    assert isinstance(r2.json()["items"], list)


def test_entry_guard_blocks_unsafe_conditions_and_approves_healthy_setup():
    from datetime import datetime, timezone
    from core.entry_guard import evaluate_entry_safety
    from strategy_config import get_strategy_profile

    p30 = get_strategy_profile("30m")
    assert p30.tp_pct == pytest.approx(0.012)
    assert p30.sl_pct == pytest.approx(0.008)
    assert p30.confidence >= 0.62

    p1h = get_strategy_profile("1h")
    assert p1h.tp_pct == pytest.approx(0.018)
    assert p1h.sl_pct == pytest.approx(0.011)
    assert p1h.confidence >= 0.65

    # Cenário saudável: aprovado
    ok, reasons = evaluate_entry_safety(
        features_row={"rsi_14": 54.0, "bb_pos": 0.60, "ema_trend_ratio": 0.002, "atr_pct": 0.006},
        macro_snapshot={"regime": "RISK-ON FAVORÁVEL", "macro_score": 25.0},
        last_trade={"reason": "TAKE_PROFIT", "pnl_usd": 1.2, "exit_time": "2026-11-21 12:00:00"},
        cooldown_seconds=7200,
        now_utc=datetime(2026, 11, 21, 12, 10, 0, tzinfo=timezone.utc),
    )
    assert ok is True and reasons == []

    # Cenário inseguro: RISK-OFF + sobrecompra + Bollinger esticado + tendência de baixa + volatilidade extrema + cooldown pós-stop
    ok2, reasons2 = evaluate_entry_safety(
        features_row={"rsi_14": 76.0, "bb_pos": 0.99, "ema_trend_ratio": -0.008, "atr_pct": 0.035},
        macro_snapshot={"regime": "RISK-OFF SEVERO", "macro_score": -65.0},
        last_trade={"reason": "STOP_LOSS", "pnl_usd": -0.8, "exit_time": "2026-11-21 12:00:00"},
        cooldown_seconds=7200,
        now_utc=datetime(2026, 11, 21, 12, 20, 0, tzinfo=timezone.utc),
    )
    assert ok2 is False
    assert len(reasons2) == 6


def test_global_factors_and_ai_evolution_progress(tmp_path, monkeypatch):
    items = [
        {"title": "Guerra no Oriente Medio eleva petroleo Brent e alerta geopolitico", "summary": "Conflito e sancoes", "sentiment": "negativo"},
        {"title": "Seca severa afeta safra e energia global", "summary": "Clima extremo", "sentiment": "negativo"},
        {"title": "Demanda por terras raras, litio e ouro bate recorde", "summary": "Minerios estrategicos", "sentiment": "positivo"},
    ]
    news_stats = macro_intelligence.analyze_news_and_inflation(items)
    assert news_stats["war_geopolitics_mentions"] >= 1
    assert news_stats["climate_energy_mentions"] >= 1
    assert news_stats["rare_earth_minerals_mentions"] >= 1

    db_path = str(tmp_path / "evo.db")
    evo = macro_intelligence.compute_ai_evolution_progress(
        db_path=db_path,
        symbol="BTC/USDT",
        timeframe="30m",
        macro_snapshot={"macro_score": -55.0, "war_geopolitics_mentions": 3, "oil_change_pct": 3.2},
    )
    assert 5.0 <= evo["progress_pct"] <= 100.0
    assert evo["level"] >= 1
    assert evo["adaptation_tone"] == "warning"

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(
        macro_intelligence,
        "fetch_macro_snapshot",
        lambda: {"macro_score": 15.0, "oil_price": 72.5, "oil_change_pct": 0.4, "gold_price": 2650.0, "gold_change_pct": 0.5, "silver_change_pct": 0.3, "rare_earth_price": 45.2, "rare_earth_change_pct": 1.1, "copper_price": 4.2, "copper_change_pct": 0.6, "war_geopolitics_mentions": 1, "climate_energy_mentions": 1, "rare_earth_minerals_mentions": 2},
    )
    client = TestClient(server.app)
    r = client.get("/api/ai/evolution?symbol=BTC/USDT&timeframe=30m")
    assert r.status_code == 200
    assert "evolution" in r.json() and "macro" in r.json()


def test_dynamic_atr_barriers_training_and_trailing_profit(tmp_path):
    import numpy as np
    import pandas as pd
    import position_manager as pm
    import trading_bot as tb
    from core.ml import build_features, create_target
    from strategy_config import get_strategy_profile, resolve_barriers

    p1h = get_strategy_profile("1h")
    assert p1h.use_atr_barriers is True

    # Mercado calmo (ATR 0.45%) -> TP razoável ~0.90%, SL ~0.585%
    tp_calm, sl_calm = resolve_barriers(p1h, atr_pct=0.0045)
    assert tp_calm == pytest.approx(0.009)
    assert sl_calm == pytest.approx(0.00585)

    # Mercado agitado (ATR 1.50%) -> respeita teto seguro TP 2.5%, SL 1.5%
    tp_vol, sl_vol = resolve_barriers(p1h, atr_pct=0.015)
    assert tp_vol == pytest.approx(0.025)
    assert sl_vol == pytest.approx(0.015)

    # create_target com use_atr_barriers=True funciona sem erros
    rng = np.random.default_rng(12)
    closes = 60000.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.005, size=100))
    df = pd.DataFrame({
        "timestamp": pd.to_datetime([1_700_000_000_000 + i * 3_600_000 for i in range(100)], unit="ms"),
        "open": closes,
        "high": closes * 1.006,
        "low": closes * 0.995,
        "close": closes,
        "volume": np.full(100, 10.0),
    })
    feat, _ = build_features(df)
    ds = create_target(feat, mode="triple_barrier", use_atr_barriers=True, horizon_candles=8)
    assert not ds.empty and "Target" in ds.columns

    # Proteção de Lucro (Breakeven + Trailing Profit) no PositionManager
    db = tb.TradingDatabase(db_path=str(tmp_path / "trail.db"))
    mgr = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.011, take_profit_pct=0.018, trade_amount_usdt=50.0, use_exchange_oco=False,
    )
    assert mgr.try_open(100_000.0, 1, 0.70, tp_pct_override=0.010, sl_pct_override=0.006)
    assert mgr.position.tp_price == pytest.approx(101_000.0)
    assert mgr.position.sl_price == pytest.approx(99_400.0)

    # Preço avança 85% até o alvo (100.850) -> trailing sobe o stop acima da entrada (~100.425)
    mgr.check_exits(100_850.0, candle_high=100_850.0, candle_low=100_500.0)
    assert mgr.in_position
    assert mgr.position.sl_price > 100_400.0

    # Se mercado recuar e bater no stop protegido -> fecha com TRAILING_PROFIT no lucro!
    mgr.check_exits(100_400.0, candle_high=100_500.0, candle_low=100_400.0)
    assert not mgr.in_position
    with db._get_connection() as conn:
        t = dict(conn.execute("SELECT * FROM simulated_trades ORDER BY id DESC LIMIT 1").fetchone())
    assert t["reason"] == "TRAILING_PROFIT"
    assert t["pnl_usd"] > 0


def test_real_mode_updates_binance_oco_on_trailing_profit(tmp_path, monkeypatch):
    import binance_client
    import position_manager as pm
    import trading_bot as tb

    db = tb.TradingDatabase(db_path=str(tmp_path / "real_oco_trail.db"))
    mgr = pm.PositionManager(
        db, object(), mode="testnet", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.011, take_profit_pct=0.018, trade_amount_usdt=50.0, use_exchange_oco=True,
    )
    mgr.position = pm.Position(
        symbol="BTC/USDT", timeframe="1h", mode="testnet", model_version=1,
        entry_time="2026-11-22 10:00:00", entry_price=100_000.0, quantity=0.0005,
        order_id="B1", sl_price=99_000.0, tp_price=101_500.0, oco_list_id="OCO_OLD",
    )

    events = []
    monkeypatch.setattr(
        binance_client, "get_oco_status",
        lambda *a, **k: {"success": True, "list_status": "EXECUTING", "filled": False},
    )
    monkeypatch.setattr(
        binance_client, "cancel_oco",
        lambda ex, sym, oid: (events.append(("cancel", oid)) or {"success": True}),
    )
    monkeypatch.setattr(
        binance_client, "place_protective_oco",
        lambda ex, sym, qty, tp, sl: (events.append(("place", round(sl, 2))) or {"success": True, "order_list_id": "OCO_NEW"}),
    )

    # Preço avança 85% do alvo (101.275) -> cancela OCO antiga e envia nova OCO com stop protegido no lucro!
    mgr.check_exits(101_275.0, candle_high=101_275.0, candle_low=100_800.0)
    assert ("cancel", "OCO_OLD") in events
    assert mgr.position.oco_list_id == "OCO_NEW"
    assert mgr.position.sl_price > 100_600.0
