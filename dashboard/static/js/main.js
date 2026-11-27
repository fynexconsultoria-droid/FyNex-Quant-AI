/**
 * Entrypoint Modular do Dashboard FyNex Quant AI (ES Modules)
 * Coordena navegação entre views, troca de ativo/timeframe e vinculação de eventos.
 */

import {
  applyAccountTheme,
  controlAccountBot,
  detectInitialAccount,
  fetchAccountsOverview,
  fetchLiveApprovals,
  pollTelegramApprovalsNow,
  promoteModelToLive,
  saveAccountPoliciesFromUI,
  switchAccount,
  syncPromoteButtonLabel,
  updateAutoPromotePolicy,
} from "./accounts.js";
import {
  fetchAiErrorLessons,
  fetchAiEvolution,
  fetchAiTrainSchedule,
  fetchMacroIntelligence,
  pollAiTrainStatus,
  saveAiTrainScheduleUI,
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
import { fetchFinanceOverview, initFinanceUI, renderAllFinanceCharts } from "./finance.js";
import { fetchNewsFeed } from "./news.js";
import { hooks, state } from "./state.js";
import { connectLiveMarketStream, disconnectLiveMarketStream, updateCandleCountdown } from "./stream.js";
import {
  fetchBinanceBalanceAuto,
  fetchBinanceHealth,
  fetchInitialData,
  fetchLogs,
  fetchStatus,
  fetchTrades,
  pollDashboardData,
  toggleBot,
} from "./terminal.js";
import { getInstrumentMeta, showToast } from "./utils.js";
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
    const validOpts = Array.from(levSel.options)
      .map((o) => Number(o.value))
      .filter((v) => Number.isFinite(v) && v > 0)
      .sort((a, b) => a - b);
    const chosen = validOpts.filter((v) => v <= targetLev).pop() || validOpts[0] || 10;
    levSel.value = String(chosen);
  }
  updateXtbMarginEstimate();
}

async function onSymbolChange(newSym) {
  state.activeSymbol = newSym || "BTC/USDT";
  const sel = document.getElementById("symbolSelect");
  if (sel && sel.value !== state.activeSymbol) sel.value = state.activeSymbol;

  disconnectLiveMarketStream();
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
  state.activeTimeframe = newTf || "1h";
  const trainTf = document.getElementById("aiTrainTimeframe");
  if (trainTf && Array.from(trainTf.options).some((o) => o.value === state.activeTimeframe)) {
    trainTf.value = state.activeTimeframe;
  }
  disconnectLiveMarketStream();
  state.chartInitializedWithData = false;
  state.loadedCandles = [];
  clearPositionPriceLines();
  const tag = document.getElementById("activeTimeframeTag");
  if (tag) tag.textContent = state.activeTimeframe;
  updateCandleCountdown();
  await fetchInitialData(syncActiveAssetUI);
  connectLiveMarketStream();
}

function closeMobileSidebar() {
  const sb = document.getElementById("appSidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (sb) sb.classList.remove("mobile-open");
  if (bd) bd.classList.remove("open");
}

function toggleMobileSidebar() {
  const sb = document.getElementById("appSidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (!sb) return;
  const open = !sb.classList.contains("mobile-open");
  sb.classList.toggle("mobile-open", open);
  if (bd) bd.classList.toggle("open", open);
}

function switchView(viewName) {
  state.activeView = viewName || "terminal";
  closeMobileSidebar();
  document.querySelectorAll(".view-section").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${state.activeView}`);
  });
  document.querySelectorAll(".sidebar-nav .nav-item[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === state.activeView);
  });

  if (state.activeView === "terminal") setTimeout(resizeChart, 30);
  if (state.activeView === "ai") {
    fetchAccountsOverview();
    fetchLiveApprovals();
    fetchMacroIntelligence(false);
    fetchAiEvolution();
    fetchAiErrorLessons();
    fetchXtbCopilotSignals();
    fetchAiPerformanceAndWelcome();
    pollAiTrainStatus();
  }
  if (state.activeView === "finance") {
    fetchFinanceOverview(false).then(() => setTimeout(renderAllFinanceCharts, 30));
  }
  if (state.activeView === "news" && state.newsCacheItems.length === 0) fetchNewsFeed(false);
  if (state.activeView === "portfolio") {
    fetchXtbAccount();
    if (state.binanceHasKeys) fetchBinanceBalanceAuto(true);
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

  on("btnMobileMenu", "click", toggleMobileSidebar);
  on("sidebarBackdrop", "click", closeMobileSidebar);
  on("btnSwitchDemo", "click", () => switchAccount("demo"));
  on("btnSwitchLive", "click", () => switchAccount("live"));
  on("btnQuickSwitchAccount", "click", () => switchAccount(state.activeAccount === "live" ? "demo" : "live"));
  on("accBannerUpgradeBtn", "click", () => switchView("ai"));
  on("accBannerPendingBtn", "click", () => {
    switchView("ai");
    fetchLiveApprovals();
  });
  on("btnRefreshLiveApprovals", "click", () => fetchLiveApprovals());
  on("btnPollTelegramApprovals", "click", () => pollTelegramApprovalsNow());
  on("btnPromoteModelToLive", "click", () => promoteModelToLive());
  on("govPromoteVersionSelect", "change", () => syncPromoteButtonLabel());
  on("chkAutoPromoteLive", "change", (e) => updateAutoPromotePolicy(Boolean(e.target && e.target.checked)));
  on("chkDemoAlwaysOn", "change", () => saveAccountPoliciesFromUI());
  on("chkLiveHighPotentialOnly", "change", () => saveAccountPoliciesFromUI());
  on("liveMinConfSelect", "change", () => saveAccountPoliciesFromUI());
  on("liveMinScoreSelect", "change", () => saveAccountPoliciesFromUI());
  on("chkLiveMicroCompound", "change", () => saveAccountPoliciesFromUI());
  on("liveCompoundPctSelect", "change", () => saveAccountPoliciesFromUI());
  on("btnGovToggleDemoBot", "click", () => controlAccountBot("demo"));
  on("btnGovToggleLiveBot", "click", () => controlAccountBot("live"));
  document.querySelectorAll("#consoleAccountTabs .fin-pill[data-log-acc]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.logAccountFilter = btn.dataset.logAcc === "live" ? "live" : "demo";
      applyAccountTheme(state.activeAccount);
      fetchLogs();
    });
  });
  on("symbolSelect", "change", (e) => onSymbolChange(e.target.value));
  on("timeframeSelect", "change", (e) => onTimeframeChange(e.target.value));
  on("btnToggleEma", "click", toggleEmaOverlay);
  on("btnFitChart", "click", fitChartContent);
  on("btnOpenBinance", "click", openBinanceModal);
  on("binanceWalletCard", "click", openBinanceModal);
  on("btnToggleBot", "click", toggleBot);
  on("btnRefreshDashboard", "click", async () => {
    const btn = document.getElementById("btnRefreshDashboard");
    if (btn) {
      btn.disabled = true;
      btn.style.opacity = "0.6";
    }
    try {
      await Promise.all([
        fetchInitialData(syncActiveAssetUI),
        fetchMacroIntelligence(true),
        fetchNewsFeed(true),
      ]);
      showToast("✅ Dados do painel atualizados com sucesso!", "success");
    } catch (err) {
      showToast(`Erro ao atualizar dados: ${err}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.style.opacity = "";
      }
    }
  });

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
  let newsSearchDebounce = null;
  on("newsSearchInput", "input", () => {
    if (newsSearchDebounce) clearTimeout(newsSearchDebounce);
    newsSearchDebounce = setTimeout(() => fetchNewsFeed(false), 250);
  });
  on("btnRefreshNews", "click", async () => {
    const btn = document.getElementById("btnRefreshNews");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ Atualizando...";
    }
    try {
      await fetchNewsFeed(true);
      showToast("📰 Feed de notícias atualizado!", "success");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🔄 Atualizar Feed";
      }
    }
  });

  on("btnRecheckHealth", "click", () => fetchBinanceHealth(true));
  on("btnStartAiTrain", "click", startAiTraining);
  on("chkAiAutoTrainEnabled", "change", () => saveAiTrainScheduleUI());
  on("aiAutoTrainIntervalSelect", "change", () => saveAiTrainScheduleUI());
  on("aiTrainTimeframe", "change", (e) => {
    if (e.target) e.target.dataset.userEdited = "1";
    if (document.getElementById("chkAiAutoTrainEnabled")?.checked) saveAiTrainScheduleUI();
  });
  on("aiTrainCandles", "change", (e) => {
    if (e.target) e.target.dataset.userEdited = "1";
    if (document.getElementById("chkAiAutoTrainEnabled")?.checked) saveAiTrainScheduleUI();
  });
  on("btnRefreshMacro", "click", async () => {
    const btn = document.getElementById("btnRefreshMacro");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ Atualizando...";
    }
    try {
      await Promise.all([fetchMacroIntelligence(true), fetchAiEvolution(), fetchAiErrorLessons()]);
      showToast("🌎 Inteligência Macro e Evolução da IA atualizadas!", "success");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🔄 Atualizar";
      }
    }
  });
  on("sidebarEvoBox", "click", () => switchView("ai"));

  on("btnScanXtbNow", "click", scanXtbCopilotNow);
  on("btnCopilotSound", "click", toggleCopilotSound);
  on("btnToggleWhatsAppBox", "click", () => {
    const box = document.getElementById("copilotWhatsAppBox");
    if (box) {
      const opening = window.getComputedStyle(box).display === "none";
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
    if (box) {
      const opening = window.getComputedStyle(box).display === "none";
      box.style.display = opening ? "flex" : "none";
    }
  });
  on("btnSaveTelegram", "click", saveTelegramConfigUI);

  on("btnOpenAiChatFab", "click", () => toggleAiChatDrawer());
  on("btnCloseAiChatDrawer", "click", () => toggleAiChatDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeBinanceModal();
      toggleAiChatDrawer(false);
      closeMobileSidebar();
    }
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
hooks.onAccountChange = async () => {
  clearPositionPriceLines();
  await Promise.all([
    fetchStatus(),
    fetchAccountsOverview(),
    fetchLiveApprovals(),
    fetchTrades(),
    fetchLogs(),
    fetchFinanceOverview(false),
  ]);
  if (state.activeView === "finance") {
    setTimeout(renderAllFinanceCharts, 30);
  }
  if (state.binanceHasKeys) {
    fetchBinanceBalanceAuto(true);
  }
};

document.addEventListener("DOMContentLoaded", () => {
  const initialAcc = detectInitialAccount();
  state.logAccountFilter = initialAcc;
  applyAccountTheme(initialAcc);

  bindUIEvents();
  initFinanceUI();
  initTradingViewChart();

  const urlView = new URLSearchParams(window.location.search).get("view");
  const validViews = new Set(["terminal", "news", "ai", "finance", "portfolio", "system"]);
  if (urlView && validViews.has(urlView)) {
    switchView(urlView);
  }

  fetchInitialData(syncActiveAssetUI);
  fetchFinanceOverview(false);
  fetchAiTrainSchedule();
  connectLiveMarketStream();

  window.addEventListener("popstate", () => {
    const acc = detectInitialAccount();
    switchAccount(acc, { pushUrl: false, refresh: true });
  });

  let pollTick = 0;
  state.pollingInterval = setInterval(() => {
    pollTick += 1;
    pollDashboardData();
    if (state.activeView === "finance") fetchFinanceOverview(false);
    if (state.activeView === "ai" && pollTick % 5 === 0) pollAiTrainStatus();
  }, 3000);
  updateCandleCountdown();
  state.countdownInterval = setInterval(updateCandleCountdown, 1000);

  window.addEventListener("resize", resizeChart);
  window.addEventListener("beforeunload", disconnectLiveMarketStream);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollDashboardData();
  });
});
