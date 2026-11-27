"""
Testes Unitários da Arquitetura Refatorada (FyNex Quant AI)
===========================================================
Cobre os novos módulos de infraestrutura (`core.infra`), proteção dinâmica de
lucro compartilhada (`core.trading.profit_protection`), facades de compatibilidade
e integridade dos módulos estáticos do frontend (ES Modules + CSS).
"""

from __future__ import annotations

import math
import os
import re
import time

import pytest

from core.infra.cache import CooldownTracker, TTLCache
from core.infra.env_store import mask_secret, read_env_file, scrub_secrets, update_env_keys
from core.infra.sqlite import SchemaRegistry, ensure_column, sqlite_connection, sqlite_readonly_optional, table_exists
from core.infra.utils import finite_float, parse_utc_str, sanitize_token, tail_lines, to_float, utc_now_str
from core.trading.profit_protection import compute_profit_protection_stop


def test_infra_utils_numeric_time_and_tokens(tmp_path):
    assert finite_float("12.5") == 12.5
    assert finite_float(float("nan")) is None
    assert finite_float(float("inf"), default=0.0) == 0.0
    assert to_float("bad", 42.0) == 42.0

    now_s = utc_now_str()
    parsed = parse_utc_str(now_s)
    assert parsed is not None
    assert parsed.strftime("%Y-%m-%d %H:%M:%S") == now_s
    assert parse_utc_str("invalid") is None

    assert sanitize_token("BTC/USDT", "symbol") == "BTC_USDT"
    with pytest.raises(ValueError):
        sanitize_token("../etc/passwd", "symbol")

    log_file = tmp_path / "sample.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(1, 21)), encoding="utf-8")
    assert tail_lines(str(log_file), max_lines=3) == ["line-18", "line-19", "line-20"]


def test_ttl_cache_expiration_lru_and_cooldown():
    cache: TTLCache[str, int] = TTLCache(max_size=2, default_ttl=5.0)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == (True, 1)  # promove 'a' para MRU
    cache.set("c", 3)  # deve despejar 'b' (LRU)
    assert cache.get("b") == (False, None)
    assert cache.get("a") == (True, 1)
    assert cache.get("c") == (True, 3)

    assert cache.delete_matching(lambda k: k == "a") == 1
    assert len(cache) == 1
    assert cache.get("c", ttl_seconds=0.0) == (False, None)

    cd = CooldownTracker()
    assert cd.allow("news", 10.0) is True
    assert cd.allow("news", 10.0) is False
    cd.clear()
    assert cd.allow("news", 10.0) is True


def test_env_store_atomic_update_preserves_comments_and_scrubs(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# Comentario preservado\nFOO=old\nBAR=keep\n", encoding="utf-8")

    update_env_keys({"FOO": "new", "BAZ": "added"}, str(env_path))
    raw = env_path.read_text(encoding="utf-8")
    assert "# Comentario preservado" in raw
    parsed = read_env_file(str(env_path))
    assert parsed["FOO"] == "new"
    assert parsed["BAR"] == "keep"
    assert parsed["BAZ"] == "added"

    secret_msg = "Erro em https://api.telegram.org/bot123456789:ABCDEF1234567890/sendMessage?apikey=MYSECRETKEY99"
    cleaned = scrub_secrets(secret_msg, ("MYSECRETKEY99",))
    assert "ABCDEF1234567890" not in cleaned
    assert "MYSECRETKEY99" not in cleaned
    assert mask_secret("1234567890abcdef") == "1234...cdef"
    assert mask_secret("short") == "****"


def test_sqlite_infra_schema_registry_and_readonly_optional(tmp_path):
    db_file = str(tmp_path / "infra.db")
    with sqlite_readonly_optional(db_file) as conn:
        assert conn is None

    reg = SchemaRegistry()
    calls = []

    def init_schema():
        calls.append(1)
        with sqlite_connection(db_file) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)")
            ensure_column(conn.cursor(), "items", "extra", "INTEGER DEFAULT 0")
            ensure_column(conn.cursor(), "items", "extra", "INTEGER DEFAULT 0")  # idempotente

    reg.ensure_initialized("test_schema", db_file, init_schema)
    reg.ensure_initialized("test_schema", db_file, init_schema)
    assert len(calls) == 1

    with sqlite_readonly_optional(db_file) as conn:
        assert conn is not None
        assert table_exists(conn, "items") is True


def test_shared_profit_protection_breakeven_trailing_and_oco_clearance():
    # Entrada 100, TP 110 (+10%), SL inicial 95
    # 1. Progresso 50% (< 60%): não move
    d1 = compute_profit_protection_stop(
        entry_price=100.0, tp_price=110.0, current_sl=95.0, high_check=105.0, current_price=105.0
    )
    assert d1.should_update is False

    # 2. Progresso 65% (>= 60%): ativa breakeven (+0.25% = 100.25)
    d2 = compute_profit_protection_stop(
        entry_price=100.0, tp_price=110.0, current_sl=95.0, high_check=106.5, current_price=106.5
    )
    assert d2.should_update is True
    assert math.isclose(d2.new_sl, 100.25, rel_tol=1e-6)

    # 3. Progresso 85% (>= 80%): trava 50% do lucro atual (100 + 8.5 * 0.5 = 104.25)
    d3 = compute_profit_protection_stop(
        entry_price=100.0, tp_price=110.0, current_sl=100.25, high_check=108.5, current_price=108.5
    )
    assert d3.should_update is True
    assert math.isclose(d3.new_sl, 104.25, rel_tol=1e-6)

    # 4. Preço recuou para perto do novo stop -> bloqueia atualização para evitar stop fantasma
    d4 = compute_profit_protection_stop(
        entry_price=100.0, tp_price=110.0, current_sl=100.25, high_check=108.5, current_price=104.20
    )
    assert d4.should_update is False


def test_modular_packages_and_root_facades_exports():
    import alerts
    import binance_client
    import position_manager
    import trading_bot
    from core import error_learning, forex_market, ml
    from dashboard import server

    assert hasattr(ml, "build_features") and hasattr(ml, "PlattCalibratedModel")
    assert hasattr(error_learning, "ErrorLearningStore") and hasattr(error_learning, "compute_hard_example_multipliers")
    assert hasattr(binance_client, "execute_spot_buy") and hasattr(binance_client, "place_protective_oco")
    assert hasattr(position_manager, "PositionManager") and hasattr(position_manager, "RiskManager")
    assert hasattr(alerts, "send_alert") and hasattr(alerts, "build_daily_summary")
    assert hasattr(forex_market, "XTBDeskStore") and hasattr(forex_market, "fetch_watchlist")
    assert hasattr(trading_bot, "TradingBotRunner") and trading_bot.LIVE_TRADING_ENABLED is False
    assert server.app is not None


def test_frontend_es_modules_and_css_manifest_integrity():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    static_dir = os.path.join(root, "dashboard", "static")

    index_html = open(os.path.join(static_dir, "index.html"), encoding="utf-8").read()
    assert '<script type="module" src="/static/js/main.js"></script>' in index_html

    js_dir = os.path.join(static_dir, "js")
    main_js = open(os.path.join(js_dir, "main.js"), encoding="utf-8").read()
    imported_js = re.findall(r'from\s+"\.\/([^"]+)"', main_js)
    assert len(imported_js) >= 10
    for rel in imported_js:
        assert os.path.exists(os.path.join(js_dir, rel)), f"Módulo JS ausente: {rel}"

    style_css = open(os.path.join(static_dir, "style.css"), encoding="utf-8").read()
    imported_css = re.findall(r'@import\s+url\("/static/css/([^"]+)"\)', style_css)
    assert len(imported_css) >= 9
    for fname in imported_css:
        assert os.path.exists(os.path.join(static_dir, "css", fname)), f"Módulo CSS ausente: {fname}"

    # Integridade de IDs HTML (sem duplicados) e ausência de handlers inline (bloqueados por CSP)
    html_ids = re.findall(r'\bid=["\']([^"\']+)["\']', index_html)
    assert len(html_ids) == len(set(html_ids)), "IDs duplicados detectados em index.html"
    assert not re.search(r'\bon(click|change|input|submit)\s*=', index_html, re.I), "Evento inline bloqueado por CSP"

    # Todos os imports relativos entre módulos JS devem existir e ser exportados pelo destino
    exports_map = {}
    js_sources = {}
    for fname in os.listdir(js_dir):
        if not fname.endswith(".js"):
            continue
        src = open(os.path.join(js_dir, fname), encoding="utf-8").read()
        js_sources[fname] = src
        exports_map[fname] = set(
            re.findall(r'export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)', src)
        )

    all_exported = {sym for syms in exports_map.values() for sym in syms}
    for fname, src in js_sources.items():
        local_syms = set(re.findall(r'(?:function|const|let|var|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)', src))
        for block, target in re.findall(r'import\s*\{([^}]+)\}\s*from\s*["\']\.\/([^"\']+)["\']', src, re.S):
            assert target in exports_map, f"{fname} importa módulo inexistente {target}"
            for item in block.split(","):
                item = item.strip()
                if not item:
                    continue
                orig = item.split(" as ")[0].strip()
                alias = item.split(" as ")[-1].strip()
                assert orig in exports_map[target], f"{fname} importa '{orig}' não exportado por {target}"
                local_syms.add(alias)
        clean = re.sub(r'//.*|/\*[\s\S]*?\*/', '', src)
        for called in re.findall(r'(?<![.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(', clean):
            if called in all_exported:
                assert called in local_syms, f"{fname} chama '{called}()' sem importar o símbolo"


def test_forex_anti_correlation_sl_cooldown_and_xtb_breakeven(tmp_path):
    from datetime import datetime, timedelta, timezone
    from core import forex_ai_trader, forex_market

    # 1. Classificação de exposição ao Dólar (anti-correlação)
    assert forex_ai_trader._usd_exposure_direction("USD/JPY", "BUY") == "LONG_USD"
    assert forex_ai_trader._usd_exposure_direction("USD/CHF", "BUY") == "LONG_USD"
    assert forex_ai_trader._usd_exposure_direction("GBP/USD", "SELL") == "LONG_USD"
    assert forex_ai_trader._usd_exposure_direction("EUR/USD", "BUY") == "SHORT_USD"
    assert forex_ai_trader._usd_exposure_direction("US500/USD", "BUY") is None

    # 2. Cooldown pós-Stop Loss bloqueia reentrada no mesmo par por 4h
    trader = forex_ai_trader.ForexAITrader(db_path=str(tmp_path / "fx_cd.db"), sl_cooldown_hours=4.0)
    now = datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc)
    recent_1h = [{"symbol": "USD/CHF", "reason": "STOP_LOSS", "pnl_usd": -33.5, "closed_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}]
    recent_5h = [{"symbol": "USD/CHF", "reason": "STOP_LOSS", "pnl_usd": -33.5, "closed_at": (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")}]
    assert trader._is_symbol_in_sl_cooldown("USD/CHF", recent_1h, now) is True
    assert trader._is_symbol_in_sl_cooldown("USD/CHF", recent_5h, now) is False

    # 3. Proteção automática de Breakeven na mesa XTB em >= 60% do alvo TP
    store = forex_market.XTBDeskStore(db_path=str(tmp_path / "xtb_be.db"))
    pos = store.open_position(
        symbol="EUR/USD", side="BUY", lots=0.10, leverage=20,
        sl_price=1.0900, tp_price=1.1100,
        quote_override={"price": 1.1000, "bid": 1.0999, "ask": 1.1000},
    )
    # Progresso 70% até o TP (1.1070) -> deve elevar SL automaticamente para Breakeven (> 1.1000)
    snap = store.get_account_snapshot(live_prices={"EUR/USD": {"price": 1.1070, "bid": 1.1070, "ask": 1.1071}})
    assert len(snap["open_positions"]) == 1
    assert snap["open_positions"][0]["sl_price"] > 1.1000
    assert snap["open_positions"][0]["sl_price"] < 1.1070


def test_forex_weekend_history_training_and_ml_confluence(tmp_path):
    import numpy as np
    import pandas as pd
    import train_ai
    from core import forex_ai_trader
    from core.market_data import prepare_training_history

    # Cria 260 velas de 1h com uma lacuna de fim de semana (50h) no meio
    ts_list = []
    t = 1_700_000_000_000
    for i in range(260):
        if i == 120:
            t += 50 * 3600 * 1000  # salto de fim de semana
        ts_list.append(t)
        t += 3600 * 1000

    rng = np.random.default_rng(123)
    rets = rng.normal(0.0002, 0.0045, size=260)
    closes = 2600.0 * np.cumprod(1.0 + rets)
    highs = closes * (1.0 + np.abs(rng.normal(0.003, 0.002, size=260)))
    lows = closes * (1.0 - np.abs(rng.normal(0.003, 0.002, size=260)))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(ts_list, unit="ms"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": rng.uniform(100.0, 500.0, size=260),
    })

    # Com allow_weekend_gaps=True, preserva todas as 260 velas através do fim de semana
    cleaned = prepare_training_history(df, "1h", allow_weekend_gaps=True)
    assert len(cleaned) == 260

    # Treina modelo ML para XAU/USD com sucesso
    summary = train_ai.train_and_select_best_model(
        df,
        symbol="XAU/USD",
        timeframe="1h",
        candidates=("rf", "et"),
        models_dir=str(tmp_path / "fx_models"),
        save_winner=True,
        db_path=str(tmp_path / "fx_train.db"),
    )
    assert summary["winner"] is not None
    assert summary["candles_used"] == 260
    assert os.path.exists(summary["saved_path"])

    # Confluência ML em evaluate_forex_opportunity: modelo bullish reforça BUY
    class _FakeBullModel:
        classes_ = np.array([0, 1])

        def predict_proba(self, X):
            return np.tile(np.array([[0.20, 0.80]]), (len(X), 1))

    up_closes = []
    px = 1.0700
    for i in range(60):
        px += 0.0006 if (i % 2 == 0) else -0.0004
        up_closes.append(round(px, 5))
    up_df = pd.DataFrame({
        "timestamp": pd.to_datetime([1_700_000_000 + i * 3600 for i in range(60)], unit="s"),
        "open": [c - 0.0002 for c in up_closes],
        "high": [c + 0.0006 for c in up_closes],
        "low": [c - 0.0005 for c in up_closes],
        "close": up_closes,
        "volume": [1000.0] * 60,
    })
    opp_plain = forex_ai_trader.evaluate_forex_opportunity("EUR/USD", up_df, macro_snapshot={"dxy_change_pct": -0.35})
    opp_ml = forex_ai_trader.evaluate_forex_opportunity(
        "EUR/USD", up_df, macro_snapshot={"dxy_change_pct": -0.35}, ml_model=_FakeBullModel()
    )
    assert opp_plain is not None and opp_ml is not None
    assert opp_ml["ml_active"] is True
    assert opp_ml["ml_prob_up"] == pytest.approx(0.80)
    assert "ML BUY" in opp_ml["reason"]
