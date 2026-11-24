/**
 * TradingView Lightweight Charts: Candles, Volume, EMAs e Linhas de Posição
 */

import { state } from "./state.js";

export function initTradingViewChart() {
  const container = document.getElementById("tvChartContainer");
  if (!container || typeof window.LightweightCharts === "undefined") {
    console.warn("TradingView Lightweight Charts não carregado ainda.");
    return;
  }

  const LWC = window.LightweightCharts;
  container.innerHTML = "";

  state.tvChart = LWC.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 440,
    layout: {
      background: { color: "#07090e" },
      textColor: "#94a3b8",
      fontSize: 12,
      fontFamily: "'JetBrains Mono', 'Outfit', sans-serif",
    },
    grid: {
      vertLines: { color: "rgba(255, 255, 255, 0.04)" },
      horzLines: { color: "rgba(255, 255, 255, 0.04)" },
    },
    crosshair: {
      mode: LWC.CrosshairMode.Normal,
      vertLine: {
        color: "rgba(0, 242, 254, 0.4)",
        width: 1,
        style: LWC.LineStyle.Dashed,
      },
      horzLine: {
        color: "rgba(0, 242, 254, 0.4)",
        width: 1,
        style: LWC.LineStyle.Dashed,
      },
    },
    rightPriceScale: {
      borderColor: "rgba(255, 255, 255, 0.08)",
      autoScale: true,
    },
    timeScale: {
      borderColor: "rgba(255, 255, 255, 0.08)",
      timeVisible: true,
      secondsVisible: false,
    },
  });

  state.candleSeries = state.tvChart.addCandlestickSeries({
    upColor: "#10b981",
    downColor: "#f43f5e",
    borderUpColor: "#10b981",
    borderDownColor: "#f43f5e",
    wickUpColor: "#10b981",
    wickDownColor: "#f43f5e",
  });

  state.volumeSeries = state.tvChart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "",
  });
  state.volumeSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 },
  });

  state.ema9Series = state.tvChart.addLineSeries({
    color: "#00f2fe",
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  state.ema21Series = state.tvChart.addLineSeries({
    color: "#f59e0b",
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
}

export function resizeChart() {
  if (!state.tvChart) return;
  const container = document.getElementById("tvChartContainer");
  if (container && container.clientWidth > 0) {
    state.tvChart.applyOptions({
      width: container.clientWidth,
      height: container.clientHeight,
    });
  }
}

export function fitChartContent() {
  if (state.tvChart) state.tvChart.timeScale().fitContent();
}

export function computeEma(candles, period) {
  if (!candles || candles.length < period) return [];
  const k = 2 / (period + 1);
  let ema = 0;
  for (let i = 0; i < period; i += 1) {
    ema += candles[i].close;
  }
  ema /= period;
  const out = [{ time: candles[period - 1].time, value: ema }];
  for (let i = period; i < candles.length; i += 1) {
    ema = candles[i].close * k + ema * (1 - k);
    out.push({ time: candles[i].time, value: ema });
  }
  return out;
}

export function refreshEmaSeries() {
  if (!state.ema9Series || !state.ema21Series) return;
  if (!state.showEmaOverlay || state.loadedCandles.length < 9) {
    state.ema9Series.setData([]);
    state.ema21Series.setData([]);
    return;
  }
  state.ema9Series.setData(computeEma(state.loadedCandles, 9));
  state.ema21Series.setData(computeEma(state.loadedCandles, 21));
}

export function updateLoadedCandlesWithTick(bar) {
  if (!state.loadedCandles.length) return;
  const last = state.loadedCandles[state.loadedCandles.length - 1];
  if (bar.time === last.time) {
    state.loadedCandles[state.loadedCandles.length - 1] = bar;
  } else if (bar.time > last.time) {
    state.loadedCandles.push(bar);
    if (state.loadedCandles.length > 300) state.loadedCandles.shift();
  }
  if (state.showEmaOverlay && state.ema9Series && state.ema21Series && state.loadedCandles.length >= 21) {
    const e9 = computeEma(state.loadedCandles, 9);
    const e21 = computeEma(state.loadedCandles, 21);
    if (e9.length) state.ema9Series.update(e9[e9.length - 1]);
    if (e21.length) state.ema21Series.update(e21[e21.length - 1]);
  }
}

export function toggleEmaOverlay() {
  state.showEmaOverlay = !state.showEmaOverlay;
  const btn = document.getElementById("btnToggleEma");
  if (btn) btn.classList.toggle("active", state.showEmaOverlay);
  refreshEmaSeries();
}

export function clearPositionPriceLines() {
  if (state.candleSeries && state.positionPriceLines.length) {
    for (const line of state.positionPriceLines) {
      try {
        state.candleSeries.removePriceLine(line);
      } catch (_) {
        // ignora se já removida
      }
    }
  }
  state.positionPriceLines = [];
  state.lastPositionKey = "";
}

export function renderOpenPositionLines(pos) {
  if (!state.candleSeries || typeof window.LightweightCharts === "undefined") return;
  const LWC = window.LightweightCharts;
  if (!pos || !pos.entry_price) {
    if (state.positionPriceLines.length) clearPositionPriceLines();
    return;
  }
  const key = `${pos.symbol}|${pos.timeframe}|${pos.entry_price}|${pos.tp_price}|${pos.sl_price}`;
  if (key === state.lastPositionKey) return;
  clearPositionPriceLines();
  state.lastPositionKey = key;

  try {
    state.positionPriceLines.push(
      state.candleSeries.createPriceLine({
        price: Number(pos.entry_price),
        color: "#38bdf8",
        lineWidth: 1,
        lineStyle: LWC.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `ENTRADA (${(pos.mode || "paper").toUpperCase()})`,
      })
    );
    if (pos.tp_price > 0) {
      state.positionPriceLines.push(
        state.candleSeries.createPriceLine({
          price: Number(pos.tp_price),
          color: "#10b981",
          lineWidth: 1,
          lineStyle: LWC.LineStyle.Solid,
          axisLabelVisible: true,
          title: "TAKE PROFIT",
        })
      );
    }
    if (pos.sl_price > 0) {
      state.positionPriceLines.push(
        state.candleSeries.createPriceLine({
          price: Number(pos.sl_price),
          color: "#f43f5e",
          lineWidth: 1,
          lineStyle: LWC.LineStyle.Solid,
          axisLabelVisible: true,
          title: "STOP LOSS",
        })
      );
    }
  } catch (_) {
    // ignora erro se gráfico ainda não inicializado
  }
}
