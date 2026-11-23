"""
Testes da Integração WhatsApp via CallMeBot (FyNex Quant AI)
============================================================
Valida:
- Normalização de telefone BR (+55), bloqueio de injeção no .env e mascaramento de API Key
- Envio HTTP ao CallMeBot (sucesso, rejeição de APIKey inválida e scrub de segredos)
- Alertas automáticos de entrada, saída (lucro vs prejuízo) e falha em Cripto e Forex/XTB
- Consolidação e disparo idempotente (1x por dia) do resumo diário em horário de Brasília
- Endpoints FastAPI de configuração e envio de resumo diário do dashboard
"""

import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alerts  # noqa: E402
import binance_client  # noqa: E402
import position_manager as pm  # noqa: E402
import trading_bot as tb  # noqa: E402
from core import forex_market  # noqa: E402
from dashboard import server  # noqa: E402


class _FakeHttpResp:
    def __init__(self, body: str = "Message queued.", status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_whatsapp_phone_normalization_and_env_security(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_MODE=paper\n", encoding="utf-8")
    monkeypatch.setattr(alerts, "ENV_PATH", str(env_file))
    for k in ("WHATSAPP_PHONE", "WHATSAPP_CALLMEBOT_APIKEY", "WHATSAPP_ENABLED", "WHATSAPP_DAILY_REPORT_HOUR"):
        monkeypatch.delenv(k, raising=False)

    assert alerts.normalize_whatsapp_phone("62986290913") == "+5562986290913"
    assert alerts.normalize_whatsapp_phone("062986290913") == "+5562986290913"
    assert alerts.normalize_whatsapp_phone("(62) 98629-0913") == "+5562986290913"
    assert alerts.normalize_whatsapp_phone("+55 62 98629-0913") == "+5562986290913"

    # Rejeita strings sem dígitos e número E.164 começando com zero (sem apagar telefone salvo)
    with pytest.raises(ValueError):
        alerts.normalize_whatsapp_phone("abc+++")
    with pytest.raises(ValueError):
        alerts.normalize_whatsapp_phone("+062986290913")

    # Bloqueia injeção de nova linha no .env
    with pytest.raises(ValueError):
        alerts.save_whatsapp_config(phone="62986290913\nTRADING_MODE=live", apikey="123456", env_path=str(env_file))
    assert "TRADING_MODE=live" not in env_file.read_text(encoding="utf-8")

    # Bloqueia API key inválida e hora fora do intervalo
    with pytest.raises(ValueError):
        alerts.save_whatsapp_config(phone="62986290913", apikey="bad key with spaces!", env_path=str(env_file))
    with pytest.raises(ValueError):
        alerts.save_whatsapp_config(phone="62986290913", apikey="1234567", daily_report_hour=25, env_path=str(env_file))

    cfg = alerts.save_whatsapp_config(
        phone="62986290913",
        apikey="7654321",
        enabled=True,
        daily_report_hour=21,
        env_path=str(env_file),
    )
    assert cfg["phone"] == "+5562986290913"
    assert cfg["configured"] is True
    assert cfg["active"] is True
    assert "7654321" not in cfg["apikey_masked"]

    # Scrub de segredos em erros
    scrubbed = alerts._scrub_token("https://api.callmebot.com/whatsapp.php?phone=+5562986290913&apikey=7654321", "7654321")
    assert "7654321" not in scrubbed


def test_callmebot_http_send_and_error_handling(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(alerts, "ENV_PATH", str(env_file))
    monkeypatch.setattr(alerts, "_WHATSAPP_LAST_SENT_MONO", None)
    alerts.save_whatsapp_config(phone="62986290913", apikey="9988776", enabled=True, env_path=str(env_file))

    captured_urls = []

    def fake_urlopen(req, timeout=8.0):
        captured_urls.append(req.full_url)
        return _FakeHttpResp("Message queued. You will receive it in a few seconds.")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    res = alerts.send_whatsapp_message("Entrada BTC/USDT executada!")
    assert res["sent"] is True
    assert len(captured_urls) == 1
    assert "api.callmebot.com/whatsapp.php" in captured_urls[0]
    assert "%2B5562986290913" in captured_urls[0]
    assert "apikey=9988776" in captured_urls[0]

    # Resposta de erro do CallMeBot com HTTP 200 deve retornar sent=False sem vazar a chave
    monkeypatch.setattr(alerts, "_WHATSAPP_LAST_SENT_MONO", None)
    monkeypatch.setattr(
        alerts.urllib.request,
        "urlopen",
        lambda req, timeout=8.0: _FakeHttpResp("<b>APIKey is invalid for 9988776</b>"),
    )
    bad = alerts.send_whatsapp_message("Teste")
    assert bad["sent"] is False
    assert "9988776" not in (bad["error"] or "")


def test_trade_entry_exit_fail_alerts_crypto_and_xtb(tmp_path, monkeypatch):
    sent_events = []

    def fake_send_alert(message, level="INFO", **kwargs):
        sent_events.append((level, message))
        return True

    monkeypatch.setattr(alerts, "send_alert", fake_send_alert)

    # 1. Cripto Paper: Entrada + Saída com lucro
    db = tb.TradingDatabase(db_path=str(tmp_path / "alerts_crypto.db"))
    mgr = pm.PositionManager(
        db, object(), mode="paper", symbol="BTC/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=100.0, use_exchange_oco=False,
    )
    assert mgr.try_open(100_000.0, 1, 0.72) is True
    assert any(lvl == "TRADE_ENTRY" and "BTC/USDT" in msg for lvl, msg in sent_events)

    assert mgr.close("TAKE_PROFIT", 102_500.0) is True
    assert any(lvl == "TRADE_EXIT" and "DEU RESULTADO (LUCRO)" in msg for lvl, msg in sent_events)

    # 2. Cripto Real/Testnet: Falha na ordem de compra gera TRADE_FAIL
    sent_events.clear()
    mgr_real = pm.PositionManager(
        db, object(), mode="testnet", symbol="ETH/USDT", timeframe="1h",
        stop_loss_pct=0.01, take_profit_pct=0.02, trade_amount_usdt=50.0, use_exchange_oco=False,
    )
    monkeypatch.setattr(binance_client, "get_asset_balances", lambda *a, **k: (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        binance_client, "execute_spot_buy",
        lambda *a, **k: {"success": False, "message": "Saldo USDT insuficiente"},
    )
    assert mgr_real.try_open(3000.0, 1, 0.70) is False
    assert any(lvl == "TRADE_FAIL" and "Saldo USDT insuficiente" in msg for lvl, msg in sent_events)

    # 3. Forex/XTB: Fechamento no prejuízo gera TRADE_EXIT com PREJUÍZO
    sent_events.clear()
    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "alerts_xtb.db"))
    pos = store.open_position(
        "EUR/USD", "BUY", lots=0.10, leverage=30,
        quote_override={"price": 1.1000, "bid": 1.0999, "ask": 1.1000},
    )
    store.close_position(
        pos["id"], reason="STOP_LOSS",
        quote_override={"price": 1.0950, "bid": 1.0950, "ask": 1.0951},
    )
    assert any(lvl == "TRADE_EXIT" and "PREJUÍZO" in msg and "EUR/USD" in msg for lvl, msg in sent_events)


def test_daily_summary_consolidation_and_once_per_day_schedule(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    db_path = str(tmp_path / "daily.db")
    monkeypatch.setattr(alerts, "ENV_PATH", str(env_file))
    alerts.save_whatsapp_config(phone="62986290913", apikey="1234567", enabled=True, daily_report_hour=21, env_path=str(env_file))

    # Popula 1 trade Cripto (lucro) e 1 trade Forex/XTB (prejuízo) hoje
    db = tb.TradingDatabase(db_path=db_path)
    # 2026-11-23 18:00 UTC = 2026-11-23 15:00 BRT
    db.record_trade(
        "BTC/USDT", "1h", 1, "2026-11-23 17:00:00", 90000.0,
        "2026-11-23 18:00:00", 91800.0, "TAKE_PROFIT", 2.0, 20.0, mode="paper", quantity=0.01,
    )
    store = forex_market.XTBDeskStore(db_path=db_path)
    pos = store.open_position(
        "EUR/USD", "BUY", lots=0.05, leverage=30,
        quote_override={"price": 1.1000, "bid": 1.0999, "ask": 1.1000},
    )
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    store.close_position(
        pos["id"], reason="STOP_LOSS",
        quote_override={"price": 1.0980, "bid": 1.0980, "ask": 1.0981},
    )

    # Ajusta closed_at do trade XTB para 2026-11-23 18:30:00 UTC para determinismo
    with db._get_connection() as conn:
        conn.execute("UPDATE xtb_trades SET closed_at = '2026-11-23 18:30:00'")

    # 2026-11-23 23:00 UTC = 2026-11-23 20:00 BRT (antes das 21h -> NÃO envia)
    before_hour = datetime(2026, 11, 23, 23, 0, 0, tzinfo=timezone.utc)
    # 2026-11-24 00:15 UTC = 2026-11-23 21:15 BRT (após 21h -> ENVIA 1x)
    after_hour = datetime(2026, 11, 24, 0, 15, 0, tzinfo=timezone.utc)

    summary = alerts.build_daily_summary(db_path=db_path, now_utc=after_hour)
    assert summary["date"] == "2026-11-23"
    assert summary["total_ops"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert "RESUMO DO DIA" in summary["message"]
    assert "BTC/USDT" in summary["message"]

    dispatched = []
    monkeypatch.setattr(alerts, "send_alert", lambda msg, level="INFO", **k: (dispatched.append((level, msg)) or True))

    assert alerts.maybe_send_scheduled_daily_summary(db_path=db_path, now_utc=before_hour, env_path=str(env_file)) is False
    assert len(dispatched) == 0

    assert alerts.maybe_send_scheduled_daily_summary(db_path=db_path, now_utc=after_hour, env_path=str(env_file)) is True
    assert len(dispatched) == 1
    assert dispatched[0][0] == "DAILY_SUMMARY"

    # Segunda chamada no mesmo dia NÃO duplica o envio
    assert alerts.maybe_send_scheduled_daily_summary(db_path=db_path, now_utc=after_hour, env_path=str(env_file)) is False
    assert len(dispatched) == 1


def test_dashboard_whatsapp_endpoints(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    db_path = str(tmp_path / "dash_wa.db")
    monkeypatch.setattr(alerts, "ENV_PATH", str(env_file))
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(alerts, "_WHATSAPP_LAST_SENT_MONO", None)
    monkeypatch.setattr(
        alerts.urllib.request,
        "urlopen",
        lambda req, timeout=8.0: _FakeHttpResp("Message queued."),
    )

    res = server.save_whatsapp_alert_config(
        server.WhatsAppConfigPayload(
            phone="62986290913",
            apikey="5544332",
            enabled=True,
            daily_report_hour=21,
            send_test=True,
        )
    )
    assert res["status"] == "saved"
    assert res["whatsapp"]["phone"] == "+5562986290913"
    assert res["test_result"]["sent"] is True

    cfg = server.get_whatsapp_alert_config()
    assert cfg["active"] is True

    daily = server.send_whatsapp_daily_summary_now()
    assert daily["status"] == "sent"
    assert daily["send_result"]["sent"] is True

    # Pausar alertas (enabled=False) NÃO deve tentar envio de teste nem retornar erro
    paused = server.save_whatsapp_alert_config(
        server.WhatsAppConfigPayload(
            phone="62986290913",
            apikey="",
            enabled=False,
            daily_report_hour=8,
            send_test=True,
        )
    )
    assert paused["whatsapp"]["enabled"] is False
    assert paused["whatsapp"]["daily_report_hour"] == 8
    assert paused["test_result"] is None
