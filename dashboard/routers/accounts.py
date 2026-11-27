"""
Rotas de Divisão de Contas (Demo / Treinamento IA vs Conta Real / Live)
=======================================================================
Fornece visão consolidada das duas contas estilo corretora internacional,
governança de promoção do Modelo Campeão (Demo -> Conta Real), política de
auto-promoção e checklist de prontidão para operação com dinheiro real.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import alerts
import binance_client
from core.database import TradingDatabase
from core.infra.sqlite import sqlite_readonly_optional, table_exists
from core.logging_setup import DEFAULT_DB_PATH, DEFAULT_MODELS_DIR
from core.markets.xtb_desk import DEFAULT_XTB_CAPITAL_USD
from core.ml import ModelPersistence
from core.notifications.telegram import get_telegram_config
from core.trading.live_approvals import LiveOrderApprovalStore
from dashboard.bot_controller import get_all_accounts_bot_status
from dashboard.schemas import (
    OPERATIONAL_TIMEFRAME_PATTERN,
    SYMBOL_PATTERN,
    TIMEFRAME_PATTERN,
    AccountPolicyPayload,
    LiveOrderDecisionPayload,
    PromoteModelPayload,
)
from dashboard.security import require_csrf

router = APIRouter(tags=["accounts"])
_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_TF_RE = re.compile(OPERATIONAL_TIMEFRAME_PATTERN)


def _srv_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _query_account_trade_summary(
    conn: Optional[sqlite3.Connection],
    *,
    is_live: bool,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> Dict[str, Any]:
    empty = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl_pct": 0.0,
        "total_pnl_usd": 0.0,
    }
    if conn is None or not table_exists(conn, "simulated_trades"):
        return empty
    mode_clause = "LOWER(COALESCE(mode, 'paper')) = 'live'" if is_live else "LOWER(COALESCE(mode, 'paper')) != 'live'"
    clauses = [mode_clause]
    params: list[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total_trades,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) AS losses,
                   COALESCE(SUM(pnl_pct), 0.0) AS total_pnl_pct,
                   COALESCE(SUM(pnl_usd), 0.0) AS total_pnl_usd
            FROM simulated_trades
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        ).fetchone()
        if not row or not row["total_trades"]:
            return empty
        tot = int(row["total_trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        return {
            "total_trades": tot,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / tot) * 100.0, 1) if tot > 0 else 0.0,
            "total_pnl_pct": round(float(row["total_pnl_pct"] or 0.0), 2),
            "total_pnl_usd": round(float(row["total_pnl_usd"] or 0.0), 2),
        }
    except sqlite3.OperationalError:
        return empty


def _query_account_open_position(
    conn: Optional[sqlite3.Connection],
    *,
    is_live: bool,
    symbol: str,
    timeframe: str,
) -> Optional[Dict[str, Any]]:
    if conn is None or not table_exists(conn, "open_positions"):
        return None
    mode_clause = "LOWER(COALESCE(mode, 'paper')) = 'live'" if is_live else "LOWER(COALESCE(mode, 'paper')) != 'live'"
    try:
        row = conn.execute(
            f"""
            SELECT symbol, timeframe, mode, model_version, entry_time, entry_price,
                   quantity, order_id, sl_price, tp_price, oco_list_id, status
            FROM open_positions
            WHERE symbol = ? AND timeframe = ? AND {mode_clause}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def _query_account_bot_state(conn: Optional[sqlite3.Connection], account: str) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    try:
        if table_exists(conn, "bot_account_state"):
            row = conn.execute(
                "SELECT * FROM bot_account_state WHERE account = ?",
                (account,),
            ).fetchone()
            if row:
                return dict(row)
        if table_exists(conn, "bot_state"):
            row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
            if row:
                d = dict(row)
                mode = str(d.get("trading_mode", "paper")).lower()
                inferred = "live" if mode == "live" else "demo"
                if inferred == account:
                    return d
    except sqlite3.OperationalError:
        return None
    return None


def _build_live_readiness(symbol: str, champion_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    import trading_bot

    bin_cfg = binance_client.load_binance_config()
    has_keys = bool(bin_cfg["has_keys"])
    is_prod_keys = has_keys and not bool(bin_cfg["testnet"])
    live_flag_enabled = bool(getattr(trading_bot, "LIVE_TRADING_ENABLED", False))
    has_champion = champion_meta is not None

    checks = [
        {
            "key": "binance_keys",
            "label": "Chaves API Binance Spot (Produção)",
            "ok": is_prod_keys,
            "detail": (
                f"Conectada ({bin_cfg['api_key_masked']})"
                if is_prod_keys
                else ("Chaves atuais são de Testnet (Sandbox)" if has_keys else "API Key/Secret não configuradas")
            ),
        },
        {
            "key": "champion_model",
            "label": "Modelo Campeão Promovido",
            "ok": has_champion,
            "detail": (
                f"v{champion_meta['version']} ({float(champion_meta.get('accuracy', 0.0)) * 100:.1f}%)"
                if has_champion
                else "Promova o melhor modelo treinado da Conta Demo"
            ),
        },
        {
            "key": "live_safety_lock",
            "label": "Trava de Segurança LIVE_TRADING_ENABLED",
            "ok": live_flag_enabled,
            "detail": (
                "Liberada para envio de ordens reais"
                if live_flag_enabled
                else "Bloqueada por segurança (LIVE_TRADING_ENABLED = False em trading_bot.py)"
            ),
        },
        {
            "key": "risk_limits",
            "label": "Gestão de Risco & Proteção OCO",
            "ok": True,
            "detail": (
                f"Alocação ${bin_cfg['trade_amount_usdt']:.0f} • Perda máx/dia ${bin_cfg['max_daily_loss_usdt']:.0f} "
                f"• OCO {'Ativa' if bin_cfg['use_exchange_oco'] else 'Desativada'}"
            ),
        },
    ]
    all_ready = all(c["ok"] for c in checks)
    return {
        "ready": all_ready,
        "has_keys": has_keys,
        "is_production_keys": is_prod_keys,
        "live_trading_enabled": live_flag_enabled,
        "has_promoted_champion": has_champion,
        "symbol": symbol,
        "checks": checks,
    }


def _format_model_card(meta: Optional[Dict[str, Any]], *, is_explicit_champion: bool = False) -> Optional[Dict[str, Any]]:
    if not meta:
        return None
    val = meta.get("validation") if isinstance(meta.get("validation"), dict) else {}
    return {
        "version": int(meta.get("version", 1)),
        "accuracy_pct": round(float(meta.get("accuracy", 0.5)) * 100.0, 2),
        "precision_buy_pct": round(float(val.get("precision_buy", meta.get("accuracy", 0.5))) * 100.0, 2),
        "profit_factor": round(float(val.get("profit_factor", 0.0)), 2),
        "roc_auc": round(float(val.get("roc_auc", 0.5)), 3),
        "model_type": str(val.get("model_type", meta.get("model_type", "ensemble"))),
        "saved_at": meta.get("saved_at"),
        "promoted_at": meta.get("promoted_at"),
        "promoted_by": meta.get("promoted_by"),
        "notes": meta.get("notes", ""),
        "is_explicit_champion": is_explicit_champion,
        "symbol": meta.get("symbol"),
        "timeframe": meta.get("timeframe"),
    }


@router.get("/api/accounts/overview")
def get_accounts_overview(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
):
    """Retorna visão comparativa completa das duas contas (Demo / Treino IA e Conta Real / Live)."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_srv_attr("MODELS_DIR", DEFAULT_MODELS_DIR))

    bots = get_all_accounts_bot_status()
    model_storage = ModelPersistence(models_dir=models_dir)
    demo_meta = model_storage.load_metadata(symbol, timeframe)
    live_champ_meta = model_storage.load_live_champion_metadata(symbol, timeframe)
    available_versions = model_storage.list_available_versions(symbol, timeframe)

    demo_model = _format_model_card(demo_meta, is_explicit_champion=False)
    live_model = (
        _format_model_card(live_champ_meta, is_explicit_champion=True)
        if live_champ_meta
        else _format_model_card(demo_meta, is_explicit_champion=False)
    )

    demo_ver = int(demo_meta.get("version", 0)) if demo_meta else 0
    live_ver = int(live_champ_meta.get("version", 0)) if live_champ_meta else 0
    has_newer_demo_model = bool(demo_meta) and (not live_champ_meta or demo_ver > live_ver)

    demo_state = None
    live_state = None
    demo_trades_pair = _query_account_trade_summary(None, is_live=False)
    demo_trades_all = _query_account_trade_summary(None, is_live=False)
    live_trades_pair = _query_account_trade_summary(None, is_live=True)
    live_trades_all = _query_account_trade_summary(None, is_live=True)
    demo_open_pos = None
    live_open_pos = None
    xtb_realized = 0.0
    auto_promote = True
    demo_always_on = True
    live_high_potential_only = True
    live_min_confidence_pct = 72
    live_min_potential_score = 78
    live_min_risk_reward = 1.5
    live_micro_compound_enabled = True
    live_compound_alloc_pct = 40
    live_min_order_usdt = 10.0
    promotions: list[Dict[str, Any]] = []

    with sqlite_readonly_optional(db_path) as conn:
        if conn is not None:
            demo_state = _query_account_bot_state(conn, "demo")
            live_state = _query_account_bot_state(conn, "live")
            demo_trades_pair = _query_account_trade_summary(conn, is_live=False, symbol=symbol, timeframe=timeframe)
            demo_trades_all = _query_account_trade_summary(conn, is_live=False)
            live_trades_pair = _query_account_trade_summary(conn, is_live=True, symbol=symbol, timeframe=timeframe)
            live_trades_all = _query_account_trade_summary(conn, is_live=True)
            demo_open_pos = _query_account_open_position(conn, is_live=False, symbol=symbol, timeframe=timeframe)
            live_open_pos = _query_account_open_position(conn, is_live=True, symbol=symbol, timeframe=timeframe)
            if table_exists(conn, "xtb_trades"):
                try:
                    xr = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0.0) AS s FROM xtb_trades").fetchone()
                    xtb_realized = float(xr["s"] or 0.0) if xr else 0.0
                except sqlite3.OperationalError:
                    pass
            if table_exists(conn, "account_settings"):
                try:
                    rows = conn.execute(
                        """
                        SELECT key, value FROM account_settings
                        WHERE key IN (
                            'auto_promote_to_live',
                            'demo_always_on_training',
                            'live_high_potential_only',
                            'live_min_confidence_pct',
                            'live_min_potential_score',
                            'live_min_risk_reward',
                            'live_micro_compound_enabled',
                            'live_compound_alloc_pct',
                            'live_min_order_usdt'
                        )
                        """
                    ).fetchall()
                    smap = {str(r["key"]): str(r["value"]).strip() for r in rows}
                    if "auto_promote_to_live" in smap:
                        auto_promote = smap["auto_promote_to_live"].lower() in ("1", "true", "yes", "sim", "on")
                    if "demo_always_on_training" in smap:
                        demo_always_on = smap["demo_always_on_training"].lower() in ("1", "true", "yes", "sim", "on")
                    if "live_high_potential_only" in smap:
                        live_high_potential_only = smap["live_high_potential_only"].lower() in ("1", "true", "yes", "sim", "on")
                    if "live_min_confidence_pct" in smap:
                        live_min_confidence_pct = max(60, min(90, int(float(smap["live_min_confidence_pct"]))))
                    if "live_min_potential_score" in smap:
                        live_min_potential_score = max(60, min(95, int(float(smap["live_min_potential_score"]))))
                    if "live_min_risk_reward" in smap:
                        live_min_risk_reward = max(1.1, min(4.0, round(float(smap["live_min_risk_reward"]), 2)))
                    if "live_micro_compound_enabled" in smap:
                        live_micro_compound_enabled = smap["live_micro_compound_enabled"].lower() in ("1", "true", "yes", "sim", "on")
                    if "live_compound_alloc_pct" in smap:
                        live_compound_alloc_pct = max(20, min(65, int(float(smap["live_compound_alloc_pct"]))))
                    if "live_min_order_usdt" in smap:
                        live_min_order_usdt = max(5.0, min(50.0, round(float(smap["live_min_order_usdt"]), 2)))
                except (sqlite3.OperationalError, ValueError, TypeError):
                    pass
            if table_exists(conn, "model_promotions"):
                try:
                    pr_rows = conn.execute(
                        """
                        SELECT id, symbol, timeframe, model_version, model_type, accuracy,
                               precision_buy, profit_factor, promoted_by, notes, promoted_at
                        FROM model_promotions
                        ORDER BY id DESC LIMIT 15
                        """
                    ).fetchall()
                    promotions = [dict(r) for r in pr_rows]
                except sqlite3.OperationalError:
                    pass

    bin_cfg = binance_client.load_binance_config()
    readiness = _build_live_readiness(symbol, live_champ_meta)
    pending_approvals_count = 0
    try:
        pending_approvals_count = LiveOrderApprovalStore(db_path).count_pending()
    except Exception:
        pending_approvals_count = 0

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "demo": {
            "account": "demo",
            "label": "Conta Demo • Treinamento IA",
            "bot_online": bots["demo"]["running"],
            "bot_pid": bots["demo"]["pid"],
            "bot_state": demo_state,
            "trading_mode": (demo_state.get("trading_mode") if demo_state else "paper"),
            "simulated_capital_usd": round(1000.0 + demo_trades_all["total_pnl_usd"], 2),
            "xtb_simulated_capital_usd": round(DEFAULT_XTB_CAPITAL_USD + xtb_realized, 2),
            "trade_stats": demo_trades_pair,
            "trade_stats_all": demo_trades_all,
            "open_position": demo_open_pos,
            "active_model": demo_model,
            "log_file": "logs/trading_bot_demo.log",
        },
        "live": {
            "account": "live",
            "label": "Conta Real • Dinheiro Real",
            "bot_online": bots["live"]["running"],
            "bot_pid": bots["live"]["pid"],
            "bot_state": live_state,
            "trading_mode": "live",
            "has_keys": bin_cfg["has_keys"],
            "api_key_masked": bin_cfg["api_key_masked"],
            "is_production_keys": readiness["is_production_keys"],
            "trade_amount_usdt": bin_cfg["trade_amount_usdt"],
            "max_daily_loss_usdt": bin_cfg["max_daily_loss_usdt"],
            "max_trades_per_day": bin_cfg["max_trades_per_day"],
            "use_exchange_oco": bin_cfg["use_exchange_oco"],
            "trade_stats": live_trades_pair,
            "trade_stats_all": live_trades_all,
            "open_position": live_open_pos,
            "champion_model": live_model,
            "has_explicit_champion": live_champ_meta is not None,
            "has_newer_demo_model": has_newer_demo_model,
            "pending_approvals_count": pending_approvals_count,
            "readiness": readiness,
            "log_file": "logs/trading_bot_live.log",
        },
        "policy": {
            "auto_promote_to_live": auto_promote,
            "auto_evolve_both_accounts": auto_promote,
            "demo_always_on_training": demo_always_on,
            "live_high_potential_only": live_high_potential_only,
            "live_min_confidence_pct": live_min_confidence_pct,
            "live_min_potential_score": live_min_potential_score,
            "live_min_risk_reward": live_min_risk_reward,
            "live_micro_compound_enabled": live_micro_compound_enabled,
            "live_compound_alloc_pct": live_compound_alloc_pct,
            "live_min_order_usdt": live_min_order_usdt,
        },
        "available_versions": available_versions,
        "promotions": promotions,
    }


@router.get("/api/accounts/live-readiness")
def get_live_readiness(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
):
    models_dir = str(_srv_attr("MODELS_DIR", DEFAULT_MODELS_DIR))
    champ_meta = ModelPersistence(models_dir=models_dir).load_live_champion_metadata(symbol, timeframe)
    return _build_live_readiness(symbol, champ_meta)


@router.post("/api/accounts/promote-model", dependencies=[Depends(require_csrf)])
def promote_model_to_live(payload: PromoteModelPayload):
    """Promove o modelo mais treinado (ou versão específica) da Conta Demo para Campeão da Conta Real."""
    sym = (payload.symbol or "BTC/USDT").strip().upper()
    tf = (payload.timeframe or "1h").strip().lower()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"Símbolo inválido: '{sym}'.")
    if not _TF_RE.match(tf):
        raise HTTPException(status_code=400, detail=f"Timeframe inválido: '{tf}'.")

    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_srv_attr("MODELS_DIR", DEFAULT_MODELS_DIR))
    storage = ModelPersistence(models_dir=models_dir)
    try:
        champ_meta = storage.promote_to_live(
            symbol=sym,
            timeframe=tf,
            version=payload.version,
            promoted_by="manual",
            notes=(payload.notes or "").strip()[:240],
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    val = champ_meta.get("validation") if isinstance(champ_meta.get("validation"), dict) else {}
    db = TradingDatabase(db_path=db_path)
    promo_id = db.record_model_promotion(
        symbol=sym,
        timeframe=tf,
        model_version=int(champ_meta["version"]),
        model_type=str(champ_meta.get("model_type", "ensemble")),
        accuracy=float(champ_meta.get("accuracy", 0.0)),
        precision_buy=float(val.get("precision_buy", champ_meta.get("accuracy", 0.0))),
        profit_factor=float(val.get("profit_factor", 0.0)),
        promoted_by="manual",
        notes=(payload.notes or "Promovido da Conta Demo para Conta Real pelo operador").strip()[:240],
    )
    return {
        "status": "promoted",
        "promotion_id": promo_id,
        "champion": _format_model_card(champ_meta, is_explicit_champion=True),
        "message": (
            f"Modelo v{champ_meta['version']} ({sym} {tf}) promovido a Campeão da Conta Real! "
            f"Se o robô Real estiver online, ele recarregará o modelo automaticamente."
        ),
    }


@router.post("/api/accounts/policy", dependencies=[Depends(require_csrf)])
def update_account_policy(payload: AccountPolicyPayload):
    """Atualiza a política de auto-evolução, treino contínuo na Demo e filtro Sniper de alto potencial na Conta Real."""
    from dashboard.bot_controller import ensure_demo_training_bot_running, set_demo_manual_pause

    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_srv_attr("MODELS_DIR", DEFAULT_MODELS_DIR))
    db = TradingDatabase(db_path=db_path)
    val = "1" if payload.auto_promote_to_live else "0"
    db.set_account_setting("auto_promote_to_live", val)

    if payload.demo_always_on_training is not None:
        db.set_account_setting("demo_always_on_training", "1" if payload.demo_always_on_training else "0")
        if payload.demo_always_on_training:
            set_demo_manual_pause(False)
            try:
                ensure_demo_training_bot_running()
            except Exception:
                pass
    if payload.live_high_potential_only is not None:
        db.set_account_setting("live_high_potential_only", "1" if payload.live_high_potential_only else "0")
    if payload.live_min_confidence_pct is not None:
        conf_v = max(60, min(90, int(payload.live_min_confidence_pct)))
        db.set_account_setting("live_min_confidence_pct", str(conf_v))
    if payload.live_min_potential_score is not None:
        score_v = max(60, min(95, int(payload.live_min_potential_score)))
        db.set_account_setting("live_min_potential_score", str(score_v))
    if payload.live_micro_compound_enabled is not None:
        db.set_account_setting("live_micro_compound_enabled", "1" if payload.live_micro_compound_enabled else "0")
    if payload.live_compound_alloc_pct is not None:
        alloc_v = max(20, min(65, int(payload.live_compound_alloc_pct)))
        db.set_account_setting("live_compound_alloc_pct", str(alloc_v))

    synced_version = None
    if payload.auto_promote_to_live and payload.symbol and payload.timeframe:
        sym = payload.symbol.strip().upper()
        tf = payload.timeframe.strip().lower()
        if _SYMBOL_RE.match(sym) and _TF_RE.match(tf):
            storage = ModelPersistence(models_dir=models_dir)
            demo_meta = storage.load_metadata(sym, tf)
            live_meta = storage.load_live_champion_metadata(sym, tf)
            d_ver = int(demo_meta.get("version", 0)) if demo_meta else 0
            l_ver = int(live_meta.get("version", 0)) if live_meta else 0
            if d_ver > l_ver:
                try:
                    champ = storage.promote_to_live(
                        sym, tf, version=d_ver, promoted_by="auto",
                        notes="Sincronização imediata ao ativar auto-evolução nas duas contas",
                    )
                    v_m = champ.get("validation") if isinstance(champ.get("validation"), dict) else {}
                    db.record_model_promotion(
                        symbol=sym, timeframe=tf, model_version=d_ver,
                        model_type=str(champ.get("model_type", "ensemble")),
                        accuracy=float(champ.get("accuracy", 0.5)),
                        precision_buy=float(v_m.get("precision_buy", champ.get("accuracy", 0.5))),
                        profit_factor=float(v_m.get("profit_factor", 0.0)),
                        promoted_by="auto",
                        notes="Sincronização imediata ao ativar auto-evolução nas duas contas",
                    )
                    synced_version = d_ver
                except Exception:
                    synced_version = None

    demo_always = db.is_demo_always_on_enabled()
    live_pol = db.get_live_potential_policy()

    return {
        "status": "updated",
        "synced_version": synced_version,
        "policy": {
            "auto_promote_to_live": bool(payload.auto_promote_to_live),
            "auto_evolve_both_accounts": bool(payload.auto_promote_to_live),
            "demo_always_on_training": demo_always,
            **live_pol,
        },
        "message": (
            f"Política atualizada: Demo {'sempre treinando 24/7' if demo_always else 'sob demanda'} • "
            f"Conta Real {'Sniper (Só Alto Potencial: ≥' + str(live_pol['live_min_confidence_pct']) + '% IA / ' + str(live_pol['live_min_potential_score']) + ' pts)' if live_pol['live_high_potential_only'] else 'filtro padrão'}"
            + (f" • Modelo v{synced_version} sincronizado!" if synced_version else ".")
        ),
    }


@router.get("/api/live/pre-order-intel")
def get_live_pre_order_intel(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    refresh: bool = Query(default=False),
):
    """Pesquisa ao vivo de informações relevantes (Bolsas Globais S&P500/Nasdaq/VIX, Macro e Notícias) antes de uma ordem real."""
    from core import macro_intelligence

    return macro_intelligence.build_live_pre_order_intelligence(
        symbol=symbol,
        side="BUY",
        refresh_ttl_seconds=0.0 if refresh else 60.0,
    )


@router.get("/api/live/approvals")
def get_live_order_approvals(
    status: Optional[str] = Query(default=None, pattern="^(PENDING|EXECUTED|REJECTED|EXPIRED|FAILED)$"),
    limit: int = Query(default=30, ge=1, le=100),
):
    """Lista a fila de ordens reais aguardando aprovação do administrador e histórico recente."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    store = LiveOrderApprovalStore(db_path=db_path)
    items = store.list_proposals(status=status, limit=limit)
    pending_count = store.count_pending()
    return {
        "pending_count": pending_count,
        "items": items,
        "telegram": get_telegram_config(),
        "whatsapp": alerts.get_whatsapp_config(),
    }


@router.post("/api/live/approvals/{order_id}/approve", dependencies=[Depends(require_csrf)])
def approve_live_order(order_id: int, payload: Optional[LiveOrderDecisionPayload] = None):
    """Aprova uma proposta pendente e executa a ordem real na Binance Spot com proteção OCO."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    store = LiveOrderApprovalStore(db_path=db_path)
    note = (payload.note if payload else "").strip()
    try:
        executed = store.approve_and_execute(order_id=order_id, decided_by="admin_dashboard", note=note)
        return {
            "status": "executed",
            "order": executed,
            "message": f"Ordem real #{order_id} ({executed['symbol']}) aprovada e executada na Binance Spot!",
        }
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/live/approvals/{order_id}/reject", dependencies=[Depends(require_csrf)])
def reject_live_order(order_id: int, payload: Optional[LiveOrderDecisionPayload] = None):
    """Rejeita uma proposta pendente na Conta Real; a IA segue buscando novas oportunidades."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    store = LiveOrderApprovalStore(db_path=db_path)
    note = (payload.note if payload else "").strip()
    try:
        rejected = store.reject_proposal(order_id=order_id, decided_by="admin_dashboard", note=note)
        return {
            "status": "rejected",
            "order": rejected,
            "message": f"Ordem #{order_id} ({rejected['symbol']}) rejeitada. A IA continua buscando outras oportunidades.",
        }
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/live/approvals/poll-telegram", dependencies=[Depends(require_csrf)])
def poll_telegram_approvals():
    """Sincroniza comandos `/aprovar <id>` e `/rejeitar <id>` recebidos do administrador no Telegram."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    store = LiveOrderApprovalStore(db_path=db_path)
    actions = store.poll_telegram_admin_commands()
    return {
        "status": "polled",
        "processed_count": len(actions),
        "actions": actions,
    }
