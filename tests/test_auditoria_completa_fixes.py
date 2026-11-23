"""
Testes de Regressão da Auditoria Completa (FyNex Quant AI)
==========================================================
Valida todas as correções críticas e altas aplicadas nas Fases 0, 1 e 2:
- Injeção de .env no Telegram + leitura persistente + redação de token
- Auth fail-closed em hosts públicos + validação de path traversal em ModelPersistence
- Fim do deadlock permanente do circuit breaker (entry_guard) e janela 24h (RiskManager)
- Preservação de PENDING em timeout ambíguo na Binance + geo-block 451 preservando endpoints privados
- Escala LME/tonelada de COPPER/USD (2.204x), fechamento atômico sem duplicidade e rejeição de NaN na mesa XTB
- Horário Forex com DST (Nova York), neutralidade RSI/ADX e desacoplamento do Copiloto
- Proteção contra cache macro all-zero, NLP com fronteiras de palavra e AI Chat sem sequestro por 'mesa'
- Alinhamento validação/produção do ML (PlattCalibratedModel), save_candles(len=1) e supervisor exit_code=2
"""

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alerts  # noqa: E402
import binance_client  # noqa: E402
import position_manager as pm  # noqa: E402
import strategy_config  # noqa: E402
import supervisor  # noqa: E402
import trading_bot as tb  # noqa: E402
from core import ai_chat, entry_guard, forex_ai_trader, forex_market, macro_intelligence, ml, news_feed, xtb_copilot  # noqa: E402
from dashboard import server  # noqa: E402


def _run_coro_sync(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("Corrotina suspendeu inesperadamente")


# 1. Segurança .env / Telegram (C2, A9)
def test_telegram_config_blocks_env_injection_and_persists_across_env_reload(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_MODE=paper\n", encoding="utf-8")

    with pytest.raises(ValueError):
        xtb_copilot.save_telegram_config(
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ12345",
            chat_id="123456\nTRADING_MODE=live",
            env_path=str(env_file),
        )
    assert "TRADING_MODE=live" not in env_file.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        xtb_copilot.save_telegram_config(
            bot_token="../../evil",
            chat_id="12345678",
            env_path=str(env_file),
        )

    valid_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg = xtb_copilot.save_telegram_config(valid_token, "-100123456789", env_path=str(env_file))
    assert cfg["configured"] is True

    # Simula reinício do servidor (os.environ limpo) -> ainda lê do .env
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg_reloaded = xtb_copilot.get_telegram_config(env_path=str(env_file))
    assert cfg_reloaded["configured"] is True
    assert cfg_reloaded["chat_id"] == "-100123456789"

    # Scrub de token em erro
    scrubbed = xtb_copilot._scrub_telegram_error(f"Error calling https://api.telegram.org/bot{valid_token}/sendMessage", valid_token)
    assert valid_token not in scrubbed
    assert alerts._scrub_token(f"bot{valid_token}", valid_token) == "bot***"


# 2. Auth Fail-Open + Path Traversal (C3, A6)
def test_dashboard_auth_fails_closed_on_public_host_without_password(monkeypatch):
    monkeypatch.delenv("FYNEX_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(server, "_PUBLIC_HOSTS_REGISTERED", True)
    monkeypatch.setattr(server, "ENV_PATH", "/nonexistent/.env")
    server.ALLOWED_HOSTS.add("app.example.com")
    try:
        req = Request({
            "type": "http", "method": "GET", "scheme": "https",
            "path": "/api/status", "headers": [(b"host", b"app.example.com")], "query_string": b"",
        })

        async def ok_next(_r):
            return JSONResponse({"ok": True})

        resp = _run_coro_sync(server.security_middleware(req, ok_next))
        assert resp.status_code == 401
        assert "content-security-policy" in {k.lower() for k in resp.headers}
    finally:
        server.ALLOWED_HOSTS.discard("app.example.com")


def test_model_persistence_blocks_path_traversal(tmp_path):
    store = ml.ModelPersistence(models_dir=str(tmp_path / "models"))
    with pytest.raises(ValueError):
        store.get_meta_path("..\\evil", "1h")
    with pytest.raises(ValueError):
        store.get_meta_path("BTC/USDT", "../../etc")
    with pytest.raises(ValueError):
        store.next_version("..\\evil", "1h")
    with pytest.raises(ValueError):
        store.archive_incompatible_models("BTC/USDT", "../1h")
    with pytest.raises(ValueError):
        store.load_latest_model("..\\evil", "1h")
    with pytest.raises(ValueError):
        store.save_model(object(), 1, 0.5, ["f1"], "..\\evil", "1h")


def test_host_and_origin_reject_userinfo_and_path_smuggling():
    assert server._parse_host_header("127.0.0.1:8000") == "127.0.0.1"
    assert server._parse_host_header("[::1]:8000") == "::1"
    assert server._parse_host_header("evil.com@127.0.0.1") == ""
    assert server._parse_host_header("127.0.0.1/path") == ""
    assert server._parse_host_header("127.0.0.1?x=1") == ""

    assert server.is_allowed_origin("http://127.0.0.1:8000") is True
    assert server.is_allowed_origin("http://evil.com@127.0.0.1:8000") is False
    assert server.is_allowed_origin("http://127.0.0.1:8000/evil") is False
    assert server.is_allowed_origin("http://localhost.evil.com:8000") is False


# 3. Circuit Breaker sem deadlock permanente (C1) + Open-candle pre-entry wick (A1)
def test_circuit_breaker_auto_recovers_after_cooldown():
    now = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    feats = {"rsi_14": 50.0, "bb_pos": 0.5, "ema_trend_ratio": 0.002, "atr_pct": 0.006, "adx_14": 0.25, "bb_width": 0.015}

    # Perda há 30 min -> bloqueado
    recent_loss = {"reason": "STOP_LOSS", "pnl_usd": -5.0, "exit_time": (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")}
    ok_recent, reasons_recent = entry_guard.evaluate_entry_safety(
        features_row=feats, last_trade=recent_loss, consecutive_losses=3, now_utc=now
    )
    assert ok_recent is False
    assert any("Circuit breaker" in r for r in reasons_recent)

    # Perda há 5h (> 4h default) -> libera automaticamente sem deadlock!
    old_loss = {"reason": "STOP_LOSS", "pnl_usd": -5.0, "exit_time": (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")}
    ok_old, reasons_old = entry_guard.evaluate_entry_safety(
        features_row=feats, last_trade=old_loss, consecutive_losses=3, now_utc=now
    )
    assert ok_old is True
    assert reasons_old == []


def test_check_exits_ignores_pre_entry_open_candle_wick(tmp_path):
    db = tb.TradingDatabase(db_path=str(tmp_path / "wick.db"))
    mgr = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0, use_exchange_oco=False,
    )
    assert mgr.try_open(100_000.0, 1, 0.70)
    entry_ts_ms = int(
        datetime.strptime(mgr.position.entry_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    # Candle aberto começou 5 min ANTES da entrada e teve pavio abaixo do SL (98.000) antes da compra
    mgr.check_exits(
        current_price=100_050.0,
        candle_high=100_200.0,
        candle_low=98_000.0,
        candle_open_ts_ms=entry_ts_ms - 300_000,
    )
    assert mgr.in_position is True


# 4. Binance: PENDING preservado em timeout ambíguo (C8) e 451 preserva endpoints privados (A2)
def test_binance_451_mirror_preserves_private_urls_and_ambiguous_buy_keeps_pending(tmp_path, monkeypatch):
    class FakeEx:
        apiKey = "TESTKEY1234567890"
        urls = {
            "api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
                "sapi": "https://api.binance.com/sapi/v1",
            }
        }

    ex = FakeEx()
    binance_client._apply_mirror_to_exchange(ex, binance_client.BINANCE_PUBLIC_FALLBACK_MIRROR)
    assert ex.urls["api"]["public"].startswith("https://data-api.binance.vision")
    assert ex.urls["api"]["private"].startswith("https://api.binance.com")
    assert ex.urls["api"]["sapi"].startswith("https://api.binance.com")

    db = tb.TradingDatabase(db_path=str(tmp_path / "ambig.db"))
    mgr = pm.PositionManager(
        db, ex, mode="testnet", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0, use_exchange_oco=False,
    )
    monkeypatch.setattr(binance_client, "get_asset_balances", lambda *a, **k: (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        binance_client, "execute_spot_buy",
        lambda *a, **k: {"success": False, "ambiguous_network": True, "message": "timeout"},
    )
    assert mgr.try_open(100_000.0, 1, 0.70) is False
    saved = mgr.store.load("BTC/USDT", "1h", "testnet")
    assert saved is not None and saved.status == "PENDING"


# 5. XTB / Forex: Escala COPPER 2204x (C9), fechamento atômico (C10), NaN rejeitado (A10), DST (A11)
def test_copper_scale_xtb_atomic_close_nan_guard_and_forex_dst(tmp_path, monkeypatch):
    fake_hg = {
        "meta": {"regularMarketPrice": 4.20, "chartPreviousClose": 4.15},
        "indicators": {"quote": [{"close": [4.20], "high": [4.25], "low": [4.10], "volume": [100]}]},
    }
    monkeypatch.setattr(forex_market, "_fetch_yahoo_chart", lambda *a, **k: fake_hg)
    q = forex_market.fetch_forex_quote("COPPER/USD", ttl_seconds=0.0)
    assert 9000.0 < q["price"] < 9500.0  # ~9259 USD/ton (não $4.20/lb)

    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "xtb_audit.db"))
    with pytest.raises(ValueError):
        store.open_position("EUR/USD", "BUY", lots=float("nan"), quote_override={"price": 1.08, "bid": 1.08, "ask": 1.08})

    pos = store.open_position("EUR/USD", "BUY", lots=0.10, leverage=30, quote_override={"price": 1.085, "bid": 1.0849, "ask": 1.085})
    c1 = store.close_position(pos["id"], quote_override={"price": 1.09, "bid": 1.09, "ask": 1.0901})
    assert c1["pnl_usd"] > 0
    with pytest.raises(ValueError):
        store.close_position(pos["id"], quote_override={"price": 1.09, "bid": 1.09, "ask": 1.0901})

    # Sexta-feira em horário de verão dos EUA (EDT = UTC-4): 21:15 UTC = 17:15 ET -> FECHADO!
    is_open_edt, _ = forex_ai_trader.is_forex_market_open(datetime(2026, 7, 10, 21, 15, tzinfo=timezone.utc))
    assert is_open_edt is False


# 6. Macro cache protegido (A12), NLP sem falsos positivos (M12) e AI Chat sem sequestro por 'mesa' (M11)
def test_macro_outage_does_not_poison_cache_and_nlp_word_boundaries(tmp_path, monkeypatch):
    # "warning" e "forward" NÃO devem disparar guerra; "turmoil" NÃO deve disparar oil
    res = macro_intelligence.analyze_news_and_inflation([
        {"title": "Software company looks forward after warning on turmoil", "summary": "Executive cut", "sentiment": "neutro"},
    ])
    assert res["war_geopolitics_mentions"] == 0
    assert res["climate_energy_mentions"] == 0

    # News: "alta dos juros" e "despenca/despencou" devem ser negativo; HTML duplamente escapado limpo
    assert news_feed._classify_sentiment("Fed anuncia alta dos juros", "") == "negativo"
    assert news_feed._classify_sentiment("Bitcoin despenca após dados dos EUA", "") == "negativo"
    assert news_feed._classify_sentiment("Ouro despencou nesta manhã", "") == "negativo"
    assert "<script>" not in news_feed._clean_text("&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;Texto")

    # AI Chat: 'mesa' não deve cair em report_30d ('mes')
    ans = ai_chat.answer_ai_chat("Como esta a mesa XTB?", db_path=str(tmp_path / "empty.db"))
    assert ans["intent"] == "xtb_signals"

    # Macro outage realista: fetch_live_news retorna stale=True sem lançar exceção -> NÃO envenena cache
    macro_intelligence.clear_macro_cache()
    monkeypatch.setattr(macro_intelligence, "_fetch_yahoo_change", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(macro_intelligence, "_fetch_fear_and_greed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        news_feed, "fetch_live_news",
        lambda *a, **k: {"total": 0, "stale": True, "items": [], "sentiment_summary": {}},
    )
    snap = macro_intelligence.fetch_macro_snapshot(ttl_seconds=0.0)
    assert snap["available"] is False
    assert snap["stale"] is True
    assert "news" in snap["failed_sources"]


# 7. Strategy breakeven (C6), save_candles len=1 (H1), DynamicCalibrator (H6) e Supervisor exit_code=2 (A7)
def test_strategy_breakeven_save_candles_calibrator_and_supervisor(tmp_path, monkeypatch):
    for tf, prof in strategy_config.STRATEGY_PROFILES.items():
        be = strategy_config.breakeven_win_rate(prof.tp_pct, prof.sl_pct)
        assert prof.confidence > be, f"Timeframe {tf} com confiança {prof.confidence} <= breakeven {be:.3f}"

    db = tb.TradingDatabase(db_path=str(tmp_path / "single.db"))
    one_candle = pd.DataFrame([{
        "timestamp": pd.to_datetime(1_700_000_000_000, unit="ms"),
        "open": 50000.0, "high": 50100.0, "low": 49900.0, "close": 50050.0, "volume": 1.0,
    }])
    assert db.save_candles("BTC/USDT", "1m", one_candle, drop_last_open=True) == 0
    assert db.get_recent_candles("BTC/USDT", "1m").empty

    # DynamicCalibrator não afrouxa filtro em acurácia geral alta quando há poucas compras avaliadas
    cal = ml.DynamicCalibrator.get_calibration(
        rolling_accuracy=90.0, evaluated_count=30, base_threshold=0.65,
        buy_precision_pct=30.0, buy_evaluated_count=3,
    )
    assert cal["mode"] == "CALIBRAÇÃO INICIAL"
    assert cal["threshold"] == pytest.approx(0.65)

    # Supervisor aborta imediatamente em exit_code=2 (erro de CLI) sem loop infinito
    calls = []

    class DummyProc:
        def wait(self):
            return 2

    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: (calls.append(1) or DummyProc()))
    rc = supervisor.run_supervised(max_restarts=5, base_backoff_sec=0.01, stop_signal_path=str(tmp_path / "bot.stop"))
    assert rc == 2
    assert len(calls) == 1


# 8. Regressões adicionais da revisão de diff
def test_pending_restore_keeps_record_on_network_error_and_clears_on_order_not_found(tmp_path, monkeypatch):
    db = tb.TradingDatabase(db_path=str(tmp_path / "pending_reconcile.db"))

    class NetFailEx:
        urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}
        def load_markets(self):
            return {"BTC/USDT": {"id": "BTCUSDT"}}
        def privateGetOrder(self, params):
            raise ccxt.NetworkError("timeout")

    class NotFoundEx(NetFailEx):
        def privateGetOrder(self, params):
            raise ccxt.OrderNotFound("order does not exist -2013")

    mgr = pm.PositionManager(
        db, NetFailEx(), mode="testnet", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0, use_exchange_oco=False,
    )
    pending = pm.Position(
        symbol="BTC/USDT", timeframe="1h", mode="testnet", model_version=1,
        entry_time="2026-11-23 10:00:00", entry_price=90000.0, quantity=0.001,
        order_id="fynexb_test_123", sl_price=89100.0, tp_price=91800.0, status="PENDING",
    )
    mgr.store.save(pending)
    monkeypatch.setattr(binance_client.time, "sleep", lambda *_: None)

    # Erro de rede transitório -> mantém PENDING intacto
    mgr.restore()
    still_pending = mgr.store.load("BTC/USDT", "1h", "testnet")
    assert still_pending is not None and still_pending.status == "PENDING"

    # OrderNotFound definitivo -> limpa PENDING com segurança
    mgr.exchange = NotFoundEx()
    mgr.restore()
    assert mgr.store.load("BTC/USDT", "1h", "testnet") is None


def test_forex_ai_trader_scans_second_pair_when_top_pair_already_open(tmp_path, monkeypatch):
    db_path = str(tmp_path / "forex_scan.db")
    trader = forex_ai_trader.ForexAITrader(db_path=db_path, max_open_forex_positions=2, min_score=65.0)
    monkeypatch.setattr(forex_ai_trader, "is_forex_market_open", lambda *a, **k: (True, "Aberto"))
    monkeypatch.setattr(
        forex_market, "fetch_forex_quote",
        lambda sym, **k: {"symbol": sym, "price": 1.10, "bid": 1.0999, "ask": 1.1001},
    )
    # Abre posição prévia no par de maior score (EUR/USD)
    trader.store.open_position(
        "EUR/USD", "BUY", lots=0.05, leverage=30,
        quote_override={"price": 1.10, "bid": 1.0999, "ask": 1.1001},
    )

    fake_candles = [
        {"time": 1_700_000_000 + i * 3600, "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 100.0}
        for i in range(45)
    ]
    monkeypatch.setattr(forex_market, "fetch_forex_candles", lambda *a, **k: fake_candles)

    def fake_eval(sym, df, macro_snapshot=None):
        if sym == "EUR/USD":
            return {"symbol": "EUR/USD", "side": "BUY", "score": 88.0, "atr_pct": 0.004, "reason": "top"}
        if sym == "GBP/USD":
            return {"symbol": "GBP/USD", "side": "SELL", "score": 76.0, "atr_pct": 0.004, "reason": "second"}
        return {"symbol": sym, "side": "HOLD", "score": 30.0, "atr_pct": 0.004, "reason": "hold"}

    monkeypatch.setattr(forex_ai_trader, "evaluate_forex_opportunity", fake_eval)
    monkeypatch.setattr(trader.copilot, "record_signal", lambda **k: {"id": 1})

    report = trader.run_cycle(macro_snapshot={"macro_score": 0.0})
    assert report["open_forex_positions"] == 2
    assert "GBP/USD" in (report["last_action"] or "")


def test_supervisor_cleans_stop_file_during_backoff_and_monotonic_none_sentinels(tmp_path, monkeypatch):
    stop_file = tmp_path / "bot.stop"

    class CrashProc:
        def wait(self):
            return 1

    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: CrashProc())
    monkeypatch.setattr(supervisor.time, "sleep", lambda *a, **k: stop_file.write_text("stop", encoding="utf-8"))
    rc = supervisor.run_supervised(max_restarts=3, base_backoff_sec=2.0, stop_signal_path=str(stop_file))
    assert rc == 0
    assert not stop_file.exists()

    # Sentinela None para time.monotonic() baixo (ex.: uptime de 1.0s após boot)
    monkeypatch.setattr(server.time, "monotonic", lambda: 1.0)
    server._LAST_REFRESH_TS.pop("_test_low_uptime", None)
    assert server._allow_forced_refresh("_test_low_uptime", cooldown_sec=15.0) is True
    assert server._allow_forced_refresh("_test_low_uptime", cooldown_sec=15.0) is False
    server._LAST_REFRESH_TS.pop("_test_low_uptime", None)

    alerts._LAST_ALERT_TS.clear()
    monkeypatch.setattr(alerts.time, "monotonic", lambda: 1.0)

    class _DummyResp:
        status = 200
        def read(self, n=-1): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(alerts.urllib.request, "urlopen", lambda *a, **k: _DummyResp())
    assert alerts.send_alert("Erro crítico teste boot", level="CRITICAL", token="123:ABC", chat_id="999") is True
    assert alerts.send_alert("Erro crítico teste boot", level="CRITICAL", token="123:ABC", chat_id="999") is False
    alerts._LAST_ALERT_TS.clear()
