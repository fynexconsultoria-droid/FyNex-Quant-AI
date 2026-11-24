/**
 * Estado Global Centralizado do Dashboard (FyNex Quant AI)
 */

export const TIMEFRAME_SECONDS = {
  "1m": 60,
  "3m": 180,
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
};

export const state = {
  // Gráfico TradingView
  tvChart: null,
  candleSeries: null,
  volumeSeries: null,
  ema9Series: null,
  ema21Series: null,
  showEmaOverlay: true,
  loadedCandles: [],
  positionPriceLines: [],
  lastPositionKey: "",
  chartInitializedWithData: false,

  // Robô & Polling
  isBotOnline: false,
  pollingInterval: null,
  countdownInterval: null,
  isPolling: false,
  lastKnownPrice: 0,
  activeSymbol: "BTC/USDT",
  activeTimeframe: "1h",

  // WebSocket Binance
  liveWs: null,
  liveWsConnected: false,
  liveWsReconnectTimer: null,
  liveWsWatchdogTimer: null,
  liveWsReconnectAttempts: 0,
  liveWsEndpointIndex: 0,
  lastWsMessageAt: 0,
  livePriceResetTimer: null,

  // Timestamps de throttling do polling
  lastDepthFetchAt: 0,
  lastHealthFetchAt: 0,
  lastBalanceFetchAt: 0,
  lastWatchlistFetchAt: 0,
  lastNewsFetchAt: 0,
  lastXtbFetchAt: 0,

  // Mercado & Carteira
  binanceHasKeys: false,
  marketHigh24h: 0,
  marketLow24h: 0,
  activeView: "terminal",
  instrumentsMap: {},
  watchlistData: [],
  watchlistCategory: "all",
  newsCategory: "all",
  newsCacheItems: [],
  currentBidPrice: 0,
  currentAskPrice: 0,
  xtbFreeMarginUsd: 10000,
};

// Callbacks registrados pelo `main.js` para evitar imports circulares entre módulos
export const hooks = {
  onSymbolChange: null,
  onBidAskUpdated: null,
  onRefreshAll: null,
  onRefreshStatus: null,
};
