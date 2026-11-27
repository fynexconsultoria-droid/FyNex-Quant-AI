"""Testes do Agendador Automático (1h) do Centro de Treinamento Profundo da IA."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from dashboard import server, training_jobs


def test_auto_train_schedule_defaults_persistence_and_api(tmp_path, monkeypatch):
    db_file = str(tmp_path / "auto_train.db")
    monkeypatch.setattr(server, "DB_PATH", db_file)
    monkeypatch.setattr(training_jobs, "_SCHED_NEXT_RUN_TS", None)

    client = TestClient(server.app)

    # 1. Padrão: ativo a cada 60 min (1h)
    r = client.get("/api/ai/train/schedule")
    assert r.status_code == 200
    sched = r.json()
    assert sched["enabled"] is True
    assert sched["interval_minutes"] == 60
    assert sched["symbol"] == "BTC/USDT"
    assert sched["timeframe"] == "1h"
    assert sched["candles"] == 3000
    assert sched["seconds_until_next"] is not None
    assert 0 <= sched["seconds_until_next"] <= 3600

    # 2. /api/ai/train/status também expõe o bloco schedule
    r_st = client.get("/api/ai/train/status")
    assert r_st.status_code == 200
    st = r_st.json()
    assert "schedule" in st
    assert st["schedule"]["interval_minutes"] == 60

    # 3. POST sem CSRF deve falhar (403)
    r_no_csrf = client.post("/api/ai/train/schedule", json={"enabled": False, "interval_minutes": 60})
    assert r_no_csrf.status_code == 403

    headers = {"X-CSRF-Token": server.CSRF_TOKEN}

    # 4. Intervalo < 60 min ou timeframe < 1h devem ser rejeitados (422)
    r_bad_int = client.post(
        "/api/ai/train/schedule",
        headers=headers,
        json={"enabled": True, "interval_minutes": 15, "timeframe": "1h"},
    )
    assert r_bad_int.status_code == 422

    r_bad_tf = client.post(
        "/api/ai/train/schedule",
        headers=headers,
        json={"enabled": True, "interval_minutes": 60, "timeframe": "15m"},
    )
    assert r_bad_tf.status_code == 422

    # 5. Atualização válida persiste no SQLite
    r_upd = client.post(
        "/api/ai/train/schedule",
        headers=headers,
        json={
            "enabled": True,
            "interval_minutes": 120,
            "symbol": "ETH/USDT",
            "timeframe": "4h",
            "candles": 5000,
        },
    )
    assert r_upd.status_code == 200
    body = r_upd.json()
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["interval_minutes"] == 120
    assert body["schedule"]["symbol"] == "ETH/USDT"
    assert body["schedule"]["timeframe"] == "4h"
    assert body["schedule"]["candles"] == 5000

    # 6. Pausar e reativar
    r_pause = client.post(
        "/api/ai/train/schedule",
        headers=headers,
        json={"enabled": False, "interval_minutes": 60},
    )
    assert r_pause.status_code == 200
    assert r_pause.json()["schedule"]["enabled"] is False
    assert r_pause.json()["schedule"]["seconds_until_next"] is None


def test_auto_train_scheduler_completion_and_trigger(tmp_path, monkeypatch):
    db_file = str(tmp_path / "auto_train_trigger.db")
    monkeypatch.setattr(server, "DB_PATH", db_file)
    monkeypatch.setattr(training_jobs, "_SCHED_NEXT_RUN_TS", None)

    # Registra conclusão de um ciclo automático e verifica persistência e próximo horário
    training_jobs._record_training_completion(
        "Concluído! Campeão Demo: RF (v16) • Sincronizado automaticamente na Demo e Conta Real!",
        trigger="auto",
        db_path=db_file,
    )
    sched = training_jobs.get_auto_train_schedule(db_path=db_file)
    assert sched["total_runs"] == 1
    assert sched["last_run_at"] is not None
    assert "v16" in (sched["last_summary"] or "")
    assert sched["seconds_until_next"] is not None and sched["seconds_until_next"] >= 3550

    # Simula chegada do horário de disparo e garante que o agendador aciona start_training_job(trigger='auto')
    calls = []

    def fake_start_job(symbol, timeframe, candles, offline, trigger="manual"):
        calls.append((symbol, timeframe, candles, offline, trigger))
        training_jobs._SCHED_STOP_EVENT.set()
        return {"status": "started", "trigger": trigger}

    monkeypatch.setattr(training_jobs, "start_training_job", fake_start_job)
    training_jobs._SCHED_STOP_EVENT.clear()
    training_jobs._SCHED_NEXT_RUN_TS = time.time() - 1.0
    training_jobs._auto_train_scheduler_loop(poll_seconds=0.02)

    assert len(calls) == 1
    assert calls[0] == ("BTC/USDT", "1h", 3000, False, "auto")


def test_live_high_potential_sniper_filter_and_demo_always_on(tmp_path, monkeypatch):
    from core import entry_guard
    from dashboard import bot_controller

    # 1. Sinal mediano (62% confiança, R/R fraco, tendência fraca) -> bloqueado na Conta Real
    ok_low, score_low, reasons_low, _ = entry_guard.evaluate_live_high_potential(
        prob_up=0.62,
        effective_confidence=0.58,
        tp_pct=0.010,
        sl_pct=0.010,
        features_row={"ema_trend_ratio": -0.001, "adx_14": 0.12, "rsi_14": 67.0, "bb_pos": 0.90},
        macro_snapshot={"macro_score": -18.0, "vix_price": 28.0, "regime": "CAUTELA MACRO"},
        champion_metrics={"precision_buy": 0.58},
    )
    assert ok_low is False
    assert score_low < 78.0
    assert len(reasons_low) >= 4

    # 2. Sinal de alto potencial (80% confiança, R/R 2.0x, tendência forte, macro favorável) -> aprovado!
    ok_hi, score_hi, reasons_hi, badge_hi = entry_guard.evaluate_live_high_potential(
        prob_up=0.80,
        effective_confidence=0.60,
        tp_pct=0.024,
        sl_pct=0.012,
        features_row={"ema_trend_ratio": 0.0035, "adx_14": 0.26, "rsi_14": 54.0, "bb_pos": 0.62},
        macro_snapshot={"macro_score": 18.0, "vix_price": 15.5, "regime": "RISK-ON"},
        pre_order_intel={"allow_live_proposal": True},
        champion_metrics={"precision_buy": 0.75},
    )
    assert ok_hi is True
    assert score_hi >= 78.0
    assert reasons_hi == []
    assert "Potencial" in badge_hi

    # 3. API de política persiste demo_always_on_training e live_high_potential_only
    db_file = str(tmp_path / "policy_sniper.db")
    monkeypatch.setattr(server, "DB_PATH", db_file)
    client = TestClient(server.app)

    r_ov = client.get("/api/accounts/overview")
    assert r_ov.status_code == 200
    pol = r_ov.json()["policy"]
    assert pol["demo_always_on_training"] is True
    assert pol["live_high_potential_only"] is True
    assert pol["live_min_confidence_pct"] == 72
    assert pol["live_min_potential_score"] == 78

    r_post = client.post(
        "/api/accounts/policy",
        headers={"X-CSRF-Token": server.CSRF_TOKEN},
        json={
            "auto_promote_to_live": True,
            "demo_always_on_training": True,
            "live_high_potential_only": True,
            "live_min_confidence_pct": 75,
            "live_min_potential_score": 82,
        },
    )
    assert r_post.status_code == 200
    pol2 = r_post.json()["policy"]
    assert pol2["live_min_confidence_pct"] == 75
    assert pol2["live_min_potential_score"] == 82

    # 4. Watchdog Demo inicia robô Demo automaticamente quando offline e respeita pausa manual
    started_args = []
    monkeypatch.setattr(bot_controller, "_check_is_bot_running", lambda **kw: (False, None))
    monkeypatch.setattr(
        bot_controller,
        "start_bot_process",
        lambda **kw: (started_args.append(kw) or {"status": "started", "pid": 9999}),
    )
    bot_controller.set_demo_manual_pause(False)
    res_auto = bot_controller.ensure_demo_training_bot_running(force=True)
    assert res_auto["status"] == "started"
    assert len(started_args) == 1
    assert started_args[0]["account"] == "demo"
    assert started_args[0]["trading_mode"] == "paper"

    # 5. Gestão de Micro-Banca (ex.: R$ 150 ≈ $27 USDT -> entrada mínima segura e crescimento composto)
    amt_150, note_150 = entry_guard.compute_micro_bankroll_amount(
        free_usdt_balance=27.0, alloc_pct=0.40, min_order_usdt=10.0, max_trade_amount_usdt=100.0
    )
    assert amt_150 == 10.8
    assert "Micro-Banca" in note_150

    amt_grow, _ = entry_guard.compute_micro_bankroll_amount(
        free_usdt_balance=60.0, alloc_pct=0.40, min_order_usdt=10.0, max_trade_amount_usdt=100.0
    )
    assert amt_grow == 24.0

    amt_low, note_low = entry_guard.compute_micro_bankroll_amount(
        free_usdt_balance=7.5, alloc_pct=0.40, min_order_usdt=10.0, max_trade_amount_usdt=100.0
    )
    assert amt_low == 0.0
    assert "abaixo do mínimo" in note_low


