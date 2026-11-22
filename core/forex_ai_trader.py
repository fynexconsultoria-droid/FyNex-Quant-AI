"""
Operador Autônomo de Pares de Moedas (Forex) com Confluência Técnica + Macro (FyNex Quant AI)
=============================================================================================
- Verifica automaticamente o horário real de funcionamento do mercado Forex
  (Domingo 22:00 UTC até Sexta 22:00 UTC, evitando rollover de 30 min)
- Analisa múltiplos pares (EUR/USD, GBP/USD, USD/JPY, USD/BRL, AUD/USD, USD/CHF, XAU/USD)
  em velas de 1h combinando indicadores técnicos + influência do Dólar (DXY) + Juros EUA + Notícias
- Aplica travas conservadoras (anti-lateralização, anti-topo/fundo, anti-volatilidade extrema)
- Seleciona apenas as MELHORES oportunidades (score >= 72/100) e executa BUY/SELL na mesa XTB
  com SL/TP dinâmicos baseados no ATR e limite máximo de posições simultâneas.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core import forex_market
from core.ml import build_features

logger = logging.getLogger("QuantTradingBot")

FOREX_AI_PAIRS: Tuple[str, ...] = (
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/BRL",
    "AUD/USD",
    "USD/CHF",
    "XAU/USD",
)


def is_forex_market_open(now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Verifica se o mercado global de Forex está aberto (horário UTC):
    - Abre: Domingo 22:30 UTC (evita os primeiros 30 min de spread de abertura)
    - Fecha: Sexta 21:30 UTC (evita os últimos 30 min de spread de fechamento)
    - Sábado: sempre fechado.
    """
    dt = now_utc or datetime.now(timezone.utc)
    wd = dt.weekday()  # 0=Seg ... 4=Sex, 5=Sáb, 6=Dom
    mins = dt.hour * 60 + dt.minute

    if wd == 5:
        return False, "Mercado Forex fechado (Sábado — abre Domingo 19h30 BRT)."
    if wd == 6 and mins < (22 * 60 + 30):
        return False, "Mercado Forex fechado (Domingo — abre às 22:30 UTC / 19:30 BRT)."
    if wd == 4 and mins >= (21 * 60 + 30):
        return False, "Mercado Forex fechado (encerramento de Sexta-feira)."
    return True, "Mercado Forex ABERTO (liquidez ativa)."


def evaluate_forex_opportunity(
    symbol: str,
    df: pd.DataFrame,
    macro_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Avalia um par Forex em velas de 1h e retorna um dicionário de oportunidade
    com `side` ('BUY' | 'SELL' | 'NEUTRAL'), `score` (0..100), `atr_pct` e motivos.
    """
    if df.empty or len(df) < 35:
        return None

    feat_df, _ = build_features(df)
    if feat_df.empty:
        return None
    row = feat_df.iloc[-1]

    rsi = float(row.get("rsi_14", 50.0) or 50.0)
    macd_diff = float(row.get("macd_diff", 0.0) or 0.0)
    ema_ratio = float(row.get("ema_trend_ratio", 0.0) or 0.0)
    sma_ratio = float(row.get("sma_ratio", 0.0) or 0.0)
    adx = float(row.get("adx_14", 0.0) or 0.0)
    bb_pos = float(row.get("bb_pos", 0.5) or 0.5)
    bb_width = float(row.get("bb_width", 0.01) or 0.01)
    atr_pct = float(row.get("atr_pct", 0.003) or 0.003)
    close_price = float(row.get("close", 0.0) or 0.0)

    if close_price <= 0:
        return None

    # Travas conservadoras: sem lateralização morta nem volatilidade extrema
    if adx < 0.16 and bb_width < 0.004:
        return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Lateralizado (ADX/BB baixos)"}
    if atr_pct > 0.020:
        return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": f"Volatilidade extrema (ATR {atr_pct*100:.2f}%)"}

    buy_pts = 0.0
    sell_pts = 0.0
    reasons: List[str] = []

    # 1. Tendência EMA 12/26 + SMA 9/21 (até 40 pts)
    if ema_ratio > 0.0005:
        buy_pts += 22.0
        reasons.append("EMA alta")
    elif ema_ratio < -0.0005:
        sell_pts += 22.0
        reasons.append("EMA baixa")

    if sma_ratio > 0.0003:
        buy_pts += 18.0
    elif sma_ratio < -0.0003:
        sell_pts += 18.0

    # 2. Momentum MACD + RSI saudável (até 30 pts)
    if macd_diff > 0:
        buy_pts += 16.0
    elif macd_diff < 0:
        sell_pts += 16.0

    if 46.0 <= rsi <= 66.0:
        buy_pts += 14.0
    elif 34.0 <= rsi <= 54.0:
        sell_pts += 14.0

    # 3. Força de tendência ADX (até 15 pts)
    if adx >= 0.22:
        buy_pts += 15.0 if buy_pts >= sell_pts else 0.0
        sell_pts += 15.0 if sell_pts > buy_pts else 0.0
        reasons.append(f"ADX {adx*100:.0f}")

    # 4. Influência do Dólar (DXY) e Juros EUA (até 15 pts)
    base, _, quote = symbol.upper().partition("/")
    if macro_snapshot:
        dxy_chg = float(macro_snapshot.get("dxy_change_pct") or 0.0)
        if quote == "USD":
            # DXY caindo favorece EUR/USD, GBP/USD, AUD/USD, XAU/USD
            if dxy_chg <= -0.15:
                buy_pts += 15.0
                reasons.append(f"DXY {dxy_chg:+.2f}%")
            elif dxy_chg >= 0.15:
                sell_pts += 15.0
                reasons.append(f"DXY {dxy_chg:+.2f}%")
        elif base == "USD":
            # DXY subindo favorece USD/JPY, USD/BRL, USD/CHF
            if dxy_chg >= 0.15:
                buy_pts += 15.0
                reasons.append(f"DXY {dxy_chg:+.2f}%")
            elif dxy_chg <= -0.15:
                sell_pts += 15.0
                reasons.append(f"DXY {dxy_chg:+.2f}%")

    # Bloqueio anti-topo / anti-fundo (Bollinger + RSI)
    if buy_pts >= sell_pts:
        if rsi > 68.0 or bb_pos > 0.92:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Sobrecomprado (anti-topo)"}
        side = "BUY"
        score = round(min(100.0, buy_pts), 1)
    else:
        if rsi < 32.0 or bb_pos < 0.08:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Sobrevendido (anti-fundo)"}
        side = "SELL"
        score = round(min(100.0, sell_pts), 1)

    return {
        "symbol": symbol,
        "side": side,
        "score": score,
        "close": close_price,
        "atr_pct": atr_pct,
        "rsi_14": round(rsi, 1),
        "adx_14": round(adx * 100.0, 1),
        "reason": ", ".join(reasons) or "Confluência técnica",
    }


class ForexAITrader:
    """
    Escaneia os principais pares de moedas quando o mercado Forex está aberto,
    escolhe a melhor oportunidade acima do limiar mínimo de convicção (`min_score=72`)
    e executa na mesa XTB (`XTBDeskStore`) com SL/TP dinâmicos pelo ATR.
    """

    def __init__(
        self,
        db_path: str,
        pairs: Tuple[str, ...] = FOREX_AI_PAIRS,
        min_score: float = 72.0,
        max_open_forex_positions: int = 2,
        leverage: int = 20,
    ):
        self.db_path = db_path
        self.pairs = pairs
        self.min_score = min_score
        self.max_open_forex_positions = max_open_forex_positions
        self.leverage = leverage
        self.store = forex_market.XTBDeskStore(db_path=db_path)
        self.last_scan_report: Dict[str, Any] = {
            "market_open": False,
            "status_message": "Aguardando primeiro ciclo",
            "opportunities": [],
            "last_action": None,
        }

    def run_cycle(
        self,
        macro_snapshot: Optional[Dict[str, Any]] = None,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        is_open, open_msg = is_forex_market_open(now_utc=now_utc)
        if not is_open:
            self.last_scan_report = {
                "market_open": False,
                "status_message": open_msg,
                "opportunities": [],
                "last_action": None,
            }
            logger.info(f"💱 [FOREX IA] {open_msg}")
            return self.last_scan_report

        # 1. Atualiza posições existentes (dispara SL/TP automaticamente se atingido)
        snap = self.store.get_account_snapshot()
        open_forex = [p for p in snap.get("open_positions", []) if p.get("category") == "forex"]
        open_syms = {p["symbol"] for p in open_forex}

        opportunities: List[Dict[str, Any]] = []
        for sym in self.pairs:
            try:
                raw_candles = forex_market.fetch_forex_candles(sym, timeframe="1h", limit=80)
                if not raw_candles or len(raw_candles) < 35:
                    continue
                df = pd.DataFrame(raw_candles)
                df["timestamp"] = pd.to_datetime(df["time"], unit="s")
                opp = evaluate_forex_opportunity(sym, df, macro_snapshot=macro_snapshot)
                if opp is not None:
                    opportunities.append(opp)
            except Exception as e:
                logger.debug(f"[FOREX IA] Falha ao analisar {sym}: {e}")
                continue

        opportunities.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        last_action = None

        # 2. Se há vaga e existe oportunidade forte (>= min_score), abre na MELHOR!
        if len(open_forex) < self.max_open_forex_positions:
            for best in opportunities:
                if best["side"] not in ("BUY", "SELL"):
                    continue
                if best["score"] < self.min_score:
                    break
                if best["symbol"] in open_syms:
                    continue

                sym = best["symbol"]
                inst = forex_market.MARKET_INSTRUMENTS.get(sym, {})
                decimals = int(inst.get("decimals", 5))
                atr_pct = float(best.get("atr_pct", 0.003))
                # TP/SL razoáveis adaptados ao par Forex (mínimo 0.35% TP / 0.25% SL)
                tp_pct = min(max(atr_pct * 2.0, 0.0035), 0.0180)
                sl_pct = min(max(atr_pct * 1.3, 0.0025), 0.0110)

                try:
                    q = forex_market.fetch_forex_quote(sym)
                    ref_price = float(q["ask"] if best["side"] == "BUY" else q["bid"])
                    if best["side"] == "BUY":
                        tp_price = round(ref_price * (1.0 + tp_pct), decimals)
                        sl_price = round(ref_price * (1.0 - sl_pct), decimals)
                    else:
                        tp_price = round(ref_price * (1.0 - tp_pct), decimals)
                        sl_price = round(ref_price * (1.0 + sl_pct), decimals)

                    # Lote conservador (metade do padrão em volatilidade maior)
                    base_lot = float(inst.get("default_lot", 0.05))
                    safe_lot = round(base_lot * (0.5 if atr_pct > 0.008 else 1.0), 3)
                    safe_lot = max(float(inst.get("min_lot", 0.01)), safe_lot)

                    pos = self.store.open_position(
                        symbol=sym,
                        side=best["side"],
                        lots=safe_lot,
                        leverage=min(self.leverage, int(inst.get("max_leverage", 30))),
                        sl_price=sl_price,
                        tp_price=tp_price,
                        quote_override=q,
                    )
                    last_action = (
                        f"Ordem {pos['side']} {pos['lots']} lote(s) em {sym} @ {pos['entry_price']} "
                        f"(Score {best['score']:.0f}/100 | TP {tp_price} | SL {sl_price})"
                    )
                    logger.info(f"💱🚀 [FOREX IA ENTRADA] {last_action} | Motivos: {best['reason']}")
                    break
                except Exception as e:
                    logger.warning(f"💱 [FOREX IA] Não foi possível abrir {sym}: {e}")

        self.last_scan_report = {
            "market_open": True,
            "status_message": open_msg,
            "open_forex_positions": len(open_forex),
            "opportunities": opportunities[:7],
            "last_action": last_action,
        }
        return self.last_scan_report
