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
import re
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from dotenv import dotenv_values

logger = logging.getLogger("QuantTradingBot")

_DEFAULT_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
_ENV_WRITE_LOCK = threading.Lock()
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,64}$")
_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d{5,20}$")
_TELEGRAM_TOKEN_SCRUB_RE = re.compile(r"bot\d{6,15}:[A-Za-z0-9_-]{10,}")

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


def _scrub_telegram_error(err: Any, token: str = "") -> str:
    msg = str(err or "")
    if token:
        msg = msg.replace(token, "***")
    msg = _TELEGRAM_TOKEN_SCRUB_RE.sub("bot***", msg)
    return msg[:300]


def _load_telegram_creds(env_path: Optional[str] = None) -> tuple[str, str]:
    path = env_path or _DEFAULT_ENV_PATH
    env_data: Dict[str, Optional[str]] = {}
    if os.path.exists(path):
        try:
            env_data = dotenv_values(path)
        except Exception:
            env_data = {}
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or env_data.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or env_data.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def get_telegram_config(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Lê configuração opcional do Telegram a partir do .env e variáveis de ambiente."""
    token, chat_id = _load_telegram_creds(env_path=env_path)
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
    """Valida e salva TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID de forma atômica no .env e os.environ."""
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()

    if any(ch in token for ch in ("\r", "\n")) or any(ch in cid for ch in ("\r", "\n")):
        raise ValueError("Credenciais do Telegram não podem conter quebras de linha.")

    updates: Dict[str, str] = {}
    if token and "..." not in token and not token.startswith(("*", "•")):
        if not _TELEGRAM_TOKEN_RE.match(token):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN inválido. Use o formato oficial do BotFather (ex.: 123456789:AA...)."
            )
        updates["TELEGRAM_BOT_TOKEN"] = token

    if cid:
        if not _TELEGRAM_CHAT_ID_RE.match(cid):
            raise ValueError("TELEGRAM_CHAT_ID inválido. Informe apenas o ID numérico do chat/grupo.")
        updates["TELEGRAM_CHAT_ID"] = cid

    if env_path is None:
        env_path = _DEFAULT_ENV_PATH

    with _ENV_WRITE_LOCK:
        lines: List[str] = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

        seen = set()
        new_lines: List[str] = []
        key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
        for line in lines:
            m = key_re.match(line)
            if m and m.group(1) in updates:
                k = m.group(1)
                if k not in seen:
                    new_lines.append(f"{k}={updates[k]}")
                    seen.add(k)
            else:
                new_lines.append(line)

        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f"{k}={v}")

        content = "\n".join(new_lines) + "\n"
        _atomic_write_text(env_path, content)

        for k, v in updates.items():
            os.environ[k] = v

    return get_telegram_config(env_path=env_path)


def format_copilot_copy_text(sig: Dict[str, Any]) -> str:
    """Formata um sinal em texto limpo pronto para replicar na xStation 5."""
    side_pt = "COMPRA (BUY)" if sig.get("side") == "BUY" else "VENDA (SELL)"
    entry = float(sig.get("entry_price") or 0.0)
    sl = sig.get("sl_price")
    tp = sig.get("tp_price")
    dist_note = ""
    if entry > 0 and sl is not None and tp is not None:
        sl_pct = abs(float(sl) - entry) / entry * 100.0
        tp_pct = abs(float(tp) - entry) / entry * 100.0
        dist_note = f" (SL {sl_pct:.2f}% | TP {tp_pct:.2f}%)"
    return (
        f"XTB xStation 5 | {sig.get('xtb_symbol')} ({sig.get('symbol')})\n"
        f"Direção: {side_pt} | Score IA: {float(sig.get('score', 0)):.0f}/100\n"
        f"Entrada: {sig.get('entry_price')} | SL: {sig.get('sl_price')} | TP: {sig.get('tp_price')}{dist_note}\n"
        f"Lote sugerido: {sig.get('suggested_lots')} | Alavancagem: 1:{sig.get('leverage')}\n"
        f"Motivo: {sig.get('reason') or 'Confluência técnica + macro'}"
    )


def send_telegram_message(text: str, timeout: float = 8.0) -> Dict[str, Any]:
    """Envia mensagem via Telegram Bot API usando apenas stdlib (urllib) com sanitização."""
    token, chat_id = _load_telegram_creds()
    if not token or not chat_id:
        return {"sent": False, "error": "Telegram não configurado (defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)."}
    if not _TELEGRAM_TOKEN_RE.match(token) or not _TELEGRAM_CHAT_ID_RE.match(chat_id):
        return {"sent": False, "error": "Configuração de Telegram inválida."}

    safe_text = str(text or "")
    if len(safe_text) > 4000:
        safe_text = safe_text[:3997] + "..."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": safe_text,
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
            raw_bytes = resp.read(65536)
            body = json.loads(raw_bytes.decode("utf-8", errors="ignore") or "{}")
            ok = bool(body.get("ok"))
            return {"sent": ok, "error": None if ok else _scrub_telegram_error(body.get("description") or "Erro Telegram", token)}
    except urllib.error.HTTPError as e:
        return {"sent": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"sent": False, "error": _scrub_telegram_error(e, token)}


_COPILOT_INIT_LOCK = threading.Lock()
_INITIALIZED_COPILOT_DBS: set = set()


class XTBCopilotStore:
    """Armazena e gerencia sinais do Copiloto XTB em SQLite (WAL)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with _COPILOT_INIT_LOCK:
            if db_path not in _INITIALIZED_COPILOT_DBS or not os.path.exists(db_path):
                self._init_tables()
                _INITIALIZED_COPILOT_DBS.add(db_path)

    @contextmanager
    def _connect(self, *, write_immediate: bool = False) -> Iterator[sqlite3.Connection]:
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
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_copilot_dedup
                ON xtb_copilot_signals(symbol, side, created_at)
                """
            )

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
        """Registra um novo sinal para o Copiloto XTB (com deduplicação atômica por símbolo/direção)."""
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_iso = (now_utc - timedelta(minutes=dedup_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        sym = symbol.upper().strip()
        xtb_sym = to_xtb_symbol(sym)
        side_u = side.upper().strip()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"Direção inválida para sinal XTB: '{side}'. Use 'BUY' ou 'SELL'.")

        with self._connect(write_immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT * FROM xtb_copilot_signals
                WHERE symbol = ? AND side = ? AND substr(created_at, 1, 19) >= ?
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
