"""
Motor da Mesa de Operações XTB (Simulador Multi-Ativos — FyNex Quant AI)
========================================================================
Gerencia posições abertas e histórico de operações manuais estilo XTB
(BUY/SELL, lotes, alavancagem, margem, SL/TP e PnL flutuante em USD).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import alerts
from core.infra.sqlite import SCHEMA_REGISTRY
from core.infra.utils import finite_float, utc_now_str
from core.markets.catalog import MARKET_INSTRUMENTS
from core.markets.yahoo_feed import FOREX_QUOTE_CACHE, fetch_any_quote

DEFAULT_XTB_CAPITAL_USD = 10_000.0


def _is_finite_pos(val: Any) -> bool:
    f = finite_float(val)
    return f is not None and f > 0.0


def _resolve_fetch_any_quote():
    fm = sys.modules.get("core.forex_market")
    if fm is not None and hasattr(fm, "fetch_any_quote"):
        return fm.fetch_any_quote
    return fetch_any_quote


class XTBDeskStore:
    """Gerencia posições abertas e histórico de operações manuais estilo corretora XTB."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        SCHEMA_REGISTRY.ensure_initialized("xtb_desk", self.db_path, self._init_tables)

    @contextmanager
    def _connect(self, *, write_immediate: bool = False) -> Iterator[sqlite3.Connection]:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None if write_immediate else "")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000;")
        try:
            if write_immediate:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            else:
                with conn:
                    yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xtb_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots REAL NOT NULL,
                    contract_size REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    margin_usd REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    opened_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xtb_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    margin_usd REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    reason TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _cross_usd_rate(currency: str, fallback: float = 1.0) -> float:
        cur = currency.upper()
        if cur in ("USD", "USDT"):
            return 1.0
        hit, direct = FOREX_QUOTE_CACHE.peek(f"{cur}/USD")
        if hit and direct and direct.get("price", 0.0) > 0:
            return float(direct["price"])
        hit_inv, inv = FOREX_QUOTE_CACHE.peek(f"USD/{cur}")
        if hit_inv and inv and inv.get("price", 0.0) > 0:
            return 1.0 / float(inv["price"])
        return fallback

    @staticmethod
    def calculate_notional_usd(symbol: str, lots: float, price: float, contract_size: float) -> float:
        if not (_is_finite_pos(lots) and _is_finite_pos(price) and _is_finite_pos(contract_size)):
            return 0.0
        sym = symbol.upper()
        base, _, quote = sym.partition("/")
        units = float(lots) * float(contract_size)
        if quote in ("USD", "USDT"):
            return units * float(price)
        if base == "USD":
            return units
        base_usd = XTBDeskStore._cross_usd_rate(base, fallback=1.08 if base == "EUR" else 1.0)
        return units * base_usd

    @staticmethod
    def calculate_pnl_usd(
        symbol: str,
        side: str,
        lots: float,
        contract_size: float,
        entry_price: float,
        current_price: float,
    ) -> float:
        if not (_is_finite_pos(lots) and _is_finite_pos(contract_size) and _is_finite_pos(entry_price) and _is_finite_pos(current_price)):
            return 0.0
        sym = symbol.upper()
        base, _, quote = sym.partition("/")
        units = float(lots) * float(contract_size)
        diff = (float(current_price) - float(entry_price)) if side.upper() == "BUY" else (float(entry_price) - float(current_price))
        if quote in ("USD", "USDT"):
            return diff * units
        if base == "USD":
            return (diff * units) / float(current_price)
        quote_usd = XTBDeskStore._cross_usd_rate(quote, fallback=0.0)
        if quote_usd > 0:
            return (diff * units) * quote_usd
        base_usd = XTBDeskStore._cross_usd_rate(base, fallback=1.08 if base == "EUR" else 1.0)
        return (diff * units) / max(1e-6, float(current_price) / base_usd)

    def _realized_pnl_usd(self, conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0.0) AS total FROM xtb_trades").fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def _used_margin_usd(self, conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT COALESCE(SUM(margin_usd), 0.0) AS total FROM xtb_positions").fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def _cached_floating_pnl_usd(self, conn: sqlite3.Connection) -> float:
        from core.exchange.connection import TICKER_CACHE

        rows = conn.execute("SELECT symbol, category, side, lots, contract_size, entry_price FROM xtb_positions").fetchall()
        total = 0.0
        for r in rows:
            if r["category"] == "crypto":
                hit, q = TICKER_CACHE.peek((r["symbol"], False))
            else:
                hit, q = FOREX_QUOTE_CACHE.peek(r["symbol"])
            if not hit or not q:
                continue
            mark = float((q.get("bid") if r["side"] == "BUY" else q.get("ask")) or q.get("price") or 0.0)
            if mark > 0:
                total += self.calculate_pnl_usd(
                    r["symbol"], r["side"], r["lots"], r["contract_size"], r["entry_price"], mark
                )
        return total

    def open_position(
        self,
        symbol: str,
        side: str,
        lots: float,
        leverage: int = 30,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        quote_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sym = symbol.upper()
        inst = MARKET_INSTRUMENTS.get(sym)
        if not inst:
            raise ValueError(f"Instrumento '{symbol}' não suportado na mesa XTB.")

        side_u = (side or "").upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError("Lado da ordem inválido: use 'BUY' ou 'SELL'.")

        if not _is_finite_pos(lots):
            raise ValueError(f"Lote inválido: informe um número finito maior que zero (mínimo {inst['min_lot']}).")
        lots_f = float(lots)
        if lots_f < float(inst["min_lot"]) or lots_f > 1000.0:
            raise ValueError(f"Lote fora do intervalo permitido (mínimo {inst['min_lot']}, máximo 1000).")

        lev = max(1, min(int(leverage), int(inst["max_leverage"])))
        quote = quote_override if quote_override is not None else _resolve_fetch_any_quote()(sym)
        entry_price = float((quote.get("ask") if side_u == "BUY" else quote.get("bid")) or 0.0)
        if not _is_finite_pos(entry_price):
            entry_price = float(quote.get("price") or 0.0)
        if not _is_finite_pos(entry_price):
            raise ValueError(f"Cotação indisponível no momento para {sym}.")

        if sl_price is not None:
            if not _is_finite_pos(sl_price):
                if sl_price == 0:
                    sl_price = None
                else:
                    raise ValueError("Stop Loss inválido (deve ser um número finito positivo).")
            else:
                sl_price = float(sl_price)
                if side_u == "BUY" and sl_price >= entry_price:
                    raise ValueError("Stop Loss em ordem BUY deve ficar abaixo do preço de entrada.")
                if side_u == "SELL" and sl_price <= entry_price:
                    raise ValueError("Stop Loss em ordem SELL deve ficar acima do preço de entrada.")

        if tp_price is not None:
            if not _is_finite_pos(tp_price):
                if tp_price == 0:
                    tp_price = None
                else:
                    raise ValueError("Take Profit inválido (deve ser um número finito positivo).")
            else:
                tp_price = float(tp_price)
                if side_u == "BUY" and tp_price <= entry_price:
                    raise ValueError("Take Profit em ordem BUY deve ficar acima do preço de entrada.")
                if side_u == "SELL" and tp_price >= entry_price:
                    raise ValueError("Take Profit em ordem SELL deve ficar abaixo do preço de entrada.")

        contract_size = float(inst["contract_size"])
        notional = self.calculate_notional_usd(sym, lots_f, entry_price, contract_size)
        margin_usd = max(0.01, round(notional / lev, 2))

        with self._connect(write_immediate=True) as conn:
            balance = DEFAULT_XTB_CAPITAL_USD + self._realized_pnl_usd(conn)
            floating = min(0.0, self._cached_floating_pnl_usd(conn))
            free_margin = (balance + floating) - self._used_margin_usd(conn)
            if margin_usd > free_margin:
                raise ValueError(
                    f"Margem livre insuficiente: necessária ${margin_usd:,.2f}, disponível ${max(0.0, free_margin):,.2f}."
                )
            opened_at = utc_now_str()
            cur = conn.execute(
                """
                INSERT INTO xtb_positions
                    (symbol, category, side, lots, contract_size, leverage, entry_price, margin_usd, sl_price, tp_price, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sym, inst["category"], side_u, lots_f, contract_size, lev, entry_price, margin_usd, sl_price, tp_price, opened_at),
            )
            pos_id = cur.lastrowid

        return {
            "id": pos_id,
            "symbol": sym,
            "category": inst["category"],
            "side": side_u,
            "lots": lots_f,
            "leverage": lev,
            "entry_price": entry_price,
            "margin_usd": margin_usd,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "opened_at": opened_at,
        }

    def close_position(
        self,
        position_id: int,
        reason: str = "MANUAL",
        quote_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM xtb_positions WHERE id = ?", (int(position_id),)).fetchone()
            if not row:
                raise ValueError(f"Posição #{position_id} não encontrada.")
            pos = dict(row)

        quote = quote_override if quote_override is not None else _resolve_fetch_any_quote()(pos["symbol"])
        exit_price = float((quote.get("bid") if pos["side"] == "BUY" else quote.get("ask")) or 0.0)
        if not _is_finite_pos(exit_price):
            exit_price = float(quote.get("price") or 0.0)
        if not _is_finite_pos(exit_price):
            raise ValueError(f"Cotação indisponível para encerrar posição #{position_id} ({pos['symbol']}).")

        pnl_usd = round(
            self.calculate_pnl_usd(
                pos["symbol"], pos["side"], pos["lots"], pos["contract_size"], pos["entry_price"], exit_price
            ),
            2,
        )
        pnl_pct = round((pnl_usd / pos["margin_usd"] * 100.0), 2) if pos["margin_usd"] > 0 else 0.0
        closed_at = utc_now_str()

        with self._connect(write_immediate=True) as conn:
            del_cur = conn.execute("DELETE FROM xtb_positions WHERE id = ?", (pos["id"],))
            if del_cur.rowcount != 1:
                raise ValueError(f"Posição #{position_id} já foi fechada por outra operação.")
            ins_cur = conn.execute(
                """
                INSERT INTO xtb_trades
                    (symbol, category, side, lots, leverage, entry_price, exit_price, margin_usd, pnl_usd, pnl_pct, reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos["symbol"], pos["category"], pos["side"], pos["lots"], pos["leverage"],
                    pos["entry_price"], exit_price, pos["margin_usd"], pnl_usd, pnl_pct,
                    reason, pos["opened_at"], closed_at,
                ),
            )
            trade_id = ins_cur.lastrowid

        icon = "✅" if pnl_usd > 0 else ("❌" if pnl_usd < 0 else "⚪")
        verdict = "DEU RESULTADO (LUCRO)" if pnl_usd > 0 else ("PREJUÍZO" if pnl_usd < 0 else "EMPATE")
        if pnl_usd < 0:
            try:
                from core.error_learning import ErrorLearningStore
                sl_dist_pct = (
                    abs(float(pos["entry_price"]) - float(exit_price)) / float(pos["entry_price"])
                    if float(pos.get("entry_price") or 0) > 0
                    else 0.0
                )
                ErrorLearningStore(self.db_path).record_error_postmortem(
                    source="XTB_TRADE_LOSS",
                    symbol=pos["symbol"],
                    timeframe="1h",
                    params={
                        "side": pos["side"],
                        "category": pos["category"],
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_price,
                        "sl_pct": round(sl_dist_pct, 5),
                    },
                    outcome_reason=reason,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                )
            except Exception:
                pass
        alerts.send_alert(
            (
                f"{icon} [{reason} • Forex/XTB] {pos['symbol']} ({pos['side']}) — {verdict}\n"
                f"Entrada: {pos['entry_price']} | Saída: {exit_price}\n"
                f"PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}% sobre margem) | Lotes: {pos['lots']}"
            ),
            level="TRADE_EXIT",
        )

        return {
            "closed_id": pos["id"],
            "trade_id": trade_id,
            "symbol": pos["symbol"],
            "side": pos["side"],
            "lots": pos["lots"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "closed_at": closed_at,
        }

    def get_account_snapshot(self, live_prices: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Retorna resumo completo da conta XTB: saldo, equity, margem, posições e histórico."""
        with self._connect() as conn:
            pos_rows = [dict(r) for r in conn.execute("SELECT * FROM xtb_positions ORDER BY id DESC").fetchall()]
            trade_rows = [dict(r) for r in conn.execute("SELECT * FROM xtb_trades ORDER BY id DESC LIMIT 30").fetchall()]
            realized_pnl = self._realized_pnl_usd(conn)

        quote_fn = _resolve_fetch_any_quote()
        quotes_map: Dict[str, Dict[str, Any]] = dict(live_prices or {})
        unique_syms = {p["symbol"] for p in pos_rows if p["symbol"] not in quotes_map}
        for sym in unique_syms:
            try:
                quotes_map[sym] = quote_fn(sym)
            except Exception:
                quotes_map[sym] = {"price": 0.0, "bid": 0.0, "ask": 0.0}

        enriched_positions: List[Dict[str, Any]] = []
        floating_pnl_total = 0.0
        used_margin = 0.0

        for pos in pos_rows:
            q = quotes_map.get(pos["symbol"]) or {}
            mark = float((q.get("bid") if pos["side"] == "BUY" else q.get("ask")) or 0.0)
            if mark <= 0:
                mark = float(q.get("price") or pos["entry_price"])

            sl = pos.get("sl_price")
            tp = pos.get("tp_price")

            # Proteção automática de lucro (Breakeven em >= 60% do caminho até o TP)
            if sl is not None and tp is not None and pos["entry_price"] > 0:
                entry = float(pos["entry_price"])
                inst_dec = int(MARKET_INSTRUMENTS.get(pos["symbol"], {}).get("decimals", 5))
                if pos["side"] == "BUY" and tp > entry:
                    prog = (mark - entry) / (tp - entry)
                    be_sl = round(entry * 1.0005, inst_dec)
                    if prog >= 0.60 and be_sl > float(sl) and be_sl < mark:
                        try:
                            with self._connect(write_immediate=True) as conn:
                                conn.execute("UPDATE xtb_positions SET sl_price = ? WHERE id = ?", (be_sl, pos["id"]))
                            sl = be_sl
                            pos["sl_price"] = be_sl
                        except Exception:
                            pass
                elif pos["side"] == "SELL" and tp < entry:
                    prog = (entry - mark) / (entry - tp)
                    be_sl = round(entry * 0.9995, inst_dec)
                    if prog >= 0.60 and be_sl < float(sl) and be_sl > mark:
                        try:
                            with self._connect(write_immediate=True) as conn:
                                conn.execute("UPDATE xtb_positions SET sl_price = ? WHERE id = ?", (be_sl, pos["id"]))
                            sl = be_sl
                            pos["sl_price"] = be_sl
                        except Exception:
                            pass

            auto_reason = None
            if sl is not None:
                if (pos["side"] == "BUY" and mark <= sl) or (pos["side"] == "SELL" and mark >= sl):
                    sl_is_profit_locked = (
                        (pos["side"] == "BUY" and float(sl) > float(pos["entry_price"]))
                        or (pos["side"] == "SELL" and float(sl) < float(pos["entry_price"]))
                    )
                    auto_reason = "BREAKEVEN" if sl_is_profit_locked else "STOP_LOSS"
            if auto_reason is None and tp is not None:
                if (pos["side"] == "BUY" and mark >= tp) or (pos["side"] == "SELL" and mark <= tp):
                    auto_reason = "TAKE_PROFIT"

            if auto_reason is not None:
                try:
                    closed = self.close_position(pos["id"], reason=auto_reason, quote_override=q)
                    realized_pnl += closed["pnl_usd"]
                    trade_rows.insert(0, {
                        "id": closed.get("trade_id") or closed["closed_id"],
                        "symbol": pos["symbol"],
                        "category": pos["category"],
                        "side": pos["side"],
                        "lots": pos["lots"],
                        "leverage": pos["leverage"],
                        "entry_price": pos["entry_price"],
                        "exit_price": closed["exit_price"],
                        "margin_usd": pos["margin_usd"],
                        "pnl_usd": closed["pnl_usd"],
                        "pnl_pct": closed["pnl_pct"],
                        "reason": auto_reason,
                        "opened_at": pos["opened_at"],
                        "closed_at": closed["closed_at"],
                    })
                except ValueError:
                    # Outra thread encerrou primeiro: ressincroniza saldo realizado e histórico
                    with self._connect() as conn:
                        realized_pnl = self._realized_pnl_usd(conn)
                        trade_rows = [
                            dict(r) for r in conn.execute("SELECT * FROM xtb_trades ORDER BY id DESC LIMIT 30").fetchall()
                        ]
                continue

            pnl_usd = round(
                self.calculate_pnl_usd(
                    pos["symbol"], pos["side"], pos["lots"], pos["contract_size"], pos["entry_price"], mark
                ),
                2,
            )
            pnl_pct = round((pnl_usd / pos["margin_usd"] * 100.0), 2) if pos["margin_usd"] > 0 else 0.0
            floating_pnl_total += pnl_usd
            used_margin += float(pos["margin_usd"])

            inst = MARKET_INSTRUMENTS.get(pos["symbol"], {})
            pos["current_price"] = round(mark, inst.get("decimals", 4))
            pos["pnl_usd"] = pnl_usd
            pos["pnl_pct"] = pnl_pct
            pos["decimals"] = inst.get("decimals", 4)
            enriched_positions.append(pos)

        balance = round(DEFAULT_XTB_CAPITAL_USD + realized_pnl, 2)
        equity = round(balance + floating_pnl_total, 2)
        free_margin = round(equity - used_margin, 2)
        margin_level_pct = round((equity / used_margin * 100.0), 1) if used_margin > 0 else None

        return {
            "balance_usd": balance,
            "equity_usd": equity,
            "used_margin_usd": round(used_margin, 2),
            "free_margin_usd": free_margin,
            "margin_level_pct": margin_level_pct,
            "floating_pnl_usd": round(floating_pnl_total, 2),
            "realized_pnl_usd": round(realized_pnl, 2),
            "open_positions": enriched_positions,
            "recent_trades": trade_rows[:30],
        }
