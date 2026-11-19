/**
 * FyNex Quant AI Dashboard - Client Application Logic
 * Integração com TradingView Lightweight Charts e APIs do backend (com CSRF, escape XSS e filtro por Timeframe)
 */

let tvChart = null;
let candleSeries = null;
let isBotOnline = false;
let pollingInterval = null;
let lastKnownPrice = 0;
let activeSymbol = "BTC/USDT";
let activeTimeframe = "1m";
let chartInitializedWithData = false;

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
  initTradingViewChart();
  fetchInitialData();

  // Inicia polling periódico a cada 3 segundos (inclui trades!)
  pollingInterval = setInterval(pollDashboardData, 3000);

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
});

/**
 * Troca o timeframe ativo no dashboard e recarrega o gráfico, previsões e métricas
 */
async function onTimeframeChange(newTf) {
  activeTimeframe = newTf || "1m";
  chartInitializedWithData = false;
  const tag = document.getElementById("activeTimeframeTag");
  if (tag) tag.textContent = `Candles ${activeTimeframe}`;
  await fetchInitialData();
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
}

/**
 * Coleta todos os dados iniciais
 */
async function fetchInitialData() {
  await Promise.all([
    fetchStatus(),
    fetchCandles(true),
    fetchPredictions(),
    fetchTrades(),
    fetchLogs(),
  ]);
}

/**
 * Loop de polling leve a cada 3 segundos (M8 corrigido: inclui fetchTrades)
 */
async function pollDashboardData() {
  await Promise.all([
    fetchStatus(),
    fetchCandles(false),
    fetchPredictions(),
    fetchTrades(),
    fetchLogs(),
  ]);
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

    // 1. Preço e Ticker
    const priceEl = document.getElementById("tickerPrice");
    const currentPrice = data.latest_price || 0;
    if (priceEl && currentPrice > 0) {
      priceEl.textContent = `$${currentPrice.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

      if (lastKnownPrice > 0) {
        if (currentPrice > lastKnownPrice) {
          priceEl.style.color = "#10b981";
        } else if (currentPrice < lastKnownPrice) {
          priceEl.style.color = "#f43f5e";
        }
        setTimeout(() => {
          priceEl.style.color = "var(--accent-cyan)";
        }, 600);
      }
      lastKnownPrice = currentPrice;
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
    if (chartUpdateEl && data.latest_update) {
      chartUpdateEl.textContent = `Último candle (UTC): ${data.latest_update}`;
    }

    // 8. Informações da Binance
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
      candleSeries.setData(candles);
      chartInitializedWithData = true;
    } else {
      for (const c of candles) {
        candleSeries.update(c);
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
      const q = new URLSearchParams({ timeframe: activeTimeframe, retrain: "5" });
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
        <div style="font-size: 0.725rem; color: var(--text-dim); display: flex; gap: 0.75rem;">
          <span>Trading Spot: ${data.can_trade ? "✅ Ativo" : "❌ Inativo"}</span>
          <span>Saques: ${
            data.can_withdraw ? "⚠️ Permitido" : "🔒 Bloqueado (Segurança Máxima)"
          }</span>
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
