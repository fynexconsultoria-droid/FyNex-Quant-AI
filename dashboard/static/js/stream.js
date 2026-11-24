/**
 * Stream WebSocket Binance, Ticker 24h, Bid/Ask e Countdown de Candle
 */

import { updateLoadedCandlesWithTick } from "./chart.js";
import { hooks, state, TIMEFRAME_SECONDS } from "./state.js";
import { formatCompactUsd, formatPrice, getInstrumentMeta, isActiveForex } from "./utils.js";

export function updateCandleCountdown() {
  const el = document.getElementById("mktCandleCountdown");
  if (!el) return;
  const tfSec = TIMEFRAME_SECONDS[state.activeTimeframe] || 60;
  const nowSec = Math.floor(Date.now() / 1000);
  const rem = tfSec - (nowSec % tfSec);
  const mins = Math.floor(rem / 60);
  const secs = rem % 60;
  el.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function update24hRangeBar(price) {
  if (!state.marketHigh24h || !state.marketLow24h || state.marketHigh24h <= state.marketLow24h || !price) return;
  const pct = Math.max(0, Math.min(100, ((price - state.marketLow24h) / (state.marketHigh24h - state.marketLow24h)) * 100));
  const fill = document.getElementById("mktRangeFill");
  const dot = document.getElementById("mktRangeDot");
  if (fill) fill.style.width = `${pct.toFixed(1)}%`;
  if (dot) dot.style.left = `${pct.toFixed(1)}%`;
}

export function updateLiveTickerPrice(price) {
  const priceEl = document.getElementById("tickerPrice");
  if (!priceEl || !price || price <= 0) return;

  priceEl.textContent = formatPrice(price);

  if (state.lastKnownPrice > 0 && price !== state.lastKnownPrice) {
    priceEl.style.color = price > state.lastKnownPrice ? "#10b981" : "#f43f5e";
    if (state.livePriceResetTimer) clearTimeout(state.livePriceResetTimer);
    state.livePriceResetTimer = setTimeout(() => {
      priceEl.style.color = "var(--accent-cyan)";
    }, 450);
  }
  state.lastKnownPrice = price;
  update24hRangeBar(price);
}

export function update24hStatsUI({ changePct, high, low, quoteVolume }) {
  const chgEl = document.getElementById("tickerChange24h");
  if (chgEl && Number.isFinite(changePct)) {
    chgEl.textContent = `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
    chgEl.className = `ticker-change ${changePct >= 0 ? "positive" : "negative"}`;
  }
  if (high > 0) {
    state.marketHigh24h = high;
    const highEl = document.getElementById("mktHigh24h");
    if (highEl) highEl.textContent = formatPrice(high);
  }
  if (low > 0) {
    state.marketLow24h = low;
    const lowEl = document.getElementById("mktLow24h");
    if (lowEl) lowEl.textContent = formatPrice(low);
  }
  if (quoteVolume > 0) {
    const volEl = document.getElementById("mktVol24h");
    if (volEl) volEl.textContent = formatCompactUsd(quoteVolume);
  }
  if (state.lastKnownPrice > 0) update24hRangeBar(state.lastKnownPrice);
}

export function updateBidAskUI(bid, ask) {
  if (!bid || !ask || bid <= 0 || ask <= 0) return;
  state.currentBidPrice = bid;
  state.currentAskPrice = ask;

  const bidEl = document.getElementById("mktBestBid");
  const askEl = document.getElementById("mktBestAsk");
  const spreadEl = document.getElementById("mktSpread");
  const sellBtnPrice = document.getElementById("xtbSellPrice");
  const buyBtnPrice = document.getElementById("xtbBuyPrice");
  const xtbSpreadInfo = document.getElementById("xtbSpreadInfo");

  const bidStr = formatPrice(bid);
  const askStr = formatPrice(ask);
  if (bidEl) bidEl.textContent = bidStr;
  if (askEl) askEl.textContent = askStr;
  if (sellBtnPrice) sellBtnPrice.textContent = bidStr;
  if (buyBtnPrice) buyBtnPrice.textContent = askStr;

  const mid = (bid + ask) / 2;
  const spreadPct = mid > 0 ? ((ask - bid) / mid) * 100 : 0;
  if (spreadEl) spreadEl.textContent = `${spreadPct.toFixed(3)}%`;
  if (xtbSpreadInfo) {
    const meta = getInstrumentMeta(state.activeSymbol);
    if (meta.category === "forex" && meta.pip_size > 0) {
      const pips = (ask - bid) / meta.pip_size;
      xtbSpreadInfo.textContent = `${pips.toFixed(1)} pips (${spreadPct.toFixed(3)}%)`;
    } else {
      xtbSpreadInfo.textContent = `${formatPrice(ask - bid)} (${spreadPct.toFixed(3)}%)`;
    }
  }
  if (typeof hooks.onBidAskUpdated === "function") {
    hooks.onBidAskUpdated();
  }
}

export function setLiveBadgeState(badgeState) {
  const badge = document.getElementById("tickerBadge");
  if (!badge) return;
  if (badgeState === "ws") {
    badge.textContent = "Spot Live • WS";
    badge.style.color = "#10b981";
  } else if (badgeState === "forex") {
    badge.textContent = "Forex Live • XTB";
    badge.style.color = "#38bdf8";
  } else if (badgeState === "reconnecting") {
    badge.textContent = "Reconectando...";
    badge.style.color = "#f59e0b";
  } else {
    badge.textContent = "Spot Live • REST";
    badge.style.color = "#38bdf8";
  }
}

export function disconnectLiveMarketStream() {
  if (state.liveWsReconnectTimer) {
    clearTimeout(state.liveWsReconnectTimer);
    state.liveWsReconnectTimer = null;
  }
  if (state.liveWsWatchdogTimer) {
    clearInterval(state.liveWsWatchdogTimer);
    state.liveWsWatchdogTimer = null;
  }
  if (state.liveWs) {
    try {
      state.liveWs.onopen = null;
      state.liveWs.onclose = null;
      state.liveWs.onerror = null;
      state.liveWs.onmessage = null;
      state.liveWs.close();
    } catch (_) {
      // ignora erro de fechamento
    }
    state.liveWs = null;
  }
  state.liveWsConnected = false;
}

export function connectLiveMarketStream() {
  disconnectLiveMarketStream();
  if (isActiveForex()) {
    setLiveBadgeState("forex");
    return;
  }
  const s = state.activeSymbol.replace("/", "").toLowerCase();
  const streams = `${s}@kline_${state.activeTimeframe}/${s}@ticker/${s}@bookTicker`;
  const endpoints = [
    `wss://stream.binance.com:9443/stream?streams=${streams}`,
    `wss://stream.binance.com:443/stream?streams=${streams}`,
  ];
  const wsUrl = endpoints[state.liveWsEndpointIndex % endpoints.length];

  try {
    const ws = new WebSocket(wsUrl);
    state.liveWs = ws;

    ws.onopen = () => {
      if (ws !== state.liveWs) return;
      state.liveWsConnected = true;
      state.liveWsReconnectAttempts = 0;
      state.lastWsMessageAt = Date.now();
      setLiveBadgeState("ws");

      if (state.liveWsWatchdogTimer) clearInterval(state.liveWsWatchdogTimer);
      state.liveWsWatchdogTimer = setInterval(() => {
        if (ws !== state.liveWs) {
          clearInterval(state.liveWsWatchdogTimer);
          return;
        }
        if (state.liveWsConnected && Date.now() - state.lastWsMessageAt > 15000) {
          state.liveWsEndpointIndex += 1;
          connectLiveMarketStream();
        }
      }, 5000);
    };

    ws.onmessage = (event) => {
      if (ws !== state.liveWs) return;
      try {
        state.lastWsMessageAt = Date.now();
        const envelope = JSON.parse(event.data);
        const streamName = envelope && envelope.stream ? String(envelope.stream) : "";
        const payload = envelope && envelope.data ? envelope.data : envelope;
        if (!payload) return;

        if (streamName.includes("@kline_") || payload.k) {
          const k = payload.k;
          if (!k) return;
          const closePrice = parseFloat(k.c);
          const openPrice = parseFloat(k.o);
          const highPrice = parseFloat(k.h);
          const lowPrice = parseFloat(k.l);
          const volume = parseFloat(k.v);
          const candleTime = Math.floor(Number(k.t) / 1000);

          updateLiveTickerPrice(closePrice);

          if (state.candleSeries && state.chartInitializedWithData) {
            const bar = {
              time: candleTime,
              open: openPrice,
              high: highPrice,
              low: lowPrice,
              close: closePrice,
            };
            state.candleSeries.update(bar);
            if (state.volumeSeries) {
              state.volumeSeries.update({
                time: candleTime,
                value: volume,
                color: closePrice >= openPrice ? "rgba(16, 185, 129, 0.35)" : "rgba(244, 63, 94, 0.35)",
              });
            }
            updateLoadedCandlesWithTick(bar);
          }

          const chartUpdateEl = document.getElementById("chartLastUpdate");
          if (chartUpdateEl) {
            const dt = new Date(Number(payload.E || Date.now()));
            chartUpdateEl.textContent = `Ao vivo (WS): ${dt.toLocaleTimeString("pt-BR")}`;
          }
        } else if (streamName.endsWith("@ticker") || payload.e === "24hrTicker") {
          update24hStatsUI({
            changePct: parseFloat(payload.P),
            high: parseFloat(payload.h),
            low: parseFloat(payload.l),
            quoteVolume: parseFloat(payload.q),
          });
        } else if (streamName.endsWith("@bookTicker") || (payload.b && payload.a)) {
          updateBidAskUI(parseFloat(payload.b), parseFloat(payload.a));
        }
      } catch (_) {
        // ignora frame malformado
      }
    };

    ws.onerror = () => {
      state.liveWsConnected = false;
      setLiveBadgeState("reconnecting");
    };

    ws.onclose = () => {
      state.liveWsConnected = false;
      if (state.liveWsWatchdogTimer) {
        clearInterval(state.liveWsWatchdogTimer);
        state.liveWsWatchdogTimer = null;
      }
      setLiveBadgeState("reconnecting");
      state.liveWsReconnectAttempts += 1;
      state.liveWsEndpointIndex += 1;
      const delayMs = Math.min(15000, 1000 * Math.pow(2, Math.min(state.liveWsReconnectAttempts - 1, 4)));
      state.liveWsReconnectTimer = setTimeout(() => {
        connectLiveMarketStream();
      }, delayMs);
    };
  } catch (_) {
    state.liveWsConnected = false;
    setLiveBadgeState("fallback");
  }
}
