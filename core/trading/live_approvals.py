"""
Fila de Aprovação do Administrador para Ordens da Conta Real (FyNex Quant AI)
=============================================================================
Na Conta Real (Dinheiro Real), a IA nunca executa uma ordem diretamente sem aprovação
explícita do administrador:
1. A IA identifica oportunidades em gráficos >= 1h (1h, 4h, 1d) usando o Modelo Campeão.
2. Enfileira a ordem como `PENDING` em `live_order_approvals` (SQLite WAL).
3. Envia imediatamente um pedido de aprovação via Telegram (com botões inline e
   comandos `/aprovar <id>` / `/rejeitar <id>`) e/ou WhatsApp CallMeBot.
4. Enquanto aguarda aprovação, a IA NÃO trava e continua buscando novas oportunidades.
5. Ao aprovar (pelo painel `/live` ou pelo Telegram autorizado do administrador),
   executa a ordem real na Binance Spot com proteção OCO de forma atômica e idempotente.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import alerts
import binance_client
from core import macro_intelligence
from core.database import TradingDatabase
from core.infra.sqlite import SCHEMA_REGISTRY
from core.infra.utils import parse_utc_str, utc_now_str
from core.notifications.telegram import (
    answer_telegram_callback,
    fetch_telegram_updates,
    get_telegram_config,
    send_telegram_approval_prompt,
    send_telegram_message,
)
from core.notifications.whatsapp import get_whatsapp_config, send_whatsapp_message
from core.trading.position_manager import PositionManager
from core.trading.risk import RiskManager
from strategy_config import OPERATIONAL_TIMEFRAMES, get_strategy_profile, is_operational_timeframe

logger = logging.getLogger("QuantTradingBot")

VALID_APPROVAL_STATUSES = ("PENDING", "APPROVED", "EXECUTING", "EXECUTED", "REJECTED", "EXPIRED", "FAILED")

# Validade padrão da proposta de aprovação conforme o tempo gráfico
APPROVAL_TTL_MINUTES: Dict[str, int] = {
    "1h": 60,
    "4h": 180,
    "1d": 360,
}

_CMD_APPROVE_RE = re.compile(r"^/?(?:aprovar|approve|sim)\s+#?(\d+)\b", re.IGNORECASE)
_CMD_REJECT_RE = re.compile(r"^/?(?:rejeitar|reject|recusar|nao|não)\s+#?(\d+)\b", re.IGNORECASE)


def format_live_approval_message(proposal: Dict[str, Any], *, channel: str = "telegram") -> str:
    """Formata mensagem clara de pedido de aprovação de ordem na Conta Real."""
    oid = int(proposal["id"])
    sym = str(proposal["symbol"])
    tf = str(proposal["timeframe"])
    side = "COMPRA (BUY)" if str(proposal.get("side", "BUY")).upper() == "BUY" else "VENDA (SELL)"
    entry = float(proposal.get("entry_price") or 0.0)
    sl = float(proposal.get("sl_price") or 0.0)
    tp = float(proposal.get("tp_price") or 0.0)
    sl_pct = float(proposal.get("sl_pct") or 0.0) * 100.0
    tp_pct = float(proposal.get("tp_pct") or 0.0) * 100.0
    amount = float(proposal.get("amount_usdt") or 0.0)
    prob = float(proposal.get("prob_up") or 0.0) * 100.0
    th = float(proposal.get("confidence_threshold") or 0.0) * 100.0
    ver = int(proposal.get("model_version") or 1)
    reason = str(proposal.get("reason") or "Confluência IA Campeã + Macro")

    ctx_parsed: Dict[str, Any] = {}
    try:
        raw_ctx = json.loads(proposal.get("context_json") or "{}")
        if isinstance(raw_ctx, dict):
            ctx_parsed = raw_ctx
    except Exception:
        ctx_parsed = {}

    intel = proposal.get("pre_order_intel")
    if not isinstance(intel, dict):
        intel = ctx_parsed.get("pre_order_intel") if isinstance(ctx_parsed.get("pre_order_intel"), dict) else None

    pot_score = ctx_parsed.get("live_potential_score")
    pot_badge = str(ctx_parsed.get("live_potential_badge") or "")
    pot_line = (
        f"🎯 Potencial Conta Real: {pot_badge}\n"
        if pot_badge
        else (f"🎯 Score de Potencial: {float(pot_score):.0f}/100\n" if pot_score is not None else "")
    )

    intel_block = ""
    if isinstance(intel, dict) and intel:
        verdict_badge = str(intel.get("verdict_badge") or "🔎 Pesquisado")
        eq_line = str(intel.get("equities_line") or "")
        macro_line = str(intel.get("macro_line") or "")
        top_hl = str(intel.get("top_headline") or "")
        flags = intel.get("risk_flags") or []
        lines = [f"📊 Pesquisa Pré-Ordem: {verdict_badge}"]
        if eq_line:
            lines.append(f"🏛️ Bolsas: {eq_line}")
        if macro_line:
            lines.append(f"🌎 {macro_line}")
        if top_hl:
            lines.append(f"📰 Notícia: {top_hl[:110]}")
        if flags:
            lines.append(f"⚠️ Alertas: {'; '.join(str(f) for f in flags[:2])}")
        intel_block = "\n" + "\n".join(lines) + "\n"

    action_hint = (
        f"Toque nos botões abaixo ou responda:\n/aprovar {oid}  ou  /rejeitar {oid}"
        if channel == "telegram"
        else f"Acesse o painel /live para APROVAR ou REJEITAR a ordem #{oid}."
    )
    return (
        f"🔐 APROVAÇÃO NECESSÁRIA — CONTA REAL (#{oid})\n"
        f"Ativo: {sym} | Gráfico: {tf} | {side}\n"
        f"Preço Ref.: ${entry:,.4f} | Valor: ${amount:,.2f} USDT\n"
        f"Take Profit: ${tp:,.4f} (+{tp_pct:.2f}%)\n"
        f"Stop Loss: ${sl:,.4f} (-{sl_pct:.2f}%)\n"
        f"IA Campeã: v{ver} | Confiança: {prob:.1f}% (filtro {th:.1f}%)\n"
        f"{pot_line}"
        f"Motivo: {reason}"
        f"{intel_block}\n"
        f"👉 {action_hint}\n"
        f"ℹ️ Enquanto aguarda sua decisão, a IA segue buscando novas oportunidades."
    )


class LiveOrderApprovalStore:
    """Gerencia propostas de ordens reais pendentes de aprovação do administrador."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        SCHEMA_REGISTRY.ensure_initialized("live_approvals", self.db_path, self._init_tables)

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_order_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'BUY',
                    model_version INTEGER NOT NULL,
                    prob_up REAL NOT NULL,
                    confidence_threshold REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    sl_price REAL NOT NULL,
                    tp_price REAL NOT NULL,
                    sl_pct REAL NOT NULL,
                    tp_pct REAL NOT NULL,
                    amount_usdt REAL NOT NULL,
                    estimated_qty REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    notified_telegram INTEGER NOT NULL DEFAULT 0,
                    notified_whatsapp INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_note TEXT DEFAULT '',
                    execution_order_id TEXT DEFAULT '',
                    execution_price REAL,
                    execution_error TEXT DEFAULT ''
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_live_approvals_status
                ON live_order_approvals(status, id DESC);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_live_approvals_sym_tf_status
                ON live_order_approvals(symbol, timeframe, status);
                """
            )

    def expire_stale_orders(self, now_utc: Optional[datetime] = None) -> int:
        now = now_utc or datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect(write_immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE live_order_approvals
                SET status = 'EXPIRED', decided_at = ?, decided_by = 'system_ttl',
                    decision_note = 'Expirada sem resposta dentro da janela do candle'
                WHERE status = 'PENDING' AND expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            return int(cur.rowcount or 0)

    def _notify_admin_of_proposal(self, proposal: Dict[str, Any]) -> Tuple[bool, bool]:
        """Envia o pedido de aprovação para Telegram (com botões inline) e/ou WhatsApp CallMeBot."""
        oid = int(proposal["id"])
        tg_ok = False
        wa_ok = False

        try:
            if get_telegram_config().get("configured"):
                tg_msg = format_live_approval_message(proposal, channel="telegram")
                res = send_telegram_approval_prompt(tg_msg, order_id=oid)
                tg_ok = bool(res.get("sent"))
        except Exception as e:
            logger.debug(f"[APROVAÇÃO LIVE] Falha ao notificar Telegram: {e}")

        try:
            wa_cfg = get_whatsapp_config()
            if wa_cfg.get("active"):
                wa_msg = format_live_approval_message(proposal, channel="whatsapp")
                res = send_whatsapp_message(wa_msg)
                wa_ok = bool(res.get("sent"))
        except Exception as e:
            logger.debug(f"[APROVAÇÃO LIVE] Falha ao notificar WhatsApp: {e}")

        if tg_ok or wa_ok:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE live_order_approvals SET notified_telegram = ?, notified_whatsapp = ? WHERE id = ?",
                        (1 if tg_ok else 0, 1 if wa_ok else 0, oid),
                    )
            except Exception:
                pass
        return tg_ok, wa_ok

    def create_proposal(
        self,
        *,
        symbol: str,
        timeframe: str,
        model_version: int,
        prob_up: float,
        confidence_threshold: float,
        entry_price: float,
        sl_pct: float,
        tp_pct: float,
        amount_usdt: float,
        side: str = "BUY",
        reason: str = "",
        entry_context: Optional[Dict[str, Any]] = None,
        ttl_minutes: Optional[int] = None,
        notify: bool = True,
        pre_order_intel: Optional[Dict[str, Any]] = None,
        collect_intel: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Cria uma proposta de ordem real `PENDING` aguardando aprovação do administrador.
        Antes de enfileirar, consolida o dossiê pré-ordem (Bolsas Globais S&P500/Nasdaq/VIX,
        Macro e Notícias relevantes) para embasar a decisão do administrador.
        """
        tf = str(timeframe or "").strip().lower()
        if not is_operational_timeframe(tf):
            raise ValueError(
                f"Timeframe '{timeframe}' não permitido para Conta Real. "
                f"Use apenas gráficos >= 1h: {', '.join(OPERATIONAL_TIMEFRAMES)}."
            )
        sym = str(symbol or "").strip().upper()
        side_u = str(side or "BUY").strip().upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"Lado inválido: '{side}'.")
        if not (math.isfinite(entry_price) and entry_price > 0):
            raise ValueError(f"Preço de referência inválido: {entry_price}")
        if not (math.isfinite(amount_usdt) and amount_usdt > 0):
            raise ValueError(f"Valor USDT inválido: {amount_usdt}")

        ctx_dict = dict(entry_context or {})
        intel_data = pre_order_intel or ctx_dict.get("pre_order_intel")
        if intel_data is None and collect_intel:
            try:
                intel_data = macro_intelligence.build_live_pre_order_intelligence(
                    sym,
                    side=side_u,
                    macro_snapshot=ctx_dict.get("macro") if isinstance(ctx_dict.get("macro"), dict) else None,
                    refresh_ttl_seconds=60.0,
                )
            except Exception as intel_err:
                logger.debug(f"[APROVAÇÃO LIVE] Pesquisa pré-ordem indisponível: {intel_err}")
                intel_data = None
        if isinstance(intel_data, dict):
            ctx_dict["pre_order_intel"] = intel_data

        now_utc = datetime.now(timezone.utc)
        self.expire_stale_orders(now_utc)
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        ttl = int(ttl_minutes if ttl_minutes is not None else APPROVAL_TTL_MINUTES.get(tf, 60))
        expires_iso = (now_utc + timedelta(minutes=max(5, ttl))).strftime("%Y-%m-%d %H:%M:%S")

        sl_price = entry_price * (1.0 - float(sl_pct)) if side_u == "BUY" else entry_price * (1.0 + float(sl_pct))
        tp_price = entry_price * (1.0 + float(tp_pct)) if side_u == "BUY" else entry_price * (1.0 - float(tp_pct))
        est_qty = float(amount_usdt) / float(entry_price)
        ctx_str = json.dumps(ctx_dict, default=str)

        with self._connect(write_immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT * FROM live_order_approvals
                WHERE symbol = ? AND timeframe = ? AND side = ? AND status = 'PENDING' AND expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (sym, tf, side_u, now_iso),
            ).fetchone()
            if existing is not None:
                return dict(existing), False

            cur = conn.execute(
                """
                INSERT INTO live_order_approvals (
                    symbol, timeframe, side, model_version, prob_up, confidence_threshold,
                    entry_price, sl_price, tp_price, sl_pct, tp_pct, amount_usdt, estimated_qty,
                    reason, context_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    sym, tf, side_u, int(model_version), float(prob_up), float(confidence_threshold),
                    float(entry_price), float(sl_price), float(tp_price), float(sl_pct), float(tp_pct),
                    float(amount_usdt), float(est_qty), str(reason or "Sinal IA Campeã"),
                    ctx_str, now_iso, expires_iso,
                ),
            )
            order_id = int(cur.lastrowid or 0)
            row = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (order_id,)).fetchone()

        proposal = self._hydrate_proposal(dict(row))
        if notify:
            tg_ok, wa_ok = self._notify_admin_of_proposal(proposal)
            proposal["notified_telegram"] = 1 if tg_ok else 0
            proposal["notified_whatsapp"] = 1 if wa_ok else 0

        logger.info(
            f"🔐 [APROVAÇÃO CONTA REAL #{proposal['id']}] {sym} ({tf}) @ ${entry_price:,.4f} | "
            f"${amount_usdt:.2f} USDT | Confiança {prob_up:.1%} | Aguardando administrador "
            f"(Telegram={'OK' if proposal['notified_telegram'] else '-'}, "
            f"WhatsApp={'OK' if proposal['notified_whatsapp'] else '-'})."
        )
        return proposal, True

    @staticmethod
    def _hydrate_proposal(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Expõe `pre_order_intel` já decodificado a partir de `context_json`."""
        try:
            ctx = json.loads(row_dict.get("context_json") or "{}")
            if isinstance(ctx, dict) and isinstance(ctx.get("pre_order_intel"), dict):
                row_dict["pre_order_intel"] = ctx["pre_order_intel"]
            else:
                row_dict["pre_order_intel"] = None
        except Exception:
            row_dict["pre_order_intel"] = None
        return row_dict

    def get_proposal(self, order_id: int) -> Optional[Dict[str, Any]]:
        self.expire_stale_orders()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (int(order_id),)).fetchone()
        return self._hydrate_proposal(dict(row)) if row else None

    def list_proposals(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        self.expire_stale_orders()
        with self._connect() as conn:
            if status and status.upper() in VALID_APPROVAL_STATUSES:
                rows = conn.execute(
                    "SELECT * FROM live_order_approvals WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status.upper(), max(1, min(int(limit), 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM live_order_approvals
                    ORDER BY CASE WHEN status = 'PENDING' THEN 0 ELSE 1 END ASC, id DESC
                    LIMIT ?
                    """,
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
        return [self._hydrate_proposal(dict(r)) for r in rows]

    def count_pending(self) -> int:
        self.expire_stale_orders()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM live_order_approvals WHERE status = 'PENDING'").fetchone()
        return int(row["n"] or 0) if row else 0

    def reject_proposal(
        self,
        order_id: int,
        *,
        decided_by: str = "admin_dashboard",
        note: str = "",
    ) -> Dict[str, Any]:
        self.expire_stale_orders()
        now_iso = utc_now_str()
        with self._connect(write_immediate=True) as conn:
            row = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (int(order_id),)).fetchone()
            if row is None:
                raise KeyError(f"Ordem pendente #{order_id} não encontrada.")
            if row["status"] != "PENDING":
                raise ValueError(f"Ordem #{order_id} não está pendente (status atual: {row['status']}).")
            conn.execute(
                """
                UPDATE live_order_approvals
                SET status = 'REJECTED', decided_at = ?, decided_by = ?, decision_note = ?
                WHERE id = ?
                """,
                (now_iso, str(decided_by), str(note or "Rejeitada pelo administrador")[:240], int(order_id)),
            )
            updated = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (int(order_id),)).fetchone()
        return dict(updated)

    def approve_and_execute(
        self,
        order_id: int,
        *,
        decided_by: str = "admin_dashboard",
        note: str = "",
        exchange_override: Any = None,
    ) -> Dict[str, Any]:
        """
        Aprova e executa de forma atômica uma ordem pendente na Conta Real (Binance Spot Live).
        Garante que:
        - Apenas ordens `PENDING` não expiradas podem ser executadas (idempotente).
        - Chaves de produção e trava `LIVE_TRADING_ENABLED` estejam liberadas antes de travar execução.
        """
        import trading_bot

        self.expire_stale_orders()

        # 1. Pré-checagem de segurança (mantém a proposta PENDING se a trava ainda estiver bloqueada)
        bin_cfg = binance_client.load_binance_config()
        if exchange_override is None:
            if not getattr(trading_bot, "LIVE_TRADING_ENABLED", False):
                raise PermissionError(
                    "Trava de segurança ativa (LIVE_TRADING_ENABLED = False em trading_bot.py). "
                    "A ordem permanece pendente até você liberar ordens reais."
                )
            if not bin_cfg["has_keys"] or bin_cfg["testnet"]:
                raise PermissionError(
                    "Credenciais de PRODUÇÃO da Binance Spot não configuradas (ou marcadas como Testnet). "
                    "Configure suas chaves reais antes de aprovar a execução."
                )

        now_iso = utc_now_str()
        with self._connect(write_immediate=True) as conn:
            row = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (int(order_id),)).fetchone()
            if row is None:
                raise KeyError(f"Ordem #{order_id} não encontrada.")
            if row["status"] != "PENDING":
                raise ValueError(f"Ordem #{order_id} já foi processada (status atual: {row['status']}).")
            exp_dt = parse_utc_str(row["expires_at"])
            if exp_dt is not None and exp_dt <= datetime.now(timezone.utc):
                conn.execute(
                    "UPDATE live_order_approvals SET status = 'EXPIRED', decided_at = ?, decided_by = 'system_ttl' WHERE id = ?",
                    (now_iso, int(order_id)),
                )
                raise ValueError(f"Ordem #{order_id} expirou antes da aprovação.")
            conn.execute(
                """
                UPDATE live_order_approvals
                SET status = 'EXECUTING', decided_at = ?, decided_by = ?, decision_note = ?
                WHERE id = ?
                """,
                (now_iso, str(decided_by), str(note or "Aprovada pelo administrador")[:240], int(order_id)),
            )
            proposal = dict(row)

        # 2. Execução real na Binance Spot via PositionManager(mode="live")
        db = TradingDatabase(db_path=self.db_path)
        try:
            exchange = exchange_override or binance_client.get_binance_exchange(
                api_key=bin_cfg["api_key"],
                api_secret=bin_cfg["api_secret"],
                testnet=False,
            )
            exec_price = float(proposal["entry_price"])
            if exchange_override is None:
                try:
                    tick = binance_client.fetch_live_ticker(symbol=proposal["symbol"], testnet=False)
                    live_p = float(tick.get("price") or tick.get("ask") or 0.0)
                    if math.isfinite(live_p) and live_p > 0:
                        exec_price = live_p
                except Exception:
                    pass

            profile = get_strategy_profile(proposal["timeframe"])
            risk = RiskManager(
                db,
                mode="live",
                max_daily_loss_usdt=bin_cfg["max_daily_loss_usdt"],
                max_trades_per_day=bin_cfg["max_trades_per_day"],
                max_trade_amount_usdt=bin_cfg["max_trade_amount_usdt"],
            )
            pm = PositionManager(
                db,
                exchange,
                mode="live",
                symbol=proposal["symbol"],
                timeframe=proposal["timeframe"],
                stop_loss_pct=float(proposal["sl_pct"]),
                take_profit_pct=float(proposal["tp_pct"]),
                trade_amount_usdt=float(proposal["amount_usdt"]),
                use_exchange_oco=bin_cfg["use_exchange_oco"],
                risk=risk,
                horizon_candles=profile.horizon_candles,
            )
            pm.restore()
            if pm.in_position:
                raise RuntimeError(
                    f"Já existe uma posição REAL aberta em {proposal['symbol']} ({proposal['timeframe']})."
                )

            try:
                ctx = json.loads(proposal.get("context_json") or "{}")
            except Exception:
                ctx = {}

            opened = pm.try_open(
                exec_price,
                int(proposal["model_version"]),
                float(proposal["prob_up"]),
                tp_pct_override=float(proposal["tp_pct"]),
                sl_pct_override=float(proposal["sl_pct"]),
                amount_usdt_override=float(proposal["amount_usdt"]),
                entry_context=ctx,
            )
            if not opened or pm.position is None:
                raise RuntimeError(
                    "Ordem aprovada não pôde ser aberta na Binance (verifique saldo USDT, limites diários ou logs)."
                )

            with self._connect(write_immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE live_order_approvals
                    SET status = 'EXECUTED', execution_order_id = ?, execution_price = ?, execution_error = ''
                    WHERE id = ?
                    """,
                    (str(pm.position.order_id or ""), float(pm.position.entry_price), int(order_id)),
                )
                updated = conn.execute("SELECT * FROM live_order_approvals WHERE id = ?", (int(order_id),)).fetchone()

            res_dict = dict(updated)
            alerts.send_alert(
                (
                    f"✅ ORDEM REAL #{order_id} APROVADA E EXECUTADA!\n"
                    f"Ativo: {proposal['symbol']} ({proposal['timeframe']})\n"
                    f"Preço Executado: ${pm.position.entry_price:,.4f} | Qtd: {pm.position.quantity:.6f}\n"
                    f"TP: ${pm.position.tp_price:,.4f} | SL: ${pm.position.sl_price:,.4f}\n"
                    f"Order ID: {pm.position.order_id or 'N/A'}"
                ),
                level="LIVE_EXECUTED",
            )
            return res_dict

        except Exception as exc:
            err_msg = str(exc)[:300]
            with self._connect(write_immediate=True) as conn:
                conn.execute(
                    "UPDATE live_order_approvals SET status = 'FAILED', execution_error = ? WHERE id = ?",
                    (err_msg, int(order_id)),
                )
            raise RuntimeError(f"Falha ao executar ordem real #{order_id}: {err_msg}") from exc

    def poll_telegram_admin_commands(self) -> List[Dict[str, Any]]:
        """
        Verifica comandos `/aprovar <id>`, `/rejeitar <id>` ou cliques nos botões inline
        enviados exclusivamente pelo `TELEGRAM_CHAT_ID` autorizado do administrador.
        """
        db = TradingDatabase(db_path=self.db_path)
        raw_offset = db.get_account_setting("telegram_last_update_id", "0")
        try:
            offset = int(raw_offset) + 1 if int(raw_offset) > 0 else 0
        except Exception:
            offset = 0

        res = fetch_telegram_updates(offset=offset)
        if not res.get("ok"):
            return []

        authorized_chat_id = str(res.get("chat_id") or "").strip()
        updates = res.get("updates") or []
        if not updates:
            return []

        max_update_id = offset - 1
        actions: List[Dict[str, Any]] = []

        for upd in updates:
            uid = int(upd.get("update_id") or 0)
            if uid > max_update_id:
                max_update_id = uid

            action_kind: Optional[str] = None
            target_order_id: Optional[int] = None
            cb_id: Optional[str] = None
            sender_chat_id = ""

            if "callback_query" in upd:
                cq = upd["callback_query"] or {}
                cb_id = str(cq.get("id") or "")
                msg_obj = cq.get("message") or {}
                sender_chat_id = str((msg_obj.get("chat") or {}).get("id") or "")
                data_str = str(cq.get("data") or "").strip()
                if data_str.startswith("approve:") and data_str.split(":", 1)[1].isdigit():
                    action_kind = "approve"
                    target_order_id = int(data_str.split(":", 1)[1])
                elif data_str.startswith("reject:") and data_str.split(":", 1)[1].isdigit():
                    action_kind = "reject"
                    target_order_id = int(data_str.split(":", 1)[1])
            elif "message" in upd:
                msg_obj = upd["message"] or {}
                sender_chat_id = str((msg_obj.get("chat") or {}).get("id") or "")
                text = str(msg_obj.get("text") or "").strip()
                m_app = _CMD_APPROVE_RE.match(text)
                m_rej = _CMD_REJECT_RE.match(text)
                if m_app:
                    action_kind = "approve"
                    target_order_id = int(m_app.group(1))
                elif m_rej:
                    action_kind = "reject"
                    target_order_id = int(m_rej.group(1))

            # Segurança estrita: aceita comandos SOMENTE do chat autorizado do administrador
            if not action_kind or target_order_id is None or sender_chat_id != authorized_chat_id:
                continue

            try:
                if action_kind == "approve":
                    updated = self.approve_and_execute(target_order_id, decided_by="admin_telegram")
                    reply = f"✅ Ordem #{target_order_id} ({updated['symbol']}) APROVADA e EXECUTADA!"
                    actions.append({"action": "approve", "order_id": target_order_id, "status": "executed"})
                else:
                    updated = self.reject_proposal(target_order_id, decided_by="admin_telegram")
                    reply = f"❌ Ordem #{target_order_id} ({updated['symbol']}) REJEITADA. A IA segue buscando novas oportunidades."
                    actions.append({"action": "reject", "order_id": target_order_id, "status": "rejected"})
            except Exception as err:
                reply = f"⚠️ Ordem #{target_order_id}: {err}"
                actions.append({"action": action_kind, "order_id": target_order_id, "status": "error", "error": str(err)})

            if cb_id:
                answer_telegram_callback(cb_id, text=reply)
            send_telegram_message(reply)

        if max_update_id > 0:
            db.set_account_setting("telegram_last_update_id", str(max_update_id))
        return actions
