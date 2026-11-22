/**
 * FyNex Quant AI Dashboard - Client Application Logic
 * Integração com TradingView Lightweight Charts e APIs do backend (com CSRF, escape XSS e filtro por Timeframe)
 */

let tvChart = null;
let candleSeries = null;
let volumeSeries = null;
let ema9Series = null;
let ema21Series = null;
let showEmaOverlay = true;
let loadedCandles = [];
let positionPriceLines = [];
let lastPositionKey = "";

let isBotOnline = false;
let pollingInterval = null;
let countdownInterval = null;
let isPolling = false;
let lastKnownPrice = 0;
let activeSymbol = "BTC/USDT";
let activeTimeframe = "1h";
let chartInitializedWithData = false;

let liveWs = null;
let liveWsConnected = false;
let liveWsReconnectTimer = null;
let liveWsWatchdogTimer = null;
let liveWsReconnectAttempts = 0;
let liveWsEndpointIndex = 0;
let lastWsMessageAt = 0;
let livePriceResetTimer = null;

let lastDepthFetchAt = 0;
let lastHealthFetchAt = 0;
let lastBalanceFetchAt = 0;
let lastWatchlistFetchAt = 0;
let lastNewsFetchAt = 0;
let lastXtbFetchAt = 0;
let binanceHasKeys = false;
let marketHigh24h = 0;
let marketLow24h = 0;

let activeView = "terminal";
let instrumentsMap = {};
let watchlistData = [];
let watchlistCategory = "all";
let newsCategory = "all";
let newsCacheItems = [];
let currentBidPrice = 0;
let currentAskPrice = 0;
let xtbFreeMarginUsd = 10000;

const TIMEFRAME_SECONDS = {
  "1m": 60,
  "3m": 180,
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
};

function getInstrumentMeta(symbol = activeSymbol) {
  return (
    instrumentsMap[symbol] || {
      symbol,
      name: symbol,
      category: symbol.endsWith("/USDT") ? "crypto" : "forex",
      badge: symbol.endsWith("/USDT") ? "CRIPTO" : "FOREX",
      decimals: symbol.endsWith("/USDT") ? 2 : symbol.includes("JPY") ? 3 : 5,
      contract_size: symbol.endsWith("/USDT") ? 1 : 100000,
      default_lot: symbol.endsWith("/USDT") ? 0.01 : 0.1,
      min_lot: symbol.endsWith("/USDT") ? 0.001 : 0.01,
      max_leverage: symbol.endsWith("/USDT") ? 10 : 100,
      typical_spread_pips: 1.0,
    }
  );
}

function isActiveForex() {
  return getInstrumentMeta(activeSymbol).category !== "crypto";
}

function formatPrice(val, symbol = activeSymbol) {
  const num = Number(val || 0);
  if (!Number.isFinite(num) || num <= 0) return "--";
  const meta = getInstrumentMeta(symbol);
  const decimals = Number.isInteger(meta.decimals) ? meta.decimals : num >= 100 ? 2 : num >= 1 ? 3 : 5;
  const prefix = meta.category !== "forex" ? "$" : "";
  return `${prefix}${num.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function formatCompactUsd(val) {
  const num = Number(val || 0);
  if (!Number.isFinite(num) || num <= 0) return "--";
  if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
  if (num >= 1e6) return `$${(num / 1e6).toFixed(2)}M`;
  if (num >= 1e3) return `$${(num / 1e3).toFixed(1)}K`;
  return `$${num.toFixed(0)}`;
}

function showToast(message, type = "info") {
  const container = document.getElementById("toastContainer");
  if (!container) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.remove();
  }, 4200);
}

/**
 * Escapa caracteres HTML para impedir XSS em dados vindos da API ou da exchange
 */
function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Recupera o token CSRF injetado pelo servidor na meta tag
 */
function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

function csrfHeaders(extra = {}) {
  return {
    "X-CSRF-Token": getCsrfToken(),
    ...extra,
  };
}

// Inicialização ao carregar a página
document.addEventListener("DOMContentLoaded", () => {
  bindUIEvents();
  initTradingViewChart();
  fetchInitialData();
  connectLiveMarketStream();

  // Inicia polling periódico a cada 3 segundos (inclui trades!)
  pollingInterval = setInterval(pollDashboardData, 3000);
  // Atualiza countdown de fechamento do candle a cada 1 segundo
  updateCandleCountdown();
  countdownInterval = setInterval(updateCandleCountdown, 1000);

  // Redimensionamento responsivo do gráfico
  window.addEventListener("resize", () => {
    if (tvChart) {
      const container = document.getElementById("tvChartContainer");
      if (container) {
        tvChart.applyOptions({
          width: container.clientWidth,
          height: container.clientHeight,
        });
      }
    }
  });

  window.addEventListener("beforeunload", disconnectLiveMarketStream);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollDashboardData();
    }
  });
});

/**
 * Vincula todos os eventos de UI via addEventListener (compatível com CSP estrita sem unsafe-inline)
 */
function bindUIEvents() {
  const on = (id, evt, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(evt, handler);
  };

  on("symbolSelect", "change", (e) => onSymbolChange(e.target.value));
  on("timeframeSelect", "change", (e) => onTimeframeChange(e.target.value));
  on("btnToggleEma", "click", toggleEmaOverlay);
  on("btnFitChart", "click", () => {
    if (tvChart) tvChart.timeScale().fitContent();
  });
  on("btnOpenBinance", "click", openBinanceModal);
  on("binanceWalletCard", "click", openBinanceModal);
  on("btnToggleBot", "click", toggleBot);
  on("btnRefreshDashboard", "click", refreshDashboard);

  on("binanceModalOverlay", "click", closeBinanceModalOnOutsideClick);
  on("btnCloseBinanceModal", "click", closeBinanceModal);
  on("segTestnet", "click", () => selectEnvironment(true));
  on("segLive", "click", () => selectEnvironment(false));
  on("btnPasteApiKey", "click", pasteApiKey);
  on("btnToggleSecret", "click", toggleSecretVisibility);
  on("btnModalTest", "click", testBinanceConnectionModal);
  on("btnModalSave", "click", saveBinanceConnectionModal);

  document.querySelectorAll(".quick-btn[data-amount]").forEach((btn) => {
    btn.addEventListener("click", () => setQuickAmount(Number(btn.dataset.amount)));
  });

  // Navegação lateral entre áreas separadas
  document.querySelectorAll(".sidebar-nav .nav-item[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  on("btnGoToNews", "click", () => switchView("news"));

  // Watchlist filtros & busca
  document.querySelectorAll("#watchlistTabs .wl-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      watchlistCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#watchlistTabs .wl-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderWatchlist();
    });
  });
  on("watchlistSearch", "input", renderWatchlist);

  // Boleta XTB
  on("btnXtbBuy", "click", () => submitXtbOrder("BUY"));
  on("btnXtbSell", "click", () => submitXtbOrder("SELL"));
  on("xtbLotsInput", "input", updateXtbMarginEstimate);
  on("xtbLeverageSelect", "change", updateXtbMarginEstimate);
  document.querySelectorAll(".quick-lot-btn[data-lot]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const inp = document.getElementById("xtbLotsInput");
      if (inp) {
        inp.value = btn.dataset.lot;
        updateXtbMarginEstimate();
      }
    });
  });

  // Notícias ao vivo
  document.querySelectorAll("#newsFilterPills .filter-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      newsCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#newsFilterPills .filter-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      fetchNewsFeed(false);
    });
  });
  on("newsSearchInput", "input", () => fetchNewsFeed(false));
  on("btnRefreshNews", "click", () => fetchNewsFeed(true));

  // Diagnóstico de saúde na aba Console
  on("btnRecheckHealth", "click", () => fetchBinanceHealth(true));

  // Centro de Treinamento da IA & Inteligência Macro
  on("btnStartAiTrain", "click", startAiTraining);
  on("btnRefreshMacro", "click", () => {
    fetchMacroIntelligence(true);
    fetchAiEvolution();
  });
  on("sidebarEvoBox", "click", () => switchView("ai"));

  // Copiloto XTB (Modo Híbrido)
  on("btnScanXtbNow", "click", scanXtbCopilotNow);
  on("btnCopilotSound", "click", toggleCopilotSound);
  on("btnToggleTelegramBox", "click", () => {
    const box = document.getElementById("copilotTelegramBox");
    if (box) box.style.display = box.style.display === "none" ? "flex" : "none";
  });
  on("btnSaveTelegram", "click", saveTelegramConfigUI);
}

function switchView(viewName) {
  activeView = viewName || "terminal";
  document.querySelectorAll(".view-section").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${activeView}`);
  });
  document.querySelectorAll(".sidebar-nav .nav-item[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === activeView);
  });

  if (activeView === "terminal" && tvChart) {
    setTimeout(() => {
      const container = document.getElementById("tvChartContainer");
      if (container && container.clientWidth > 0) {
        tvChart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      }
    }, 30);
  }
  if (activeView === "ai") {
    fetchMacroIntelligence(false);
    fetchAiEvolution();
    fetchXtbCopilotSignals();
    pollAiTrainStatus();
  }
  if (activeView === "news" && newsCacheItems.length === 0) {
    fetchNewsFeed(false);
  }
  if (activeView === "portfolio") {
    fetchXtbAccount();
    if (binanceHasKeys) fetchBinanceBalanceAuto(false);
  }
  if (activeView === "system") {
    fetchBinanceHealth(true);
    fetchLogs();
  }
}

/**
 * Troca o par ativo no dashboard (Cripto ou Forex) e recarrega gráfico, stream e boleta XTB
 */
async function onSymbolChange(newSym) {
  activeSymbol = newSym || "BTC/USDT";
  const sel = document.getElementById("symbolSelect");
  if (sel && sel.value !== activeSymbol) sel.value = activeSymbol;

  lastKnownPrice = 0;
  currentBidPrice = 0;
  currentAskPrice = 0;
  marketHigh24h = 0;
  marketLow24h = 0;
  chartInitializedWithData = false;
  loadedCandles = [];
  clearPositionPriceLines();
  syncActiveAssetUI();
  renderWatchlist();
  await fetchInitialData();
  connectLiveMarketStream();
}

function syncActiveAssetUI() {
  const meta = getInstrumentMeta(activeSymbol);
  const cat = meta.category || "crypto";
  const isGlobal = cat !== "crypto";

  const catPill = document.getElementById("activeCategoryPill");
  if (catPill) {
    catPill.textContent = meta.badge || (isGlobal ? "GLOBAL" : "CRIPTO");
    catPill.className = `asset-category-pill ${isGlobal ? "forex" : "crypto"}`;
  }

  const titleEl = document.getElementById("chartTitle");
  if (titleEl) {
    if (cat === "commodities") {
      titleEl.textContent = `${activeSymbol} • Metais & Commodities (Modo XTB)`;
    } else if (cat === "indices") {
      titleEl.textContent = `${activeSymbol} • Índice Global (Modo XTB)`;
    } else if (cat === "forex") {
      titleEl.textContent = `${activeSymbol} • Forex (Modo XTB)`;
    } else {
      titleEl.textContent = `${activeSymbol} • Binance Spot`;
    }
  }

  const tkSym = document.getElementById("xtbTicketSymbol");
  const tkName = document.getElementById("xtbTicketName");
  const tkBadge = document.getElementById("xtbTicketBadge");
  const tkHint = document.getElementById("xtbContractHint");
  const lotsInp = document.getElementById("xtbLotsInput");
  const levSel = document.getElementById("xtbLeverageSelect");

  if (tkSym) tkSym.textContent = activeSymbol;
  if (tkName) tkName.textContent = meta.name || activeSymbol;
  if (tkBadge) tkBadge.textContent = meta.badge || (isGlobal ? "GLOBAL" : "CRIPTO");
  if (tkHint) {
    tkHint.textContent = isGlobal
      ? `1 lote = ${Number(meta.contract_size).toLocaleString("en-US")} un.`
      : `1 lote = 1 ${activeSymbol.split("/")[0]}`;
  }
  if (lotsInp) {
    lotsInp.min = String(meta.min_lot || 0.01);
    lotsInp.step = String(meta.min_lot || 0.01);
    lotsInp.value = String(meta.default_lot || 0.1);
  }
  if (levSel) {
    const targetLev = isGlobal ? Math.min(30, meta.max_leverage || 20) : Math.min(10, meta.max_leverage || 10);
    levSel.value = String(targetLev);
  }
  updateXtbMarginEstimate();
}

/**
 * Troca o timeframe ativo no dashboard e recarrega o gráfico, previsões e métricas
 */
async function onTimeframeChange(newTf) {
  activeTimeframe = newTf || "1m";
  chartInitializedWithData = false;
  loadedCandles = [];
  clearPositionPriceLines();
  const tag = document.getElementById("activeTimeframeTag");
  if (tag) tag.textContent = `Candles ${activeTimeframe}`;
  updateCandleCountdown();
  await fetchInitialData();
  connectLiveMarketStream();
}

function updateCandleCountdown() {
  const el = document.getElementById("mktCandleCountdown");
  if (!el) return;
  const tfSec = TIMEFRAME_SECONDS[activeTimeframe] || 60;
  const nowSec = Math.floor(Date.now() / 1000);
  const rem = tfSec - (nowSec % tfSec);
  const mins = Math.floor(rem / 60);
  const secs = rem % 60;
  el.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

/**
 * Atualiza o preço no cabeçalho com pulso visual suave (verde/vermelho) e faixa 24h
 */
function updateLiveTickerPrice(price) {
  const priceEl = document.getElementById("tickerPrice");
  if (!priceEl || !price || price <= 0) return;

  priceEl.textContent = formatPrice(price);

  if (lastKnownPrice > 0 && price !== lastKnownPrice) {
    priceEl.style.color = price > lastKnownPrice ? "#10b981" : "#f43f5e";
    if (livePriceResetTimer) clearTimeout(livePriceResetTimer);
    livePriceResetTimer = setTimeout(() => {
      priceEl.style.color = "var(--accent-cyan)";
    }, 450);
  }
  lastKnownPrice = price;
  update24hRangeBar(price);
}

function update24hRangeBar(price) {
  if (!marketHigh24h || !marketLow24h || marketHigh24h <= marketLow24h || !price) return;
  const pct = Math.max(0, Math.min(100, ((price - marketLow24h) / (marketHigh24h - marketLow24h)) * 100));
  const fill = document.getElementById("mktRangeFill");
  const dot = document.getElementById("mktRangeDot");
  if (fill) fill.style.width = `${pct.toFixed(1)}%`;
  if (dot) dot.style.left = `${pct.toFixed(1)}%`;
}

function update24hStatsUI({ changePct, high, low, quoteVolume }) {
  const chgEl = document.getElementById("tickerChange24h");
  if (chgEl && Number.isFinite(changePct)) {
    chgEl.textContent = `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
    chgEl.className = `ticker-change ${changePct >= 0 ? "positive" : "negative"}`;
  }
  if (high > 0) {
    marketHigh24h = high;
    const highEl = document.getElementById("mktHigh24h");
    if (highEl) highEl.textContent = formatPrice(high);
  }
  if (low > 0) {
    marketLow24h = low;
    const lowEl = document.getElementById("mktLow24h");
    if (lowEl) lowEl.textContent = formatPrice(low);
  }
  if (quoteVolume > 0) {
    const volEl = document.getElementById("mktVol24h");
    if (volEl) volEl.textContent = formatCompactUsd(quoteVolume);
  }
  if (lastKnownPrice > 0) update24hRangeBar(lastKnownPrice);
}

function updateBidAskUI(bid, ask) {
  if (!bid || !ask || bid <= 0 || ask <= 0) return;
  currentBidPrice = bid;
  currentAskPrice = ask;

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
    const meta = getInstrumentMeta(activeSymbol);
    if (meta.category === "forex" && meta.pip_size > 0) {
      const pips = (ask - bid) / meta.pip_size;
      xtbSpreadInfo.textContent = `${pips.toFixed(1)} pips (${spreadPct.toFixed(3)}%)`;
    } else {
      xtbSpreadInfo.textContent = `${formatPrice(ask - bid)} (${spreadPct.toFixed(3)}%)`;
    }
  }
  updateXtbMarginEstimate();
}

function setLiveBadgeState(state) {
  const badge = document.getElementById("tickerBadge");
  if (!badge) return;
  if (state === "ws") {
    badge.textContent = "Spot Live • WS";
    badge.style.color = "#10b981";
  } else if (state === "forex") {
    badge.textContent = "Forex Live • XTB";
    badge.style.color = "#38bdf8";
  } else if (state === "reconnecting") {
    badge.textContent = "Reconectando...";
    badge.style.color = "#f59e0b";
  } else {
    badge.textContent = "Spot Live • REST";
    badge.style.color = "#38bdf8";
  }
}

function disconnectLiveMarketStream() {
  if (liveWsReconnectTimer) {
    clearTimeout(liveWsReconnectTimer);
    liveWsReconnectTimer = null;
  }
  if (liveWsWatchdogTimer) {
    clearInterval(liveWsWatchdogTimer);
    liveWsWatchdogTimer = null;
  }
  if (liveWs) {
    try {
      liveWs.onclose = null;
      liveWs.onerror = null;
      liveWs.onmessage = null;
      liveWs.close();
    } catch (_) {
      // ignora erro de fechamento
    }
    liveWs = null;
  }
  liveWsConnected = false;
}

/**
 * Combined Stream WebSocket público da Binance:
 * kline (candles ao vivo) + ticker (variação/volume 24h) + bookTicker (bid/ask/spread)
 * com failover automático de porta (9443 <-> 443) e watchdog de inatividade (15s).
 */
function connectLiveMarketStream() {
  disconnectLiveMarketStream();
  if (isActiveForex()) {
    setLiveBadgeState("forex");
    return;
  }
  const s = activeSymbol.replace("/", "").toLowerCase();
  const streams = `${s}@kline_${activeTimeframe}/${s}@ticker/${s}@bookTicker`;
  const endpoints = [
    `wss://stream.binance.com:9443/stream?streams=${streams}`,
    `wss://stream.binance.com:443/stream?streams=${streams}`,
  ];
  const wsUrl = endpoints[liveWsEndpointIndex % endpoints.length];

  try {
    const ws = new WebSocket(wsUrl);
    liveWs = ws;

    ws.onopen = () => {
      liveWsConnected = true;
      liveWsReconnectAttempts = 0;
      lastWsMessageAt = Date.now();
      setLiveBadgeState("ws");

      liveWsWatchdogTimer = setInterval(() => {
        if (liveWsConnected && Date.now() - lastWsMessageAt > 15000) {
          liveWsEndpointIndex += 1;
          connectLiveMarketStream();
        }
      }, 5000);
    };

    ws.onmessage = (event) => {
      try {
        lastWsMessageAt = Date.now();
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

          if (candleSeries && chartInitializedWithData) {
            const bar = {
              time: candleTime,
              open: openPrice,
              high: highPrice,
              low: lowPrice,
              close: closePrice,
            };
            candleSeries.update(bar);
            if (volumeSeries) {
              volumeSeries.update({
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
      liveWsConnected = false;
      setLiveBadgeState("reconnecting");
    };

    ws.onclose = () => {
      liveWsConnected = false;
      if (liveWsWatchdogTimer) {
        clearInterval(liveWsWatchdogTimer);
        liveWsWatchdogTimer = null;
      }
      setLiveBadgeState("reconnecting");
      liveWsReconnectAttempts += 1;
      liveWsEndpointIndex += 1;
      const delayMs = Math.min(15000, 1000 * Math.pow(2, Math.min(liveWsReconnectAttempts - 1, 4)));
      liveWsReconnectTimer = setTimeout(() => {
        connectLiveMarketStream();
      }, delayMs);
    };
  } catch (_) {
    liveWsConnected = false;
    setLiveBadgeState("fallback");
  }
}

/**
 * Inicializa a instância do TradingView Lightweight Charts
 */
function initTradingViewChart() {
  const container = document.getElementById("tvChartContainer");
  if (!container || typeof LightweightCharts === "undefined") {
    console.warn("TradingView Lightweight Charts não carregado ainda.");
    return;
  }

  container.innerHTML = "";

  tvChart = LightweightCharts.createChart(container, {
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
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: {
        color: "rgba(0, 242, 254, 0.4)",
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
      },
      horzLine: {
        color: "rgba(0, 242, 254, 0.4)",
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
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

  candleSeries = tvChart.addCandlestickSeries({
    upColor: "#10b981",
    downColor: "#f43f5e",
    borderUpColor: "#10b981",
    borderDownColor: "#f43f5e",
    wickUpColor: "#10b981",
    wickDownColor: "#f43f5e",
  });

  volumeSeries = tvChart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "",
  });
  volumeSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 },
  });

  ema9Series = tvChart.addLineSeries({
    color: "#00f2fe",
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  ema21Series = tvChart.addLineSeries({
    color: "#f59e0b",
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
}

function computeEma(candles, period) {
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

function refreshEmaSeries() {
  if (!ema9Series || !ema21Series) return;
  if (!showEmaOverlay || loadedCandles.length < 9) {
    ema9Series.setData([]);
    ema21Series.setData([]);
    return;
  }
  ema9Series.setData(computeEma(loadedCandles, 9));
  ema21Series.setData(computeEma(loadedCandles, 21));
}

function updateLoadedCandlesWithTick(bar) {
  if (!loadedCandles.length) return;
  const last = loadedCandles[loadedCandles.length - 1];
  if (bar.time === last.time) {
    loadedCandles[loadedCandles.length - 1] = bar;
  } else if (bar.time > last.time) {
    loadedCandles.push(bar);
    if (loadedCandles.length > 300) loadedCandles.shift();
  }
  if (showEmaOverlay && ema9Series && ema21Series && loadedCandles.length >= 21) {
    const e9 = computeEma(loadedCandles, 9);
    const e21 = computeEma(loadedCandles, 21);
    if (e9.length) ema9Series.update(e9[e9.length - 1]);
    if (e21.length) ema21Series.update(e21[e21.length - 1]);
  }
}

function toggleEmaOverlay() {
  showEmaOverlay = !showEmaOverlay;
  const btn = document.getElementById("btnToggleEma");
  if (btn) btn.classList.toggle("active", showEmaOverlay);
  refreshEmaSeries();
}

function clearPositionPriceLines() {
  if (candleSeries && positionPriceLines.length) {
    for (const line of positionPriceLines) {
      try {
        candleSeries.removePriceLine(line);
      } catch (_) {
        // ignora se já removida
      }
    }
  }
  positionPriceLines = [];
  lastPositionKey = "";
}

function renderOpenPositionLines(pos) {
  if (!candleSeries) return;
  if (!pos || !pos.entry_price) {
    if (positionPriceLines.length) clearPositionPriceLines();
    return;
  }
  const key = `${pos.symbol}|${pos.timeframe}|${pos.entry_price}|${pos.tp_price}|${pos.sl_price}`;
  if (key === lastPositionKey) return;
  clearPositionPriceLines();
  lastPositionKey = key;

  try {
    positionPriceLines.push(
      candleSeries.createPriceLine({
        price: Number(pos.entry_price),
        color: "#38bdf8",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `ENTRADA (${(pos.mode || "paper").toUpperCase()})`,
      })
    );
    if (pos.tp_price > 0) {
      positionPriceLines.push(
        candleSeries.createPriceLine({
          price: Number(pos.tp_price),
          color: "#10b981",
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Solid,
          axisLabelVisible: true,
          title: "TAKE PROFIT",
        })
      );
    }
    if (pos.sl_price > 0) {
      positionPriceLines.push(
        candleSeries.createPriceLine({
          price: Number(pos.sl_price),
          color: "#f43f5e",
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Solid,
          axisLabelVisible: true,
          title: "STOP LOSS",
        })
      );
    }
  } catch (_) {
    // ignora erro se gráfico ainda não inicializado
  }
}

/**
 * Coleta todos os dados iniciais
 */
async function fetchInitialData() {
  if (Object.keys(instrumentsMap).length === 0) {
    await fetchInstrumentsCatalog();
    syncActiveAssetUI();
  }
  await Promise.all([
    fetchStatus(),
    fetchCandles(true),
    fetchPredictions(),
    fetchTrades(),
    fetchLogs(),
    fetchLiveTickerFallback(),
    fetchMarketDepth(),
    fetchBinanceHealth(),
    fetchWatchlist(),
    fetchXtbAccount(),
    fetchXtbCopilotSignals(),
    fetchNewsFeed(false),
    fetchAiEvolution(),
  ]);
  if (binanceHasKeys) {
    await fetchBinanceBalanceAuto(true);
  }
}

/**
 * Loop de polling leve a cada 3 segundos (sem sobreposição de requisições)
 */
async function pollDashboardData() {
  if (isPolling || document.hidden) return;
  isPolling = true;
  try {
    const now = Date.now();
    const tasks = [
      fetchStatus(),
      fetchPredictions(),
      fetchTrades(),
      fetchLogs(),
    ];
    // Se o WebSocket não estiver conectado (ou se for par Forex), usa polling de candles + ticker HTTP
    if (!liveWsConnected || isActiveForex()) {
      tasks.push(fetchCandles(false));
      tasks.push(fetchLiveTickerFallback());
    }
    if (now - lastDepthFetchAt >= 6000) {
      tasks.push(fetchMarketDepth());
    }
    if (now - lastXtbFetchAt >= 4000) {
      tasks.push(fetchXtbAccount());
    }
    if (now - lastWatchlistFetchAt >= 12000) {
      tasks.push(fetchWatchlist());
      tasks.push(fetchXtbCopilotSignals());
    }
    if (now - lastHealthFetchAt >= 15000) {
      tasks.push(fetchBinanceHealth());
    }
    if (now - lastNewsFetchAt >= 90000) {
      tasks.push(fetchNewsFeed(false));
    }
    if (binanceHasKeys && now - lastBalanceFetchAt >= 25000) {
      tasks.push(fetchBinanceBalanceAuto(false));
    }
    await Promise.all(tasks);
  } finally {
    isPolling = false;
  }
}

async function fetchLiveTickerFallback() {
  try {
    const q = new URLSearchParams({ symbol: activeSymbol });
    const res = await fetch(`/api/market/live?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.price > 0) {
      if (!liveWsConnected) updateLiveTickerPrice(data.price);
      update24hStatsUI({
        changePct: Number(data.change_24h_pct),
        high: Number(data.high_24h),
        low: Number(data.low_24h),
        quoteVolume: Number(data.volume_24h),
      });
      if (data.bid > 0 && data.ask > 0) {
        updateBidAskUI(Number(data.bid), Number(data.ask));
      }
      const latEl = document.getElementById("tickerLatency");
      if (latEl && data.latency_ms !== undefined) {
        latEl.textContent = `${Math.round(data.latency_ms)} ms`;
      }
    }
  } catch (_) {
    // fallback silencioso
  }
}

async function fetchMarketDepth() {
  lastDepthFetchAt = Date.now();
  try {
    const q = new URLSearchParams({ symbol: activeSymbol, limit: "15" });
    const res = await fetch(`/api/market/depth?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data) return;

    if (data.best_bid > 0 && data.best_ask > 0 && !liveWsConnected) {
      updateBidAskUI(Number(data.best_bid), Number(data.best_ask));
    }
    const buyPct = Number(data.buy_pressure_pct ?? 50);
    const sellPct = Number(data.sell_pressure_pct ?? (100 - buyPct));
    const buyEl = document.getElementById("mktBuyPressure");
    const sellEl = document.getElementById("mktSellPressure");
    const barEl = document.getElementById("mktPressureBar");
    if (buyEl) buyEl.textContent = `${buyPct.toFixed(0)}% C`;
    if (sellEl) sellEl.textContent = `${sellPct.toFixed(0)}% V`;
    if (barEl) barEl.style.width = `${Math.max(5, Math.min(95, buyPct)).toFixed(1)}%`;
  } catch (_) {
    // silencioso
  }
}

async function fetchBinanceHealth() {
  lastHealthFetchAt = Date.now();
  try {
    const res = await fetch("/api/binance/health");
    if (!res.ok) return;
    const h = await res.json();
    const dot = document.getElementById("mktHealthDot");
    const statusEl = document.getElementById("mktConnStatus");
    const detailEl = document.getElementById("mktConnDetail");
    const latBadge = document.getElementById("tickerLatency");

    const lat = h.latency_ms !== null && h.latency_ms !== undefined ? `${Math.round(h.latency_ms)} ms` : "-- ms";
    if (latBadge) latBadge.textContent = lat;

    if (h.ok) {
      if (dot) dot.className = `health-dot ${h.clock_synced ? "healthy" : "degraded"}`;
      if (statusEl) statusEl.textContent = h.clock_synced ? "Conectado" : "Clock Desinc.";
      const driftStr =
        h.clock_drift_ms !== null && h.clock_drift_ms !== undefined
          ? `${h.clock_drift_ms >= 0 ? "+" : ""}${h.clock_drift_ms}ms`
          : "--";
      const mirrorHost = String(h.active_mirror || "api.binance.com").replace(/^https?:\/\//, "");
      if (detailEl) detailEl.textContent = `${lat} • Clock ${driftStr} • ${mirrorHost}`;

      // Atualiza painel detalhado da aba Console & APIs
      const sysLat = document.getElementById("sysHealthLatency");
      const sysSt = document.getElementById("sysHealthStatus");
      const sysCd = document.getElementById("sysClockDrift");
      const sysMir = document.getElementById("sysActiveMirror");
      const sysSw = document.getElementById("sysMirrorSwitches");
      const sysReq = document.getElementById("sysTotalReqs");
      const sysFail = document.getElementById("sysFailRetries");
      const m = h.metrics || {};

      if (sysLat) sysLat.textContent = lat;
      if (sysSt) sysSt.textContent = h.clock_synced ? "🟢 Operacional (Healthy)" : "🟡 Alerta de Relógio";
      if (sysCd) sysCd.textContent = driftStr;
      if (sysMir) sysMir.textContent = mirrorHost;
      if (sysSw) sysSw.textContent = `Failovers: ${m.mirror_switches || 0}`;
      if (sysReq) sysReq.textContent = String(m.total_requests || 0);
      if (sysFail) sysFail.textContent = `Falhas: ${m.failed_requests || 0} • Retries: ${m.retries || 0}`;
    } else {
      if (dot) dot.className = "health-dot offline";
      if (statusEl) statusEl.textContent = "Offline";
      if (detailEl) detailEl.textContent = h.error ? String(h.error).slice(0, 42) : "Sem resposta da Binance";
      const sysSt = document.getElementById("sysHealthStatus");
      if (sysSt) sysSt.textContent = "🔴 Sem resposta da Binance";
    }
  } catch (_) {
    // silencioso
  }
}

async function fetchBinanceBalanceAuto(force = false) {
  if (!force && Date.now() - lastBalanceFetchAt < 20000) return;
  lastBalanceFetchAt = Date.now();
  try {
    const res = await fetch("/api/binance/balance");
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.success) {
      const kpiBalance = document.getElementById("kpiBinanceBalance");
      const kpiBtc = document.getElementById("kpiBinanceBtc");
      if (kpiBalance) {
        kpiBalance.innerHTML = `$${escapeHtml(
          Number(data.usdt_free || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        )} <span class="kpi-currency">USDT</span>`;
      }
      if (kpiBtc) {
        kpiBtc.textContent = `${Number(data.btc_total || 0).toFixed(6)} BTC`;
      }
    }
  } catch (_) {
    // silencioso
  }
}

/**
 * Atualiza o status geral do robô e métricas filtrados pelo timeframe selecionado
 */
async function fetchStatus() {
  try {
    const q = new URLSearchParams({ symbol: activeSymbol, timeframe: activeTimeframe });
    const res = await fetch(`/api/status?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();

    // Sincroniza tag de timeframe
    const tfTag = document.getElementById("activeTimeframeTag");
    if (tfTag) tfTag.textContent = `Candles ${activeTimeframe}`;

    // 1. Preço e Ticker (só sobrescreve via SQLite se o WebSocket ao vivo não estiver conectado)
    const currentPrice = data.latest_price || 0;
    if (!liveWsConnected && currentPrice > 0) {
      updateLiveTickerPrice(currentPrice);
    }

    // 2. Status do Robô (Online / Offline)
    isBotOnline = data.bot_online;
    const dot = document.getElementById("statusDot");
    const label = document.getElementById("statusLabel");
    const btn = document.getElementById("btnToggleBot");
    const btnText = document.getElementById("btnBotText");
    const btnIcon = document.getElementById("btnBotIcon");

    if (isBotOnline) {
      dot.className = "status-dot online";
      label.textContent = `Online (PID ${data.bot_pid} • ${data.bot_active_timeframe || activeTimeframe})`;
      btn.className = "btn btn-danger";
      btnText.textContent = "Parar Robô";
      btnIcon.textContent = "⏹";
    } else {
      dot.className = "status-dot offline";
      label.textContent = "Offline";
      btn.className = "btn btn-primary";
      btnText.textContent = "Iniciar Robô";
      btnIcon.textContent = "▶";
    }

    // 3. Saldo Simulado & PnL
    const capitalEl = document.getElementById("kpiCapital");
    const pnlBadge = document.getElementById("pnlBadge");
    const pnlUsd = document.getElementById("pnlUsd");
    if (capitalEl) {
      capitalEl.textContent = `$${(data.simulated_capital || 1000).toLocaleString("en-US", {
        minimumFractionDigits: 2,
      })}`;
    }
    if (pnlBadge && data.trade_stats) {
      const pPct = data.trade_stats.total_pnl_pct || 0;
      const pUsdVal = data.trade_stats.total_pnl_usd || 0;
      pnlBadge.textContent = `${pPct >= 0 ? "+" : ""}${pPct.toFixed(2)}%`;
      pnlBadge.className = `pnl-badge ${pPct >= 0 ? "positive" : "negative"}`;
      pnlUsd.textContent = `${pUsdVal >= 0 ? "+$" : "-$"}${Math.abs(pUsdVal).toFixed(2)} acumulado`;
    }

    // 4. Acurácia Viva
    const accVal = document.getElementById("kpiAccuracy");
    const accProg = document.getElementById("accuracyProgress");
    const accEval = document.getElementById("kpiEvaluations");
    if (accVal && data.rolling_accuracy) {
      const acc = data.rolling_accuracy.accuracy_pct || 0;
      accVal.textContent = `${acc.toFixed(1)}%`;
      if (accProg) accProg.style.width = `${Math.min(100, Math.max(5, acc))}%`;
      if (accEval) {
        accEval.textContent = `${data.rolling_accuracy.correct} acertos / ${data.rolling_accuracy.total} previsões avaliadas`;
      }
    }

    // 5. Auto-Calibração (lida do estado real do robô)
    const calibText = document.getElementById("calibModeText");
    const calibIcon = document.getElementById("calibBadgeIcon");
    const calibDesc = document.getElementById("kpiCalibDesc");
    if (calibText && data.calibration) {
      calibText.textContent = data.calibration.mode;
      calibIcon.textContent = data.calibration.badge;
      const th = Number(data.calibration.threshold || 55).toFixed(0);
      calibDesc.textContent = `Filtro de entrada ativo: ${th}%`;
    }

    // 6. Win Rate & Trades
    const winRateEl = document.getElementById("kpiWinRate");
    const tradesCountEl = document.getElementById("kpiTradesCount");
    if (winRateEl && data.trade_stats) {
      winRateEl.innerHTML = `${escapeHtml(data.trade_stats.win_rate.toFixed(1))}% <span class="kpi-small">Win Rate</span>`;
      if (tradesCountEl) {
        tradesCountEl.textContent = `${data.trade_stats.total_trades} trades (${data.trade_stats.wins}V / ${data.trade_stats.losses}D)`;
      }
    }

    // 7. Detalhes do Modelo
    const modelBadge = document.getElementById("modelVerBadge");
    const modelAcc = document.getElementById("modelInitialAcc");
    if (modelBadge && data.model) {
      modelBadge.textContent = `Versão v${data.model.version} (${data.model.timeframe || activeTimeframe})`;
      if (modelAcc) modelAcc.textContent = `${Number(data.model.accuracy || 0).toFixed(2)}%`;
    }

    const chartUpdateEl = document.getElementById("chartLastUpdate");
    if (chartUpdateEl && data.latest_update && !liveWsConnected) {
      chartUpdateEl.textContent = `Último candle (UTC): ${data.latest_update}`;
    }

    // 8. Linhas da posição aberta no gráfico (Entrada, TP, SL)
    renderOpenPositionLines(data.open_position);

    // 9. Informações da Binance
    binanceHasKeys = Boolean(data.binance && data.binance.has_keys);
    updateBinanceStatusUI(data.binance);
  } catch (err) {
    console.error("Erro ao buscar status do robô:", err);
  }
}

/**
 * Busca candles e plota no TradingView Lightweight Charts.
 * Na carga inicial (ou troca de timeframe) usa setData; nos pollings seguintes usa update() incremental.
 */
async function fetchCandles(fullReload = false) {
  if (!candleSeries) return;
  try {
    const limit = fullReload || !chartInitializedWithData ? 150 : 5;
    const q = new URLSearchParams({
      symbol: activeSymbol,
      timeframe: activeTimeframe,
      limit: String(limit),
    });
    const res = await fetch(`/api/candles?${q.toString()}`);
    if (!res.ok) return;
    const candles = await res.json();
    if (!candles || candles.length === 0) return;

    if (fullReload || !chartInitializedWithData) {
      loadedCandles = candles.slice();
      candleSeries.setData(candles);
      if (volumeSeries) {
        volumeSeries.setData(
          candles.map((c) => ({
            time: c.time,
            value: c.volume || 0,
            color: c.close >= c.open ? "rgba(16, 185, 129, 0.35)" : "rgba(244, 63, 94, 0.35)",
          }))
        );
      }
      refreshEmaSeries();
      chartInitializedWithData = true;
    } else {
      for (const c of candles) {
        candleSeries.update(c);
        if (volumeSeries) {
          volumeSeries.update({
            time: c.time,
            value: c.volume || 0,
            color: c.close >= c.open ? "rgba(16, 185, 129, 0.35)" : "rgba(244, 63, 94, 0.35)",
          });
        }
        updateLoadedCandlesWithTick(c);
      }
    }
  } catch (err) {
    console.error("Erro ao carregar candles:", err);
  }
}

/**
 * Busca histórico de previsões filtrado por timeframe e exibe na lista
 */
async function fetchPredictions() {
  const container = document.getElementById("predictionsList");
  const countBadge = document.getElementById("predictionsCountBadge");
  if (!container) return;

  try {
    const q = new URLSearchParams({
      symbol: activeSymbol,
      timeframe: activeTimeframe,
      limit: "12",
    });
    const res = await fetch(`/api/predictions?${q.toString()}`);
    if (!res.ok) return;
    const list = await res.json();

    if (countBadge) {
      countBadge.textContent = `${list.length} recentes (${activeTimeframe})`;
    }

    if (!list || list.length === 0) {
      container.innerHTML = '<div class="empty-state">Nenhuma previsão registrada ainda para este tempo gráfico.</div>';
      return;
    }

    container.innerHTML = list
      .map((item) => {
        const isUp = item.prediction === 1;
        const prob = (isUp ? item.prob_up : item.prob_down) * 100;

        let evalBadge = '<span class="eval-badge pending">⏳ Pendente</span>';
        if (item.is_correct !== null && item.is_correct !== undefined) {
          evalBadge =
            item.is_correct === 1
              ? '<span class="eval-badge correct">✅ Acerto</span>'
              : '<span class="eval-badge wrong">❌ Erro</span>';
        }

        const timeStr = item.predicted_at ? item.predicted_at.split(" ")[1] : "--:--:--";

        return `
        <div class="prediction-item">
          <div class="pred-left">
            <span class="pred-id-time">#${escapeHtml(item.id)} • ${escapeHtml(timeStr)} UTC (v${escapeHtml(item.model_version)})</span>
            <span class="pred-signal ${isUp ? "up" : "down"}">
              ${isUp ? "▲ ALTA" : "▼ BAIXA / NEUTRO"}
            </span>
          </div>
          <div class="pred-right">
            <span class="pred-prob">${escapeHtml(prob.toFixed(1))}% Confiança</span>
            ${evalBadge}
          </div>
        </div>
      `;
      })
      .join("");
  } catch (err) {
    console.error("Erro ao buscar previsões:", err);
  }
}

/**
 * Busca histórico de trades
 */
async function fetchTrades() {
  const container = document.getElementById("tradesList");
  const countBadge = document.getElementById("tradesCountBadge");
  if (!container) return;

  try {
    const q = new URLSearchParams({
      symbol: activeSymbol,
      timeframe: activeTimeframe,
      limit: "10",
    });
    const res = await fetch(`/api/trades?${q.toString()}`);
    if (!res.ok) return;
    const trades = await res.json();

    if (countBadge) {
      countBadge.textContent = `${trades.length} operações`;
    }

    if (!trades || trades.length === 0) {
      container.innerHTML = '<div class="empty-state">Nenhuma operação encerrada até o momento.</div>';
      return;
    }

    container.innerHTML = trades
      .map((t) => {
        const isWin = t.pnl_pct > 0;
        const mode = escapeHtml((t.mode || "paper").toUpperCase());
        const orderIdStr = t.order_id ? ` • ID: ${escapeHtml(String(t.order_id).slice(-6))}` : "";
        const reasonLabel =
          t.reason === "TAKE_PROFIT" ? "🎯 TAKE PROFIT" : "🚨 " + escapeHtml(t.reason || "STOP LOSS");
        return `
        <div class="trade-item">
          <div class="trade-left">
            <span class="trade-time">${escapeHtml(t.exit_time || t.entry_time)} UTC${orderIdStr}</span>
            <div style="display: flex; align-items: center; gap: 0.4rem;">
              <span style="font-weight: 600; font-size: 0.85rem; color: ${isWin ? "#10b981" : "#f43f5e"};">
                ${reasonLabel}
              </span>
              <span style="font-size: 0.65rem; padding: 1px 5px; border-radius: 4px; background: rgba(255,255,255,0.06); color: var(--text-dim); font-family: var(--font-mono);">
                ${mode}
              </span>
            </div>
          </div>
          <div class="trade-right">
            <span class="pnl-badge ${isWin ? "positive" : "negative"}">
              ${t.pnl_pct >= 0 ? "+" : ""}${escapeHtml(Number(t.pnl_pct).toFixed(2))}%
            </span>
            <span style="font-size: 0.75rem; color: var(--text-dim);">
              ${t.pnl_usd >= 0 ? "+$" : "-$"}${escapeHtml(Math.abs(Number(t.pnl_usd)).toFixed(2))}
            </span>
          </div>
        </div>
      `;
      })
      .join("");
  } catch (err) {
    console.error("Erro ao buscar trades:", err);
  }
}

/**
 * Busca logs recentes e rola para o final
 */
async function fetchLogs() {
  const body = document.getElementById("terminalBody");
  if (!body) return;

  try {
    const res = await fetch("/api/logs?lines=30");
    if (!res.ok) return;
    const data = await res.json();
    if (data.logs && data.logs.length > 0) {
      body.textContent = data.logs.join("\n");
      body.scrollTop = body.scrollHeight;
    }
  } catch (err) {
    console.error("Erro ao buscar logs:", err);
  }
}

/**
 * Inicia ou Para o Robô diretamente pela interface Web (no timeframe selecionado)
 */
async function toggleBot() {
  const btn = document.getElementById("btnToggleBot");
  btn.disabled = true;

  try {
    if (isBotOnline) {
      const res = await fetch("/api/bot/stop", {
        method: "POST",
        headers: csrfHeaders(),
      });
      const data = await res.json();
      alert(data.message || data.detail || "Robô finalizado.");
    } else {
      const q = new URLSearchParams({ symbol: activeSymbol, timeframe: activeTimeframe });
      const res = await fetch(`/api/bot/start?${q.toString()}`, {
        method: "POST",
        headers: csrfHeaders(),
      });
      const data = await res.json();
      alert(data.message || data.detail || "Robô iniciado em segundo plano!");
    }
    await fetchStatus();
  } catch (err) {
    alert("Falha ao comunicar com o servidor: " + err);
  } finally {
    btn.disabled = false;
  }
}

/**
 * Força atualização de todos os componentes
 */
function refreshDashboard() {
  fetchInitialData();
}

/* ==========================================================================
   Binance Client Logic & Modal Controller
   ========================================================================== */

let currentSelectedEnvIsTestnet = false;
let hasLoadedSavedKeys = false;
let savedApiKeyMasked = "";

function updateBinanceStatusUI(binance) {
  const btnText = document.getElementById("binanceBtnText");
  const badgeDot = document.getElementById("binanceBadgeDot");
  const tag = document.getElementById("binanceStatusTag");

  if (!btnText || !badgeDot) return;

  if (binance && binance.has_keys) {
    const modeUpper = (binance.trading_mode || "paper").toUpperCase();
    if (binance.testnet) {
      btnText.textContent = "Binance Testnet";
      badgeDot.className = "binance-badge-dot testnet";
      if (tag) {
        tag.textContent = `Testnet (${modeUpper})`;
        tag.className = "binance-status-tag testnet";
      }
    } else {
      btnText.textContent = "Binance Conectada";
      badgeDot.className = "binance-badge-dot connected";
      if (tag) {
        tag.textContent = `Conta Real (${modeUpper})`;
        tag.className = "binance-status-tag connected";
      }
    }
  } else {
    btnText.textContent = "Conectar Binance";
    badgeDot.className = "binance-badge-dot";
    if (tag) {
      tag.textContent = "Desconectada";
      tag.className = "binance-status-tag disconnected";
    }
  }
}

async function openBinanceModal() {
  const modal = document.getElementById("binanceModalOverlay");
  if (modal) {
    modal.classList.add("active");
    await loadBinanceModalData();
  }
}

function closeBinanceModal() {
  const modal = document.getElementById("binanceModalOverlay");
  if (modal) {
    modal.classList.remove("active");
  }
}

function closeBinanceModalOnOutsideClick(e) {
  if (e.target && e.target.id === "binanceModalOverlay") {
    closeBinanceModal();
  }
}

function selectEnvironment(isTestnet) {
  currentSelectedEnvIsTestnet = isTestnet;
  const btnTestnet = document.getElementById("segTestnet");
  const btnLive = document.getElementById("segLive");
  if (btnTestnet && btnLive) {
    if (isTestnet) {
      btnTestnet.classList.add("active");
      btnLive.classList.remove("active");
    } else {
      btnLive.classList.add("active");
      btnTestnet.classList.remove("active");
    }
  }

  const checked = document.querySelector('input[name="tradingMode"]:checked');
  if (
    checked &&
    ((checked.value === "testnet" && !isTestnet) || (checked.value === "live" && isTestnet))
  ) {
    const paper = document.getElementById("modePaper");
    if (paper) paper.checked = true;
  }
}

document.addEventListener("change", (e) => {
  if (e.target && e.target.name === "tradingMode") {
    if (e.target.value === "testnet") selectEnvironment(true);
    if (e.target.value === "live") selectEnvironment(false);
  }
});

async function pasteApiKey() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      document.getElementById("inputApiKey").value = text.trim();
    }
  } catch (err) {
    alert("Não foi possível ler da área de transferência. Por favor, cole manualmente (Ctrl+V).");
  }
}

function toggleSecretVisibility() {
  const secretInput = document.getElementById("inputApiSecret");
  const btn = document.getElementById("btnToggleSecret");
  if (secretInput.type === "password") {
    secretInput.type = "text";
    btn.textContent = "🔒";
  } else {
    secretInput.type = "password";
    btn.textContent = "👁️";
  }
}

function setQuickAmount(val) {
  const amountInput = document.getElementById("inputTradeAmount");
  if (amountInput) {
    amountInput.value = Number(val).toFixed(2);
  }
}

async function loadBinanceModalData() {
  try {
    const res = await fetch("/api/binance/config");
    if (!res.ok) return;
    const cfg = await res.json();

    selectEnvironment(cfg.testnet);

    const modeRadio = document.querySelector(`input[name="tradingMode"][value="${cfg.trading_mode}"]`);
    if (modeRadio) modeRadio.checked = true;

    const amountInput = document.getElementById("inputTradeAmount");
    if (amountInput && cfg.trade_amount_usdt) {
      amountInput.value = Number(cfg.trade_amount_usdt).toFixed(2);
    }

    const keyHint = document.getElementById("apiKeyStatusHint");
    const secretHint = document.getElementById("apiSecretStatusHint");
    const banner = document.getElementById("modalConnectionBanner");
    const bannerIcon = document.getElementById("modalBannerIcon");
    const bannerTitle = document.getElementById("modalBannerTitle");
    const bannerDesc = document.getElementById("modalBannerDesc");

    savedApiKeyMasked = cfg.api_key_masked || "";

    if (cfg.has_keys) {
      document.getElementById("inputApiKey").value = cfg.api_key_masked;
      document.getElementById("inputApiSecret").value = "••••••••••••••••••••••••••••••••";
      hasLoadedSavedKeys = true;

      if (keyHint) keyHint.textContent = `(Chave salva: ${cfg.api_key_masked})`;
      if (secretHint) secretHint.textContent = "(Chave secreta salva de forma segura)";

      banner.className = `binance-status-banner ${cfg.testnet ? "testnet" : "connected"}`;
      bannerIcon.textContent = cfg.testnet ? "🧪" : "✅";
      bannerTitle.textContent = cfg.testnet
        ? "Binance Testnet Conectada"
        : "Binance Spot Oficial Conectada";
      bannerDesc.textContent = `Modo Ativo: ${cfg.trading_mode.toUpperCase()} | Alocação: $${Number(
        cfg.trade_amount_usdt
      ).toFixed(2)} USDT`;
    } else {
      hasLoadedSavedKeys = false;
      document.getElementById("inputApiKey").value = "";
      document.getElementById("inputApiSecret").value = "";
      if (keyHint) keyHint.textContent = "";
      if (secretHint) secretHint.textContent = "";

      banner.className = "binance-status-banner disconnected";
      bannerIcon.textContent = "⚠️";
      bannerTitle.textContent = "Nenhuma Chave Binance Configurada";
      bannerDesc.textContent =
        "Preencha a API Key e API Secret abaixo para sincronizar seus saldos e permissões.";
    }

    if (cfg.has_keys) {
      await testBinanceConnectionModal(true);
    }
  } catch (err) {
    console.error("Erro ao carregar configurações da Binance:", err);
  }
}

async function testBinanceConnectionModal(silent = false) {
  const resultBox = document.getElementById("testResultBox");
  const spinner = document.getElementById("modalTestSpinner");
  const btnText = document.getElementById("btnModalTestText");

  if (!silent) {
    spinner.style.display = "inline";
    btnText.textContent = "Testando...";
  }

  const apiKeyInput = document.getElementById("inputApiKey").value.trim();
  const apiSecretInput = document.getElementById("inputApiSecret").value.trim();

  const isKeyMasked = apiKeyInput === savedApiKeyMasked;
  const isSecretMasked = apiSecretInput.startsWith("•") || apiSecretInput.startsWith("*");

  const payload = {
    api_key: isKeyMasked ? null : apiKeyInput || null,
    api_secret: isSecretMasked ? null : apiSecretInput || null,
    testnet: currentSelectedEnvIsTestnet,
  };

  try {
    const res = await fetch("/api/binance/test", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await res.json();

    resultBox.style.display = "flex";

    if (data.success) {
      resultBox.className = "test-result-box";

      let balancesHtml = "";
      if (data.assets && data.assets.length > 0) {
        balancesHtml = `
          <div class="test-balances-grid">
            ${data.assets
              .map(
                (a) => `
              <div class="test-balance-item">
                <span class="asset-name">${escapeHtml(a.asset)}</span>
                <span class="asset-val">${escapeHtml(
                  Number(a.free).toLocaleString("en-US", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 6,
                  })
                )}</span>
              </div>
            `
              )
              .join("")}
          </div>
        `;
      }

      resultBox.innerHTML = `
        <div class="test-result-title">
          <span>✅</span> ${escapeHtml(data.message || "Conexão estabelecida com sucesso!")}
        </div>
        <div style="font-size: 0.8rem; color: var(--text-muted);">
          Saldo USDT Livre: <strong style="color: #10b981;">$${escapeHtml(
            Number(data.usdt_free || 0).toFixed(2)
          )} USDT</strong> | 
          BTC Total: <strong style="color: #F0B90B;">${escapeHtml(
            Number(data.btc_total || 0).toFixed(6)
          )} BTC</strong>
        </div>
        ${balancesHtml}
        <div style="font-size: 0.725rem; color: var(--text-dim); display: flex; gap: 0.75rem; flex-wrap: wrap;">
          <span>Trading Spot: ${data.can_trade ? "✅ Ativo" : "❌ Inativo"}</span>
          <span>Saques: ${
            data.can_withdraw ? "⚠️ Permitido" : "🔒 Bloqueado (Segurança Máxima)"
          }</span>
          ${
            data.clock_drift_ms !== null && data.clock_drift_ms !== undefined
              ? `<span>Clock Drift: ${escapeHtml(String(data.clock_drift_ms))} ms</span>`
              : ""
          }
          ${
            data.active_mirror
              ? `<span>Endpoint: ${escapeHtml(String(data.active_mirror).replace(/^https?:\/\//, ""))}</span>`
              : ""
          }
        </div>
      `;

      const kpiBalance = document.getElementById("kpiBinanceBalance");
      const kpiBtc = document.getElementById("kpiBinanceBtc");
      if (kpiBalance)
        kpiBalance.innerHTML = `$${escapeHtml(
          Number(data.usdt_free || 0).toFixed(2)
        )} <span class="kpi-currency">USDT</span>`;
      if (kpiBtc) kpiBtc.textContent = `${Number(data.btc_total || 0).toFixed(6)} BTC`;
    } else {
      resultBox.className = "test-result-box error";
      resultBox.innerHTML = `
        <div class="test-result-title">
          <span>❌</span> Falha de Conexão com a Binance
        </div>
        <div style="font-size: 0.8rem; color: #fda4af;">
          ${escapeHtml(data.message || data.detail || "Verifique se a API Key e Secret estão corretas.")}
        </div>
        ${
          data.detail
            ? `<div style="font-size: 0.725rem; color: var(--text-dim); font-family: var(--font-mono);">${escapeHtml(
                data.detail
              )}</div>`
            : ""
        }
      `;
    }
  } catch (err) {
    resultBox.style.display = "flex";
    resultBox.className = "test-result-box error";
    resultBox.innerHTML = `
      <div class="test-result-title"><span>❌</span> Erro de Requisição</div>
      <div style="font-size: 0.8rem; color: #fda4af;">${escapeHtml(err.message || err)}</div>
    `;
  } finally {
    if (!silent) {
      spinner.style.display = "none";
      btnText.textContent = "🔍 Testar Conexão";
    }
  }
}

async function saveBinanceConnectionModal() {
  const apiKeyInput = document.getElementById("inputApiKey").value.trim();
  const apiSecretInput = document.getElementById("inputApiSecret").value.trim();
  const amountVal = parseFloat(document.getElementById("inputTradeAmount").value) || 50.0;

  const modeRadio = document.querySelector('input[name="tradingMode"]:checked');
  const tradingMode = modeRadio ? modeRadio.value : "paper";

  const finalKey = apiKeyInput === savedApiKeyMasked ? "" : apiKeyInput;
  const finalSecret =
    apiSecretInput.startsWith("•") || apiSecretInput.startsWith("*") ? "" : apiSecretInput;

  const saveBtn = document.getElementById("btnModalSave");
  const saveBtnText = document.getElementById("btnModalSaveText");
  saveBtn.disabled = true;
  saveBtnText.textContent = "Salvando...";

  try {
    const res = await fetch("/api/binance/save", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        api_key: finalKey,
        api_secret: finalSecret,
        testnet: currentSelectedEnvIsTestnet,
        trading_mode: tradingMode,
        trade_amount_usdt: amountVal,
      }),
    });

    const data = await res.json();
    if (!res.ok) {
      alert("Falha ao salvar: " + (data.detail || "Erro no servidor"));
      return;
    }

    if (data.test_result && data.test_result.success) {
      alert(
        `✅ ${data.message}\nConexão estabelecida com a ${data.test_result.environment}.\nSaldo USDT Livre: $${(
          data.test_result.usdt_free || 0
        ).toFixed(2)}`
      );
    } else {
      alert(
        `⚠️ ${data.message}\nPorém o teste de conexão retornou:\n${
          (data.test_result && data.test_result.message) || "Verifique suas chaves."
        }`
      );
    }

    closeBinanceModal();
    await fetchInitialData();
  } catch (err) {
    alert("Erro ao salvar dados: " + err);
  } finally {
    saveBtn.disabled = false;
    saveBtnText.textContent = "💾 Salvar e Aplicar";
  }
}


/* ==========================================================================
   Watchlist Multi-Ativos, Mesa Forex/Cripto (Estilo XTB) & Notícias Ao Vivo
   ========================================================================== */

async function fetchInstrumentsCatalog() {
  try {
    const res = await fetch("/api/market/instruments");
    if (!res.ok) return;
    const data = await res.json();
    const map = {};
    for (const inst of data.instruments || []) {
      map[inst.symbol] = inst;
    }
    instrumentsMap = map;
  } catch (_) {
    // fallback silencioso
  }
}

async function fetchWatchlist() {
  lastWatchlistFetchAt = Date.now();
  try {
    const res = await fetch("/api/market/watchlist?category=all");
    if (!res.ok) return;
    const data = await res.json();
    watchlistData = data.items || [];
    renderWatchlist();
  } catch (_) {
    // silencioso
  }
}

function renderWatchlist() {
  const container = document.getElementById("watchlistContainer");
  if (!container) return;

  const qInput = document.getElementById("watchlistSearch");
  const query = (qInput ? qInput.value : "").trim().toLowerCase();

  const filtered = watchlistData.filter((item) => {
    if (watchlistCategory === "commodities") {
      if (item.category !== "commodities" && item.category !== "indices") return false;
    } else if (watchlistCategory !== "all" && item.category !== watchlistCategory) {
      return false;
    }
    if (query) {
      return (
        item.symbol.toLowerCase().includes(query) ||
        (item.name || "").toLowerCase().includes(query) ||
        (item.badge || "").toLowerCase().includes(query)
      );
    }
    return true;
  });

  if (!filtered.length) {
    container.innerHTML = '<div class="empty-state">Nenhum instrumento encontrado.</div>';
    return;
  }

  container.innerHTML = filtered
    .map((item) => {
      const isAct = item.symbol === activeSymbol;
      const chg = Number(item.change_24h_pct || 0);
      const chgCls = chg >= 0 ? "pos" : "neg";
      const priceStr = item.price > 0 ? formatPrice(item.price, item.symbol) : "--";
      return `
        <div class="wl-row ${isAct ? "active" : ""}" data-symbol="${escapeHtml(item.symbol)}">
          <div class="wl-left">
            <span class="wl-sym">
              ${escapeHtml(item.symbol)}
              <span class="asset-category-pill ${item.category !== "crypto" ? "forex" : "crypto"}" style="font-size:0.56rem;padding:0.06rem 0.3rem;">
                ${escapeHtml(item.badge || "")}
              </span>
            </span>
            <span class="wl-name">${escapeHtml(item.name || "")}</span>
          </div>
          <div class="wl-right">
            <span class="wl-price">${escapeHtml(priceStr)}</span>
            <span class="wl-chg ${chgCls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".wl-row[data-symbol]").forEach((row) => {
    row.addEventListener("click", () => {
      const sym = row.dataset.symbol;
      if (sym && sym !== activeSymbol) onSymbolChange(sym);
    });
  });
}

function updateXtbMarginEstimate() {
  const lotsEl = document.getElementById("xtbLotsInput");
  const levEl = document.getElementById("xtbLeverageSelect");
  const marginEl = document.getElementById("xtbEstMargin");
  if (!lotsEl || !levEl || !marginEl) return;

  const lots = parseFloat(lotsEl.value) || 0;
  const lev = Math.max(1, parseInt(levEl.value, 10) || 1);
  const price = currentAskPrice || lastKnownPrice || 0;
  const meta = getInstrumentMeta(activeSymbol);
  const contractSize = Number(meta.contract_size || 1);

  if (lots <= 0 || price <= 0) {
    marginEl.textContent = "$0.00";
    return;
  }

  const [base, quote] = activeSymbol.split("/");
  const units = lots * contractSize;
  let notional = units * price;
  if (quote !== "USD" && quote !== "USDT") {
    notional = base === "USD" ? units : units * 1.08;
  }
  const margin = notional / lev;
  marginEl.textContent = `$${margin.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

async function submitXtbOrder(side) {
  const lots = parseFloat(document.getElementById("xtbLotsInput")?.value || "0");
  const leverage = parseInt(document.getElementById("xtbLeverageSelect")?.value || "10", 10);
  const slRaw = document.getElementById("xtbSlInput")?.value?.trim();
  const tpRaw = document.getElementById("xtbTpInput")?.value?.trim();

  if (!lots || lots <= 0) {
    showToast("Informe um volume (lotes) válido.", "error");
    return;
  }

  const payload = {
    symbol: activeSymbol,
    side,
    lots,
    leverage,
    sl_price: slRaw ? parseFloat(slRaw) : null,
    tp_price: tpRaw ? parseFloat(tpRaw) : null,
  };

  try {
    const res = await fetch("/api/xtb/order", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Não foi possível abrir a ordem.", "error");
      return;
    }
    showToast(`✅ ${data.message}`, "success");
    const slEl = document.getElementById("xtbSlInput");
    const tpEl = document.getElementById("xtbTpInput");
    if (slEl) slEl.value = "";
    if (tpEl) tpEl.value = "";
    await fetchXtbAccount();
  } catch (err) {
    showToast(`Erro ao enviar ordem: ${err}`, "error");
  }
}

async function closeXtbPosition(posId) {
  try {
    const res = await fetch(`/api/xtb/close/${posId}`, {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Erro ao fechar posição.", "error");
      return;
    }
    showToast(`💰 ${data.message}`, "success");
    await fetchXtbAccount();
  } catch (err) {
    showToast(`Erro ao fechar posição: ${err}`, "error");
  }
}

async function fetchXtbAccount() {
  lastXtbFetchAt = Date.now();
  try {
    const res = await fetch("/api/xtb/account");
    if (!res.ok) return;
    const acc = await res.json();

    xtbFreeMarginUsd = Number(acc.free_margin_usd || 0);
    const fmtUsd = (v) =>
      `$${Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const fmtSignedUsd = (v) =>
      `${Number(v || 0) >= 0 ? "+$" : "-$"}${Math.abs(Number(v || 0)).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

    const positions = acc.open_positions || [];
    const trades = acc.recent_trades || [];
    const floatPnl = Number(acc.floating_pnl_usd || 0);

    // Sidebar & Boleta
    const sideEq = document.getElementById("sideXtbEquity");
    const sideFl = document.getElementById("sideXtbFloating");
    const sideFm = document.getElementById("sideXtbFreeMargin");
    const navBadge = document.getElementById("navOpenPosBadge");
    const tkFm = document.getElementById("xtbFreeMarginTicket");
    const termPnl = document.getElementById("terminalFloatingPnl");
    const openCount = document.getElementById("openPositionsCount");

    if (sideEq) sideEq.textContent = fmtUsd(acc.equity_usd);
    if (sideFl) {
      sideFl.textContent = fmtSignedUsd(floatPnl);
      sideFl.className = `mono-val ${floatPnl >= 0 ? "text-up" : "text-down"}`;
    }
    if (sideFm) sideFm.textContent = fmtUsd(acc.free_margin_usd);
    if (navBadge) navBadge.textContent = String(positions.length);
    if (tkFm) tkFm.textContent = fmtUsd(acc.free_margin_usd);
    if (openCount) openCount.textContent = `${positions.length} aberta(s)`;
    if (termPnl) {
      termPnl.textContent = `PnL Aberto: ${fmtSignedUsd(floatPnl)}`;
      termPnl.className = `mono-val ${floatPnl >= 0 ? "text-up" : "text-down"}`;
    }

    // Aba Portfólio
    const pBal = document.getElementById("portXtbBalance");
    const pReal = document.getElementById("portXtbRealized");
    const pEq = document.getElementById("portXtbEquity");
    const pFl = document.getElementById("portXtbFloating");
    const pMar = document.getElementById("portXtbMargin");
    const pFm = document.getElementById("portXtbFreeMargin");
    if (pBal) pBal.textContent = fmtUsd(acc.balance_usd);
    if (pReal) pReal.textContent = `Realizado: ${fmtSignedUsd(acc.realized_pnl_usd)}`;
    if (pEq) pEq.textContent = fmtUsd(acc.equity_usd);
    if (pFl) pFl.textContent = `PnL Flutuante: ${fmtSignedUsd(floatPnl)}`;
    if (pMar) pMar.textContent = fmtUsd(acc.used_margin_usd);
    if (pFm) pFm.textContent = `Livre: ${fmtUsd(acc.free_margin_usd)}`;

    // Tabela de Posições Abertas (Terminal)
    const posBody = document.getElementById("xtbPositionsBody");
    if (posBody) {
      if (!positions.length) {
        posBody.innerHTML =
          '<tr><td colspan="9" class="empty-cell">Nenhuma posição aberta. Use a boleta ao lado para operar Forex ou Cripto!</td></tr>';
      } else {
        posBody.innerHTML = positions
          .map((p) => {
            const isUp = Number(p.pnl_usd) >= 0;
            const slStr = p.sl_price ? formatPrice(p.sl_price, p.symbol) : "--";
            const tpStr = p.tp_price ? formatPrice(p.tp_price, p.symbol) : "--";
            return `
              <tr>
                <td><strong>${escapeHtml(p.symbol)}</strong></td>
                <td><span class="side-pill ${p.side === "BUY" ? "buy" : "sell"}">${escapeHtml(p.side)} ${escapeHtml(p.leverage)}x</span></td>
                <td>${escapeHtml(p.lots)}</td>
                <td>${escapeHtml(formatPrice(p.entry_price, p.symbol))}</td>
                <td>${escapeHtml(formatPrice(p.current_price, p.symbol))}</td>
                <td class="dim">${escapeHtml(slStr)} / ${escapeHtml(tpStr)}</td>
                <td>${escapeHtml(fmtUsd(p.margin_usd))}</td>
                <td class="${isUp ? "text-up" : "text-down"}">
                  <strong>${escapeHtml(fmtSignedUsd(p.pnl_usd))}</strong> (${Number(p.pnl_pct) >= 0 ? "+" : ""}${Number(p.pnl_pct).toFixed(2)}%)
                </td>
                <td>
                  <button type="button" class="btn-close-pos" data-close-id="${escapeHtml(p.id)}">Fechar</button>
                </td>
              </tr>
            `;
          })
          .join("");

        posBody.querySelectorAll(".btn-close-pos[data-close-id]").forEach((btn) => {
          btn.addEventListener("click", () => closeXtbPosition(Number(btn.dataset.closeId)));
        });
      }
    }

    // Tabela de Histórico XTB (Portfólio)
    const histBody = document.getElementById("xtbTradesHistoryBody");
    const histCount = document.getElementById("xtbTradesCount");
    if (histCount) histCount.textContent = `${trades.length} encerrada(s)`;
    if (histBody) {
      if (!trades.length) {
        histBody.innerHTML = '<tr><td colspan="9" class="empty-cell">Nenhuma operação manual encerrada ainda.</td></tr>';
      } else {
        histBody.innerHTML = trades
          .map((t) => {
            const isWin = Number(t.pnl_usd) >= 0;
            return `
              <tr>
                <td class="dim">${escapeHtml(t.closed_at)}</td>
                <td><strong>${escapeHtml(t.symbol)}</strong></td>
                <td><span class="side-pill ${t.side === "BUY" ? "buy" : "sell"}">${escapeHtml(t.side)}</span></td>
                <td>${escapeHtml(t.lots)}</td>
                <td>${escapeHtml(formatPrice(t.entry_price, t.symbol))}</td>
                <td>${escapeHtml(formatPrice(t.exit_price, t.symbol))}</td>
                <td>${escapeHtml(t.reason)}</td>
                <td class="${isWin ? "text-up" : "text-down"}"><strong>${escapeHtml(fmtSignedUsd(t.pnl_usd))}</strong></td>
                <td class="${isWin ? "text-up" : "text-down"}">${Number(t.pnl_pct) >= 0 ? "+" : ""}${Number(t.pnl_pct).toFixed(2)}%</td>
              </tr>
            `;
          })
          .join("");
      }
    }
  } catch (_) {
    // silencioso
  }
}

async function fetchNewsFeed(forceRefresh = false) {
  lastNewsFetchAt = Date.now();
  try {
    const qInput = document.getElementById("newsSearchInput");
    const qVal = qInput ? qInput.value.trim() : "";
    const params = new URLSearchParams({
      category: newsCategory,
      q: qVal,
      limit: "36",
      refresh: forceRefresh ? "true" : "false",
    });
    const res = await fetch(`/api/news?${params.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    newsCacheItems = data.items || [];

    const updEl = document.getElementById("newsLastUpdated");
    if (updEl && data.updated_at) updEl.textContent = `Atualizado ${data.updated_at}`;

    renderMiniNews(newsCacheItems.slice(0, 4));
    renderFullNews(newsCacheItems);
  } catch (_) {
    // silencioso
  }
}

function sentimentBadgeHtml(sent) {
  if (sent === "positivo") return '<span class="sent-chip" style="color:#10b981;">🟢 Alta</span>';
  if (sent === "negativo") return '<span class="sent-chip" style="color:#f43f5e;">🔴 Baixa</span>';
  return '<span class="sent-chip" style="color:#94a3b8;">⚪ Neutro</span>';
}

function renderMiniNews(items) {
  const container = document.getElementById("miniNewsList");
  if (!container) return;
  if (!items || !items.length) {
    container.innerHTML = '<div class="empty-state">Sem manchetes disponíveis no momento.</div>';
    return;
  }
  container.innerHTML = items
    .map(
      (n) => `
      <a class="mini-news-item" href="${escapeHtml(n.link || "#")}" target="_blank" rel="noopener noreferrer">
        <span class="mini-news-title">${escapeHtml(n.title)}</span>
        <div class="mini-news-meta">
          <span>${escapeHtml(n.source)} • ${escapeHtml((n.category || "").toUpperCase())}</span>
          <span>${escapeHtml(n.published_at)}</span>
        </div>
      </a>
    `
    )
    .join("");
}

function renderFullNews(items) {
  const grid = document.getElementById("fullNewsGrid");
  if (!grid) return;

  let pos = 0;
  let neg = 0;
  let neu = 0;
  for (const n of items) {
    if (n.sentiment === "positivo") pos += 1;
    else if (n.sentiment === "negativo") neg += 1;
    else neu += 1;
  }
  const elPos = document.getElementById("sentCountPos");
  const elNeg = document.getElementById("sentCountNeg");
  const elNeu = document.getElementById("sentCountNeu");
  if (elPos) elPos.textContent = `🟢 ${pos} Alta`;
  if (elNeg) elNeg.textContent = `🔴 ${neg} Baixa`;
  if (elNeu) elNeu.textContent = `⚪ ${neu} Neutro`;

  if (!items || !items.length) {
    grid.innerHTML = '<div class="empty-state">Nenhuma notícia encontrada para esse filtro.</div>';
    return;
  }

  grid.innerHTML = items
    .map(
      (n) => `
      <a class="news-card" href="${escapeHtml(n.link || "#")}" target="_blank" rel="noopener noreferrer">
        <div class="news-card-top">
          <span class="news-source-tag">${escapeHtml(n.source)} • ${escapeHtml((n.category || "").toUpperCase())}</span>
          ${sentimentBadgeHtml(n.sentiment)}
        </div>
        <div class="news-card-title">${escapeHtml(n.title)}</div>
        ${n.summary ? `<div class="news-card-summary">${escapeHtml(n.summary)}</div>` : ""}
        <div class="news-card-footer">
          <span>${escapeHtml(n.published_at)}</span>
          <span style="color: var(--accent-cyan); font-weight: 600;">Ler matéria ↗</span>
        </div>
      </a>
    `
    )
    .join("");
}


/* ==========================================================================
   Centro de Treinamento da IA (Walk-Forward + Comparativo de Arquiteturas)
   ========================================================================== */

let aiTrainPollTimer = null;

async function startAiTraining() {
  if (isActiveForex()) {
    showToast("Selecione um par Cripto (ex: BTC/USDT) para treinar o modelo da Binance.", "error");
    return;
  }
  const btn = document.getElementById("btnStartAiTrain");
  const trainTf = document.getElementById("aiTrainTimeframe")?.value || "1h";
  const candles = document.getElementById("aiTrainCandles")?.value || "3000";
  if (btn) btn.disabled = true;

  try {
    const q = new URLSearchParams({
      symbol: activeSymbol,
      timeframe: trainTf,
      candles: String(candles),
    });
    const res = await fetch(`/api/ai/train?${q.toString()}`, {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao iniciar treinamento.", "error");
      if (btn) btn.disabled = false;
      return;
    }
    showToast(`🧠 ${data.message}`, "success");
    pollAiTrainStatus();
    if (aiTrainPollTimer) clearInterval(aiTrainPollTimer);
    aiTrainPollTimer = setInterval(pollAiTrainStatus, 2000);
  } catch (err) {
    showToast(`Erro ao iniciar treino: ${err}`, "error");
    if (btn) btn.disabled = false;
  }
}

async function pollAiTrainStatus() {
  try {
    const res = await fetch("/api/ai/train/status");
    if (!res.ok) return;
    const st = await res.json();

    const stageEl = document.getElementById("aiTrainStage");
    const progEl = document.getElementById("aiTrainProgress");
    const btn = document.getElementById("btnStartAiTrain");
    const tbody = document.getElementById("aiTrainResultsBody");

    if (stageEl) {
      stageEl.textContent = st.error ? `Erro: ${st.error}` : st.stage || "Pronto";
    }
    if (progEl) {
      progEl.style.width = `${Math.max(0, Math.min(100, Number(st.progress_pct || 0)))}%`;
    }
    if (btn) {
      btn.disabled = Boolean(st.running);
      btn.textContent = st.running ? "⏳ Treinando..." : "🧠 Treinar IA Agora";
    }

    if (!st.running && aiTrainPollTimer) {
      clearInterval(aiTrainPollTimer);
      aiTrainPollTimer = null;
      if (st.last_result) {
        showToast(`✅ Treinamento concluído! ${st.stage}`, "success");
        fetchStatus();
      } else if (st.error) {
        showToast(`❌ ${st.error}`, "error");
      }
    }

    if (tbody && st.last_result && Array.isArray(st.last_result.candidates)) {
      const winnerType = st.last_result.winner?.model_type || "";
      tbody.innerHTML = st.last_result.candidates
        .map((c) => {
          const isWin = c.model_type === winnerType;
          const pnl = Number(c.sim_pnl_pct || 0);
          return `
            <tr>
              <td><strong>${escapeHtml(String(c.model_type).toUpperCase())}</strong></td>
              <td>${isWin ? '<span class="xtb-tag">🏆 CAMPEÃO</span>' : '<span class="dim">Candidato</span>'}</td>
              <td>${(Number(c.precision_buy || 0) * 100).toFixed(1)}%</td>
              <td>${Number(c.profit_factor || 0).toFixed(2)}</td>
              <td>${Number(c.roc_auc || 0.5).toFixed(3)}</td>
              <td class="${pnl >= 0 ? "text-up" : "text-down"}"><strong>${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%</strong></td>
              <td>${(Number(c.optimal_threshold || 0.55) * 100).toFixed(0)}%</td>
              <td>${escapeHtml(c.buy_signals)}</td>
            </tr>
          `;
        })
        .join("");
    }
  } catch (_) {
    // silencioso
  }
}


async function fetchMacroIntelligence(forceRefresh = false) {
  try {
    const res = await fetch(`/api/macro/live?refresh=${forceRefresh ? "true" : "false"}`);
    if (!res.ok) return;
    const m = await res.json();

    const set = (id, text, cls = null) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      if (cls !== null) el.className = cls;
    };

    set("macroRegimeBadge", `${m.badge || "⚖️"} ${m.regime || "MACRO NEUTRO"}`);
    set("macroRegimeDesc", m.description || "Contexto macro atualizado.");

    set("macroDxyVal", m.dxy_price > 0 ? Number(m.dxy_price).toFixed(2) : "--");
    const dxyChg = Number(m.dxy_change_pct || 0);
    set("macroDxyChg", `${dxyChg >= 0 ? "+" : ""}${dxyChg.toFixed(2)}% (hoje)`, `market-metric-sub ${dxyChg <= 0 ? "text-up" : "text-down"}`);

    set("macroUs10yVal", m.us10y_yield > 0 ? `${Number(m.us10y_yield).toFixed(2)}%` : "--");
    const yChg = Number(m.us10y_change_pct || 0);
    set("macroUs10yChg", `${yChg >= 0 ? "+" : ""}${yChg.toFixed(2)}%`, `market-metric-sub ${yChg <= 0 ? "text-up" : "text-down"}`);

    set("macroSp500Val", m.sp500_price > 0 ? Number(m.sp500_price).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--");
    const spChg = Number(m.sp500_change_pct || 0);
    set("macroSp500Chg", `${spChg >= 0 ? "+" : ""}${spChg.toFixed(2)}%`, `market-metric-sub ${spChg >= 0 ? "text-up" : "text-down"}`);

    set("macroFngVal", m.fear_greed_value !== null && m.fear_greed_value !== undefined ? `${m.fear_greed_value}/100` : "--");
    set("macroFngLabel", m.fear_greed_label || "N/D");

    const ns = Number(m.news_sentiment_score || 0);
    set("macroNewsScore", `${ns >= 0 ? "+" : ""}${(ns * 100).toFixed(0)}%`);
    const fb = Number(m.inflation_fed_bias || 0);
    set("macroFedBias", `Fed/Inflação: ${fb >= 0 ? "+" : ""}${(fb * 100).toFixed(0)}% (${m.inflation_fed_mentions || 0}x)`);

    const sc = Number(m.macro_score || 0);
    set("macroTotalScore", `${sc >= 0 ? "+" : ""}${sc.toFixed(1)}`, `market-metric-value ${sc >= 0 ? "text-up" : "text-down"}`);
    const adj = Math.round(Number(m.confidence_adjustment || 0) * 100);
    set("macroAdjFilter", `Ajuste filtro: ${adj >= 0 ? "+" : ""}${adj}%`);
  } catch (_) {
    // silencioso
  }
}


async function fetchAiEvolution() {
  try {
    const q = new URLSearchParams({
      symbol: isActiveForex() ? "BTC/USDT" : activeSymbol,
      timeframe: "1h",
    });
    const res = await fetch(`/api/ai/evolution?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    const evo = data.evolution || {};
    const m = data.macro || {};
    const p = evo.pillars || {};

    const pct = Math.max(0, Math.min(100, Number(evo.progress_pct || 0)));
    const set = (id, text, cls = null) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      if (cls !== null) el.className = cls;
    };

    // Sidebar
    set("sideEvoPct", `${pct.toFixed(0)}%`);
    set("sideEvoLevel", `Nível ${evo.level || 1} • ${evo.stage_name || ""}`);
    set("sideEvoStatus", evo.adaptation_tone === "warning" ? "Adaptando..." : "Sincronizada");
    const sideBar = document.getElementById("sideEvoBar");
    if (sideBar) sideBar.style.width = `${pct}%`;

    // Card Principal
    const mainBar = document.getElementById("evoMainBar");
    if (mainBar) mainBar.style.width = `${pct}%`;
    set("evoLevelBadge", `Nível ${evo.level || 1} • ${evo.stage_name || ""} (${pct.toFixed(0)}%)`);
    set("evoAdaptationBadge", evo.adaptation_status || "🟢 Sincronizada");
    set("evoNextMilestone", `Próximo marco: ${evo.next_milestone || "Evolução contínua"}`);

    set("evoCandlesVal", `${Number(p.candles_count || 0).toLocaleString("pt-BR")} / ${Number(p.candles_target || 3000).toLocaleString("pt-BR")}`);
    set("evoMacroVal", `${p.macro_snapshots || 0} / ${p.macro_target || 100}`);
    set("evoCyclesVal", `${p.learning_cycles || 0} ciclo(s) (v${p.model_version || 1})`);
    set("evoAccVal", p.eval_total > 0 ? `${Number(p.eval_accuracy_pct || 0).toFixed(1)}% (${p.eval_total})` : "Coletando...");

    // Radar Global: Petróleo, Ouro/Prata, Terras Raras, Cobre, Guerras & Clima
    const oilChg = Number(m.oil_change_pct || 0);
    set("globOilVal", m.oil_price > 0 ? `$${Number(m.oil_price).toFixed(2)}` : "--");
    set("globOilChg", `${oilChg >= 0 ? "+" : ""}${oilChg.toFixed(2)}%`, `market-metric-sub ${oilChg <= 2.0 ? "text-up" : "text-down"}`);

    const goldChg = Number(m.gold_change_pct || 0);
    const silChg = Number(m.silver_change_pct || 0);
    set("globGoldVal", m.gold_price > 0 ? `$${Number(m.gold_price).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "--");
    set("globSilverChg", `Ouro ${goldChg >= 0 ? "+" : ""}${goldChg.toFixed(1)}% • Prata ${silChg >= 0 ? "+" : ""}${silChg.toFixed(1)}%`);

    const reChg = Number(m.rare_earth_change_pct || 0);
    set("globRareVal", m.rare_earth_price > 0 ? `$${Number(m.rare_earth_price).toFixed(2)}` : "--");
    set("globRareChg", `${reChg >= 0 ? "+" : ""}${reChg.toFixed(2)}%`, `market-metric-sub ${reChg >= 0 ? "text-up" : "text-down"}`);

    const copChg = Number(m.copper_change_pct || 0);
    set("globCopperVal", m.copper_price > 0 ? `$${Number(m.copper_price).toFixed(2)}` : "--");
    set("globCopperChg", `${copChg >= 0 ? "+" : ""}${copChg.toFixed(2)}%`, `market-metric-sub ${copChg >= 0 ? "text-up" : "text-down"}`);

    const warM = Number(m.war_geopolitics_mentions || 0);
    set("globWarVal", `${warM} notícia(s)`, `market-metric-value ${warM >= 3 ? "text-down" : "text-up"}`);
    set("globWarNote", warM >= 3 ? "Atenção geopolítica elevada" : "Estável nas manchetes");

    const cliM = Number(m.climate_energy_mentions || 0);
    const minM = Number(m.rare_earth_minerals_mentions || 0);
    set("globClimateVal", `${cliM} clima/energia`);
    set("globMineralsNote", `Minérios/Terras Raras: ${minM}`);
  } catch (_) {
    // silencioso
  }
}

/* ==========================================================================
   Copiloto XTB (Modo Híbrido: Binance Automática + Sinais xStation 5)
   ========================================================================== */

let copilotSoundEnabled = true;
let lastKnownCopilotSignalId = 0;

function toggleCopilotSound() {
  copilotSoundEnabled = !copilotSoundEnabled;
  const btn = document.getElementById("btnCopilotSound");
  if (btn) btn.textContent = copilotSoundEnabled ? "🔊 Som: ON" : "🔇 Som: OFF";
  if (copilotSoundEnabled) playCopilotChime();
}

function playCopilotChime() {
  if (!copilotSoundEnabled) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(1174.66, ctx.currentTime + 0.14);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.36);
  } catch (_) {
    // autoplay bloqueado antes de interação do usuário
  }
}

async function fetchXtbCopilotSignals() {
  try {
    const res = await fetch("/api/xtb/copilot/signals?limit=25");
    if (!res.ok) return;
    const data = await res.json();

    const statusEl = document.getElementById("copilotMarketStatus");
    const badgeEl = document.getElementById("copilotModeBadge");
    if (statusEl) {
      statusEl.textContent = `Binance: Cripto Automático • XTB: ${data.status_message || "Monitorando"}`;
    }
    if (badgeEl) {
      badgeEl.textContent = data.market_open
        ? `🟢 XTB ABERTO • ${data.new_count || 0} novo(s)`
        : "⏸️ XTB FECHADO (FDS)";
    }

    const tgStatus = document.getElementById("tgConfigStatus");
    const tgChatInp = document.getElementById("tgChatIdInput");
    if (data.telegram) {
      if (tgStatus) {
        tgStatus.textContent = data.telegram.configured
          ? `✅ Telegram conectado (Chat ${data.telegram.chat_id}) — alertas ativos!`
          : "Opcional: informe Bot Token e Chat ID para receber alertas no celular.";
      }
      if (tgChatInp && !tgChatInp.value && data.telegram.chat_id) {
        tgChatInp.value = data.telegram.chat_id;
      }
    }

    const signals = data.signals || [];
    if (signals.length > 0) {
      const topId = Number(signals[0].id || 0);
      if (lastKnownCopilotSignalId > 0 && topId > lastKnownCopilotSignalId && signals[0].status === "NEW") {
        playCopilotChime();
        showToast(
          `🧭 Novo sinal XTB: ${signals[0].xtb_symbol} ${signals[0].side} @ ${signals[0].entry_price}`,
          "success"
        );
      }
      lastKnownCopilotSignalId = Math.max(lastKnownCopilotSignalId, topId);
    }

    renderXtbCopilotTable(signals);
  } catch (_) {
    // silencioso
  }
}

function renderXtbCopilotTable(signals) {
  const tbody = document.getElementById("xtbCopilotBody");
  if (!tbody) return;
  if (!signals || !signals.length) {
    tbody.innerHTML =
      '<tr><td colspan="10" class="empty-cell">Nenhum sinal XTB gerado ainda. Clique em "Escanear XTB Agora" quando o mercado global estiver aberto.</td></tr>';
    return;
  }

  tbody.innerHTML = signals
    .map((s) => {
      const sideCls = s.side === "BUY" ? "buy" : "sell";
      const isDone = s.status === "EXECUTED_XTB";
      const isDismissed = s.status === "DISMISSED";
      const statusHtml = isDone
        ? '<span class="copilot-mini-btn done">✅ Na XTB</span>'
        : isDismissed
        ? '<span class="dim">Dispensado</span>'
        : `
          <button type="button" class="copilot-mini-btn" data-cop-copy="${escapeHtml(s.id)}">📋 Copiar</button>
          <button type="button" class="copilot-mini-btn" data-cop-exec="${escapeHtml(s.id)}">✅ Executei</button>
          <button type="button" class="copilot-mini-btn" data-cop-dis="${escapeHtml(s.id)}">✖️</button>
        `;
      return `
        <tr style="${isDismissed ? "opacity:0.48;" : ""}">
          <td class="dim">${escapeHtml(String(s.created_at || "").replace(" UTC", ""))}</td>
          <td><strong>${escapeHtml(s.xtb_symbol)}</strong> <span class="dim">(${escapeHtml(s.symbol)})</span></td>
          <td><span class="side-pill ${sideCls}">${escapeHtml(s.side)}</span></td>
          <td><strong>${Number(s.score || 0).toFixed(0)}/100</strong></td>
          <td class="mono-val">${escapeHtml(formatPrice(s.entry_price, s.symbol))}</td>
          <td class="mono-val text-down">${s.sl_price ? escapeHtml(formatPrice(s.sl_price, s.symbol)) : "--"}</td>
          <td class="mono-val text-up">${s.tp_price ? escapeHtml(formatPrice(s.tp_price, s.symbol)) : "--"}</td>
          <td class="mono-val">${escapeHtml(s.suggested_lots)} (1:${escapeHtml(s.leverage)})</td>
          <td class="dim">${escapeHtml(s.reason || "")}</td>
          <td><div class="copilot-btn-row">${statusHtml}</div></td>
        </tr>
      `;
    })
    .join("");

  const byId = {};
  for (const s of signals) byId[String(s.id)] = s;

  tbody.querySelectorAll("[data-cop-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sig = byId[btn.dataset.copCopy];
      if (!sig) return;
      try {
        await navigator.clipboard.writeText(sig.copy_text || `${sig.xtb_symbol} ${sig.side} @ ${sig.entry_price}`);
        showToast(`📋 Ordem ${sig.xtb_symbol} copiada para a xStation 5!`, "success");
      } catch (_) {
        showToast("Não foi possível copiar automaticamente.", "error");
      }
    });
  });

  tbody.querySelectorAll("[data-cop-exec]").forEach((btn) => {
    btn.addEventListener("click", () => updateCopilotSignalStatus(Number(btn.dataset.copExec), "EXECUTED_XTB"));
  });

  tbody.querySelectorAll("[data-cop-dis]").forEach((btn) => {
    btn.addEventListener("click", () => updateCopilotSignalStatus(Number(btn.dataset.copDis), "DISMISSED"));
  });
}

async function scanXtbCopilotNow() {
  const btn = document.getElementById("btnScanXtbNow");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Escaneando...";
  }
  try {
    const res = await fetch("/api/xtb/copilot/scan", {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao escanear mercados XTB.", "error");
      return;
    }
    if (!data.market_open) {
      showToast(`⏸️ ${data.status_message}`, "info");
    } else if (data.last_action) {
      playCopilotChime();
      showToast(`🧭 ${data.last_action}`, "success");
    } else {
      const top = (data.opportunities || [])[0];
      const msg = top
        ? `Varredura concluída. Melhor score atual: ${top.symbol} (${top.side} ${Number(top.score || 0).toFixed(0)}/100) — aguardando >= 72.`
        : "Varredura concluída. Nenhuma entrada forte acima de 72/100 agora.";
      showToast(msg, "info");
    }
    await fetchXtbCopilotSignals();
  } catch (err) {
    showToast(`Erro no scanner XTB: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🔎 Escanear XTB Agora";
    }
  }
}

async function updateCopilotSignalStatus(signalId, status) {
  try {
    const res = await fetch(`/api/xtb/copilot/signals/${signalId}/status`, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ status }),
    });
    if (!res.ok) return;
    showToast(status === "EXECUTED_XTB" ? "✅ Marcado como executado na XTB!" : "Sinal dispensado.", "info");
    await fetchXtbCopilotSignals();
  } catch (_) {
    // silencioso
  }
}

async function saveTelegramConfigUI() {
  const token = document.getElementById("tgBotTokenInput")?.value?.trim() || "";
  const chatId = document.getElementById("tgChatIdInput")?.value?.trim() || "";
  if (!chatId) {
    showToast("Informe o seu Chat ID do Telegram.", "error");
    return;
  }
  const btn = document.getElementById("btnSaveTelegram");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Testando...";
  }
  try {
    const res = await fetch("/api/xtb/copilot/telegram", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ bot_token: token, chat_id: chatId, send_test: true }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao salvar Telegram.", "error");
      return;
    }
    if (data.test_result && data.test_result.sent) {
      showToast("✅ Telegram conectado! Mensagem de teste enviada.", "success");
    } else {
      showToast(`⚠️ Salvo, mas o teste falhou: ${(data.test_result && data.test_result.error) || "verifique token/chat_id"}`, "error");
    }
    await fetchXtbCopilotSignals();
  } catch (err) {
    showToast(`Erro ao salvar Telegram: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "💾 Salvar & Testar";
    }
  }
}
