/**
 * Terminal Principal: Polling, Status do Robô, Candles, Previsões, Trades, Logs e Saúde
 */

import { fetchAiErrorLessons, fetchAiEvolution } from "./ai_center.js";
import { updateBinanceStatusUI } from "./binance_modal.js";
import { refreshEmaSeries, renderOpenPositionLines, updateLoadedCandlesWithTick } from "./chart.js";
import { fetchXtbCopilotSignals } from "./copilot_alerts.js";
import { fetchNewsFeed } from "./news.js";
import { state } from "./state.js";
import { update24hStatsUI, updateBidAskUI, updateLiveTickerPrice } from "./stream.js";
import { csrfHeaders, escapeHtml, isActiveForex, showToast } from "./utils.js";
import { fetchInstrumentsCatalog, fetchWatchlist, fetchXtbAccount } from "./xtb_desk.js";

export async function fetchLiveTickerFallback() {
  try {
    const q = new URLSearchParams({ symbol: state.activeSymbol });
    const res = await fetch(`/api/market/live?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.price > 0) {
      if (!state.liveWsConnected) updateLiveTickerPrice(data.price);
      update24hStatsUI({
        changePct: Number(data.change_24h_pct),
        high: Number(data.high_24h),
        low: Number(data.low_24h),
        quoteVolume: Number(data.volume_24h),
      });
      if (data.bid > 0 && data.ask > 0) updateBidAskUI(Number(data.bid), Number(data.ask));
      const latEl = document.getElementById("tickerLatency");
      if (latEl && data.latency_ms !== undefined) latEl.textContent = `${Math.round(data.latency_ms)} ms`;
    }
  } catch (_) {
    // fallback silencioso
  }
}

export async function fetchMarketDepth() {
  state.lastDepthFetchAt = Date.now();
  try {
    const q = new URLSearchParams({ symbol: state.activeSymbol, limit: "15" });
    const res = await fetch(`/api/market/depth?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data) return;

    if (data.best_bid > 0 && data.best_ask > 0 && !state.liveWsConnected) {
      updateBidAskUI(Number(data.best_bid), Number(data.best_ask));
    }
    const buyPct = Number(data.buy_pressure_pct ?? 50);
    const sellPct = Number(data.sell_pressure_pct ?? 100 - buyPct);
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

export async function fetchBinanceHealth(forceRefresh = false) {
  state.lastHealthFetchAt = Date.now();
  try {
    const url = forceRefresh ? "/api/binance/health?refresh=true" : "/api/binance/health";
    const res = await fetch(url);
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

      const m = h.metrics || {};
      const set = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.textContent = v;
      };
      set("sysHealthLatency", lat);
      set("sysHealthStatus", h.clock_synced ? "🟢 Operacional (Healthy)" : "🟡 Alerta de Relógio");
      set("sysClockDrift", driftStr);
      set("sysActiveMirror", mirrorHost);
      set("sysMirrorSwitches", `Failovers: ${m.mirror_switches || 0}`);
      set("sysTotalReqs", String(m.total_requests || 0));
      set("sysFailRetries", `Falhas: ${m.failed_requests || 0} • Retries: ${m.retries || 0}`);
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

export async function fetchBinanceBalanceAuto(force = false) {
  if (!force && Date.now() - state.lastBalanceFetchAt < 20000) return;
  state.lastBalanceFetchAt = Date.now();
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
      if (kpiBtc) kpiBtc.textContent = `${Number(data.btc_total || 0).toFixed(6)} BTC`;
    }
  } catch (_) {
    // silencioso
  }
}

export async function fetchStatus() {
  try {
    const q = new URLSearchParams({ symbol: state.activeSymbol, timeframe: state.activeTimeframe });
    const res = await fetch(`/api/status?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();

    const tfTag = document.getElementById("activeTimeframeTag");
    if (tfTag) tfTag.textContent = `Candles ${state.activeTimeframe}`;

    const currentPrice = data.latest_price || 0;
    if (!state.liveWsConnected && currentPrice > 0) updateLiveTickerPrice(currentPrice);

    state.isBotOnline = data.bot_online;
    const dot = document.getElementById("statusDot");
    const label = document.getElementById("statusLabel");
    const btn = document.getElementById("btnToggleBot");
    const btnText = document.getElementById("btnBotText");
    const btnIcon = document.getElementById("btnBotIcon");

    if (state.isBotOnline) {
      dot.className = "status-dot online";
      label.textContent = `Online (PID ${data.bot_pid} • ${data.bot_active_timeframe || state.activeTimeframe})`;
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

    const calibText = document.getElementById("calibModeText");
    const calibIcon = document.getElementById("calibBadgeIcon");
    const calibDesc = document.getElementById("kpiCalibDesc");
    if (calibText && data.calibration) {
      calibText.textContent = data.calibration.mode;
      calibIcon.textContent = data.calibration.badge;
      const th = Number(data.calibration.threshold || 55).toFixed(0);
      calibDesc.textContent = `Filtro de entrada ativo: ${th}%`;
    }

    const winRateEl = document.getElementById("kpiWinRate");
    const tradesCountEl = document.getElementById("kpiTradesCount");
    if (winRateEl && data.trade_stats) {
      winRateEl.innerHTML = `${escapeHtml(data.trade_stats.win_rate.toFixed(1))}% <span class="kpi-small">Win Rate</span>`;
      if (tradesCountEl) {
        tradesCountEl.textContent = `${data.trade_stats.total_trades} trades (${data.trade_stats.wins}V / ${data.trade_stats.losses}D)`;
      }
    }

    const modelBadge = document.getElementById("modelVerBadge");
    const modelAcc = document.getElementById("modelInitialAcc");
    if (modelBadge && data.model) {
      modelBadge.textContent = `Versão v${data.model.version} (${data.model.timeframe || state.activeTimeframe})`;
      if (modelAcc) modelAcc.textContent = `${Number(data.model.accuracy || 0).toFixed(2)}%`;
    }

    const chartUpdateEl = document.getElementById("chartLastUpdate");
    if (chartUpdateEl && data.latest_update && !state.liveWsConnected) {
      chartUpdateEl.textContent = `Último candle (UTC): ${data.latest_update}`;
    }

    renderOpenPositionLines(data.open_position);
    state.binanceHasKeys = Boolean(data.binance && data.binance.has_keys);
    updateBinanceStatusUI(data.binance);
  } catch (err) {
    console.error("Erro ao buscar status do robô:", err);
  }
}

export async function fetchCandles(fullReload = false) {
  if (!state.candleSeries) return;
  const reqSymbol = state.activeSymbol;
  const reqTf = state.activeTimeframe;
  try {
    const limit = fullReload || !state.chartInitializedWithData ? 150 : 5;
    const q = new URLSearchParams({ symbol: reqSymbol, timeframe: reqTf, limit: String(limit) });
    const res = await fetch(`/api/candles?${q.toString()}`);
    if (!res.ok) return;
    const candles = await res.json();
    if (reqSymbol !== state.activeSymbol || reqTf !== state.activeTimeframe) return;
    if (!candles || candles.length === 0) return;

    if (fullReload || !state.chartInitializedWithData) {
      state.loadedCandles = candles.slice();
      state.candleSeries.setData(candles);
      if (state.volumeSeries) {
        state.volumeSeries.setData(
          candles.map((c) => ({
            time: c.time,
            value: c.volume || 0,
            color: c.close >= c.open ? "rgba(16, 185, 129, 0.35)" : "rgba(244, 63, 94, 0.35)",
          }))
        );
      }
      refreshEmaSeries();
      state.chartInitializedWithData = true;
    } else {
      const lastTime = state.loadedCandles.length ? Number(state.loadedCandles[state.loadedCandles.length - 1].time) : 0;
      const freshBars = candles.filter((c) => Number(c.time) >= lastTime);
      for (const c of freshBars) {
        state.candleSeries.update(c);
        if (state.volumeSeries) {
          state.volumeSeries.update({
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

export async function fetchPredictions() {
  const container = document.getElementById("predictionsList");
  const countBadge = document.getElementById("predictionsCountBadge");
  if (!container) return;

  try {
    const q = new URLSearchParams({ symbol: state.activeSymbol, timeframe: state.activeTimeframe, limit: "12" });
    const res = await fetch(`/api/predictions?${q.toString()}`);
    if (!res.ok) return;
    const list = await res.json();

    if (countBadge) countBadge.textContent = `${list.length} recentes (${state.activeTimeframe})`;
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

export async function fetchTrades() {
  const container = document.getElementById("tradesList");
  const countBadge = document.getElementById("tradesCountBadge");
  if (!container) return;

  try {
    const q = new URLSearchParams({ symbol: state.activeSymbol, timeframe: state.activeTimeframe, limit: "10" });
    const res = await fetch(`/api/trades?${q.toString()}`);
    if (!res.ok) return;
    const trades = await res.json();

    if (countBadge) countBadge.textContent = `${trades.length} operações`;
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

export async function fetchLogs() {
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

export async function toggleBot() {
  const btn = document.getElementById("btnToggleBot");
  if (btn) btn.disabled = true;
  try {
    let res;
    if (state.isBotOnline) {
      res = await fetch("/api/bot/stop", { method: "POST", headers: csrfHeaders() });
    } else {
      const targetSymbol = isActiveForex() ? "BTC/USDT" : state.activeSymbol;
      const q = new URLSearchParams({ symbol: targetSymbol, timeframe: state.activeTimeframe });
      res = await fetch(`/api/bot/start?${q.toString()}`, { method: "POST", headers: csrfHeaders() });
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || `Falha ao controlar o robô (HTTP ${res.status}).`, "error");
    } else {
      showToast(data.message || (state.isBotOnline ? "Robô finalizado." : "Robô iniciado em segundo plano!"), "success");
    }
    await fetchStatus();
  } catch (err) {
    showToast(`Falha ao comunicar com o servidor: ${err}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function fetchInitialData(syncAssetUiCb = null) {
  if (Object.keys(state.instrumentsMap).length === 0) {
    await fetchInstrumentsCatalog();
    if (typeof syncAssetUiCb === "function") syncAssetUiCb();
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
    fetchAiErrorLessons(),
  ]);
  if (state.binanceHasKeys) await fetchBinanceBalanceAuto(true);
}

export async function pollDashboardData() {
  if (state.isPolling || document.hidden) return;
  state.isPolling = true;
  try {
    const now = Date.now();
    const tasks = [fetchStatus(), fetchPredictions(), fetchTrades(), fetchLogs()];
    if (!state.liveWsConnected || isActiveForex()) {
      tasks.push(fetchCandles(false));
      tasks.push(fetchLiveTickerFallback());
    }
    if (now - state.lastDepthFetchAt >= 6000) tasks.push(fetchMarketDepth());
    if (now - state.lastXtbFetchAt >= 4000) tasks.push(fetchXtbAccount());
    if (now - state.lastWatchlistFetchAt >= 12000) {
      tasks.push(fetchWatchlist());
      tasks.push(fetchXtbCopilotSignals());
    }
    if (now - state.lastHealthFetchAt >= 15000) tasks.push(fetchBinanceHealth());
    if (now - state.lastNewsFetchAt >= 90000) tasks.push(fetchNewsFeed(false));
    if (state.binanceHasKeys && now - state.lastBalanceFetchAt >= 25000) {
      tasks.push(fetchBinanceBalanceAuto(false));
    }
    await Promise.all(tasks);
  } finally {
    state.isPolling = false;
  }
}
