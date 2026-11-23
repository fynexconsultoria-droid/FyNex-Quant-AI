"""
Chat Quantitativo da IA (FyNex Quant AI)
========================================
Assistente conversacional 100% local conectado diretamente ao banco SQLite
(`trading_data.db`), informando em tempo real:
- Evolução contínua da IA (nível, estágio, candles memorizados, ciclos de aprendizado, acurácia)
- Resultados reais em janelas móveis de 24 horas, 1 semana (7 dias) e 1 mês (30 dias)
  tanto na Binance (Cripto Paper/Live) quanto na Mesa/Copiloto XTB (Forex, Metais, Índices)
- Status de posições abertas, sinais recentes do Copiloto XTB e cenário macroeconômico.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from core import error_learning, forex_ai_trader, macro_intelligence, xtb_copilot


@contextmanager
def _connect_readonly(db_path: str) -> Iterator[Optional[sqlite3.Connection]]:
    if not db_path or not os.path.exists(db_path):
        yield None
        return
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _normalize_text(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").lower().strip())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def compute_period_metrics(
    db_path: str,
    hours: int,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Calcula métricas reais consolidadas para uma janela de `hours` horas (24h, 168h, 720h):
    - Trades Binance/IA (`simulated_trades`)
    - Trades Mesa XTB (`xtb_trades`)
    - Previsões da IA (`predictions`)
    - Sinais do Copiloto XTB (`xtb_copilot_signals`)
    """
    ref = now_utc or datetime.now(timezone.utc)
    cutoff_str = (ref - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    out: Dict[str, Any] = {
        "hours": hours,
        "crypto_trades": 0,
        "crypto_wins": 0,
        "crypto_losses": 0,
        "crypto_pnl_usd": 0.0,
        "crypto_pnl_pct": 0.0,
        "xtb_trades": 0,
        "xtb_wins": 0,
        "xtb_losses": 0,
        "xtb_pnl_usd": 0.0,
        "total_trades": 0,
        "total_wins": 0,
        "total_losses": 0,
        "win_rate_pct": 0.0,
        "total_pnl_usd": 0.0,
        "predictions_total": 0,
        "predictions_evaluated": 0,
        "predictions_correct": 0,
        "accuracy_pct": 0.0,
        "copilot_signals": 0,
    }

    with _connect_readonly(db_path) as conn:
        if conn is None:
            return out

        # 1. Trades do Robô IA (Binance Paper/Live)
        if _table_exists(conn, "simulated_trades"):
            clauses = ["exit_time >= ?"]
            params: List[Any] = [cutoff_str]
            if symbol:
                clauses.append("symbol = ?")
                params.append(symbol)
            if timeframe:
                clauses.append("timeframe = ?")
                params.append(timeframe)
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS n,
                    COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END), 0) AS losses,
                    COALESCE(SUM(pnl_usd), 0.0) AS pnl_usd,
                    COALESCE(SUM(pnl_pct), 0.0) AS pnl_pct
                FROM simulated_trades
                WHERE {' AND '.join(clauses)}
                """,
                tuple(params),
            ).fetchone()
            if row:
                out["crypto_trades"] = int(row["n"] or 0)
                out["crypto_wins"] = int(row["wins"] or 0)
                out["crypto_losses"] = int(row["losses"] or 0)
                out["crypto_pnl_usd"] = round(float(row["pnl_usd"] or 0.0), 2)
                out["crypto_pnl_pct"] = round(float(row["pnl_pct"] or 0.0), 2)

        # 2. Trades da Mesa XTB (Forex, Ouro, Prata, Petróleo, Índices)
        if _table_exists(conn, "xtb_trades"):
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS n,
                    COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END), 0) AS losses,
                    COALESCE(SUM(pnl_usd), 0.0) AS pnl_usd
                FROM xtb_trades
                WHERE closed_at >= ?
                """,
                (cutoff_str,),
            ).fetchone()
            if row:
                out["xtb_trades"] = int(row["n"] or 0)
                out["xtb_wins"] = int(row["wins"] or 0)
                out["xtb_losses"] = int(row["losses"] or 0)
                out["xtb_pnl_usd"] = round(float(row["pnl_usd"] or 0.0), 2)

        # 3. Previsões da IA no período
        if _table_exists(conn, "predictions"):
            clauses = ["predicted_at >= ?"]
            params = [cutoff_str]
            if symbol:
                clauses.append("symbol = ?")
                params.append(symbol)
            if timeframe:
                clauses.append("timeframe = ?")
                params.append(timeframe)
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN is_correct IS NOT NULL THEN 1 ELSE 0 END), 0) AS eval_n,
                    COALESCE(SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END), 0) AS hits
                FROM predictions
                WHERE {' AND '.join(clauses)}
                """,
                tuple(params),
            ).fetchone()
            if row:
                out["predictions_total"] = int(row["total"] or 0)
                out["predictions_evaluated"] = int(row["eval_n"] or 0)
                out["predictions_correct"] = int(row["hits"] or 0)
                if out["predictions_evaluated"] > 0:
                    out["accuracy_pct"] = round(
                        out["predictions_correct"] / out["predictions_evaluated"] * 100.0, 1
                    )

        # 4. Sinais do Copiloto XTB gerados no período
        if _table_exists(conn, "xtb_copilot_signals"):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM xtb_copilot_signals WHERE created_at >= ?",
                (cutoff_str,),
            ).fetchone()
            if row:
                out["copilot_signals"] = int(row["n"] or 0)

    out["total_trades"] = out["crypto_trades"] + out["xtb_trades"]
    out["total_wins"] = out["crypto_wins"] + out["xtb_wins"]
    out["total_losses"] = out["crypto_losses"] + out["xtb_losses"]
    out["total_pnl_usd"] = round(out["crypto_pnl_usd"] + out["xtb_pnl_usd"], 2)
    if out["total_trades"] > 0:
        out["win_rate_pct"] = round(out["total_wins"] / out["total_trades"] * 100.0, 1)

    return out


def get_performance_summary(
    db_path: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Retorna o pacote completo de evolução + resultados em 24h, 7 dias e 30 dias."""
    p24 = compute_period_metrics(db_path, hours=24, symbol=symbol, timeframe=timeframe, now_utc=now_utc)
    p7d = compute_period_metrics(db_path, hours=24 * 7, symbol=symbol, timeframe=timeframe, now_utc=now_utc)
    p30d = compute_period_metrics(db_path, hours=24 * 30, symbol=symbol, timeframe=timeframe, now_utc=now_utc)

    evo = macro_intelligence.compute_ai_evolution_progress(
        db_path=db_path,
        symbol=symbol,
        timeframe=timeframe,
        macro_snapshot=macro_intelligence.get_cached_macro_snapshot(),
    )

    bot_state: Dict[str, Any] = {}
    open_xtb_count = 0
    with _connect_readonly(db_path) as conn:
        if conn is not None:
            if _table_exists(conn, "bot_state"):
                row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
                if row:
                    bot_state = dict(row)
            if _table_exists(conn, "xtb_positions"):
                r_pos = conn.execute("SELECT COUNT(*) AS n FROM xtb_positions").fetchone()
                open_xtb_count = int(r_pos["n"] or 0) if r_pos else 0

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "periods": {
            "24h": p24,
            "7d": p7d,
            "30d": p30d,
        },
        "evolution": evo,
        "bot_state": bot_state,
        "open_xtb_positions": open_xtb_count,
        "generated_at": (now_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def _fmt_usd(val: float) -> str:
    sign = "+" if val >= 0 else "-"
    return f"{sign}${abs(val):,.2f}"


def _format_period_block(label: str, m: Dict[str, Any]) -> str:
    lines = [f"📊 **{label}**"]
    if m["total_trades"] > 0:
        lines.append(
            f"• Operações: **{m['total_trades']}** ({m['total_wins']}V / {m['total_losses']}D • "
            f"Win Rate: **{m['win_rate_pct']:.1f}%**) | Resultado: **{_fmt_usd(m['total_pnl_usd'])}**"
        )
        if m["crypto_trades"] > 0 or m["xtb_trades"] > 0:
            lines.append(
                f"  ↳ Cripto IA: {m['crypto_trades']} ({_fmt_usd(m['crypto_pnl_usd'])}) | "
                f"Mesa XTB: {m['xtb_trades']} ({_fmt_usd(m['xtb_pnl_usd'])})"
            )
    else:
        lines.append("• Operações encerradas: **0** (IA em postura conservadora aguardando entradas de alta convicção).")

    if m["predictions_evaluated"] > 0:
        lines.append(
            f"• Previsões IA: **{m['predictions_correct']}/{m['predictions_evaluated']} acertos** "
            f"(Acurácia: **{m['accuracy_pct']:.1f}%**) | Total geradas: {m['predictions_total']}"
        )
    else:
        lines.append(f"• Previsões IA geradas no período: **{m['predictions_total']}** (aguardando fechamento para avaliação).")

    if m["copilot_signals"] > 0:
        lines.append(f"• Sinais Copiloto XTB (xStation 5): **{m['copilot_signals']}** emitido(s).")
    return "\n".join(lines)


def answer_ai_chat(
    message: str,
    db_path: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Processa a pergunta do usuário e retorna uma resposta em PT-BR baseada nos dados reais da IA."""
    norm = _normalize_text(message)
    summary = get_performance_summary(db_path=db_path, symbol=symbol, timeframe=timeframe, now_utc=now_utc)
    p24 = summary["periods"]["24h"]
    p7d = summary["periods"]["7d"]
    p30d = summary["periods"]["30d"]
    evo = summary["evolution"]
    pillars = evo.get("pillars") or {}
    bs = summary["bot_state"]

    # Detecta intenção da pergunta com fronteiras de palavra e prioridade para temas específicos
    if any(re.search(rf"\b{p}\b", norm) for p in ("erro", "erros", "errou", "falha", "falhou", "licao", "licoes", "autopsia", "postmortem")):
        intent = "error_lessons"
        rep = (
            error_learning.ErrorLearningStore(db_path).get_error_learning_report(
                symbol=symbol, timeframe=timeframe, limit=4, now_utc=now_utc
            )
            if os.path.exists(db_path)
            else {"total_errors_14d": 0, "top_causes": [], "active_rules": [], "recent_errors": [], "sl_atr_boost": 1.0}
        )
        if rep["total_errors_14d"] == 0 and not rep["recent_errors"]:
            reply = (
                f"🔬 **Auto-Análise de Erros ({symbol} • {timeframe})**\n\n"
                f"Nenhum erro registrado nos últimos 14 dias. Assim que ocorrer qualquer previsão de alta incorreta "
                f"ou operação fechada no prejuízo, eu analiso automaticamente todos os indicadores técnicos, "
                f"contexto macro, volume e Stop Loss para ajustar meus filtros e reforçar o próximo retreino."
            )
        else:
            causes_lines = [
                f"  • **{c['label']}** ({c['count']}x) — _{c['advice']}_"
                for c in rep["top_causes"][:3]
            ]
            rules_lines = [
                f"  • **{r['label']}** → `{r['action']}` ({r['count']}x em 14d)"
                for r in rep["active_rules"][:4]
            ]
            last_err = rep["recent_errors"][0] if rep["recent_errors"] else None
            last_block = (
                f"\n• Último erro analisado ({last_err['source']}): **{last_err['primary_label']}**\n  ↳ _{last_err['lesson_text']}_"
                if last_err
                else ""
            )
            reply = (
                f"🔬 **Auto-Análise de Erros & Adaptação ({symbol} • {timeframe})**\n\n"
                f"• Erros analisados (14 dias): **{rep['total_errors_14d']}** | Ajuste SL/ATR: **{rep['sl_atr_boost']:.2f}x**\n"
                f"• Principais parâmetros causadores de erro:\n"
                + ("\n".join(causes_lines) if causes_lines else "  • Nenhum padrão recorrente dominante.")
                + "\n• Travas adaptativas ativas agora:\n"
                + ("\n".join(rules_lines) if rules_lines else "  • Nenhuma trava extra ativa (monitorando).")
                + last_block
            )
    elif any(re.search(rf"\b{p}\b", norm) for p in ("xtb", "forex", "ouro", "prata", "sinal", "sinais", "posicao", "posicoes", "aberta", "mesa")):
        intent = "xtb_signals"
        is_open, fx_msg = forex_ai_trader.is_forex_market_open(now_utc=now_utc)
        sigs = xtb_copilot.XTBCopilotStore(db_path=db_path).list_signals(limit=3) if os.path.exists(db_path) else []
        in_crypto_pos = bool(bs.get("in_position"))
        crypto_pos_str = (
            f"Sim, posicionada em {bs.get('symbol', symbol)} @ ${float(bs.get('entry_price') or 0):,.2f}"
            if in_crypto_pos
            else "Nenhuma posição cripto aberta (aguardando entrada limpa)."
        )
        sig_lines = []
        for s in sigs:
            sig_lines.append(
                f"  • **{s['xtb_symbol']}** {s['side']} @ {s['entry_price']} (SL {s['sl_price']} | TP {s['tp_price']} | Score {s['score']:.0f}/100)"
            )
        sigs_block = "\n".join(sig_lines) if sig_lines else "  • Nenhum sinal XTB registrado ainda."
        reply = (
            f"🧭 **Status Modo Híbrido (Binance + Copiloto XTB)**\n\n"
            f"• Mercado Global (Forex/Metais/Índices): **{'ABERTO 🟢' if is_open else 'FECHADO ⏸️'}** — {fx_msg}\n"
            f"• Posição Cripto (Binance): {crypto_pos_str}\n"
            f"• Posições abertas na Mesa XTB: **{summary['open_xtb_positions']}**\n"
            f"• Últimos sinais prontos para xStation 5:\n{sigs_block}"
        )
    elif any(re.search(rf"\b{p}\b", norm) for p in ("macro", "dolar", "dxy", "juros", "fed", "petroleo", "guerra", "clima", "noticia", "noticias")):
        intent = "macro"
        try:
            m = macro_intelligence.fetch_macro_snapshot()
            reply = (
                f"🌎 **Cenário Macro & Radar Global Agora**\n\n"
                f"• Regime: **{m.get('badge', '⚖️')} {m.get('regime', 'MACRO NEUTRO')}** (Score: **{float(m.get('macro_score', 0)):+.1f}**)\n"
                f"• Dólar (DXY): **{float(m.get('dxy_price', 0)):.2f}** ({float(m.get('dxy_change_pct', 0)):+.2f}%) | "
                f"Juros EUA 10Y: **{float(m.get('us10y_yield', 0)):.2f}%**\n"
                f"• S&P 500: **{float(m.get('sp500_price', 0)):,.0f}** ({float(m.get('sp500_change_pct', 0)):+.2f}%) | "
                f"Fear & Greed: **{m.get('fear_greed_value', '--')}/100**\n"
                f"• Commodities: Ouro **${float(m.get('gold_price', 0)):,.0f}** | Petróleo **${float(m.get('oil_price', 0)):.2f}** | "
                f"Terras Raras **${float(m.get('rare_earth_price', 0)):.2f}**\n"
                f"• Ajuste automático no filtro de entrada: **{round(float(m.get('confidence_adjustment', 0)) * 100):+d}%**"
            )
        except Exception:
            reply = "🌎 Não consegui atualizar o feed macro externo neste instante, mas o monitoramento continua ativo em cache."
    elif any(re.search(p, norm) for p in (r"\b24h\b", r"24 hora", r"\bhoje\b", r"ultimo dia", r"ultimas 24", r"\bdiario\b")):
        intent = "report_24h"
        reply = (
            f"Aqui está o meu relatório das **últimas 24 horas** ({symbol} • {timeframe}):\n\n"
            f"{_format_period_block('Últimas 24 Horas', p24)}\n\n"
            f"🧠 Evolução atual: **Nível {evo.get('level', 1)} ({evo.get('progress_pct', 0):.0f}%)** — {evo.get('adaptation_status', 'Sincronizada')}."
        )
    elif any(re.search(p, norm) for p in (r"\bsemana\b", r"7 dia", r"\b7d\b", r"\bsemanal\b")):
        intent = "report_7d"
        reply = (
            f"Aqui está o meu desempenho na **última semana (7 dias)** ({symbol} • {timeframe}):\n\n"
            f"{_format_period_block('Últimos 7 Dias', p7d)}\n\n"
            f"Comparativo rápido: nas últimas 24h fiz **{p24['total_trades']}** operação(ões) ({_fmt_usd(p24['total_pnl_usd'])})."
        )
    elif any(re.search(p, norm) for p in (r"\bmes\b", r"30 dia", r"\b30d\b", r"\bmensal\b")):
        intent = "report_30d"
        reply = (
            f"Aqui está o meu balanço do **último mês (30 dias)** ({symbol} • {timeframe}):\n\n"
            f"{_format_period_block('Últimos 30 Dias', p30d)}\n\n"
            f"📈 Progresso geral: **Nível {evo.get('level', 1)} • {evo.get('stage_name', '')}** ({evo.get('progress_pct', 0):.0f}%)."
        )
    elif any(k in norm for k in ("evolu", "nivel", "treino", "aprend", "progresso", "modelo", "acuracia")):
        intent = "evolution"
        calib_mode = bs.get("calibration_mode") or "CALIBRAÇÃO INICIAL"
        active_conf = float(bs.get("active_confidence") or 0.65) * 100.0
        reply = (
            f"🧠 **Minha Evolução Atual — Nível {evo.get('level', 1)} • {evo.get('stage_name', '')} ({evo.get('progress_pct', 0):.0f}%)**\n\n"
            f"• Status: **{evo.get('adaptation_status', '🟢 Sincronizada')}**\n"
            f"• Memória de mercado ({timeframe}): **{pillars.get('candles_count', 0):,} / {pillars.get('candles_target', 3000):,} candles**\n"
            f"• Contextos Macro & Notícias salvos: **{pillars.get('macro_snapshots', 0)} / {pillars.get('macro_target', 100)}**\n"
            f"• Ciclos de aprendizado: **{pillars.get('learning_cycles', 0)}** (Modelo **v{pillars.get('model_version', 1)}**)\n"
            f"• Regime de calibração: **{calib_mode}** (filtro de entrada em **{active_conf:.0f}%**)\n"
            f"• Próximo marco: _{evo.get('next_milestone', 'Evolução contínua')}_"
        )
    else:
        intent = "overview"
        reply = (
            f"👋 **Resumo Executivo da FyNex AI ({symbol} • {timeframe})**\n\n"
            f"🧠 **Evolução:** Nível **{evo.get('level', 1)} • {evo.get('stage_name', '')}** "
            f"(**{evo.get('progress_pct', 0):.0f}%**) — {pillars.get('candles_count', 0):,} candles na memória, "
            f"{pillars.get('learning_cycles', 0)} ciclo(s) de treino (Modelo v{pillars.get('model_version', 1)}).\n\n"
            f"{_format_period_block('24 Horas', p24)}\n\n"
            f"{_format_period_block('1 Semana (7 dias)', p7d)}\n\n"
            f"{_format_period_block('1 Mês (30 dias)', p30d)}\n\n"
            f"Você pode me perguntar sobre **24h**, **semana**, **mês**, **evolução**, **sinais XTB** ou **cenário macro**!"
        )

    return {
        "intent": intent,
        "reply": reply,
        "timestamp": summary["generated_at"],
        "summary": summary,
    }
