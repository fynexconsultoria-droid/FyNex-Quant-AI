/**
 * Entrypoint Modular do Dashboard FyNex Quant AI (ES Modules)
 * Coordena navegação entre views, troca de ativo/timeframe e vinculação de eventos.
 */

import {
  fetchAiErrorLessons,
  fetchAiEvolution,
  fetchMacroIntelligence,
  pollAiTrainStatus,
  startAiTraining,
} from "./ai_center.js";
import { fetchAiPerformanceAndWelcome, sendAiChatMessage, toggleAiChatDrawer } from "./ai_chat.js";
import {
  bindTradingModeListener,
  closeBinanceModal,
  closeBinanceModalOnOutsideClick,
  openBinanceModal,
  pasteApiKey,
  saveBinanceConnectionModal,
  selectEnvironment,
  setQuickAmount,
  testBinanceConnectionModal,
  toggleSecretVisibility,
} from "./binance_modal.js";
import {
  clearPositionPriceLines,
  fitChartContent,
  initTradingViewChart,
  resizeChart,
  toggleEmaOverlay,
} from "./chart.js";
import {
  fetchWhatsAppConfigUI,
  fetchXtbCopilotSignals,
  saveTelegramConfigUI,
  saveWhatsAppConfigUI,
  scanXtbCopilotNow,
  sendDailySummaryWhatsAppUI,
  toggleCopilotSound,
} from "./copilot_alerts.js";
import { fetchNewsFeed } from "./news.js";
import { hooks, state } from "./state.js";
import { connectLiveMarketStream, disconnectLiveMarketStream, updateCandleCountdown } from "./stream.js";
import {
  fetchBinanceBalanceAuto,
  fetchBinanceHealth,
  fetchInitialData,
  fetchLogs,
  fetchStatus,
  pollDashboardData,
  toggleBot,
} from "./terminal.js";
import { getInstrumentMeta } from "./utils.js";
import { fetchXtbAccount, renderWatchlist, submitXtbOrder, updateXtbMarginEstimate } from "./xtb_desk.js";

function syncActiveAssetUI() {
  const meta = getInstrumentMeta(state.activeSymbol);
  const cat = meta.category || "crypto";
  const isGlobal = cat !== "crypto";

  const catPill = document.getElementById("activeCategoryPill");
  if (catPill) {
    catPill.textContent = meta.badge || (isGlobal ? "GLOBAL" : "CRIPTO");
    catPill.className = `asset-category-pill ${isGlobal ? "forex" : "crypto"}`;
  }

  const titleEl = document.getElementById("chartTitle");
  if (titleEl) {
    if (cat === "commodities") titleEl.textContent = `${state.activeSymbol} • Metais & Commodities (Modo XTB)`;
    else if (cat === "indices") titleEl.textContent = `${state.activeSymbol} • Índice Global (Modo XTB)`;
    else if (cat === "forex") titleEl.textContent = `${state.activeSymbol} • Forex (Modo XTB)`;
    else titleEl.textContent = `${state.activeSymbol} • Binance Spot`;
  }

  const tkSym = document.getElementById("xtbTicketSymbol");
  const tkName = document.getElementById("xtbTicketName");
  const tkBadge = document.getElementById("xtbTicketBadge");
  const tkHint = document.getElementById("xtbContractHint");
  const lotsInp = document.getElementById("xtbLotsInput");
  const levSel = document.getElementById("xtbLeverageSelect");

  if (tkSym) tkSym.textContent = state.activeSymbol;
  if (tkName) tkName.textContent = meta.name || state.activeSymbol;
  if (tkBadge) tkBadge.textContent = meta.badge || (isGlobal ? "GLOBAL" : "CRIPTO");
  if (tkHint) {
    tkHint.textContent = isGlobal
      ? `1 lote = ${Number(meta.contract_size).toLocaleString("en-US")} un.`
      : `1 lote = 1 ${state.activeSymbol.split("/")[0]}`;
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

async function onSymbolChange(newSym) {
  state.activeSymbol = newSym || "BTC/USDT";
  const sel = document.getElementById("symbolSelect");
  if (sel && sel.value !== state.activeSymbol) sel.value = state.activeSymbol;

  state.lastKnownPrice = 0;
  state.currentBidPrice = 0;
  state.currentAskPrice = 0;
  state.marketHigh24h = 0;
  state.marketLow24h = 0;
  state.chartInitializedWithData = false;
  state.loadedCandles = [];
  clearPositionPriceLines();
  syncActiveAssetUI();
  renderWatchlist();
  await fetchInitialData(syncActiveAssetUI);
  connectLiveMarketStream();
}

async function onTimeframeChange(newTf) {
  state.activeTimeframe = newTf || "1m";
  state.chartInitializedWithData = false;
  state.loadedCandles = [];
  clearPositionPriceLines();
  const tag = document.getElementById("activeTimeframeTag");
  if (tag) tag.textContent = `Candles ${state.activeTimeframe}`;
  updateCandleCountdown();
  await fetchInitialData(syncActiveAssetUI);
  connectLiveMarketStream();
}

function switchView(viewName) {
  state.activeView = viewName || "terminal";
  document.querySelectorAll(".view-section").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${state.activeView}`);
  });
  document.querySelectorAll(".sidebar-nav .nav-item[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === state.activeView);
  });

  if (state.activeView === "terminal") setTimeout(resizeChart, 30);
  if (state.activeView === "ai") {
    fetchMacroIntelligence(false);
    fetchAiEvolution();
    fetchAiErrorLessons();
    fetchXtbCopilotSignals();
    fetchAiPerformanceAndWelcome();
    pollAiTrainStatus();
  }
  if (state.activeView === "news" && state.newsCacheItems.length === 0) fetchNewsFeed(false);
  if (state.activeView === "portfolio") {
    fetchXtbAccount();
    if (state.binanceHasKeys) fetchBinanceBalanceAuto(false);
  }
  if (state.activeView === "system") {
    fetchBinanceHealth(true);
    fetchLogs();
  }
}

function bindUIEvents() {
  const on = (id, evt, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(evt, handler);
  };

  on("symbolSelect", "change", (e) => onSymbolChange(e.target.value));
  on("timeframeSelect", "change", (e) => onTimeframeChange(e.target.value));
  on("btnToggleEma", "click", toggleEmaOverlay);
  on("btnFitChart", "click", fitChartContent);
  on("btnOpenBinance", "click", openBinanceModal);
  on("binanceWalletCard", "click", openBinanceModal);
  on("btnToggleBot", "click", toggleBot);
  on("btnRefreshDashboard", "click", () => fetchInitialData(syncActiveAssetUI));

  on("binanceModalOverlay", "click", closeBinanceModalOnOutsideClick);
  on("btnCloseBinanceModal", "click", closeBinanceModal);
  on("segTestnet", "click", () => selectEnvironment(true));
  on("segLive", "click", () => selectEnvironment(false));
  on("btnPasteApiKey", "click", pasteApiKey);
  on("btnToggleSecret", "click", toggleSecretVisibility);
  on("btnModalTest", "click", () => testBinanceConnectionModal(false));
  on("btnModalSave", "click", saveBinanceConnectionModal);

  document.querySelectorAll(".quick-btn[data-amount]").forEach((btn) => {
    btn.addEventListener("click", () => setQuickAmount(Number(btn.dataset.amount)));
  });

  document.querySelectorAll(".sidebar-nav .nav-item[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  on("btnGoToNews", "click", () => switchView("news"));

  document.querySelectorAll("#watchlistTabs .wl-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.watchlistCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#watchlistTabs .wl-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderWatchlist();
    });
  });
  on("watchlistSearch", "input", renderWatchlist);

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

  document.querySelectorAll("#newsFilterPills .filter-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.newsCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#newsFilterPills .filter-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      fetchNewsFeed(false);
    });
  });
  on("newsSearchInput", "input", () => fetchNewsFeed(false));
  on("btnRefreshNews", "click", () => fetchNewsFeed(true));

  on("btnRecheckHealth", "click", () => fetchBinanceHealth(true));
  on("btnStartAiTrain", "click", startAiTraining);
  on("btnRefreshMacro", "click", () => {
    fetchMacroIntelligence(true);
    fetchAiEvolution();
  });
  on("sidebarEvoBox", "click", () => switchView("ai"));

  on("btnScanXtbNow", "click", scanXtbCopilotNow);
  on("btnCopilotSound", "click", toggleCopilotSound);
  on("btnToggleWhatsAppBox", "click", () => {
    const box = document.getElementById("copilotWhatsAppBox");
    if (box) {
      const opening = box.style.display === "none";
      box.style.display = opening ? "flex" : "none";
      if (opening) fetchWhatsAppConfigUI();
    }
  });
  on("btnSaveWhatsApp", "click", saveWhatsAppConfigUI);
  on("btnSendDailySummaryWa", "click", sendDailySummaryWhatsAppUI);
  on("waDailyHourSelect", "change", (e) => {
    if (e.target) e.target.dataset.userEdited = "1";
  });
  on("waEnabledCheck", "change", (e) => {
    if (e.target) e.target.dataset.userEdited = "1";
  });
  on("btnToggleTelegramBox", "click", () => {
    const box = document.getElementById("copilotTelegramBox");
    if (box) box.style.display = box.style.display === "none" ? "flex" : "none";
  });
  on("btnSaveTelegram", "click", saveTelegramConfigUI);

  on("btnOpenAiChatFab", "click", () => toggleAiChatDrawer());
  on("btnCloseAiChatDrawer", "click", () => toggleAiChatDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleAiChatDrawer(false);
  });
  on("btnSendAiChat", "click", () => sendAiChatMessage());
  on("aiChatInput", "keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendAiChatMessage();
    }
  });
  document.querySelectorAll("#aiChatChips [data-chat-q]").forEach((btn) => {
    btn.addEventListener("click", () => sendAiChatMessage(btn.dataset.chatQ));
  });

  bindTradingModeListener();
}

// Registra hooks desacoplados entre módulos
hooks.onSymbolChange = onSymbolChange;
hooks.onBidAskUpdated = updateXtbMarginEstimate;
hooks.onRefreshAll = () => fetchInitialData(syncActiveAssetUI);
hooks.onRefreshStatus = fetchStatus;

document.addEventListener("DOMContentLoaded", () => {
  bindUIEvents();
  initTradingViewChart();
  fetchInitialData(syncActiveAssetUI);
  connectLiveMarketStream();

  state.pollingInterval = setInterval(pollDashboardData, 3000);
  updateCandleCountdown();
  state.countdownInterval = setInterval(updateCandleCountdown, 1000);

  window.addEventListener("resize", resizeChart);
  window.addEventListener("beforeunload", disconnectLiveMarketStream);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollDashboardData();
  });
});
