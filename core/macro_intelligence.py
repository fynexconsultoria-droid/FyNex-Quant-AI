"""
Inteligência Macroeconômica, Notícias e Memória de Aprendizado (FyNex Quant AI)
================================================================================
Combina dados reais ao vivo para apoiar a tomada de decisão da IA nas entradas (30m):
- Influência do Dólar Americano (Índice DXY: DX-Y.NYB)
- Taxa de Juros Americana de 10 Anos (US 10Y Treasury Yield: ^TNX)
- Apetite ao Risco Global (S&P 500: ^GSPC e Ouro: GC=F)
- Sentimento Cripto (Fear & Greed Index)
- Notícias reais ao vivo + Viés de Inflação / Fed / Taxas de Juros
- Persistência contínua em SQLite (`macro_context_snapshots` e `learning_history`)
  para que a IA salve cada novo aprendizado e evolua continuamente.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import forex_market, news_feed

_MACRO_CACHE_LOCK = threading.Lock()
_MACRO_CACHE: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)

_INFLATION_FED_KEYWORDS = (
    "fed", "federal reserve", "fomc", "powell", "juros", "taxa de juros", "interest rate",
    "rate cut", "rate hike", "inflação", "inflacao", "inflation", "cpi", "pce",
    "treasury", "payroll", "selic", "banco central", "central bank",
)
_DOVISH_KEYWORDS = (
    "corte", "cut", "queda da inflação", "desacelera", "alívio", "dovish", "cooling",
    "slows", "lower rates", "estímulo", "stimulus", "pausa", "pause",
)
_HAWKISH_KEYWORDS = (
    "alta dos juros", "hike", "inflação sobe", "acima do esperado", "hawkish", "hotter",
    "recessão", "recession", "aperto", "tightening", "higher for longer", "alerta",
)
_GEOPOLITICS_WAR_KEYWORDS = (
    "guerra", "war", "conflito", "conflict", "míssil", "missile", "ataque", "attack",
    "sanção", "sanctions", "oriente médio", "middle east", "ucrânia", "ukraine",
    "taiwan", "otan", "nato", "opep", "opec", "tensão geopolítica", "geopolitical",
)
_CLIMATE_ENERGY_KEYWORDS = (
    "clima", "climate", "seca", "drought", "furacão", "hurricane", "enchente", "flood",
    "el niño", "safra", "harvest", "petróleo", "oil", "brent", "wti", "gás natural", "energia",
)
_RARE_EARTH_MINERALS_KEYWORDS = (
    "terras raras", "rare earth", "lítio", "lithium", "cobre", "copper", "ouro", "gold",
    "prata", "silver", "minério", "mineral", "mining", "urânio", "uranium", "níquel", "cobalto",
)


def clear_macro_cache() -> None:
    global _MACRO_CACHE
    with _MACRO_CACHE_LOCK:
        _MACRO_CACHE = (0.0, None)


def _fetch_yahoo_change(ticker: str) -> Tuple[float, float]:
    """Retorna (preço_atual, variação_pct) de um índice/yield via Yahoo Finance."""
    chart = forex_market._fetch_yahoo_chart(ticker, interval="15m", range_str="1d")
    meta = chart.get("meta") or {}
    quote_block = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quote_block.get("close") or []) if c is not None]
    price = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0.0))
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or (closes[0] if closes else price))
    chg_pct = ((price - prev) / prev * 100.0) if prev > 0 else 0.0
    return round(price, 4), round(chg_pct, 3)


def _fetch_fear_and_greed(timeout: float = 4.0) -> Tuple[Optional[int], str]:
    """Consulta o Crypto Fear & Greed Index público (0..100)."""
    req = urllib.request.Request(
        "https://api.alternative.me/fng/?limit=1",
        headers={"User-Agent": "FyNexQuantAI/2.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    entry = (data.get("data") or [{}])[0]
    val = int(entry.get("value", 50))
    label = str(entry.get("value_classification", "Neutral"))
    return val, label


def analyze_news_and_inflation(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calcula sentimento geral das notícias, viés de Inflação/Fed e radar de Guerras, Clima e Minérios/Terras Raras."""
    if not items:
        return {
            "news_total": 0,
            "news_positive": 0,
            "news_negative": 0,
            "news_sentiment_score": 0.0,
            "inflation_fed_mentions": 0,
            "inflation_fed_bias": 0.0,
            "war_geopolitics_mentions": 0,
            "climate_energy_mentions": 0,
            "rare_earth_minerals_mentions": 0,
        }

    pos = sum(1 for i in items if i.get("sentiment") == "positivo")
    neg = sum(1 for i in items if i.get("sentiment") == "negativo")
    total = len(items)
    sent_score = (pos - neg) / max(1, total)

    inf_mentions = 0
    dovish = 0
    hawkish = 0
    war_mentions = 0
    climate_mentions = 0
    minerals_mentions = 0

    for i in items:
        blob = f"{i.get('title', '')} {i.get('summary', '')}".lower()
        if any(kw in blob for kw in _INFLATION_FED_KEYWORDS):
            inf_mentions += 1
            if any(k in blob for k in _DOVISH_KEYWORDS):
                dovish += 1
            if any(k in blob for k in _HAWKISH_KEYWORDS):
                hawkish += 1
        if any(kw in blob for kw in _GEOPOLITICS_WAR_KEYWORDS):
            war_mentions += 1
        if any(kw in blob for kw in _CLIMATE_ENERGY_KEYWORDS):
            climate_mentions += 1
        if any(kw in blob for kw in _RARE_EARTH_MINERALS_KEYWORDS):
            minerals_mentions += 1

    inf_bias = (dovish - hawkish) / max(1, inf_mentions) if inf_mentions > 0 else 0.0
    return {
        "news_total": total,
        "news_positive": pos,
        "news_negative": neg,
        "news_sentiment_score": round(float(sent_score), 3),
        "inflation_fed_mentions": inf_mentions,
        "inflation_fed_bias": round(float(inf_bias), 3),
        "war_geopolitics_mentions": war_mentions,
        "climate_energy_mentions": climate_mentions,
        "rare_earth_minerals_mentions": minerals_mentions,
    }


def compute_macro_decision(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calcula o score macro composto (-100 a +100), o regime e o ajuste de confiança
    aplicado ao limiar de entrada da IA:
    - Dólar (DXY) em queda + Juros EUA 10Y em queda + S&P500 em alta + Notícias/Fed positivos -> RISK-ON
    - Dólar disparando + Juros EUA 10Y subindo + Inflação/Notícias negativas -> RISK-OFF (proteção)
    """
    score = 0.0
    drivers: List[str] = []

    dxy_chg = float(snapshot.get("dxy_change_pct") or 0.0)
    # Dólar forte pressiona ativos de risco; dólar fraco favorece alta
    dxy_contrib = max(-25.0, min(25.0, -dxy_chg * 35.0))
    score += dxy_contrib
    if abs(dxy_chg) >= 0.15:
        drivers.append(f"Dólar (DXY) {dxy_chg:+.2f}%")

    us10y_chg = float(snapshot.get("us10y_change_pct") or 0.0)
    # Juros EUA subindo = aperto financeiro (risk-off); caindo = alívio (risk-on)
    yield_contrib = max(-25.0, min(25.0, -us10y_chg * 10.0))
    score += yield_contrib
    if abs(us10y_chg) >= 0.5:
        drivers.append(f"Juros EUA 10Y {us10y_chg:+.2f}%")

    sp_chg = float(snapshot.get("sp500_change_pct") or 0.0)
    sp_contrib = max(-15.0, min(15.0, sp_chg * 12.0))
    score += sp_contrib
    if abs(sp_chg) >= 0.3:
        drivers.append(f"S&P 500 {sp_chg:+.2f}%")

    news_score = float(snapshot.get("news_sentiment_score") or 0.0)
    score += max(-20.0, min(20.0, news_score * 30.0))

    inf_bias = float(snapshot.get("inflation_fed_bias") or 0.0)
    score += max(-15.0, min(15.0, inf_bias * 20.0))
    if snapshot.get("inflation_fed_mentions", 0) > 0:
        drivers.append(f"Fed/Inflação ({inf_bias:+.2f})")

    # Choque de Petróleo (disparada forte gera pressão inflacionária / risco geopolítico)
    oil_chg = float(snapshot.get("oil_change_pct") or 0.0)
    if oil_chg >= 2.5:
        score -= min(12.0, (oil_chg - 1.5) * 3.0)
        drivers.append(f"Choque Petróleo ({oil_chg:+.1f}%)")
    elif oil_chg <= -2.5:
        score += 4.0

    # Terras Raras (REMX) + Cobre: termômetro de crescimento industrial global
    re_chg = float(snapshot.get("rare_earth_change_pct") or 0.0)
    if abs(re_chg) >= 1.0:
        score += max(-6.0, min(6.0, re_chg * 2.0))

    # Tensão geopolítica / guerras nas manchetes -> aumenta cautela
    war_m = int(snapshot.get("war_geopolitics_mentions") or 0)
    if war_m >= 3:
        score -= min(12.0, war_m * 2.5)
        drivers.append(f"Alerta Geopolítico ({war_m} notícias)")

    fg = snapshot.get("fear_greed_value")
    if fg is not None:
        fg_int = int(fg)
        if fg_int <= 22:
            score -= 8.0
            drivers.append(f"Medo Extremo ({fg_int})")
        elif fg_int >= 82:
            score -= 6.0
            drivers.append(f"Ganância Extrema ({fg_int})")
        elif 45 <= fg_int <= 70:
            score += 5.0

    macro_score = round(max(-100.0, min(100.0, score)), 1)

    if macro_score >= 20.0:
        regime = "RISK-ON FAVORÁVEL"
        badge = "🟢"
        adj = -0.02
        desc = "Ambiente macro favorável (Dólar/Juros/Notícias apoiando entradas)."
    elif macro_score > -20.0:
        regime = "MACRO NEUTRO"
        badge = "⚖️"
        adj = 0.0
        desc = "Cenário macro equilibrado; decisão guiada pela calibração da IA."
    elif macro_score > -50.0:
        regime = "CAUTELA MACRO"
        badge = "🟡"
        adj = 0.04
        desc = "Pressão de Dólar, juros EUA ou notícias; exigência de entrada elevada (+4%)."
    else:
        regime = "RISK-OFF SEVERO"
        badge = "🔴"
        adj = 0.08
        desc = "Forte aversão a risco (Dólar/Juros/Inflação adversos); filtro endurecido (+8%)."

    return {
        "macro_score": macro_score,
        "regime": regime,
        "badge": badge,
        "confidence_adjustment": adj,
        "description": desc,
        "drivers": drivers[:4],
    }


def fetch_macro_snapshot(ttl_seconds: float = 300.0) -> Dict[str, Any]:
    """
    Coleta em paralelo todos os fatores macroeconômicos e de notícias com cache de 5 min.
    Sempre retorna um dicionário completo e seguro mesmo se alguma fonte externa falhar.
    """
    global _MACRO_CACHE
    now = time.monotonic()
    with _MACRO_CACHE_LOCK:
        cached_ts, cached_val = _MACRO_CACHE
        if cached_val is not None and (now - cached_ts) < ttl_seconds:
            return cached_val

    snap: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dxy_price": 0.0,
        "dxy_change_pct": 0.0,
        "us10y_yield": 0.0,
        "us10y_change_pct": 0.0,
        "sp500_price": 0.0,
        "sp500_change_pct": 0.0,
        "gold_price": 0.0,
        "gold_change_pct": 0.0,
        "silver_price": 0.0,
        "silver_change_pct": 0.0,
        "oil_price": 0.0,
        "oil_change_pct": 0.0,
        "copper_price": 0.0,
        "copper_change_pct": 0.0,
        "rare_earth_price": 0.0,
        "rare_earth_change_pct": 0.0,
        "fear_greed_value": None,
        "fear_greed_label": "N/D",
        "news_total": 0,
        "news_positive": 0,
        "news_negative": 0,
        "news_sentiment_score": 0.0,
        "inflation_fed_mentions": 0,
        "inflation_fed_bias": 0.0,
        "war_geopolitics_mentions": 0,
        "climate_energy_mentions": 0,
        "rare_earth_minerals_mentions": 0,
    }

    tasks = {
        "dxy": lambda: _fetch_yahoo_change("DX-Y.NYB"),
        "us10y": lambda: _fetch_yahoo_change("^TNX"),
        "sp500": lambda: _fetch_yahoo_change("^GSPC"),
        "gold": lambda: _fetch_yahoo_change("GC=F"),
        "silver": lambda: _fetch_yahoo_change("SI=F"),
        "oil": lambda: _fetch_yahoo_change("CL=F"),
        "copper": lambda: _fetch_yahoo_change("HG=F"),
        "rare_earth": lambda: _fetch_yahoo_change("REMX"),
        "fng": lambda: _fetch_fear_and_greed(),
        "news": lambda: news_feed.fetch_live_news(category="all", limit=36),
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(fn): key for key, fn in tasks.items()}
        for fut in as_completed(future_map):
            key = future_map[fut]
            try:
                res = fut.result()
                if key == "dxy":
                    snap["dxy_price"], snap["dxy_change_pct"] = res
                elif key == "us10y":
                    snap["us10y_yield"], snap["us10y_change_pct"] = res
                elif key == "sp500":
                    snap["sp500_price"], snap["sp500_change_pct"] = res
                elif key == "gold":
                    snap["gold_price"], snap["gold_change_pct"] = res
                elif key == "silver":
                    snap["silver_price"], snap["silver_change_pct"] = res
                elif key == "oil":
                    snap["oil_price"], snap["oil_change_pct"] = res
                elif key == "copper":
                    snap["copper_price"], snap["copper_change_pct"] = res
                elif key == "rare_earth":
                    snap["rare_earth_price"], snap["rare_earth_change_pct"] = res
                elif key == "fng":
                    snap["fear_greed_value"], snap["fear_greed_label"] = res
                elif key == "news":
                    snap.update(analyze_news_and_inflation(res.get("items") or []))
            except Exception:
                continue

    snap.update(compute_macro_decision(snap))

    with _MACRO_CACHE_LOCK:
        _MACRO_CACHE = (now, snap)
    return snap


# -----------------------------------------------------------------------------
# Persistência Contínua de Contexto Macro & Memória de Aprendizado no SQLite
# -----------------------------------------------------------------------------
class MacroLearningStore:
    """
    Salva continuamente no SQLite:
    - `macro_context_snapshots`: contexto macro + notícias de cada candle fechado (30m)
    - `learning_history`: cada ciclo de treinamento/retreino da IA para evolução contínua
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS macro_context_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_timestamp INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    dxy_price REAL DEFAULT 0.0,
                    dxy_change_pct REAL DEFAULT 0.0,
                    us10y_yield REAL DEFAULT 0.0,
                    us10y_change_pct REAL DEFAULT 0.0,
                    sp500_change_pct REAL DEFAULT 0.0,
                    fear_greed_value INTEGER,
                    news_sentiment_score REAL DEFAULT 0.0,
                    inflation_fed_bias REAL DEFAULT 0.0,
                    macro_score REAL DEFAULT 0.0,
                    regime TEXT DEFAULT 'MACRO NEUTRO',
                    UNIQUE(symbol, timeframe, candle_timestamp)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    model_version INTEGER NOT NULL,
                    model_type TEXT NOT NULL,
                    samples INTEGER DEFAULT 0,
                    precision_buy REAL DEFAULT 0.0,
                    profit_factor REAL DEFAULT 0.0,
                    sim_pnl_pct REAL DEFAULT 0.0,
                    optimal_threshold REAL DEFAULT 0.55,
                    macro_score REAL DEFAULT 0.0,
                    promoted INTEGER DEFAULT 1,
                    notes TEXT DEFAULT ''
                );
            """)
            conn.commit()

    def save_macro_snapshot(
        self,
        symbol: str,
        timeframe: str,
        candle_timestamp: int,
        snapshot: Dict[str, Any],
    ) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO macro_context_snapshots (
                    symbol, timeframe, candle_timestamp, recorded_at,
                    dxy_price, dxy_change_pct, us10y_yield, us10y_change_pct,
                    sp500_change_pct, fear_greed_value, news_sentiment_score,
                    inflation_fed_bias, macro_score, regime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, candle_timestamp) DO UPDATE SET
                    dxy_price=excluded.dxy_price,
                    dxy_change_pct=excluded.dxy_change_pct,
                    us10y_yield=excluded.us10y_yield,
                    us10y_change_pct=excluded.us10y_change_pct,
                    sp500_change_pct=excluded.sp500_change_pct,
                    fear_greed_value=excluded.fear_greed_value,
                    news_sentiment_score=excluded.news_sentiment_score,
                    inflation_fed_bias=excluded.inflation_fed_bias,
                    macro_score=excluded.macro_score,
                    regime=excluded.regime
                """,
                (
                    symbol,
                    timeframe,
                    int(candle_timestamp),
                    now_str,
                    float(snapshot.get("dxy_price") or 0.0),
                    float(snapshot.get("dxy_change_pct") or 0.0),
                    float(snapshot.get("us10y_yield") or 0.0),
                    float(snapshot.get("us10y_change_pct") or 0.0),
                    float(snapshot.get("sp500_change_pct") or 0.0),
                    snapshot.get("fear_greed_value"),
                    float(snapshot.get("news_sentiment_score") or 0.0),
                    float(snapshot.get("inflation_fed_bias") or 0.0),
                    float(snapshot.get("macro_score") or 0.0),
                    str(snapshot.get("regime") or "MACRO NEUTRO"),
                ),
            )
            conn.commit()

    def record_learning_cycle(
        self,
        symbol: str,
        timeframe: str,
        model_version: int,
        model_type: str,
        metrics: Dict[str, Any],
        macro_score: float = 0.0,
        promoted: bool = True,
        notes: str = "",
    ) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_history (
                    recorded_at, symbol, timeframe, model_version, model_type,
                    samples, precision_buy, profit_factor, sim_pnl_pct,
                    optimal_threshold, macro_score, promoted, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_str,
                    symbol,
                    timeframe,
                    int(model_version),
                    str(model_type),
                    int(metrics.get("samples", 0)),
                    float(metrics.get("precision_buy", 0.0)),
                    float(metrics.get("profit_factor", 0.0)),
                    float(metrics.get("sim_pnl_pct", 0.0)),
                    float(metrics.get("optimal_threshold", 0.55)),
                    float(macro_score),
                    1 if promoted else 0,
                    str(notes)[:240],
                ),
            )
            conn.commit()

    def get_learning_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_history ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_macro_snapshots(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM macro_context_snapshots ORDER BY candle_timestamp DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(r) for r in rows]


def compute_ai_evolution_progress(
    db_path: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "30m",
    macro_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Calcula o progresso real de evolução e adaptação da IA ao mercado (0% a 100%)
    combinando 4 pilares persistidos no SQLite:
    1. Memória de Mercado (candles salvos, alvo 3.000 candles) — 30 pts
    2. Memória Macro & Geopolítica (snapshots macro/notícias salvos, alvo 100) — 20 pts
    3. Ciclos de Aprendizado & Evolução do Modelo (learning_history, alvo 15 ciclos) — 25 pts
    4. Assertividade Viva (previsões avaliadas e precisão real) — 25 pts
    """
    candles_count = 0
    macro_snaps = 0
    learning_cycles = 0
    promoted_cycles = 0
    eval_total = 0
    eval_acc = 50.0
    model_version = 1

    try:
        store = MacroLearningStore(db_path=db_path)
        with store._connect() as conn:
            r_c = conn.execute(
                "SELECT COUNT(*) AS n FROM ohlcv_candles WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe),
            ).fetchone()
            candles_count = int(r_c["n"] or 0) if r_c else 0

            r_m = conn.execute("SELECT COUNT(*) AS n FROM macro_context_snapshots").fetchone()
            macro_snaps = int(r_m["n"] or 0) if r_m else 0

            r_l = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(promoted), 0) AS p, COALESCE(MAX(model_version), 1) AS mv FROM learning_history"
            ).fetchone()
            if r_l:
                learning_cycles = int(r_l["n"] or 0)
                promoted_cycles = int(r_l["p"] or 0)
                model_version = max(1, int(r_l["mv"] or 1))

            p_rows = conn.execute(
                "SELECT is_correct FROM predictions WHERE symbol = ? AND is_correct IS NOT NULL ORDER BY id DESC LIMIT 40",
                (symbol,),
            ).fetchall()
            if p_rows:
                eval_total = len(p_rows)
                eval_acc = sum(1 for r in p_rows if r["is_correct"] == 1) / eval_total * 100.0
    except Exception:
        pass

    pts_candles = min(30.0, (candles_count / 3000.0) * 30.0)
    pts_macro = min(20.0, (macro_snaps / 100.0) * 20.0)
    pts_cycles = min(25.0, ((learning_cycles + promoted_cycles * 0.5) / 15.0) * 25.0)
    if eval_total >= 5:
        acc_factor = max(0.0, min(1.0, (eval_acc - 40.0) / 25.0))
        sample_factor = min(1.0, eval_total / 30.0)
        pts_acc = acc_factor * sample_factor * 25.0
    else:
        pts_acc = 4.0

    progress_pct = round(max(5.0, min(100.0, pts_candles + pts_macro + pts_cycles + pts_acc)), 1)

    if progress_pct < 25.0:
        level, stage_name, next_milestone = 1, "Coleta & Observação", "Acumular base de candles 30m e snapshots macro"
    elif progress_pct < 50.0:
        level, stage_name, next_milestone = 2, "Aprendizado Base", "Completar novos ciclos de retreino com macro/notícias"
    elif progress_pct < 75.0:
        level, stage_name, next_milestone = 3, "Adaptação Multi-Fator", "Consolidar assertividade acima de 58% em múltiplos regimes"
    elif progress_pct < 90.0:
        level, stage_name, next_milestone = 4, "Alta Assertividade", "Atingir 100+ contextos macro (guerras/clima/commodities)"
    else:
        level, stage_name, next_milestone = 5, "Especialista Adaptativo", "Evolução contínua ativa em regime de alta maturidade"

    macro = macro_snapshot or {}
    macro_score = float(macro.get("macro_score", 0.0))
    war_m = int(macro.get("war_geopolitics_mentions", 0))
    oil_chg = abs(float(macro.get("oil_change_pct", 0.0)))
    if abs(macro_score) >= 45.0 or war_m >= 3 or oil_chg >= 2.5:
        adaptation_status = "🔄 Adaptando-se a Choque Global"
        adaptation_tone = "warning"
    else:
        adaptation_status = "🟢 Sincronizada com o Mercado"
        adaptation_tone = "healthy"

    return {
        "progress_pct": progress_pct,
        "level": level,
        "stage_name": stage_name,
        "next_milestone": next_milestone,
        "adaptation_status": adaptation_status,
        "adaptation_tone": adaptation_tone,
        "pillars": {
            "candles_count": candles_count,
            "candles_target": 3000,
            "macro_snapshots": macro_snaps,
            "macro_target": 100,
            "learning_cycles": learning_cycles,
            "promoted_cycles": promoted_cycles,
            "eval_total": eval_total,
            "eval_accuracy_pct": round(eval_acc, 1),
            "model_version": model_version,
        },
    }
