"""
Copiloto XTB (Modo Híbrido — FyNex Quant AI)
============================================
Como a XTB encerrou oficialmente sua API pública (xAPI) em março de 2025,
o Copiloto XTB registra cada sinal de alta convicção gerado pela IA para
Forex, Ouro, Prata, Petróleo, Cobre e Índices Globais já no formato exato
da plataforma xStation 5 (ticker XTB, direção, entrada, Stop Loss, Take Profit
e lote sugerido), permitindo:
  - Acompanhamento e alertas sonoros no painel local
  - Botão de 1 clique para copiar os parâmetros ou marcar como "Executei na XTB"
  - Envio opcional de alertas instantâneos no Telegram do usuário
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("QuantTradingBot")

# Mapeamento de símbolos internos -> tickers oficiais da XTB (xStation 5)
XTB_SYMBOL_MAP: Dict[str, str] = {
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "USD/BRL": "USDBRL",
    "AUD/USD": "AUDUSD",
    "USD/CHF": "USDCHF",
    "XAU/USD": "GOLD",
    "XAG/USD": "SILVER",
    "XPT/USD": "PLATINUM",
    "COPPER/USD": "COPPER",
    "OIL/USD": "OIL.WTI",
    "BRENT/USD": "OIL",
    "NATGAS/USD": "NATGAS",
    "REMX/USD": "REMX.US",
    "US500/USD": "US500",
    "US100/USD": "US100",
}

VALID_SIGNAL_STATUSES = ("NEW", "EXECUTED_XTB", "DISMISSED")


def to_xtb_symbol(symbol: str) -> str:
    """Converte símbolo interno (ex: XAU/USD) para o ticker usado na xStation 5 (ex: GOLD)."""
    key = (symbol or "").upper().strip()
    return XTB_SYMBOL_MAP.get(key, key.replace("/", ""))


def get_telegram_config() -> Dict[str, Any]:
    """Lê configuração opcional do Telegram a partir das variáveis de ambiente."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    configured = bool(token and chat_id)
    masked = ""
    if token:
        masked = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "****"
    return {
        "configured": configured,
        "bot_token_masked": masked,
        "chat_id": chat_id,
    }


def save_telegram_config(bot_token: str, chat_id: str, env_path: Optional[str] = None) -> Dict[str, Any]:
    """Salva TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no arquivo .env e atualiza os.environ."""
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()

    if env_path is None:
        env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))

    lines: List[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updates: Dict[str, str] = {}
    if token and "..." not in token and not token.startswith("*"):
        updates["TELEGRAM_BOT_TOKEN"] = token
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    if cid:
        updates["TELEGRAM_CHAT_ID"] = cid
        os.environ["TELEGRAM_CHAT_ID"] = cid

    seen = set()
    new_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        for k, v in updates.items():
            if stripped.startswith(f"{k}="):
                new_lines.append(f"{k}={v}\n")
                seen.add(k)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in seen:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return get_telegram_config()


def format_copilot_copy_text(sig: Dict[str, Any]) -> str:
    """Formata um sinal em texto limpo pronto para replicar na xStation 5."""
    side_pt = "COMPRA (BUY)" if sig.get("side") == "BUY" else "VENDA (SELL)"
    return (
        f"XTB xStation 5 | {sig.get('xtb_symbol')} ({sig.get('symbol')})\n"
        f"Direção: {side_pt} | Score IA: {float(sig.get('score', 0)):.0f}/100\n"
        f"Entrada: {sig.get('entry_price')} | SL: {sig.get('sl_price')} | TP: {sig.get('tp_price')}\n"
        f"Lote sugerido: {sig.get('suggested_lots')} | Alavancagem: 1:{sig.get('leverage')}\n"
        f"Motivo: {sig.get('reason') or 'Confluência técnica + macro'}"
    )


def send_telegram_message(text: str, timeout: float = 8.0) -> Dict[str, Any]:
    """Envia mensagem via Telegram Bot API usando apenas stdlib (urllib)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"sent": False, "error": "Telegram não configurado (defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)."}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
            ok = bool(body.get("ok"))
            return {"sent": ok, "error": None if ok else str(body.get("description") or "Erro Telegram")}
    except urllib.error.HTTPError as e:
        return {"sent": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"sent": False, "error": str(e)}


class XTBCopilotStore:
    """Armazena e gerencia sinais do Copiloto XTB em SQLite (WAL)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xtb_copilot_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    xtb_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    score REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    suggested_lots REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    telegram_sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def record_signal(
        self,
        symbol: str,
        side: str,
        score: float,
        entry_price: float,
        sl_price: Optional[float],
        tp_price: Optional[float],
        suggested_lots: float,
        leverage: int,
        reason: str = "",
        notify_telegram: bool = True,
        dedup_minutes: int = 120,
    ) -> Dict[str, Any]:
        """Registra um novo sinal para o Copiloto XTB (com deduplicação por símbolo/direção)."""
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        cutoff_iso = (now_utc - timedelta(minutes=dedup_minutes)).strftime("%Y-%m-%d %H:%M:%S UTC")
        sym = symbol.upper().strip()
        xtb_sym = to_xtb_symbol(sym)
        side_u = side.upper().strip()

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM xtb_copilot_signals
                WHERE symbol = ? AND side = ? AND status = 'NEW' AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (sym, side_u, cutoff_iso),
            ).fetchone()
            if existing is not None:
                d = dict(existing)
                d["copy_text"] = format_copilot_copy_text(d)
                d["deduplicated"] = True
                return d

            cur = conn.execute(
                """
                INSERT INTO xtb_copilot_signals (
                    created_at, symbol, xtb_symbol, side, score,
                    entry_price, sl_price, tp_price, suggested_lots,
                    leverage, reason, status, telegram_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', 0)
                """,
                (
                    now_iso,
                    sym,
                    xtb_sym,
                    side_u,
                    float(score),
                    float(entry_price),
                    float(sl_price) if sl_price is not None else None,
                    float(tp_price) if tp_price is not None else None,
                    float(suggested_lots),
                    int(leverage),
                    reason or "Confluência técnica + macro",
                ),
            )
            sig_id = cur.lastrowid
            conn.commit()
            row = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (sig_id,)).fetchone()

        sig = dict(row)
        sig["copy_text"] = format_copilot_copy_text(sig)
        sig["deduplicated"] = False

        if notify_telegram and get_telegram_config()["configured"]:
            msg = f"🧭 SINAL COPILOTO XTB (FyNex AI)\n\n{sig['copy_text']}"
            tg_res = send_telegram_message(msg)
            if tg_res.get("sent"):
                with self._connect() as conn:
                    conn.execute("UPDATE xtb_copilot_signals SET telegram_sent = 1 WHERE id = ?", (sig_id,))
                    conn.commit()
                sig["telegram_sent"] = 1
            else:
                logger.debug(f"[COPILOTO XTB] Alerta Telegram não enviado: {tg_res.get('error')}")

        return sig

    def list_signals(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM xtb_copilot_signals ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["copy_text"] = format_copilot_copy_text(d)
            out.append(d)
        return out

    def update_status(self, signal_id: int, status: str) -> Dict[str, Any]:
        st = (status or "").upper().strip()
        if st not in VALID_SIGNAL_STATUSES:
            raise ValueError(f"Status inválido: {status}. Use um de {VALID_SIGNAL_STATUSES}.")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (signal_id,)).fetchone()
            if row is None:
                raise ValueError(f"Sinal #{signal_id} não encontrado.")
            conn.execute("UPDATE xtb_copilot_signals SET status = ? WHERE id = ?", (st, signal_id))
            conn.commit()
            updated = conn.execute("SELECT * FROM xtb_copilot_signals WHERE id = ?", (signal_id,)).fetchone()
        d = dict(updated)
        d["copy_text"] = format_copilot_copy_text(d)
        return d
