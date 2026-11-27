"""
Testes da Divisão de Contas (Demo / Treinamento IA vs Conta Real / Live)
========================================================================
Valida:
1. Promoção e pinagem do Modelo Campeão Live (isolado dos treinos Demo)
2. Estado paralelo `bot_account_state`, política `auto_promote_to_live` e trilha `model_promotions`
3. Rotas SPA `/`, `/demo` e `/live` com injeção de `initial-account` e CSRF
4. Endpoints `/api/accounts/*` (overview, promote-model, policy, live-readiness)
5. Segregação estrita de `/api/status`, `/api/trades`, `/api/finance/overview` e guardrails `/api/bot/start?account=live`
6. Isolamento de processo/retreino/hot-reload em `TradingBotRunner(account='demo'|'live')`
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.ensemble import RandomForestClassifier

import dashboard.server as srv
import trading_bot
from core.database import TradingDatabase
from core.infra.sqlite import sqlite_connection
from core.ml.persistence import ModelPersistence
from dashboard.routers.finance import build_finance_overview


def _make_dummy_rf(seed: int = 42, precision_buy: float = 0.68) -> RandomForestClassifier:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(40, 4))
    y = np.array([0, 1] * 20)
    clf = RandomForestClassifier(n_estimators=5, random_state=seed)
    clf.fit(X, y)
    clf.validation_metrics_ = {
        "model_type": "rf",
        "accuracy": precision_buy,
        "precision_buy": precision_buy,
        "profit_factor": 1.65,
        "roc_auc": 0.71,
    }
    return clf


def test_live_champion_promotion_and_prune_protection(tmp_path):
    models_dir = str(tmp_path / "models")
    storage = ModelPersistence(models_dir=models_dir, max_versions_kept=2)
    cols = ["f1", "f2", "f3", "f4"]

    storage.save_model(_make_dummy_rf(1, 0.62), 1, 0.62, cols, "BTC/USDT", "1h")
    # Promove v1 como Campeão Live
    champ = storage.promote_to_live("BTC/USDT", "1h", version=1, promoted_by="manual", notes="v1 validada")
    assert champ["version"] == 1
    assert champ["promoted_by"] == "manual"

    # Salva v2, v3 e v4 na Demo (com max_versions_kept=2, v1 DEVE continuar preservada!)
    storage.save_model(_make_dummy_rf(2, 0.64), 2, 0.64, cols, "BTC/USDT", "1h")
    storage.save_model(_make_dummy_rf(3, 0.66), 3, 0.66, cols, "BTC/USDT", "1h")
    storage.save_model(_make_dummy_rf(4, 0.70), 4, 0.70, cols, "BTC/USDT", "1h")

    # Demo usa latest (v4), Conta Real permanece fixada no Campeão v1
    demo_loaded = storage.load_latest_model("BTC/USDT", "1h")
    live_loaded = storage.load_live_champion_model("BTC/USDT", "1h", allow_fallback_to_latest=False)
    assert demo_loaded is not None and demo_loaded[1] == 4
    assert live_loaded is not None and live_loaded[1] == 1

    versions = {v["version"]: v for v in storage.list_available_versions("BTC/USDT", "1h")}
    assert 1 in versions and versions[1]["is_live_champion"] is True
    assert 4 in versions and versions[4]["is_demo_latest"] is True

    # Promove v4 para Conta Real
    champ_v4 = storage.promote_to_live("BTC/USDT", "1h", version=4, promoted_by="manual")
    assert champ_v4["version"] == 4
    live_loaded_after = storage.load_live_champion_model("BTC/USDT", "1h", allow_fallback_to_latest=False)
    assert live_loaded_after is not None and live_loaded_after[1] == 4


def test_parallel_bot_account_state_and_settings(tmp_path):
    db_path = str(tmp_path / "parallel.db")
    db = TradingDatabase(db_path)

    db.upsert_bot_state(
        pid=1111, symbol="BTC/USDT", timeframe="1h", trading_mode="paper",
        model_version=5, model_accuracy=0.66, base_confidence=0.60, active_confidence=0.62,
        calibration_mode="PADRAO", calibration_badge="🟢", calibration_desc="Demo",
        in_position=False, entry_price=0.0, account="demo", model_source="demo_latest",
    )
    db.upsert_bot_state(
        pid=2222, symbol="BTC/USDT", timeframe="1h", trading_mode="live",
        model_version=3, model_accuracy=0.71, base_confidence=0.65, active_confidence=0.65,
        calibration_mode="CAUTELOSO", calibration_badge="🛡️", calibration_desc="Live",
        in_position=True, entry_price=95000.0, account="live", model_source="live_champion",
    )

    demo_st = db.get_bot_state(account="demo")
    live_st = db.get_bot_state(account="live")
    assert demo_st is not None and demo_st["pid"] == 1111 and demo_st["model_version"] == 5
    assert live_st is not None and live_st["pid"] == 2222 and live_st["model_version"] == 3
    assert live_st["in_position"] == 1 and live_st["model_source"] == "live_champion"

    db.set_order_in_flight(True, account="live")
    assert db.is_order_in_flight(account="live") is True
    assert db.is_order_in_flight(account="demo") is False

    db.set_account_setting("auto_promote_to_live", "1")
    assert db.get_account_setting("auto_promote_to_live", "0") == "1"

    pid = db.record_model_promotion(
        symbol="BTC/USDT", timeframe="1h", model_version=5,
        accuracy=0.66, precision_buy=0.68, profit_factor=1.8, promoted_by="manual", notes="OK",
    )
    assert pid > 0
    promos = db.get_model_promotions("BTC/USDT", "1h")
    assert len(promos) == 1 and promos[0]["model_version"] == 5


def test_spa_routes_and_accounts_api_flow(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dash_accounts.db")
    models_dir = str(tmp_path / "models")
    TradingDatabase(db_path)

    storage = ModelPersistence(models_dir=models_dir)
    cols = ["f1", "f2", "f3", "f4"]
    storage.save_model(_make_dummy_rf(10, 0.63), 1, 0.63, cols, "BTC/USDT", "1h")
    storage.save_model(_make_dummy_rf(11, 0.72), 2, 0.72, cols, "BTC/USDT", "1h")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO simulated_trades (
                symbol, timeframe, model_version, entry_time, entry_price,
                exit_time, exit_price, reason, pnl_pct, pnl_usd, mode, order_id, quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BTC/USDT", "1h", 2, now_str, 90000.0, now_str, 91800.0, "TAKE_PROFIT", 2.0, 20.0, "paper", "sim_1", 0.01),
        )
        conn.execute(
            """
            INSERT INTO simulated_trades (
                symbol, timeframe, model_version, entry_time, entry_price,
                exit_time, exit_price, reason, pnl_pct, pnl_usd, mode, order_id, quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BTC/USDT", "1h", 1, now_str, 90000.0, now_str, 93600.0, "TAKE_PROFIT", 4.0, 40.0, "live", "real_1", 0.01),
        )
        conn.commit()

    monkeypatch.setattr(srv, "DB_PATH", db_path)
    monkeypatch.setattr(srv, "MODELS_DIR", models_dir)
    monkeypatch.setattr(srv, "is_bot_running", lambda use_cache=True, account=None: (False, None))

    client = TestClient(srv.app)
    csrf_headers = {"X-CSRF-Token": srv.CSRF_TOKEN, "Origin": "http://127.0.0.1:8000"}

    # 1. Rotas dedicadas /demo e /live
    r_demo = client.get("/demo")
    assert r_demo.status_code == 200
    assert 'name="initial-account" content="demo"' in r_demo.text
    assert 'id="btnSwitchDemo"' in r_demo.text
    assert 'id="btnSwitchLive"' in r_demo.text
    assert 'id="accountGovernanceCard"' in r_demo.text

    r_live = client.get("/live")
    assert r_live.status_code == 200
    assert 'name="initial-account" content="live"' in r_live.text

    # 2. Overview inicial (antes da promoção)
    ov = client.get("/api/accounts/overview?symbol=BTC/USDT&timeframe=1h").json()
    assert ov["demo"]["active_model"]["version"] == 2
    assert ov["live"]["has_explicit_champion"] is False
    assert ov["live"]["has_newer_demo_model"] is True
    assert ov["demo"]["trade_stats"]["total_pnl_usd"] == pytest.approx(20.0)
    assert ov["live"]["trade_stats"]["total_pnl_usd"] == pytest.approx(40.0)

    # 3. Promoção via API exige CSRF e promove v2
    r_no_csrf = client.post("/api/accounts/promote-model", json={"symbol": "BTC/USDT", "timeframe": "1h", "version": 2})
    assert r_no_csrf.status_code == 403

    r_prom = client.post(
        "/api/accounts/promote-model",
        headers=csrf_headers,
        json={"symbol": "BTC/USDT", "timeframe": "1h", "version": 2, "notes": "Aprovado para Conta Real"},
    )
    assert r_prom.status_code == 200
    assert r_prom.json()["champion"]["version"] == 2

    ov_after = client.get("/api/accounts/overview?symbol=BTC/USDT&timeframe=1h").json()
    assert ov_after["live"]["has_explicit_champion"] is True
    assert ov_after["live"]["has_newer_demo_model"] is False
    assert ov_after["live"]["champion_model"]["version"] == 2
    assert len(ov_after["promotions"]) == 1

    # 4. Política auto-promote
    r_pol = client.post(
        "/api/accounts/policy",
        headers=csrf_headers,
        json={"auto_promote_to_live": True},
    )
    assert r_pol.status_code == 200
    assert r_pol.json()["policy"]["auto_promote_to_live"] is True

    # 5. Segregação em /api/status, /api/trades e /api/finance/overview
    st_demo = client.get("/api/status?symbol=BTC/USDT&timeframe=1h&account=demo").json()
    st_live = client.get("/api/status?symbol=BTC/USDT&timeframe=1h&account=live").json()
    assert st_demo["trade_stats"]["total_pnl_usd"] == pytest.approx(20.0)
    assert st_live["trade_stats"]["total_pnl_usd"] == pytest.approx(40.0)
    assert st_live["model"]["is_explicit_champion"] is True

    tr_demo = client.get("/api/trades?symbol=BTC/USDT&timeframe=1h&account=demo").json()
    tr_live = client.get("/api/trades?symbol=BTC/USDT&timeframe=1h&account=live").json()
    assert len(tr_demo) == 1 and tr_demo[0]["mode"] == "paper"
    assert len(tr_live) == 1 and tr_live[0]["mode"] == "live"

    fin_demo = build_finance_overview(db_path, period="30d", source="all", account="demo")
    fin_live = build_finance_overview(db_path, period="30d", source="all", account="live")
    assert fin_demo["summary"]["total_trades"] == 1
    assert fin_demo["summary"]["realized_pnl_usd"] == pytest.approx(20.0)
    assert fin_live["summary"]["total_trades"] == 1
    assert fin_live["summary"]["realized_pnl_usd"] == pytest.approx(40.0)

    # 6. Guardrail ao iniciar Conta Real com LIVE_TRADING_ENABLED=False
    r_start_live = client.post(
        "/api/bot/start?symbol=BTC/USDT&timeframe=1h&account=live&trading_mode=live",
        headers=csrf_headers,
    )
    assert r_start_live.status_code == 400


def test_trading_bot_runner_account_isolation(tmp_path):
    db_path = str(tmp_path / "runner_iso.db")
    runner_demo = trading_bot.TradingBotRunner(symbol="BTC/USDT", timeframe="1h", db_path=db_path, account="demo")
    runner_live = trading_bot.TradingBotRunner(symbol="BTC/USDT", timeframe="1h", db_path=db_path, account="live")

    assert runner_demo.account == "demo"
    assert runner_demo.pid_path.endswith("bot_demo.pid")
    assert runner_demo.stop_path.endswith("bot_demo.stop")

    assert runner_live.account == "live"
    assert runner_live.pid_path.endswith("bot_live.pid")
    assert runner_live.stop_path.endswith("bot_live.stop")

    # Auto-evolução vem ativa por padrão nas duas contas; se desativada, Conta Real pausa retreino local
    assert runner_live.db.is_auto_evolution_enabled() is True
    runner_live.db.set_account_setting("auto_promote_to_live", "0")
    assert runner_live.db.is_auto_evolution_enabled() is False
    runner_live.candles_since_retrain = 999
    runner_live._maybe_trigger_retrain()
    assert runner_live.retrain_in_progress is False


def test_dual_account_automatic_ai_evolution_sync(tmp_path, monkeypatch):
    db_path = str(tmp_path / "auto_evo.db")
    models_dir = str(tmp_path / "models")
    db = TradingDatabase(db_path)
    assert db.is_auto_evolution_enabled() is True

    storage = ModelPersistence(models_dir=models_dir)
    cols = ["f1", "f2", "f3", "f4"]
    storage.save_model(_make_dummy_rf(21, 0.65), 1, 0.65, cols, "BTC/USDT", "1h")
    storage.save_model(_make_dummy_rf(22, 0.74), 2, 0.74, cols, "BTC/USDT", "1h")

    monkeypatch.setattr(srv, "DB_PATH", db_path)
    monkeypatch.setattr(srv, "MODELS_DIR", models_dir)
    monkeypatch.setattr(srv, "is_bot_running", lambda use_cache=True, account=None: (False, None))

    client = TestClient(srv.app)
    csrf_headers = {"X-CSRF-Token": srv.CSRF_TOKEN, "Origin": "http://127.0.0.1:8000"}

    # Overview vem com auto-evolução ativa por padrão nas duas contas
    ov = client.get("/api/accounts/overview?symbol=BTC/USDT&timeframe=1h").json()
    assert ov["policy"]["auto_promote_to_live"] is True
    assert ov["policy"]["auto_evolve_both_accounts"] is True

    # Ativar/confirmar política com symbol+timeframe sincroniza imediatamente o modelo v2 da Demo para Conta Real
    r_sync = client.post(
        "/api/accounts/policy",
        headers=csrf_headers,
        json={"auto_promote_to_live": True, "symbol": "BTC/USDT", "timeframe": "1h"},
    )
    assert r_sync.status_code == 200
    assert r_sync.json()["synced_version"] == 2

    champ = storage.load_live_champion_metadata("BTC/USDT", "1h")
    assert champ is not None and champ["version"] == 2 and champ["promoted_by"] == "auto"
    promos = db.get_model_promotions("BTC/USDT", "1h")
    assert len(promos) == 1 and promos[0]["promoted_by"] == "auto" and promos[0]["model_version"] == 2


def test_operational_timeframes_only_1h_and_above(tmp_path, monkeypatch):
    from core.trading.live_approvals import LiveOrderApprovalStore

    db_path = str(tmp_path / "tf_policy.db")
    TradingDatabase(db_path)
    monkeypatch.setattr(srv, "DB_PATH", db_path)
    monkeypatch.setattr(srv, "is_bot_running", lambda use_cache=True, account=None: (False, None))

    client = TestClient(srv.app)
    csrf_headers = {"X-CSRF-Token": srv.CSRF_TOKEN, "Origin": "http://127.0.0.1:8000"}

    # Timeframes abaixo de 1h devem ser rejeitados no start do robô, treino e promoção
    for bad_tf in ("1m", "3m", "5m", "15m", "30m"):
        assert client.post(f"/api/bot/start?symbol=BTC/USDT&timeframe={bad_tf}&account=demo", headers=csrf_headers).status_code == 422
        assert client.post(f"/api/ai/train?symbol=BTC/USDT&timeframe={bad_tf}", headers=csrf_headers).status_code == 422
        assert client.post("/api/accounts/promote-model", headers=csrf_headers, json={"symbol": "BTC/USDT", "timeframe": bad_tf}).status_code == 400

    # LiveOrderApprovalStore também rejeita timeframes < 1h e aceita 1h, 4h, 1d
    store = LiveOrderApprovalStore(db_path)
    with pytest.raises(ValueError):
        store.create_proposal(
            symbol="BTC/USDT", timeframe="15m", model_version=1, prob_up=0.72,
            confidence_threshold=0.65, entry_price=90000.0, sl_pct=0.01, tp_pct=0.02,
            amount_usdt=50.0, notify=False,
        )
    for ok_tf in ("1h", "4h", "1d"):
        p, created = store.create_proposal(
            symbol="BTC/USDT", timeframe=ok_tf, model_version=2, prob_up=0.75,
            confidence_threshold=0.65, entry_price=90000.0, sl_pct=0.011, tp_pct=0.018,
            amount_usdt=50.0, notify=False,
        )
        assert created is True and p["timeframe"] == ok_tf and p["status"] == "PENDING"


def test_asset_picking_mobile_alerts_removed(tmp_path, monkeypatch):
    from core import xtb_copilot

    called = []
    monkeypatch.setattr(xtb_copilot, "send_telegram_message", lambda *a, **k: (called.append(a), {"sent": True})[1])
    monkeypatch.setattr(xtb_copilot, "get_telegram_config", lambda *a, **k: {"configured": True, "chat_id": "12345"})

    store = xtb_copilot.XTBCopilotStore(str(tmp_path / "no_spam.db"))
    sig = store.record_signal(
        symbol="XAU/USD", side="BUY", score=88.0, entry_price=2600.0,
        sl_price=2580.0, tp_price=2640.0, suggested_lots=0.05, leverage=20,
        reason="Teste automação", notify_telegram=True,
    )
    assert sig["telegram_sent"] == 0
    assert called == []


def test_live_order_approval_queue_notifications_and_admin_telegram(tmp_path, monkeypatch):
    from core.trading import live_approvals as la

    db_path = str(tmp_path / "approvals.db")
    TradingDatabase(db_path)
    monkeypatch.setattr(srv, "DB_PATH", db_path)

    sent_tg = []
    sent_wa = []
    monkeypatch.setattr(la, "get_telegram_config", lambda: {"configured": True, "chat_id": "99999"})
    monkeypatch.setattr(la, "get_whatsapp_config", lambda: {"active": True, "configured": True, "enabled": True})
    monkeypatch.setattr(la, "send_telegram_approval_prompt", lambda text, order_id, **k: (sent_tg.append((order_id, text)), {"sent": True})[1])
    monkeypatch.setattr(la, "send_whatsapp_message", lambda text, **k: (sent_wa.append(text), {"sent": True})[1])

    store = la.LiveOrderApprovalStore(db_path)

    # 1. Cria proposta em BTC/USDT 1h -> envia Telegram e WhatsApp
    p1, c1 = store.create_proposal(
        symbol="BTC/USDT", timeframe="1h", model_version=12, prob_up=0.76,
        confidence_threshold=0.65, entry_price=95000.0, sl_pct=0.011, tp_pct=0.018,
        amount_usdt=50.0, reason="Campeão v12",
    )
    assert c1 is True and p1["status"] == "PENDING"
    assert p1["notified_telegram"] == 1 and p1["notified_whatsapp"] == 1
    assert len(sent_tg) == 1 and len(sent_wa) == 1

    # 2. Deduplicação no mesmo par/timeframe, MAS permite buscar e enfileirar outro ativo (ETH/USDT) em paralelo!
    p1_dup, c1_dup = store.create_proposal(
        symbol="BTC/USDT", timeframe="1h", model_version=12, prob_up=0.77,
        confidence_threshold=0.65, entry_price=95100.0, sl_pct=0.011, tp_pct=0.018,
        amount_usdt=50.0,
    )
    assert c1_dup is False and p1_dup["id"] == p1["id"]

    p2, c2 = store.create_proposal(
        symbol="ETH/USDT", timeframe="4h", model_version=4, prob_up=0.73,
        confidence_threshold=0.65, entry_price=3200.0, sl_pct=0.018, tp_pct=0.03,
        amount_usdt=50.0,
    )
    assert c2 is True and p2["id"] != p1["id"]
    assert store.count_pending() == 2

    # 3. API: listar e rejeitar via endpoint com CSRF
    client = TestClient(srv.app)
    csrf_headers = {"X-CSRF-Token": srv.CSRF_TOKEN, "Origin": "http://127.0.0.1:8000"}
    r_list = client.get("/api/live/approvals").json()
    assert r_list["pending_count"] == 2

    # Sem trava liberada (LIVE_TRADING_ENABLED=False), aprovar retorna 400 e mantém ordem PENDING
    r_app_blocked = client.post(f"/api/live/approvals/{p1['id']}/approve", headers=csrf_headers, json={})
    assert r_app_blocked.status_code == 400
    assert store.get_proposal(p1["id"])["status"] == "PENDING"

    # Rejeitar p2 via API
    r_rej = client.post(f"/api/live/approvals/{p2['id']}/reject", headers=csrf_headers, json={"note": "Aguardar"})
    assert r_rej.status_code == 200
    assert store.get_proposal(p2["id"])["status"] == "REJECTED"

    # 4. Comando Telegram: ignora chat não autorizado e processa /rejeitar do chat autorizado do admin
    replies = []
    monkeypatch.setattr(la, "send_telegram_message", lambda text, **k: (replies.append(text), {"sent": True})[1])
    monkeypatch.setattr(
        la,
        "fetch_telegram_updates",
        lambda offset=0, **k: {
            "ok": True,
            "chat_id": "99999",
            "updates": [
                {"update_id": 100, "message": {"chat": {"id": "77777"}, "text": f"/rejeitar {p1['id']}"}},
                {"update_id": 101, "message": {"chat": {"id": "99999"}, "text": f"/rejeitar {p1['id']}"}},
            ],
        },
    )
    actions = store.poll_telegram_admin_commands()
    assert len(actions) == 1 and actions[0]["status"] == "rejected"
    assert store.get_proposal(p1["id"])["status"] == "REJECTED"
    assert store.get_proposal(p1["id"])["decided_by"] == "admin_telegram"
    assert len(replies) == 1


def test_global_stock_exchanges_vix_and_pre_order_intelligence(tmp_path, monkeypatch):
    from core import entry_guard, macro_intelligence
    from core.markets.catalog import MARKET_INSTRUMENTS
    from core.trading import live_approvals as la

    # 1. Bolsas globais presentes no catálogo
    for sym in ("US500/USD", "US100/USD", "US30/USD", "DE40/USD", "IBOV/BRL", "JP225/USD", "VIX/USD"):
        assert sym in MARKET_INSTRUMENTS

    # 2. Cenário favorável nas bolsas (S&P 500 + Nasdaq em alta, VIX baixo, notícia positiva BTC)
    bull_snap = {
        "dxy_price": 102.5, "dxy_change_pct": -0.35,
        "us10y_yield": 4.10, "us10y_change_pct": -1.2,
        "sp500_price": 5950.0, "sp500_change_pct": 1.15,
        "nasdaq_price": 21200.0, "nasdaq_change_pct": 1.45,
        "dowjones_price": 43800.0, "dowjones_change_pct": 0.85,
        "vix_price": 14.2, "vix_change_pct": -5.4,
        "ibovespa_price": 131500.0, "ibovespa_change_pct": 0.90,
        "dax_price": 19500.0, "dax_change_pct": 0.70,
        "ftse_price": 8300.0, "ftse_change_pct": 0.40,
        "nikkei_price": 39200.0, "nikkei_change_pct": 0.80,
        "fear_greed_value": 58, "news_sentiment_score": 0.45,
        "top_headlines": [
            {"title": "Bitcoin ETF registra entrada recorde após alta do S&P 500", "source": "Reuters", "sentiment": "positivo", "category": "cripto"},
        ],
    }
    intel_ok = macro_intelligence.build_live_pre_order_intelligence("BTC/USDT", macro_snapshot=bull_snap)
    assert intel_ok["verdict"] == "FAVORAVEL"
    assert intel_ok["allow_live_proposal"] is True
    assert "S&P 500" in intel_ok["equities_line"]
    assert "Nasdaq" in intel_ok["equities_line"]
    assert "VIX 14.2" in intel_ok["equities_line"]
    assert "Bitcoin ETF" in intel_ok["top_headline"]

    # 3. Cenário de pânico em Wall Street (S&P -2.8%, Nasdaq -3.1%, VIX 34.5) bloqueia entrada real!
    crash_snap = {
        "dxy_price": 106.8, "dxy_change_pct": 0.95,
        "us10y_yield": 4.75, "us10y_change_pct": 3.2,
        "sp500_price": 5600.0, "sp500_change_pct": -2.80,
        "nasdaq_price": 19800.0, "nasdaq_change_pct": -3.10,
        "vix_price": 34.5, "vix_change_pct": 18.0,
        "news_sentiment_score": -0.60,
        "war_geopolitics_mentions": 4,
        "top_headlines": [
            {"title": "Wall Street despenca com disparada do VIX e tensão global", "source": "Bloomberg", "sentiment": "negativo", "category": "macro"},
        ],
    }
    intel_block = macro_intelligence.build_live_pre_order_intelligence("BTC/USDT", macro_snapshot=crash_snap)
    assert intel_block["verdict"] == "BLOQUEADO_RISCO"
    assert intel_block["allow_live_proposal"] is False
    assert any("VIX" in f for f in intel_block["risk_flags"])

    crash_macro = dict(crash_snap)
    crash_macro.update(macro_intelligence.compute_macro_decision(crash_snap))
    safe_ok, guard_reasons = entry_guard.evaluate_entry_safety(
        features_row={"rsi_14": 50.0, "ema_trend_ratio": 0.003, "atr_pct": 0.01, "bb_pos": 0.5, "adx_14": 0.25, "bb_width": 0.02},
        macro_snapshot=crash_macro,
    )
    assert safe_ok is False
    assert any("VIX" in r or "Wall Street" in r or "Regime macro" in r for r in guard_reasons)

    # 4. Proposta Live inclui automaticamente o dossiê pré-ordem na mensagem enviada ao celular
    db_path = str(tmp_path / "intel_prop.db")
    TradingDatabase(db_path)
    store = la.LiveOrderApprovalStore(db_path)
    prop, created = store.create_proposal(
        symbol="BTC/USDT", timeframe="1h", model_version=12, prob_up=0.77,
        confidence_threshold=0.65, entry_price=95500.0, sl_pct=0.011, tp_pct=0.018,
        amount_usdt=50.0, notify=False, pre_order_intel=intel_ok,
    )
    assert created is True
    assert prop["pre_order_intel"]["verdict"] == "FAVORAVEL"
    msg = la.format_live_approval_message(prop, channel="telegram")
    assert "Pesquisa Pré-Ordem" in msg
    assert "S&P 500" in msg
    assert "Bitcoin ETF" in msg

    # 5. Endpoint /api/live/pre-order-intel retorna dossiê completo
    monkeypatch.setattr(macro_intelligence, "fetch_macro_snapshot", lambda ttl_seconds=60.0: bull_snap)
    client = TestClient(srv.app)
    r_intel = client.get("/api/live/pre-order-intel?symbol=BTC/USDT")
    assert r_intel.status_code == 200
    body = r_intel.json()
    assert body["verdict"] == "FAVORAVEL"
    assert body["equities"]["sp500_price"] == pytest.approx(5950.0)
    assert body["equities"]["vix_price"] == pytest.approx(14.2)


