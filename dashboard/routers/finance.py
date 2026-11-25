"""
Rotas Financeiras & Analytics Consolidado (FyNex Quant AI)
==========================================================
Consolida resultados reais da Mesa XTB (`xtb_trades`, `xtb_positions`) e do
Robô Quant Spot (`simulated_trades`, `open_positions`) com curva de equity,
drawdown, PnL diário/mensal, heatmap de 35 dias, alocação por classe,
ranking por ativo, eficiência de saídas e exportação CSV.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query
from fastapi.responses import Response

from core.exchange.connection import TICKER_CACHE
from core.infra.sqlite import sqlite_readonly_optional, table_exists
from core.infra.utils import parse_utc_str
from core.logging_setup import DEFAULT_DB_PATH
from core.markets.catalog import MARKET_INSTRUMENTS
from core.markets.xtb_desk import DEFAULT_XTB_CAPITAL_USD, XTBDeskStore
from core.markets.yahoo_feed import FOREX_QUOTE_CACHE

router = APIRouter(tags=["finance"])

PERIOD_DAYS: Dict[str, Optional[float]] = {
    "24h": 1.0,
    "7d": 7.0,
    "30d": 30.0,
    "90d": 90.0,
    "all": None,
}

CATEGORY_LABELS: Dict[str, str] = {
    "forex": "Forex (Moedas)",
    "commodities": "Metais & Commodities",
    "indices": "Índices Globais",
    "crypto": "Criptomoedas",
}

REASON_LABELS: Dict[str, str] = {
    "TAKE_PROFIT": "Take Profit",
    "STOP_LOSS": "Stop Loss",
    "BREAKEVEN": "Breakeven",
    "TRAILING_STOP": "Trailing Stop",
    "MANUAL": "Fechamento Manual",
    "SIGNAL": "Sinal da IA",
    "OUTRO": "Outro",
}


def _db_path() -> str:
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, "DB_PATH"):
        return str(srv.DB_PATH)
    return DEFAULT_DB_PATH


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _infer_category(symbol: str, raw_cat: Optional[str] = None) -> str:
    cat = (raw_cat or "").strip().lower()
    if cat in CATEGORY_LABELS:
        return cat
    inst = MARKET_INSTRUMENTS.get((symbol or "").upper())
    if inst and inst.get("category") in CATEGORY_LABELS:
        return str(inst["category"])
    sym_u = (symbol or "").upper()
    if sym_u.endswith("/USDT") or sym_u.endswith("/USDC"):
        return "crypto"
    if sym_u.startswith(("XAU/", "XAG/", "XPT/", "OIL/", "BRENT/", "NATGAS/", "COPPER/", "REMX/")):
        return "commodities"
    if sym_u.startswith(("US500/", "US100/", "DAX/", "SPX/")):
        return "indices"
    return "forex"


def _normalize_reason(raw_reason: Optional[str], pnl_usd: float) -> str:
    r = (raw_reason or "").strip().upper()
    if "TAKE" in r or r == "TP":
        return "TAKE_PROFIT"
    if "BREAKEVEN" in r or r == "BE":
        return "BREAKEVEN"
    if "TRAIL" in r:
        return "TRAILING_STOP"
    if "STOP" in r or r == "SL":
        return "STOP_LOSS"
    if "MANUAL" in r:
        return "MANUAL"
    if "SIGNAL" in r or "AI" in r or "SELL" in r:
        return "SIGNAL"
    if not r:
        return "TAKE_PROFIT" if pnl_usd > 0 else ("STOP_LOSS" if pnl_usd < 0 else "BREAKEVEN")
    return "OUTRO"


def _csv_safe(val: Any) -> str:
    s = str(val if val is not None else "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _load_unified_trades(
    conn: sqlite3.Connection,
    source: str,
) -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []

    if source in ("all", "xtb") and table_exists(conn, "xtb_trades"):
        try:
            rows = conn.execute(
                """
                SELECT id, symbol, category, side, lots, leverage,
                       entry_price, exit_price, margin_usd, pnl_usd, pnl_pct,
                       reason, opened_at, closed_at
                FROM xtb_trades
                ORDER BY closed_at ASC, id ASC
                """
            ).fetchall()
            for r in rows:
                dt = parse_utc_str(r["closed_at"]) or parse_utc_str(r["opened_at"])
                pnl_usd = _safe_float(r["pnl_usd"])
                sym = str(r["symbol"] or "")
                trades.append(
                    {
                        "uid": f"xtb_{r['id']}",
                        "source": "xtb",
                        "source_label": "Mesa XTB",
                        "symbol": sym,
                        "category": _infer_category(sym, r["category"]),
                        "side": str(r["side"] or "BUY").upper(),
                        "size_label": f"{_safe_float(r['lots']):.2f} lotes ({int(_safe_float(r['leverage'], 1))}x)",
                        "notional_margin_usd": _safe_float(r["margin_usd"]),
                        "entry_price": _safe_float(r["entry_price"]),
                        "exit_price": _safe_float(r["exit_price"]),
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(_safe_float(r["pnl_pct"]), 2),
                        "reason": _normalize_reason(r["reason"], pnl_usd),
                        "raw_reason": str(r["reason"] or ""),
                        "opened_at": str(r["opened_at"] or ""),
                        "closed_at": str(r["closed_at"] or ""),
                        "_dt": dt,
                    }
                )
        except sqlite3.OperationalError:
            pass

    if source in ("all", "bot") and table_exists(conn, "simulated_trades"):
        try:
            rows = conn.execute(
                """
                SELECT id, symbol, timeframe, entry_time, exit_time,
                       entry_price, exit_price, reason, pnl_pct, pnl_usd,
                       mode, quantity
                FROM simulated_trades
                ORDER BY exit_time ASC, id ASC
                """
            ).fetchall()
            for r in rows:
                dt = parse_utc_str(r["exit_time"]) or parse_utc_str(r["entry_time"])
                pnl_usd = _safe_float(r["pnl_usd"])
                sym = str(r["symbol"] or "BTC/USDT")
                qty = _safe_float(r["quantity"])
                entry_p = _safe_float(r["entry_price"])
                mode_tag = str(r["mode"] or "paper").upper()
                trades.append(
                    {
                        "uid": f"bot_{r['id']}",
                        "source": "bot",
                        "source_label": f"Robô ({mode_tag})",
                        "symbol": sym,
                        "category": _infer_category(sym, "crypto"),
                        "side": "BUY",
                        "size_label": f"{qty:.5f} un." if qty > 0 else f"${entry_p:,.2f}",
                        "notional_margin_usd": round(qty * entry_p, 2) if qty > 0 and entry_p > 0 else 50.0,
                        "entry_price": entry_p,
                        "exit_price": _safe_float(r["exit_price"]),
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(_safe_float(r["pnl_pct"]), 2),
                        "reason": _normalize_reason(r["reason"], pnl_usd),
                        "raw_reason": str(r["reason"] or ""),
                        "opened_at": str(r["entry_time"] or ""),
                        "closed_at": str(r["exit_time"] or ""),
                        "_dt": dt,
                    }
                )
        except sqlite3.OperationalError:
            pass

    trades.sort(key=lambda t: (t["_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), t["uid"]))
    return trades


def _compute_open_positions_metrics(
    conn: sqlite3.Connection,
    source: str,
) -> Tuple[int, float, float, List[Dict[str, Any]]]:
    """Retorna (count, used_margin_usd, floating_pnl_usd, open_items) usando cotações em cache sem bloquear rede."""
    open_items: List[Dict[str, Any]] = []
    used_margin = 0.0
    floating_pnl = 0.0

    if source in ("all", "xtb") and table_exists(conn, "xtb_positions"):
        try:
            rows = conn.execute(
                """
                SELECT id, symbol, category, side, lots, contract_size, leverage,
                       entry_price, margin_usd, sl_price, tp_price, opened_at
                FROM xtb_positions
                ORDER BY opened_at DESC
                """
            ).fetchall()
            for r in rows:
                sym = str(r["symbol"] or "")
                side = str(r["side"] or "BUY").upper()
                entry = _safe_float(r["entry_price"])
                lots = _safe_float(r["lots"])
                csize = _safe_float(r["contract_size"], 1.0)
                margin = _safe_float(r["margin_usd"])
                used_margin += margin

                cur_price = entry
                q = FOREX_QUOTE_CACHE.peek(sym)
                if isinstance(q, dict):
                    cand = _safe_float(q.get("bid") if side == "BUY" else q.get("ask")) or _safe_float(q.get("price"))
                    if cand > 0:
                        cur_price = cand
                else:
                    tq = TICKER_CACHE.peek(f"ticker:0:{sym}") or TICKER_CACHE.peek(f"ticker:1:{sym}")
                    if isinstance(tq, dict):
                        cand = _safe_float(tq.get("bid") if side == "BUY" else tq.get("ask")) or _safe_float(tq.get("last"))
                        if cand > 0:
                            cur_price = cand

                pnl = XTBDeskStore.calculate_pnl_usd(sym, side, lots, csize, entry, cur_price)
                floating_pnl += pnl
                open_items.append(
                    {
                        "source": "xtb",
                        "symbol": sym,
                        "category": _infer_category(sym, r["category"]),
                        "side": side,
                        "margin_usd": round(margin, 2),
                        "entry_price": entry,
                        "current_price": cur_price,
                        "floating_pnl_usd": round(pnl, 2),
                        "opened_at": str(r["opened_at"] or ""),
                    }
                )
        except sqlite3.OperationalError:
            pass

    if source in ("all", "bot") and table_exists(conn, "open_positions"):
        try:
            rows = conn.execute(
                """
                SELECT symbol, mode, entry_price, quantity, entry_time
                FROM open_positions
                WHERE status = 'OPEN'
                """
            ).fetchall()
            for r in rows:
                sym = str(r["symbol"] or "BTC/USDT")
                entry = _safe_float(r["entry_price"])
                qty = _safe_float(r["quantity"])
                cost = qty * entry
                used_margin += cost
                cur_price = entry
                tq = TICKER_CACHE.peek(f"ticker:0:{sym}") or TICKER_CACHE.peek(f"ticker:1:{sym}")
                if isinstance(tq, dict):
                    cand = _safe_float(tq.get("bid")) or _safe_float(tq.get("last"))
                    if cand > 0:
                        cur_price = cand
                pnl = (cur_price - entry) * qty
                floating_pnl += pnl
                open_items.append(
                    {
                        "source": "bot",
                        "symbol": sym,
                        "category": _infer_category(sym, "crypto"),
                        "side": "BUY",
                        "margin_usd": round(cost, 2),
                        "entry_price": entry,
                        "current_price": cur_price,
                        "floating_pnl_usd": round(pnl, 2),
                        "opened_at": str(r["entry_time"] or ""),
                    }
                )
        except sqlite3.OperationalError:
            pass

    return len(open_items), round(used_margin, 2), round(floating_pnl, 2), open_items


def build_finance_overview(
    db_path: str,
    period: str = "30d",
    source: str = "all",
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    period_key = period if period in PERIOD_DAYS else "30d"
    source_key = source if source in ("all", "xtb", "bot") else "all"

    all_trades: List[Dict[str, Any]] = []
    open_count = 0
    used_margin = 0.0
    floating_pnl = 0.0
    open_items: List[Dict[str, Any]] = []

    with sqlite_readonly_optional(db_path) as conn:
        if conn is not None:
            all_trades = _load_unified_trades(conn, source_key)
            open_count, used_margin, floating_pnl, open_items = _compute_open_positions_metrics(conn, source_key)

    days_window = PERIOD_DAYS[period_key]
    cutoff = (now - timedelta(days=days_window)) if days_window is not None else None

    base_capital = DEFAULT_XTB_CAPITAL_USD if source_key in ("all", "xtb") else 1000.0
    prior_pnl = 0.0
    period_trades: List[Dict[str, Any]] = []
    for t in all_trades:
        dt = t["_dt"]
        if cutoff is not None and dt is not None and dt < cutoff:
            prior_pnl += t["pnl_usd"]
        else:
            period_trades.append(t)

    period_start_capital = round(base_capital + prior_pnl, 2)
    realized_total_all_time = round(sum(t["pnl_usd"] for t in all_trades), 2)
    realized_period = round(sum(t["pnl_usd"] for t in period_trades), 2)
    net_period_pnl = round(realized_period + floating_pnl, 2)
    balance_usd = round(base_capital + realized_total_all_time, 2)
    equity_usd = round(balance_usd + floating_pnl, 2)
    free_margin_usd = round(max(0.0, equity_usd - used_margin), 2)
    margin_level_pct = round((equity_usd / used_margin) * 100.0, 1) if used_margin > 0 else None

    # Estatísticas quantitativas do período
    total_trades = len(period_trades)
    wins = sum(1 for t in period_trades if t["pnl_usd"] > 0)
    losses = sum(1 for t in period_trades if t["pnl_usd"] < 0)
    breakevens = total_trades - wins - losses
    win_rate_pct = round((wins / total_trades) * 100.0, 1) if total_trades > 0 else 0.0

    gross_profit = round(sum(t["pnl_usd"] for t in period_trades if t["pnl_usd"] > 0), 2)
    gross_loss = round(abs(sum(t["pnl_usd"] for t in period_trades if t["pnl_usd"] < 0)), 2)
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        profit_factor = 9.99
    else:
        profit_factor = 0.0

    avg_win = round(gross_profit / wins, 2) if wins > 0 else 0.0
    avg_loss = round(gross_loss / losses, 2) if losses > 0 else 0.0
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else (9.99 if avg_win > 0 else 0.0)
    expectancy_usd = round(realized_period / total_trades, 2) if total_trades > 0 else 0.0

    best_trade = round(max((t["pnl_usd"] for t in period_trades), default=0.0), 2)
    worst_trade = round(min((t["pnl_usd"] for t in period_trades), default=0.0), 2)

    # Sequência atual (positiva = vitórias seguidas, negativa = perdas seguidas)
    current_streak = 0
    for t in reversed(period_trades):
        p = t["pnl_usd"]
        if p == 0:
            continue
        if current_streak == 0:
            current_streak = 1 if p > 0 else -1
        elif (current_streak > 0 and p > 0) or (current_streak < 0 and p < 0):
            current_streak += 1 if p > 0 else -1
        else:
            break

    # Curva de Equity + Drawdown ponto a ponto
    equity_curve: List[Dict[str, Any]] = []
    running_equity = period_start_capital
    peak_equity = max(running_equity, 1.0)
    max_dd_usd = 0.0
    max_dd_pct = 0.0

    start_label = (
        (period_trades[0]["closed_at"][:10] if period_trades and period_trades[0]["closed_at"] else now.strftime("%Y-%m-%d"))
    )
    equity_curve.append(
        {
            "label": "Início",
            "date": start_label,
            "equity": round(running_equity, 2),
            "cumulative_pnl": 0.0,
            "drawdown_pct": 0.0,
            "trade_pnl": 0.0,
            "symbol": "Capital Base",
            "source": source_key,
        }
    )

    trade_returns: List[float] = []
    cum_pnl = 0.0
    for idx, t in enumerate(period_trades, start=1):
        prev_eq = max(running_equity, 1.0)
        pnl = t["pnl_usd"]
        trade_returns.append(pnl / prev_eq)
        running_equity += pnl
        cum_pnl += pnl
        if running_equity > peak_equity:
            peak_equity = running_equity
        dd_usd = max(0.0, peak_equity - running_equity)
        dd_pct = (dd_usd / peak_equity) * 100.0 if peak_equity > 0 else 0.0
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        dt = t["_dt"]
        lbl = dt.strftime("%d/%m %H:%M") if dt else f"#{idx}"
        equity_curve.append(
            {
                "label": lbl,
                "date": dt.strftime("%Y-%m-%d") if dt else "",
                "equity": round(running_equity, 2),
                "cumulative_pnl": round(cum_pnl, 2),
                "drawdown_pct": round(-dd_pct, 2),
                "trade_pnl": round(pnl, 2),
                "symbol": t["symbol"],
                "source": t["source_label"],
            }
        )

    # Ponto vivo atual (incluindo PnL flutuante)
    if open_count > 0 or len(equity_curve) == 1:
        live_eq = round(running_equity + floating_pnl, 2)
        if live_eq > peak_equity:
            peak_equity = live_eq
        live_dd_pct = ((peak_equity - live_eq) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
        equity_curve.append(
            {
                "label": "Agora (Vivo)",
                "date": now.strftime("%Y-%m-%d"),
                "equity": live_eq,
                "cumulative_pnl": round(cum_pnl + floating_pnl, 2),
                "drawdown_pct": round(-max(0.0, live_dd_pct), 2),
                "trade_pnl": round(floating_pnl, 2),
                "symbol": f"{open_count} aberta(s)" if open_count else "Sem posição aberta",
                "source": "Ao Vivo",
            }
        )

    # Sharpe aproximado sobre retornos por trade
    sharpe_ratio = 0.0
    if len(trade_returns) >= 2:
        mean_r = sum(trade_returns) / len(trade_returns)
        var_r = sum((r - mean_r) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
        std_r = math.sqrt(max(0.0, var_r))
        if std_r > 1e-9:
            sharpe_ratio = round((mean_r / std_r) * math.sqrt(min(len(trade_returns), 252)), 2)

    roi_pct = round((net_period_pnl / period_start_capital) * 100.0, 2) if period_start_capital > 0 else 0.0

    # Agregação diária + calendário heatmap últimos 35 dias
    daily_map: Dict[str, Dict[str, Any]] = {}
    monthly_map: Dict[str, Dict[str, Any]] = {}
    asset_map: Dict[str, Dict[str, Any]] = {}
    cat_map: Dict[str, Dict[str, Any]] = {}
    reason_map: Dict[str, Dict[str, Any]] = {}

    for t in period_trades:
        dt = t["_dt"]
        day_key = dt.strftime("%Y-%m-%d") if dt else now.strftime("%Y-%m-%d")
        mon_key = day_key[:7]
        pnl = t["pnl_usd"]
        is_win = 1 if pnl > 0 else 0
        is_loss = 1 if pnl < 0 else 0

        d_entry = daily_map.setdefault(day_key, {"date": day_key, "pnl_usd": 0.0, "trades": 0, "wins": 0, "losses": 0})
        d_entry["pnl_usd"] = round(d_entry["pnl_usd"] + pnl, 2)
        d_entry["trades"] += 1
        d_entry["wins"] += is_win
        d_entry["losses"] += is_loss

        m_entry = monthly_map.setdefault(mon_key, {"month": mon_key, "pnl_usd": 0.0, "trades": 0, "wins": 0})
        m_entry["pnl_usd"] = round(m_entry["pnl_usd"] + pnl, 2)
        m_entry["trades"] += 1
        m_entry["wins"] += is_win

        sym = t["symbol"]
        a_entry = asset_map.setdefault(
            sym,
            {
                "symbol": sym,
                "category": t["category"],
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl_usd": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        a_entry["trades"] += 1
        a_entry["wins"] += is_win
        a_entry["losses"] += is_loss
        a_entry["pnl_usd"] = round(a_entry["pnl_usd"] + pnl, 2)
        if pnl > 0:
            a_entry["gross_profit"] = round(a_entry["gross_profit"] + pnl, 2)
        elif pnl < 0:
            a_entry["gross_loss"] = round(a_entry["gross_loss"] + abs(pnl), 2)

        cat = t["category"]
        c_entry = cat_map.setdefault(
            cat,
            {
                "category": cat,
                "label": CATEGORY_LABELS.get(cat, cat.title()),
                "trades": 0,
                "pnl_usd": 0.0,
                "volume_margin_usd": 0.0,
            },
        )
        c_entry["trades"] += 1
        c_entry["pnl_usd"] = round(c_entry["pnl_usd"] + pnl, 2)
        c_entry["volume_margin_usd"] = round(c_entry["volume_margin_usd"] + max(1.0, t["notional_margin_usd"]), 2)

        rk = t["reason"]
        r_entry = reason_map.setdefault(
            rk,
            {
                "reason": rk,
                "label": REASON_LABELS.get(rk, rk.title()),
                "count": 0,
                "pnl_usd": 0.0,
            },
        )
        r_entry["count"] += 1
        r_entry["pnl_usd"] = round(r_entry["pnl_usd"] + pnl, 2)

    # Inclui posições abertas na alocação por categoria se não houver trades encerrados ainda
    if not cat_map and open_items:
        for op in open_items:
            cat = op["category"]
            c_entry = cat_map.setdefault(
                cat,
                {
                    "category": cat,
                    "label": CATEGORY_LABELS.get(cat, cat.title()),
                    "trades": 0,
                    "pnl_usd": 0.0,
                    "volume_margin_usd": 0.0,
                },
            )
            c_entry["trades"] += 1
            c_entry["pnl_usd"] = round(c_entry["pnl_usd"] + op["floating_pnl_usd"], 2)
            c_entry["volume_margin_usd"] = round(c_entry["volume_margin_usd"] + max(1.0, op["margin_usd"]), 2)

    daily_pnl = sorted(daily_map.values(), key=lambda x: x["date"])

    # Calendário Heatmap fixo de 35 dias (inclui dias sem operação)
    all_daily_for_cal: Dict[str, Dict[str, Any]] = {}
    for t in all_trades:
        dt = t["_dt"]
        if not dt:
            continue
        dk = dt.strftime("%Y-%m-%d")
        ce = all_daily_for_cal.setdefault(dk, {"pnl_usd": 0.0, "trades": 0, "wins": 0})
        ce["pnl_usd"] = round(ce["pnl_usd"] + t["pnl_usd"], 2)
        ce["trades"] += 1
        if t["pnl_usd"] > 0:
            ce["wins"] += 1

    calendar_35d: List[Dict[str, Any]] = []
    today_utc = now.date()
    for offset in range(34, -1, -1):
        d = today_utc - timedelta(days=offset)
        dk = d.strftime("%Y-%m-%d")
        info = all_daily_for_cal.get(dk, {"pnl_usd": 0.0, "trades": 0, "wins": 0})
        calendar_35d.append(
            {
                "date": dk,
                "day_label": d.strftime("%d/%m"),
                "weekday": d.weekday(),
                "pnl_usd": info["pnl_usd"],
                "trades": info["trades"],
                "wins": info["wins"],
            }
        )

    monthly_pnl = []
    for m in sorted(monthly_map.values(), key=lambda x: x["month"]):
        wr = round((m["wins"] / m["trades"]) * 100.0, 1) if m["trades"] > 0 else 0.0
        monthly_pnl.append({**m, "win_rate_pct": wr})

    asset_breakdown = []
    for a in asset_map.values():
        wr = round((a["wins"] / a["trades"]) * 100.0, 1) if a["trades"] > 0 else 0.0
        avg_p = round(a["pnl_usd"] / a["trades"], 2) if a["trades"] > 0 else 0.0
        asset_breakdown.append({**a, "win_rate_pct": wr, "avg_pnl_usd": avg_p})
    asset_breakdown.sort(key=lambda x: (-x["pnl_usd"], -x["trades"]))

    total_vol = sum(c["volume_margin_usd"] for c in cat_map.values()) or 1.0
    category_allocation = []
    for c in sorted(cat_map.values(), key=lambda x: -x["volume_margin_usd"]):
        category_allocation.append({**c, "share_pct": round((c["volume_margin_usd"] / total_vol) * 100.0, 1)})

    total_reasons = sum(r["count"] for r in reason_map.values()) or 1
    exit_reasons = []
    for r in sorted(reason_map.values(), key=lambda x: -x["count"]):
        exit_reasons.append({**r, "share_pct": round((r["count"] / total_reasons) * 100.0, 1)})

    # Extrato reverso (mais recentes primeiro, sem chave interna _dt)
    ledger = [
        {k: v for k, v in t.items() if k != "_dt"}
        for t in reversed(period_trades[-250:])
    ]

    return {
        "summary": {
            "period": period_key,
            "source": source_key,
            "starting_capital_usd": period_start_capital,
            "balance_usd": balance_usd,
            "equity_usd": equity_usd,
            "realized_pnl_usd": realized_period,
            "realized_all_time_usd": realized_total_all_time,
            "floating_pnl_usd": floating_pnl,
            "net_pnl_usd": net_period_pnl,
            "roi_pct": roi_pct,
            "used_margin_usd": used_margin,
            "free_margin_usd": free_margin_usd,
            "margin_level_pct": margin_level_pct,
            "open_positions_count": open_count,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "win_rate_pct": win_rate_pct,
            "gross_profit_usd": gross_profit,
            "gross_loss_usd": gross_loss,
            "profit_factor": profit_factor,
            "avg_win_usd": avg_win,
            "avg_loss_usd": avg_loss,
            "payoff_ratio": payoff_ratio,
            "expectancy_usd": expectancy_usd,
            "max_drawdown_usd": round(max_dd_usd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe_ratio": sharpe_ratio,
            "best_trade_usd": best_trade,
            "worst_trade_usd": worst_trade,
            "current_streak": current_streak,
        },
        "equity_curve": equity_curve,
        "daily_pnl": daily_pnl,
        "monthly_pnl": monthly_pnl,
        "calendar_35d": calendar_35d,
        "asset_breakdown": asset_breakdown,
        "category_allocation": category_allocation,
        "exit_reasons": exit_reasons,
        "open_positions": open_items,
        "ledger": ledger,
    }


@router.get("/api/finance/overview")
def get_finance_overview(
    period: str = Query(default="30d", pattern="^(24h|7d|30d|90d|all)$"),
    source: str = Query(default="all", pattern="^(all|xtb|bot)$"),
):
    return build_finance_overview(db_path=_db_path(), period=period, source=source)


@router.get("/api/finance/export.csv")
def export_finance_csv(
    period: str = Query(default="all", pattern="^(24h|7d|30d|90d|all)$"),
    source: str = Query(default="all", pattern="^(all|xtb|bot)$"),
):
    data = build_finance_overview(db_path=_db_path(), period=period, source=source)
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM para Excel em PT-BR
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        [
            "Fechamento_UTC",
            "Origem",
            "Ativo",
            "Classe",
            "Lado",
            "Volume",
            "Entrada",
            "Saida",
            "Motivo",
            "PnL_USD",
            "ROI_Pct",
        ]
    )
    for row in data["ledger"]:
        writer.writerow(
            [
                _csv_safe(row.get("closed_at", "")),
                _csv_safe(row.get("source_label", "")),
                _csv_safe(row.get("symbol", "")),
                _csv_safe(CATEGORY_LABELS.get(row.get("category", ""), row.get("category", ""))),
                _csv_safe(row.get("side", "")),
                _csv_safe(row.get("size_label", "")),
                f"{_safe_float(row.get('entry_price')):.6f}",
                f"{_safe_float(row.get('exit_price')):.6f}",
                _csv_safe(row.get("reason", "")),
                f"{_safe_float(row.get('pnl_usd')):.2f}",
                f"{_safe_float(row.get('pnl_pct')):.2f}",
            ]
        )
    filename = f"fynex_extrato_{source}_{period}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
