"""Testes da API da Área Financeira Completa (`/api/finance/overview` e `/api/finance/export.csv`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.database import TradingDatabase
from core.infra.sqlite import sqlite_connection
from core.markets.xtb_desk import XTBDeskStore
from dashboard.routers.finance import build_finance_overview, export_finance_csv


def test_finance_overview_empty_or_missing_db(tmp_path):
    missing_db = str(tmp_path / "does_not_exist.db")
    res = build_finance_overview(missing_db, period="30d", source="all")
    s = res["summary"]
    assert s["total_trades"] == 0
    assert s["equity_usd"] == pytest.approx(10000.0)
    assert s["win_rate_pct"] == 0.0
    assert len(res["calendar_35d"]) == 35
    assert res["ledger"] == []


def test_finance_overview_consolidates_xtb_and_bot_trades(tmp_path):
    db_path = str(tmp_path / "finance.db")
    TradingDatabase(db_path)
    store = XTBDeskStore(db_path)

    # 2 trades na Mesa XTB (1 lucro, 1 prejuízo) + 1 posição aberta
    p1 = store.open_position(
        symbol="XAU/USD",
        side="BUY",
        lots=0.10,
        leverage=20,
        quote_override={"price": 2500.0, "bid": 2499.8, "ask": 2500.2},
    )
    store.close_position(p1["id"], reason="TAKE_PROFIT", quote_override={"price": 2520.0, "bid": 2519.8, "ask": 2520.2})

    p2 = store.open_position(
        symbol="EUR/USD",
        side="SELL",
        lots=0.10,
        leverage=30,
        quote_override={"price": 1.1000, "bid": 1.0999, "ask": 1.1001},
    )
    store.close_position(p2["id"], reason="STOP_LOSS", quote_override={"price": 1.1050, "bid": 1.1049, "ask": 1.1051})

    store.open_position(
        symbol="GBP/USD",
        side="BUY",
        lots=0.05,
        leverage=30,
        quote_override={"price": 1.2800, "bid": 1.2799, "ask": 1.2801},
    )

    # 1 trade do Robô Spot em simulated_trades
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO simulated_trades (
                symbol, timeframe, model_version, entry_time, entry_price,
                exit_time, exit_price, reason, pnl_pct, pnl_usd, mode, order_id, quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BTC/USDT", "1h", 3, now_str, 90000.0, now_str, 91800.0, "TAKE_PROFIT", 2.0, 25.0, "paper", "ord_1", 0.01),
        )
        conn.commit()

    overview = build_finance_overview(db_path, period="30d", source="all")
    s = overview["summary"]
    assert s["total_trades"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["win_rate_pct"] == pytest.approx(66.7, abs=0.2)
    assert s["open_positions_count"] == 1
    assert s["gross_profit_usd"] > 0
    assert s["gross_loss_usd"] > 0
    assert s["profit_factor"] > 1.0
    assert len(overview["equity_curve"]) >= 4
    assert len(overview["asset_breakdown"]) == 3
    assert len(overview["category_allocation"]) == 3  # commodities, forex, crypto
    assert len(overview["exit_reasons"]) == 2  # TAKE_PROFIT, STOP_LOSS
    assert len(overview["ledger"]) == 3

    # Filtro por origem
    only_bot = build_finance_overview(db_path, period="30d", source="bot")
    assert only_bot["summary"]["total_trades"] == 1
    assert only_bot["summary"]["realized_pnl_usd"] == pytest.approx(25.0)

    only_xtb = build_finance_overview(db_path, period="30d", source="xtb")
    assert only_xtb["summary"]["total_trades"] == 2


def test_finance_export_csv_sanitizes_formulas_and_sets_headers(tmp_path, monkeypatch):
    from dashboard.routers import finance as fin_router

    db_path = str(tmp_path / "csv_test.db")
    TradingDatabase(db_path)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO simulated_trades (
                symbol, timeframe, model_version, entry_time, entry_price,
                exit_time, exit_price, reason, pnl_pct, pnl_usd, mode, order_id, quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BTC/USDT", "1h", 1, now_str, 100.0, now_str, 110.0, "=CMD()", 10.0, 10.0, "paper", "x", 1.0),
        )
        conn.commit()

    monkeypatch.setattr(fin_router, "_db_path", lambda: db_path)
    resp = export_finance_csv(period="all", source="all")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    text = resp.body.decode("utf-8-sig")
    assert "Fechamento_UTC;Origem;Ativo" in text
    assert "BTC/USDT" in text
    assert ";=CMD()" not in text
    assert ";'=CMD()" not in text or ";OUTRO;" in text
