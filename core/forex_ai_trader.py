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

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from ta.trend import EMAIndicator

import alerts
from core import forex_market, xtb_copilot
from core.database import TradingDatabase
from core.infra.utils import parse_utc_str
from core.logging_setup import DEFAULT_MODELS_DIR
from core.ml import ModelPersistence, build_features, predict_prob_up

logger = logging.getLogger("QuantTradingBot")
_FOREX_CYCLE_LOCK = threading.Lock()

FOREX_AI_PAIRS: Tuple[str, ...] = (
    # Moedas (Forex)
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/BRL",
    "AUD/USD",
    "USD/CHF",
    # Metais Preciosos, Energia, Terras Raras & Índices
    "XAU/USD",
    "XAG/USD",
    "OIL/USD",
    "COPPER/USD",
    "US500/USD",
)


def _us_eastern_dt(dt_utc: datetime) -> datetime:
    """Converte um datetime UTC para horário de Nova York (EST UTC-5 / EDT UTC-4) sem depender de tzdata."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)

    year = dt_utc.year
    # 2º domingo de março às 07:00 UTC (02:00 EST -> 03:00 EDT)
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    mar_first_sun = 1 + (6 - mar1.weekday()) % 7
    dst_start_utc = datetime(year, 3, mar_first_sun + 7, 7, 0, tzinfo=timezone.utc)

    # 1º domingo de novembro às 06:00 UTC (02:00 EDT -> 01:00 EST)
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    nov_first_sun = 1 + (6 - nov1.weekday()) % 7
    dst_end_utc = datetime(year, 11, nov_first_sun, 6, 0, tzinfo=timezone.utc)

    offset_hours = -4 if (dst_start_utc <= dt_utc < dst_end_utc) else -5
    return dt_utc + timedelta(hours=offset_hours)


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def is_forex_market_open(now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Verifica se o mercado global de Forex está aberto (horário de Nova York com DST automático):
    - Abre: Domingo 17:30 ET (30 min após abertura de 17:00 ET, evitando spread inicial)
    - Fecha: Sexta 16:30 ET (30 min antes do fechamento de 17:00 ET)
    - Rollover diário (Seg–Qui 16:55–17:30 ET): pausado por baixa liquidez
    - Sábado: sempre fechado.
    """
    dt_et = _us_eastern_dt(now_utc or datetime.now(timezone.utc))
    wd = dt_et.weekday()  # 0=Seg ... 4=Sex, 5=Sáb, 6=Dom
    mins = dt_et.hour * 60 + dt_et.minute

    if wd == 5:
        return False, "Mercado Forex fechado (Sábado — abre Domingo 17:30 NY)."
    if wd == 6 and mins < (17 * 60 + 30):
        return False, "Mercado Forex fechado (Domingo — abre às 17:30 horário de NY)."
    if wd == 4 and mins >= (16 * 60 + 30):
        return False, "Mercado Forex fechado (encerramento de Sexta-feira em NY)."
    if wd in (0, 1, 2, 3) and (16 * 60 + 55) <= mins < (17 * 60 + 30):
        return False, "Mercado Forex em janela de rollover diário (16:55–17:30 NY)."
    return True, "Mercado Forex ABERTO (liquidez ativa)."


_USD_INVERSE_BASES = {"EUR", "GBP", "AUD", "NZD", "XAU", "XAG"}


def _usd_exposure_direction(symbol: str, side: str) -> Optional[str]:
    """Classifica exposição líquida ao Dólar ('LONG_USD' | 'SHORT_USD') para evitar apostas duplicadas."""
    sym = (symbol or "").upper().strip()
    side_u = (side or "").upper().strip()
    if side_u not in ("BUY", "SELL"):
        return None
    base, _, quote = sym.partition("/")
    if base == "USD":
        return "LONG_USD" if side_u == "BUY" else "SHORT_USD"
    if quote == "USD" and base in _USD_INVERSE_BASES:
        return "SHORT_USD" if side_u == "BUY" else "LONG_USD"
    return None


def evaluate_forex_opportunity(
    symbol: str,
    df: pd.DataFrame,
    macro_snapshot: Optional[Dict[str, Any]] = None,
    ml_model: Optional[Any] = None,
    ml_feature_cols: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Avalia um par Forex/Commodity/Índice em velas fechadas de 1h combinando
    confluência técnica, EMA 50, Dólar (DXY) e (quando treinado) modelo Machine Learning local.
    """
    if df.empty or len(df) < 35:
        return None

    feat_df, default_fcols = build_features(df)
    if feat_df.empty:
        return None
    row = feat_df.iloc[-1]

    rsi = _safe_float(row.get("rsi_14"))
    macd_diff = _safe_float(row.get("macd_diff"), 0.0) or 0.0
    ema_ratio = _safe_float(row.get("ema_trend_ratio"), 0.0) or 0.0
    sma_ratio = _safe_float(row.get("sma_ratio"), 0.0) or 0.0
    adx = _safe_float(row.get("adx_14"), 0.0) or 0.0
    bb_pos = _safe_float(row.get("bb_pos"))
    bb_width = _safe_float(row.get("bb_width"), 0.01) or 0.01
    atr_pct = _safe_float(row.get("atr_pct"))
    ret_3 = _safe_float(row.get("return_3"), 0.0) or 0.0
    close_price = _safe_float(row.get("close"), 0.0) or 0.0

    ema_50: Optional[float] = None
    if len(df) >= 50:
        try:
            ema50_val = EMAIndicator(close=df["close"], window=50).ema_indicator().iloc[-1]
            ema_50 = _safe_float(ema50_val)
        except Exception:
            ema_50 = None

    if close_price <= 0 or rsi is None or bb_pos is None or atr_pct is None or atr_pct <= 0:
        return None

    # Travas conservadoras: sem lateralização morta nem volatilidade extrema
    if adx < 0.18 and bb_width < 0.005:
        return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Lateralizado (ADX/BB baixos)"}
    if atr_pct > 0.020:
        return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": f"Volatilidade extrema (ATR {atr_pct*100:.2f}%)"}

    buy_pts = 0.0
    sell_pts = 0.0
    buy_reasons: List[str] = []
    sell_reasons: List[str] = []

    # 1. Tendência EMA 12/26 + SMA 9/21 (até 40 pts)
    if ema_ratio > 0.0005:
        buy_pts += 22.0
        buy_reasons.append("EMA alta")
    elif ema_ratio < -0.0005:
        sell_pts += 22.0
        sell_reasons.append("EMA baixa")

    if sma_ratio > 0.0003:
        buy_pts += 18.0
    elif sma_ratio < -0.0003:
        sell_pts += 18.0

    # 1b. Tendência Macro (EMA 50): favorece operações a favor da tendência principal e penaliza contra-tendência
    if ema_50 is not None and ema_50 > 0:
        if close_price > ema_50:
            buy_pts += 10.0
            sell_pts -= 18.0
            buy_reasons.append("Acima EMA50")
        elif close_price < ema_50:
            sell_pts += 10.0
            buy_pts -= 18.0
            sell_reasons.append("Abaixo EMA50")

    # 2. Momentum MACD + RSI simétrico (até 30 pts)
    if macd_diff > 0:
        buy_pts += 16.0
    elif macd_diff < 0:
        sell_pts += 16.0

    if 52.0 <= rsi <= 66.0:
        buy_pts += 14.0
    elif 34.0 <= rsi <= 48.0:
        sell_pts += 14.0
    elif 48.0 < rsi < 52.0:
        if ema_ratio > 0:
            buy_pts += 7.0
        elif ema_ratio < 0:
            sell_pts += 7.0

    # 3. Força de tendência ADX (até 15 pts — sem viés em empate)
    if adx >= 0.22:
        if buy_pts > sell_pts:
            buy_pts += 15.0
            buy_reasons.append(f"ADX {adx*100:.0f}")
        elif sell_pts > buy_pts:
            sell_pts += 15.0
            sell_reasons.append(f"ADX {adx*100:.0f}")

    # 4. Influência do Dólar (DXY) e Juros EUA (até 15 pts)
    base, _, quote = symbol.upper().partition("/")
    if macro_snapshot:
        dxy_chg = _safe_float(macro_snapshot.get("dxy_change_pct"), 0.0) or 0.0
        if quote == "USD":
            # DXY caindo favorece EUR/USD, GBP/USD, AUD/USD, XAU/USD
            if dxy_chg <= -0.15:
                buy_pts += 15.0
                buy_reasons.append(f"DXY {dxy_chg:+.2f}%")
            elif dxy_chg >= 0.15:
                sell_pts += 15.0
                sell_reasons.append(f"DXY {dxy_chg:+.2f}%")
        elif base == "USD":
            # DXY subindo favorece USD/JPY, USD/BRL, USD/CHF
            if dxy_chg >= 0.15:
                buy_pts += 15.0
                buy_reasons.append(f"DXY {dxy_chg:+.2f}%")
            elif dxy_chg <= -0.15:
                sell_pts += 15.0
                sell_reasons.append(f"DXY {dxy_chg:+.2f}%")

    # 5. Confluência com modelo Machine Learning treinado no histórico real do par (quando disponível)
    # Limiares calibrados para probabilidades naturais não-infladas (base-rate Triple Barrier ~25–35%):
    # >= 50% indica vantagem estatística forte de alta; <= 18% indica cenário fortemente desfavorável para compra.
    ml_prob_up: Optional[float] = None
    if ml_model is not None:
        try:
            use_cols = list(ml_feature_cols) if ml_feature_cols else list(default_fcols)
            X_last = feat_df.iloc[-1:][use_cols]
            if not X_last.empty and not X_last.isna().values.any():
                p_up = float(predict_prob_up(ml_model, X_last))
                if math.isfinite(p_up):
                    ml_prob_up = round(p_up, 4)
                    p_down = 1.0 - p_up
                    if p_up >= 0.50:
                        buy_pts += 12.0
                        sell_pts -= 12.0
                        buy_reasons.append(f"ML BUY {p_up*100:.0f}%")
                    elif p_up <= 0.18:
                        sell_pts += 10.0
                        buy_pts -= 12.0
                        sell_reasons.append(f"ML SELL {p_down*100:.0f}%")
        except Exception as ml_err:
            logger.debug(f"[FOREX IA ML] Inferência ignorada para {symbol}: {ml_err}")

    # Empate ou falta de direção clara -> NEUTRAL
    if buy_pts == sell_pts:
        return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Sem direção dominante"}

    # Bloqueio anti-topo / anti-fundo / reversão brusca contra a entrada
    if buy_pts > sell_pts:
        if rsi > 68.0 or bb_pos > 0.92:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Sobrecomprado (anti-topo)"}
        if ret_3 <= -0.003:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Reversão curta contrária (evitando faca caindo)"}
        side = "BUY"
        score = round(max(0.0, min(100.0, buy_pts)), 1)
        reasons = buy_reasons
    else:
        if rsi < 32.0 or bb_pos < 0.08:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Sobrevendido (anti-fundo)"}
        if ret_3 >= 0.003:
            return {"symbol": symbol, "side": "NEUTRAL", "score": 0.0, "reason": "Repique brusco contrário (evitando venda em disparo)"}
        side = "SELL"
        score = round(max(0.0, min(100.0, sell_pts)), 1)
        reasons = sell_reasons

    return {
        "symbol": symbol,
        "side": side,
        "score": score,
        "close": close_price,
        "atr_pct": atr_pct,
        "rsi_14": round(rsi, 1),
        "adx_14": round(adx * 100.0, 1),
        "ml_prob_up": ml_prob_up,
        "ml_active": ml_prob_up is not None,
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
        min_score: float = 78.0,
        max_open_forex_positions: int = 2,
        leverage: int = 20,
        sl_cooldown_hours: float = 4.0,
        block_correlated_usd: bool = True,
        models_dir: str = DEFAULT_MODELS_DIR,
    ):
        self.db_path = db_path
        self.pairs = pairs
        self.min_score = min_score
        self.max_open_forex_positions = max_open_forex_positions
        self.leverage = leverage
        self.sl_cooldown_hours = max(0.0, float(sl_cooldown_hours))
        self.block_correlated_usd = bool(block_correlated_usd)
        self.models_dir = models_dir
        self.db = TradingDatabase(db_path=db_path)
        self.model_storage = ModelPersistence(models_dir=models_dir)
        self._ml_cache: Dict[str, Tuple[int, Any, List[str]]] = {}
        self._ml_cache_lock = threading.Lock()
        self.store = forex_market.XTBDeskStore(db_path=db_path)
        self.copilot = xtb_copilot.XTBCopilotStore(db_path=db_path)
        self.last_scan_report: Dict[str, Any] = {
            "market_open": False,
            "status_message": "Aguardando primeiro ciclo",
            "opportunities": [],
            "last_action": None,
        }

    def _get_symbol_ml_model(self, symbol: str) -> Tuple[Optional[Any], Optional[List[str]]]:
        """Carrega (com cache + hot-reload por versão) o modelo ML treinado para um par Forex/XTB."""
        try:
            meta = self.model_storage.load_metadata(symbol, "1h")
            if not meta:
                return None, None
            disk_ver = int(meta.get("version", 0) or 0)
            with self._ml_cache_lock:
                cached = self._ml_cache.get(symbol)
                if cached and cached[0] == disk_ver:
                    return cached[1], cached[2]
            loaded = self.model_storage.load_latest_model(symbol, "1h")
            if not loaded:
                return None, None
            mdl, ver, _acc, fcols = loaded
            if len(list(getattr(mdl, "classes_", []))) < 2:
                return None, None
            with self._ml_cache_lock:
                self._ml_cache[symbol] = (int(ver), mdl, list(fcols))
            return mdl, list(fcols)
        except Exception:
            return None, None

    def _is_symbol_in_sl_cooldown(self, symbol: str, recent_trades: List[Dict[str, Any]], now_dt: datetime) -> bool:
        """Bloqueia reentrada imediata no mesmo par após Stop Loss ou prejuízo recente."""
        if self.sl_cooldown_hours <= 0:
            return False
        sym_u = symbol.upper()
        for tr in recent_trades:
            if str(tr.get("symbol", "")).upper() != sym_u:
                continue
            reason = str(tr.get("reason", "")).upper()
            pnl = _safe_float(tr.get("pnl_usd"), 0.0) or 0.0
            if "STOP" in reason or pnl < 0:
                closed_dt = parse_utc_str(str(tr.get("closed_at") or ""))
                if closed_dt is not None:
                    elapsed_h = (now_dt - closed_dt).total_seconds() / 3600.0
                    if 0.0 <= elapsed_h < self.sl_cooldown_hours:
                        return True
            break
        return False

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
        open_forex = [p for p in snap.get("open_positions", []) if p.get("category") != "crypto"]
        open_syms = {p["symbol"] for p in open_forex}

        opportunities: List[Dict[str, Any]] = []

        now_epoch = int((now_utc or datetime.now(timezone.utc)).timestamp())

        def _analyze_one(sym: str) -> Optional[Dict[str, Any]]:
            try:
                raw_candles = forex_market.fetch_forex_candles(sym, timeframe="1h", limit=80)
                if not raw_candles or len(raw_candles) < 35:
                    return None
                # Descarta o último candle em formação (evita repainting em vela 1h ainda aberta)
                if len(raw_candles) > 35 and (int(raw_candles[-1]["time"]) + 3600) > now_epoch:
                    raw_candles = raw_candles[:-1]
                if len(raw_candles) < 35:
                    return None
                df = pd.DataFrame(raw_candles)
                df["timestamp"] = pd.to_datetime(df["time"], unit="s")
                # Persiste velas fechadas de Forex/Commodities no SQLite local para alimentar treinos ML futuros
                try:
                    self.db.save_candles(
                        sym,
                        "1h",
                        df[["timestamp", "open", "high", "low", "close", "volume"]],
                        drop_last_open=False,
                    )
                except Exception as save_err:
                    logger.debug(f"[FOREX IA DB] Falha ao persistir velas de {sym}: {save_err}")
                ml_model, ml_fcols = self._get_symbol_ml_model(sym)
                try:
                    return evaluate_forex_opportunity(
                        sym,
                        df,
                        macro_snapshot=macro_snapshot,
                        ml_model=ml_model,
                        ml_feature_cols=ml_fcols,
                    )
                except TypeError:
                    return evaluate_forex_opportunity(sym, df, macro_snapshot=macro_snapshot)
            except Exception as e:
                logger.debug(f"[FOREX IA] Falha ao analisar {sym}: {e}")
                return None

        max_workers = max(1, min(6, len(self.pairs)))
        if max_workers == 1:
            for sym in self.pairs:
                opp = _analyze_one(sym)
                if opp is not None:
                    opportunities.append(opp)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_analyze_one, sym) for sym in self.pairs]
                for fut in as_completed(futures):
                    opp = fut.result()
                    if opp is not None:
                        opportunities.append(opp)

        opportunities.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        last_action = None
        open_forex_count = len(open_forex)

        # 2. Registra sinais fortes no Copiloto XTB (desacoplado da mesa simulada) e abre na mesa se houver vaga
        now_dt_utc = now_utc or datetime.now(timezone.utc)
        if now_dt_utc.tzinfo is None:
            now_dt_utc = now_dt_utc.replace(tzinfo=timezone.utc)
        with _FOREX_CYCLE_LOCK:
            snap_latest = self.store.get_account_snapshot()
            open_forex_latest = [p for p in snap_latest.get("open_positions", []) if p.get("category") != "crypto"]
            recent_xtb_trades = snap_latest.get("recent_trades", [])
            open_syms = {p["symbol"] for p in open_forex_latest}
            open_usd_biases = {
                b for b in (_usd_exposure_direction(p["symbol"], p["side"]) for p in open_forex_latest)
                if b is not None
            }
            open_forex_count = len(open_forex_latest)

            for best in opportunities:
                if best["side"] not in ("BUY", "SELL"):
                    continue
                if best["score"] < self.min_score:
                    break

                sym = best["symbol"]
                if self._is_symbol_in_sl_cooldown(sym, recent_xtb_trades, now_dt_utc):
                    logger.info(f"🛡️ [FOREX IA COOLDOWN] {sym} em resfriamento pós-Stop Loss ({self.sl_cooldown_hours:.0f}h) — entrada ignorada.")
                    continue
                cand_usd_bias = _usd_exposure_direction(sym, best["side"])
                if self.block_correlated_usd and cand_usd_bias and cand_usd_bias in open_usd_biases:
                    logger.info(f"🛡️ [FOREX IA ANTI-CORRELAÇÃO] {sym} ({best['side']} -> {cand_usd_bias}) bloqueado: já há exposição ativa nessa direção do USD.")
                    continue

                inst = forex_market.MARKET_INSTRUMENTS.get(sym, {})
                decimals = int(inst.get("decimals", 5))
                atr_pct = _safe_float(best.get("atr_pct"), 0.003) or 0.003
                # TP/SL calibrados com folga contra ruído de 1h (R:R 1.5:1)
                tp_pct = min(max(atr_pct * 2.4, 0.0055), 0.0220)
                sl_pct = min(max(atr_pct * 1.6, 0.0035), 0.0130)

                try:
                    q = forex_market.fetch_forex_quote(sym)
                    ref_price = float((q.get("ask") if best["side"] == "BUY" else q.get("bid")) or q.get("price") or 0.0)
                    if not (ref_price > 0 and math.isfinite(ref_price)):
                        continue
                    if best["side"] == "BUY":
                        tp_price = round(ref_price * (1.0 + tp_pct), decimals)
                        sl_price = round(ref_price * (1.0 - sl_pct), decimals)
                    else:
                        tp_price = round(ref_price * (1.0 - tp_pct), decimals)
                        sl_price = round(ref_price * (1.0 + sl_pct), decimals)

                    base_lot = float(inst.get("default_lot", 0.05))
                    safe_lot = round(base_lot * (0.5 if atr_pct > 0.008 else 1.0), 3)
                    safe_lot = max(float(inst.get("min_lot", 0.01)), safe_lot)
                    eff_lev = min(self.leverage, int(inst.get("max_leverage", 30)))

                    # Sempre registra sinal pronto no Copiloto XTB (notificação unificada feita abaixo via alerts.send_alert)
                    copilot_sig: Optional[Dict[str, Any]] = None
                    try:
                        copilot_sig = self.copilot.record_signal(
                            symbol=sym,
                            side=best["side"],
                            score=best["score"],
                            entry_price=ref_price,
                            sl_price=sl_price,
                            tp_price=tp_price,
                            suggested_lots=safe_lot,
                            leverage=eff_lev,
                            reason=best.get("reason", "Confluência técnica + macro"),
                            notify_telegram=False,
                        )
                    except Exception as cop_err:
                        logger.debug(f"[COPILOTO XTB] Falha ao gravar sinal para {sym}: {cop_err}")

                    if open_forex_count < self.max_open_forex_positions and sym not in open_syms:
                        try:
                            pos = self.store.open_position(
                                symbol=sym,
                                side=best["side"],
                                lots=safe_lot,
                                leverage=eff_lev,
                                sl_price=sl_price,
                                tp_price=tp_price,
                                quote_override=q,
                            )
                        except Exception as open_err:
                            logger.warning(f"💱 [FOREX IA] Falha ao abrir posição {sym}: {open_err}")
                            alerts.send_alert(
                                f"❌ Falha ao abrir operação Forex/XTB em {sym}: {open_err}",
                                level="TRADE_FAIL",
                            )
                            continue
                        open_forex_count += 1
                        open_syms.add(sym)
                        if cand_usd_bias:
                            open_usd_biases.add(cand_usd_bias)
                        last_action = (
                            f"Ordem {pos['side']} {pos['lots']} lote(s) em {sym} @ {pos['entry_price']} "
                            f"(Score {best['score']:.0f}/100 | TP {tp_price} | SL {sl_price})"
                        )
                        logger.info(f"💱🚀 [FOREX IA ENTRADA] {last_action} | Motivos: {best['reason']}")
                        side_pt = "COMPRA (BUY)" if pos["side"] == "BUY" else "VENDA (SELL)"
                        alerts.send_alert(
                            (
                                f"💱 {side_pt} {sym} (Forex/XTB)\n"
                                f"Entrada: {pos['entry_price']} | Lotes: {pos['lots']} (1:{eff_lev})\n"
                                f"Take Profit: {tp_price} | Stop Loss: {sl_price}\n"
                                f"Score IA: {best['score']:.0f}/100 | {best.get('reason', '')}"
                            ),
                            level="TRADE_ENTRY",
                        )
                        break
                    else:
                        if last_action is None:
                            last_action = (
                                f"Sinal Copiloto XTB {best['side']} em {sym} @ {ref_price} "
                                f"(Score {best['score']:.0f}/100 | TP {tp_price} | SL {sl_price})"
                            )
                        if copilot_sig and not copilot_sig.get("deduplicated"):
                            side_pt = "COMPRA (BUY)" if best["side"] == "BUY" else "VENDA (SELL)"
                            alerts.send_alert(
                                (
                                    f"🧭 SINAL XTB {side_pt} — {copilot_sig.get('xtb_symbol', sym)}\n"
                                    f"Entrada: {ref_price} | TP: {tp_price} | SL: {sl_price}\n"
                                    f"Lote sugerido: {safe_lot} (1:{eff_lev}) | Score: {best['score']:.0f}/100"
                                ),
                                level="TRADE_ENTRY",
                            )
                    if open_forex_count >= self.max_open_forex_positions:
                        break
                except Exception as e:
                    logger.warning(f"💱 [FOREX IA] Não foi possível processar {sym}: {e}")

        self.last_scan_report = {
            "market_open": True,
            "status_message": open_msg,
            "open_forex_positions": open_forex_count,
            "opportunities": opportunities[:7],
            "last_action": last_action,
        }
        return self.last_scan_report
