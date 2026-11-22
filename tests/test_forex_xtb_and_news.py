"""
Testes da Mesa Forex/Cripto estilo XTB, Feed de Notícias ao Vivo e Endpoints do Dashboard.
100% offline (todas as chamadas de rede são substituídas por mocks).
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from core import forex_market, news_feed  # noqa: E402
from dashboard import server  # noqa: E402


@pytest.fixture(autouse=True)
def clean_caches():
    forex_market.clear_forex_caches()
    news_feed.clear_news_cache()
    yield
    forex_market.clear_forex_caches()
    news_feed.clear_news_cache()


# ----------------------------------------- 1. Forex Quote & Candles
def test_forex_quote_and_candles_parsing(monkeypatch):
    fake_chart = {
        "meta": {
            "regularMarketPrice": 1.0850,
            "chartPreviousClose": 1.0800,
            "regularMarketDayHigh": 1.0890,
            "regularMarketDayLow": 1.0780,
            "regularMarketVolume": 125000,
        },
        "timestamp": [1_700_000_000, 1_700_000_060],
        "indicators": {
            "quote": [{
                "open": [1.0810, 1.0830],
                "high": [1.0840, 1.0860],
                "low": [1.0800, 1.0825],
                "close": [1.0830, 1.0850],
                "volume": [100, 150],
            }]
        },
    }
    monkeypatch.setattr(forex_market, "_fetch_yahoo_chart", lambda *a, **k: fake_chart)

    q = forex_market.fetch_forex_quote("EUR/USD", ttl_seconds=0.0)
    assert q["symbol"] == "EUR/USD"
    assert q["category"] == "forex"
    assert q["price"] == pytest.approx(1.0850)
    assert q["ask"] > q["bid"]
    assert q["change_24h_pct"] > 0

    candles = forex_market.fetch_forex_candles("EUR/USD", timeframe="1m", limit=10, ttl_seconds=0.0)
    assert len(candles) == 2
    assert candles[0]["time"] == 1_700_000_000
    assert candles[1]["close"] == pytest.approx(1.0850)


# ----------------------------------------- 2. Mesa de Operações XTB (BUY, SELL, SL/TP, Margem)
def test_xtb_desk_open_pnl_and_close_forex(tmp_path):
    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "xtb.db"))

    # Abre BUY 0.10 lote em EUR/USD (10.000 EUR) com alavancagem 1:30
    pos = store.open_position(
        symbol="EUR/USD",
        side="BUY",
        lots=0.10,
        leverage=30,
        sl_price=1.0750,
        tp_price=1.0950,
        quote_override={"price": 1.0850, "bid": 1.0849, "ask": 1.0850},
    )
    assert pos["symbol"] == "EUR/USD"
    assert pos["side"] == "BUY"
    assert pos["entry_price"] == pytest.approx(1.0850)
    # Nocional = 0.10 * 100.000 * 1.0850 = 10.850 USD -> margem / 30 = 361.67 USD
    assert pos["margin_usd"] == pytest.approx(361.67, abs=0.05)

    # Preço sobe 50 pips (1.0900) -> lucro de $50.00 em 0.10 lote
    snap = store.get_account_snapshot(
        live_prices={"EUR/USD": {"price": 1.0900, "bid": 1.0900, "ask": 1.0901}}
    )
    assert len(snap["open_positions"]) == 1
    assert snap["floating_pnl_usd"] == pytest.approx(50.0, abs=0.1)
    assert snap["equity_usd"] == pytest.approx(10_050.0, abs=0.1)

    # Fecha manualmente
    closed = store.close_position(
        pos["id"], reason="MANUAL", quote_override={"price": 1.0900, "bid": 1.0900, "ask": 1.0901}
    )
    assert closed["pnl_usd"] == pytest.approx(50.0, abs=0.1)

    snap_after = store.get_account_snapshot()
    assert snap_after["open_positions"] == []
    assert snap_after["balance_usd"] == pytest.approx(10_050.0, abs=0.1)
    assert len(snap_after["recent_trades"]) == 1


def test_xtb_desk_sell_short_and_auto_take_profit(tmp_path):
    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "xtb_short.db"))

    # Abre SELL (Short) 0.10 lote em USD/BRL a 5.5000 com TP em 5.4000
    pos = store.open_position(
        symbol="USD/BRL",
        side="SELL",
        lots=0.10,
        leverage=30,
        tp_price=5.4000,
        quote_override={"price": 5.5000, "bid": 5.5000, "ask": 5.5010},
    )
    assert pos["side"] == "SELL"

    # Cotação cai para 5.3900 -> dispara TAKE_PROFIT automaticamente!
    snap = store.get_account_snapshot(
        live_prices={"USD/BRL": {"price": 5.3900, "bid": 5.3890, "ask": 5.3900}}
    )
    assert snap["open_positions"] == []
    assert len(snap["recent_trades"]) == 1
    assert snap["recent_trades"][0]["reason"] == "TAKE_PROFIT"
    assert snap["recent_trades"][0]["pnl_usd"] > 0


def test_xtb_desk_validates_invalid_sl_and_margin(tmp_path):
    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "xtb_val.db"))
    with pytest.raises(ValueError, match="Stop Loss"):
        store.open_position(
            symbol="EUR/USD",
            side="BUY",
            lots=0.10,
            leverage=30,
            sl_price=1.2000,  # inválido: acima da entrada em BUY
            quote_override={"price": 1.0850, "bid": 1.0849, "ask": 1.0850},
        )


# ----------------------------------------- 3. Notícias ao Vivo (RSS Parser + Filtros)
def test_news_feed_parses_rss_classifies_and_filters(monkeypatch):
    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Test Finance</title>
        <item>
          <title>Bitcoin dispara e renova recorde com entrada em ETF</title>
          <description>&lt;p&gt;BTC registra forte alta no mercado cripto.&lt;/p&gt;</description>
          <link>https://example.com/btc</link>
          <pubDate>Fri, 20 Nov 2026 20:00:00 GMT</pubDate>
        </item>
        <item>
          <title>Dolar cai frente ao Euro apos alerta de inflacao do Fed</title>
          <description>Par EUR/USD reage a queda de juros e risco macro.</description>
          <link>https://example.com/fx</link>
          <pubDate>Fri, 20 Nov 2026 19:30:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """
    items = news_feed.parse_rss_xml(sample_xml, "FonteTeste", "macro")
    assert len(items) == 2
    assert items[0]["category"] == "cripto"
    assert items[0]["sentiment"] == "positivo"
    assert "<p>" not in items[0]["summary"]
    assert items[1]["category"] == "forex"
    assert items[1]["sentiment"] == "negativo"

    monkeypatch.setattr(news_feed, "_fetch_single_feed", lambda src: items if src["name"] == "InfoMoney" else [])
    res_fx = news_feed.fetch_live_news(category="forex", query="euro", ttl_seconds=0.0)
    assert res_fx["total"] == 1
    assert "Euro" in res_fx["items"][0]["title"]


# ----------------------------------------- 4. Endpoints do Dashboard (Forex, XTB, News)
def test_dashboard_xtb_forex_and_news_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "dash_xtb.db"))
    monkeypatch.setattr(
        forex_market,
        "fetch_forex_quote",
        lambda symbol: {
            "symbol": symbol, "category": "forex", "price": 1.0850, "bid": 1.0849, "ask": 1.0850,
            "spread": 0.0001, "spread_pips": 1.0, "spread_pct": 0.009, "change_24h_pct": 0.45,
            "high_24h": 1.089, "low_24h": 1.078, "volume_24h": 10000, "latency_ms": 22.0, "timestamp": 1_700_000_000_000,
        },
    )
    monkeypatch.setattr(
        forex_market,
        "fetch_forex_candles",
        lambda symbol, timeframe="1m", limit=150: [
            {"time": 1_700_000_000, "open": 1.084, "high": 1.086, "low": 1.083, "close": 1.085, "volume": 100.0}
        ],
    )
    monkeypatch.setattr(
        news_feed,
        "fetch_live_news",
        lambda category="all", query="", limit=30: {
            "total": 1, "updated_at": "22:00:00 UTC",
            "items": [{"title": "Noticia Teste", "summary": "Resumo", "link": "https://example.com", "source": "InfoMoney", "category": "macro", "sentiment": "positivo", "published_at": "20/11 22:00 UTC"}],
        },
    )

    client = TestClient(server.app)

    # Instrumentos incluem Forex, Cripto, Metais/Commodities e Índices
    r_inst = client.get("/api/market/instruments")
    assert r_inst.status_code == 200
    syms = {i["symbol"] for i in r_inst.json()["instruments"]}
    assert {"EUR/USD", "USD/BRL", "BTC/USDT", "XAU/USD", "XAG/USD", "OIL/USD", "REMX/USD", "US500/USD"}.issubset(syms)

    # Filtros por categoria commodities e indices
    r_comm = client.get("/api/market/instruments?category=commodities")
    assert r_comm.status_code == 200
    comm_syms = {i["symbol"] for i in r_comm.json()["instruments"]}
    assert {"XAU/USD", "XAG/USD", "XPT/USD", "COPPER/USD", "OIL/USD", "BRENT/USD", "NATGAS/USD", "REMX/USD"}.issubset(comm_syms)
    for sym in ("XAU/USD", "XAG/USD", "OIL/USD", "REMX/USD", "US500/USD"):
        assert forex_market.is_forex_symbol(sym) is True

    r_idx = client.get("/api/market/instruments?category=indices")
    assert r_idx.status_code == 200
    idx_syms = {i["symbol"] for i in r_idx.json()["instruments"]}
    assert {"US500/USD", "US100/USD"}.issubset(idx_syms)

    # Candles Forex roteados corretamente
    r_fx_c = client.get("/api/candles?symbol=EUR/USD&timeframe=5m&limit=10")
    assert r_fx_c.status_code == 200
    assert r_fx_c.json()[0]["close"] == pytest.approx(1.085)

    # Notícias ao vivo
    r_news = client.get("/api/news?category=all")
    assert r_news.status_code == 200
    assert r_news.json()["total"] == 1

    # CSRF exigido em POST /api/xtb/order
    r_no_csrf = client.post("/api/xtb/order", json={"symbol": "EUR/USD", "side": "BUY", "lots": 0.1, "leverage": 30})
    assert r_no_csrf.status_code == 403

    # Abrir e fechar ordem com CSRF válido
    r_open = client.post(
        "/api/xtb/order",
        headers={"X-CSRF-Token": server.CSRF_TOKEN},
        json={"symbol": "EUR/USD", "side": "BUY", "lots": 0.1, "leverage": 30},
    )
    assert r_open.status_code == 200
    pos_id = r_open.json()["position"]["id"]

    r_acc = client.get("/api/xtb/account")
    assert r_acc.status_code == 200
    assert len(r_acc.json()["open_positions"]) == 1

    r_close = client.post(f"/api/xtb/close/{pos_id}", headers={"X-CSRF-Token": server.CSRF_TOKEN})
    assert r_close.status_code == 200
    assert r_close.json()["status"] == "closed"


# ----------------------------------------- 5. Operador Autônomo de Forex (Horário + Melhores Oportunidades)
def test_forex_market_hours_and_autonomous_trader(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import numpy as np
    from core import forex_ai_trader

    # Sábado -> fechado | Domingo 15h UTC -> fechado | Quarta 14h UTC -> aberto | Sexta 22h UTC -> fechado
    assert forex_ai_trader.is_forex_market_open(datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc))[0] is False
    assert forex_ai_trader.is_forex_market_open(datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc))[0] is False
    assert forex_ai_trader.is_forex_market_open(datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc))[0] is True
    assert forex_ai_trader.is_forex_market_open(datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc))[0] is False

    # Gera série de alta realista com pullbacks (RSI ~58-62, sem sobrecompra)
    base_ts = 1_700_000_000
    closes = []
    p = 1.0700
    for i in range(60):
        step = 0.0006 if (i % 2 == 0) else -0.0004
        p += step
        closes.append(round(p, 5))
    fake_candles = [
        {
            "time": base_ts + i * 3600,
            "open": c - 0.0002,
            "high": c + 0.0006,
            "low": c - 0.0005,
            "close": c,
            "volume": 1000.0,
        }
        for i, c in enumerate(closes)
    ]

    monkeypatch.setattr(forex_market, "fetch_forex_candles", lambda sym, timeframe="1h", limit=80: fake_candles)
    monkeypatch.setattr(
        forex_market,
        "fetch_forex_quote",
        lambda sym: {
            "symbol": sym, "category": "forex", "price": 1.0840, "bid": 1.0839, "ask": 1.0840,
            "spread": 0.0001, "spread_pips": 1.0, "spread_pct": 0.009, "change_24h_pct": 0.4,
            "high_24h": 1.086, "low_24h": 1.078, "volume_24h": 10000, "latency_ms": 20.0, "timestamp": base_ts * 1000,
        },
    )

    trader = forex_ai_trader.ForexAITrader(
        db_path=str(tmp_path / "fx_ai.db"),
        pairs=("EUR/USD",),
        min_score=65.0,
    )

    # No fim de semana não abre nada
    rep_closed = trader.run_cycle(
        macro_snapshot={"dxy_change_pct": -0.35},
        now_utc=datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc),
    )
    assert rep_closed["market_open"] is False
    assert rep_closed["last_action"] is None

    # Em dia útil com mercado aberto -> identifica oportunidade BUY em EUR/USD e abre na mesa XTB!
    rep_open = trader.run_cycle(
        macro_snapshot={"dxy_change_pct": -0.35},
        now_utc=datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc),
    )
    assert rep_open["market_open"] is True
    assert rep_open["last_action"] is not None
    assert "EUR/USD" in rep_open["last_action"]

    # Verifica que o Copiloto XTB registrou o sinal com ticker oficial xStation 5
    copilot_sigs = trader.copilot.list_signals()
    assert len(copilot_sigs) == 1
    assert copilot_sigs[0]["xtb_symbol"] == "EURUSD"
    assert copilot_sigs[0]["side"] == "BUY"
    assert copilot_sigs[0]["status"] == "NEW"

    # Endpoint /api/forex/ai-status responde 200
    client = TestClient(server.app)
    r = client.get("/api/forex/ai-status")
    assert r.status_code == 200
    assert "market_open" in r.json()


# ----------------------------------------- 6. Copiloto XTB (Modo Híbrido: Sinais, Deduplicação, Status e API)
def test_xtb_copilot_store_and_endpoints(tmp_path, monkeypatch):
    from core import xtb_copilot

    db_file = str(tmp_path / "copilot_test.db")
    monkeypatch.setattr(server, "DB_PATH", db_file)

    assert xtb_copilot.to_xtb_symbol("XAU/USD") == "GOLD"
    assert xtb_copilot.to_xtb_symbol("XAG/USD") == "SILVER"
    assert xtb_copilot.to_xtb_symbol("OIL/USD") == "OIL.WTI"
    assert xtb_copilot.to_xtb_symbol("US500/USD") == "US500"

    store = xtb_copilot.XTBCopilotStore(db_path=db_file)
    s1 = store.record_signal(
        symbol="XAU/USD",
        side="BUY",
        score=84.0,
        entry_price=2650.5,
        sl_price=2625.0,
        tp_price=2700.0,
        suggested_lots=0.05,
        leverage=20,
        reason="EMA alta, DXY -0.35%",
        notify_telegram=False,
    )
    assert s1["xtb_symbol"] == "GOLD"
    assert s1["deduplicated"] is False

    # Deduplicação dentro da janela de 2h
    s2 = store.record_signal(
        symbol="XAU/USD",
        side="BUY",
        score=86.0,
        entry_price=2652.0,
        sl_price=2626.0,
        tp_price=2702.0,
        suggested_lots=0.05,
        leverage=20,
        notify_telegram=False,
    )
    assert s2["deduplicated"] is True
    assert s2["id"] == s1["id"]

    client = TestClient(server.app)
    r_list = client.get("/api/xtb/copilot/signals")
    assert r_list.status_code == 200
    body = r_list.json()
    assert body["mode"] == "HYBRID"
    assert body["new_count"] == 1
    assert body["signals"][0]["xtb_symbol"] == "GOLD"

    # CSRF exigido ao atualizar status
    r_no_csrf = client.post(f"/api/xtb/copilot/signals/{s1['id']}/status", json={"status": "EXECUTED_XTB"})
    assert r_no_csrf.status_code == 403

    r_ok = client.post(
        f"/api/xtb/copilot/signals/{s1['id']}/status",
        headers={"X-CSRF-Token": server.CSRF_TOKEN},
        json={"status": "EXECUTED_XTB"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["signal"]["status"] == "EXECUTED_XTB"
