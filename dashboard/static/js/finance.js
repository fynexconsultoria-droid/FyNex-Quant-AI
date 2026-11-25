/**
 * Central Financeira & Gráficos Interativos HiDPI (FyNex Quant AI)
 * Consome `/api/finance/overview` e renderiza:
 * 1. Hero Patrimonial + 6 KPIs Quantitativos
 * 2. Curva de Patrimônio (Equity) / PnL Acumulado / Drawdown (%) com crosshair e tooltip
 * 3. Fluxo de PnL Diário (barras verdes/vermelhas com linha zero e tooltip)
 * 4. Donut Interativo (Alocação por Classe de Ativo / Eficiência de Saídas)
 * 5. Ranking Horizontal por Ativo + Calendário Heatmap 35 Dias + Extrato Unificado
 */

import { escapeHtml, fmtSignedUsd, fmtUsd, formatPrice, showToast } from "./utils.js";

const financeState = {
  period: "30d",
  source: "all",
  equityMode: "equity", // equity | pnl | drawdown
  donutMode: "category", // category | reasons
  ledgerOutcome: "all", // all | win | loss
  ledgerQuery: "",
  data: null,
  eventsBound: false,
  equityHoverIdx: -1,
  dailyHoverIdx: -1,
};

const PALETTE = [
  "#00f2fe",
  "#10b981",
  "#8b5cf6",
  "#f59e0b",
  "#38bdf8",
  "#f43f5e",
  "#22d3ee",
];

function fmtCompactUsd(val) {
  const n = Number(val || 0);
  const sign = n >= 0 ? "+" : "-";
  const abs = Math.abs(n);
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

function setupHiDpiCanvas(canvas) {
  if (!canvas) return null;
  const parent = canvas.parentElement;
  const width = Math.max(180, Math.floor((parent ? parent.clientWidth : canvas.clientWidth) || 300));
  const height = Math.max(150, Math.floor((parent ? parent.clientHeight : canvas.clientHeight) || 260));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function updateSidebarBadge(summary) {
  const badge = document.getElementById("navFinancePnlBadge");
  if (!badge || !summary) return;
  const net = Number(summary.net_pnl_usd || 0);
  badge.textContent = fmtCompactUsd(net);
  badge.classList.remove("pos-pill", "neg-pill");
  if (net > 0) badge.classList.add("pos-pill");
  else if (net < 0) badge.classList.add("neg-pill");
}

function renderSummary(summary) {
  if (!summary) return;
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };

  setText("finHeroEquity", fmtUsd(summary.equity_usd));
  setText("finHeroBaseCap", `Capital base: ${fmtUsd(summary.starting_capital_usd)}`);

  const roiBadge = document.getElementById("finHeroRoiBadge");
  const roi = Number(summary.roi_pct || 0);
  if (roiBadge) {
    roiBadge.textContent = `${roi >= 0 ? "+" : ""}${roi.toFixed(2)}% ROI`;
    roiBadge.className = `finance-roi-badge ${roi > 0 ? "pos" : roi < 0 ? "neg" : "neutral"}`;
  }

  setText("finHeroBalance", fmtUsd(summary.balance_usd));
  setText("finHeroRealizedPeriod", `Realizado no período: ${fmtSignedUsd(summary.realized_pnl_usd)}`);

  const floatEl = document.getElementById("finHeroFloating");
  const fl = Number(summary.floating_pnl_usd || 0);
  if (floatEl) {
    floatEl.textContent = fmtSignedUsd(fl);
    floatEl.className = `finance-hero-metric-val ${fl > 0 ? "text-up" : fl < 0 ? "text-down" : ""}`;
  }
  setText("finHeroOpenCount", `${summary.open_positions_count || 0} posição(ões) aberta(s)`);

  setText("finHeroFreeMargin", fmtUsd(summary.free_margin_usd));
  const usedM = Number(summary.used_margin_usd || 0);
  const eq = Math.max(1, Number(summary.equity_usd || 1));
  const freePct = Math.max(0, Math.min(100, (Number(summary.free_margin_usd || 0) / eq) * 100));
  const mlText = summary.margin_level_pct != null ? `Nível: ${summary.margin_level_pct}%` : "Saúde 100%";
  setText("finHeroMarginLevel", `Uso: ${fmtUsd(usedM)} • ${mlText}`);
  const bar = document.getElementById("finHeroMarginBar");
  if (bar) {
    bar.style.width = `${freePct.toFixed(1)}%`;
    bar.style.background =
      freePct < 30
        ? "linear-gradient(90deg, #e11d48, #f59e0b)"
        : "linear-gradient(90deg, #10b981, #00f2fe)";
  }

  // 6 KPIs
  const netEl = document.getElementById("finKpiNetPnl");
  const net = Number(summary.net_pnl_usd || 0);
  if (netEl) {
    netEl.textContent = fmtSignedUsd(net);
    netEl.className = `fin-kpi-val ${net > 0 ? "text-up" : net < 0 ? "text-down" : ""}`;
  }
  setText("finKpiGrossProfit", `+${fmtUsd(summary.gross_profit_usd)}`);
  setText("finKpiGrossLoss", `-${fmtUsd(summary.gross_loss_usd)}`);

  const wr = Number(summary.win_rate_pct || 0);
  setText("finKpiWinRate", `${wr.toFixed(1)}%`);
  const winBar = document.getElementById("finKpiWinBar");
  if (winBar) winBar.style.width = `${Math.max(0, Math.min(100, wr))}%`;
  setText(
    "finKpiWinLossCount",
    `${summary.wins || 0}W • ${summary.losses || 0}L (${summary.total_trades || 0} trades)`
  );

  const pf = Number(summary.profit_factor || 0);
  const pfEl = document.getElementById("finKpiProfitFactor");
  if (pfEl) {
    pfEl.textContent = pf >= 9.99 ? "∞" : pf.toFixed(2);
    pfEl.className = `fin-kpi-val ${pf >= 1.3 ? "text-up" : pf > 0 && pf < 1.0 ? "text-down" : ""}`;
  }
  const payoff = Number(summary.payoff_ratio || 0);
  setText("finKpiPayoff", `Payoff: ${payoff >= 9.99 ? "∞" : payoff.toFixed(2)}x • Méd W ${fmtUsd(summary.avg_win_usd)}`);

  const exp = Number(summary.expectancy_usd || 0);
  const expEl = document.getElementById("finKpiExpectancy");
  if (expEl) {
    expEl.textContent = fmtSignedUsd(exp);
    expEl.className = `fin-kpi-val ${exp > 0 ? "text-up" : exp < 0 ? "text-down" : ""}`;
  }
  const streak = Number(summary.current_streak || 0);
  const streakTxt =
    streak > 0 ? `Sequência: +${streak} vitória(s)` : streak < 0 ? `Sequência: ${streak} perda(s)` : "Sequência: neutra";
  setText("finKpiStreak", streakTxt);

  setText("finKpiDrawdown", `-${Number(summary.max_drawdown_pct || 0).toFixed(2)}%`);
  setText("finKpiSharpe", `${fmtUsd(summary.max_drawdown_usd)} • Sharpe ${Number(summary.sharpe_ratio || 0).toFixed(2)}`);

  setText("finKpiBestTrade", fmtSignedUsd(summary.best_trade_usd));
  setText("finKpiWorstTrade", fmtSignedUsd(summary.worst_trade_usd));

  updateSidebarBadge(summary);
}

function renderEquityChart() {
  const canvas = document.getElementById("finEquityCanvas");
  const setup = setupHiDpiCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;

  const series = (financeState.data && financeState.data.equity_curve) || [];
  const mode = financeState.equityMode;

  const titleEl = document.getElementById("finEquityChartTitle");
  const subEl = document.getElementById("finEquityChartSub");
  if (titleEl && subEl) {
    if (mode === "equity") {
      titleEl.textContent = "Curva de Patrimônio (Equity Curve)";
      subEl.textContent = "Evolução cronológica do patrimônio real + posição flutuante atual";
    } else if (mode === "pnl") {
      titleEl.textContent = "PnL Acumulado do Período ($)";
      subEl.textContent = "Lucro/prejuízo líquido acumulado trade a trade";
    } else {
      titleEl.textContent = "Curva de Drawdown (%)";
      subEl.textContent = "Queda percentual a partir do topo histórico do período";
    }
  }

  if (series.length < 2) {
    ctx.fillStyle = "#94a3b8";
    ctx.font = "500 13px Outfit, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Sem operações suficientes no período para traçar a curva.", width / 2, height / 2);
    return;
  }

  const values = series.map((pt) => {
    if (mode === "pnl") return Number(pt.cumulative_pnl || 0);
    if (mode === "drawdown") return Number(pt.drawdown_pct || 0);
    return Number(pt.equity || 0);
  });

  const pad = { top: 20, right: 20, bottom: 32, left: 74 };
  const plotW = Math.max(40, width - pad.left - pad.right);
  const plotH = Math.max(40, height - pad.top - pad.bottom);

  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  if (mode === "drawdown") {
    maxV = 0;
    if (minV === 0) minV = -1;
  } else if (mode === "pnl") {
    minV = Math.min(minV, 0);
    maxV = Math.max(maxV, 0);
  }
  if (Math.abs(maxV - minV) < 1e-6) {
    minV -= 10;
    maxV += 10;
  } else {
    const margin = (maxV - minV) * 0.12;
    maxV += mode === "drawdown" ? 0 : margin;
    minV -= margin;
  }

  const xOf = (i) => pad.left + (i / (series.length - 1)) * plotW;
  const yOf = (v) => pad.top + ((maxV - v) / (maxV - minV)) * plotH;

  // Grade horizontal + eixo Y
  ctx.strokeStyle = "rgba(148, 163, 184, 0.12)";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#94a3b8";
  ctx.font = "500 11px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  const yTicks = 5;
  for (let i = 0; i <= yTicks; i++) {
    const frac = i / yTicks;
    const v = maxV - frac * (maxV - minV);
    const y = pad.top + frac * plotH;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();

    const lbl = mode === "drawdown" ? `${v.toFixed(2)}%` : fmtUsd(v);
    ctx.fillText(lbl, pad.left - 10, y);
  }

  // Linha base (zero para PnL/Drawdown ou capital inicial para Equity)
  const baseVal = mode === "equity" ? values[0] : 0;
  if (baseVal >= minV && baseVal <= maxV) {
    const by = yOf(baseVal);
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "rgba(148, 163, 184, 0.32)";
    ctx.beginPath();
    ctx.moveTo(pad.left, by);
    ctx.lineTo(width - pad.right, by);
    ctx.stroke();
    ctx.restore();
  }

  // Cor principal conforme tendência
  const lastVal = values[values.length - 1];
  const isPositive = mode === "drawdown" ? false : lastVal >= (mode === "equity" ? values[0] : 0);
  const strokeColor = mode === "drawdown" ? "#f43f5e" : isPositive ? "#10b981" : "#f43f5e";
  const gradTop = mode === "drawdown" ? "rgba(244, 63, 94, 0.04)" : isPositive ? "rgba(16, 185, 129, 0.30)" : "rgba(244, 63, 94, 0.28)";
  const gradBot = mode === "drawdown" ? "rgba(244, 63, 94, 0.30)" : "rgba(15, 23, 42, 0.0)";

  // Área preenchida
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  grad.addColorStop(0, gradTop);
  grad.addColorStop(1, gradBot);

  ctx.beginPath();
  ctx.moveTo(xOf(0), mode === "drawdown" ? yOf(0) : pad.top + plotH);
  for (let i = 0; i < series.length; i++) {
    ctx.lineTo(xOf(i), yOf(values[i]));
  }
  ctx.lineTo(xOf(series.length - 1), mode === "drawdown" ? yOf(0) : pad.top + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Linha principal
  ctx.beginPath();
  for (let i = 0; i < series.length; i++) {
    const x = xOf(i);
    const y = yOf(values[i]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = 2.4;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Pontos discretos se série pequena
  if (series.length <= 40) {
    for (let i = 0; i < series.length; i++) {
      const x = xOf(i);
      const y = yOf(values[i]);
      ctx.beginPath();
      ctx.arc(x, y, i === financeState.equityHoverIdx ? 5.2 : 3, 0, Math.PI * 2);
      ctx.fillStyle = i === financeState.equityHoverIdx ? "#ffffff" : strokeColor;
      ctx.fill();
    }
  }

  // Rótulos X
  ctx.fillStyle = "#64748b";
  ctx.font = "500 10.5px 'JetBrains Mono', monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const maxLabels = Math.min(6, series.length);
  for (let k = 0; k < maxLabels; k++) {
    const idx = Math.round((k / Math.max(1, maxLabels - 1)) * (series.length - 1));
    ctx.fillText(series[idx].label || "", xOf(idx), height - pad.bottom + 9);
  }

  // Crosshair interativo
  const hIdx = financeState.equityHoverIdx;
  if (hIdx >= 0 && hIdx < series.length) {
    const hx = xOf(hIdx);
    const hy = yOf(values[hIdx]);
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(0, 242, 254, 0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(hx, pad.top);
    ctx.lineTo(hx, pad.top + plotH);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(hx, hy, 5.5, 0, Math.PI * 2);
    ctx.fillStyle = "#00f2fe";
    ctx.fill();
    ctx.strokeStyle = "#0b1120";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function renderDailyPnlChart() {
  const canvas = document.getElementById("finDailyCanvas");
  const setup = setupHiDpiCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;

  const daily = (financeState.data && financeState.data.daily_pnl) || [];
  const badge = document.getElementById("finDailyDaysBadge");
  if (badge) badge.textContent = `${daily.length} dia(s) ativo(s)`;

  if (!daily.length) {
    ctx.fillStyle = "#94a3b8";
    ctx.font = "500 13px Outfit, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Nenhuma operação fechada no período selecionado.", width / 2, height / 2);
    return;
  }

  const pad = { top: 18, right: 16, bottom: 32, left: 66 };
  const plotW = Math.max(40, width - pad.left - pad.right);
  const plotH = Math.max(40, height - pad.top - pad.bottom);

  const vals = daily.map((d) => Number(d.pnl_usd || 0));
  const maxAbs = Math.max(10, ...vals.map((v) => Math.abs(v))) * 1.15;
  const yOf = (v) => pad.top + ((maxAbs - v) / (2 * maxAbs)) * plotH;
  const zeroY = yOf(0);

  // Grade Y
  ctx.strokeStyle = "rgba(148, 163, 184, 0.1)";
  ctx.fillStyle = "#94a3b8";
  ctx.font = "500 10.5px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const v of [maxAbs, maxAbs / 2, 0, -maxAbs / 2, -maxAbs]) {
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(fmtSignedUsd(v), pad.left - 8, y);
  }

  // Linha zero destacada
  ctx.strokeStyle = "rgba(148, 163, 184, 0.35)";
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(pad.left, zeroY);
  ctx.lineTo(width - pad.right, zeroY);
  ctx.stroke();

  const slotW = plotW / daily.length;
  const barW = Math.max(6, Math.min(34, slotW * 0.62));

  daily.forEach((d, idx) => {
    const v = Number(d.pnl_usd || 0);
    const cx = pad.left + slotW * idx + slotW / 2;
    const x = cx - barW / 2;
    const yVal = yOf(v);
    const yTop = Math.min(zeroY, yVal);
    const bh = Math.max(2, Math.abs(yVal - zeroY));
    const hovered = idx === financeState.dailyHoverIdx;

    if (v >= 0) {
      ctx.fillStyle = hovered ? "#34d399" : "rgba(16, 185, 129, 0.85)";
    } else {
      ctx.fillStyle = hovered ? "#fb7185" : "rgba(244, 63, 94, 0.85)";
    }

    ctx.beginPath();
    const r = Math.min(4, barW / 2, bh / 2);
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(x, yTop, barW, bh, v >= 0 ? [r, r, 0, 0] : [0, 0, r, r]);
    } else {
      ctx.rect(x, yTop, barW, bh);
    }
    ctx.fill();

    // Rótulo X espaçado
    const step = Math.max(1, Math.ceil(daily.length / 7));
    if (idx % step === 0 || idx === daily.length - 1) {
      ctx.fillStyle = "#64748b";
      ctx.font = "500 10px 'JetBrains Mono', monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(d.date.slice(5), cx, height - pad.bottom + 8);
    }
  });
}

function renderDonutChart() {
  const canvas = document.getElementById("finDonutCanvas");
  const legendEl = document.getElementById("finDonutLegend");
  const centerVal = document.getElementById("finDonutCenterVal");
  const centerLbl = document.getElementById("finDonutCenterLbl");
  const titleEl = document.getElementById("finDonutTitle");
  const subEl = document.getElementById("finDonutSub");
  if (!canvas || !legendEl) return;

  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const size = 185;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);

  const isCat = financeState.donutMode === "category";
  const items = isCat
    ? (financeState.data && financeState.data.category_allocation) || []
    : (financeState.data && financeState.data.exit_reasons) || [];

  if (titleEl && subEl) {
    titleEl.textContent = isCat ? "Alocação por Classe" : "Eficiência de Saídas";
    subEl.textContent = isCat ? "Distribuição de volume financeiro" : "Motivos de encerramento das operações";
  }

  const cx = size / 2;
  const cy = size / 2;
  const radius = 68;
  const thickness = 20;

  if (!items.length) {
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(148, 163, 184, 0.16)";
    ctx.lineWidth = thickness;
    ctx.stroke();
    if (centerVal) centerVal.textContent = "0";
    if (centerLbl) centerLbl.textContent = "Sem dados";
    legendEl.innerHTML = '<div class="empty-state">Sem operações no período.</div>';
    return;
  }

  const totalCount = items.reduce((acc, it) => acc + Number(isCat ? it.trades : it.count || 0), 0);
  if (centerVal) centerVal.textContent = String(totalCount);
  if (centerLbl) centerLbl.textContent = isCat ? "Operações" : "Saídas";

  let startAngle = -Math.PI / 2;
  const reasonColors = {
    TAKE_PROFIT: "#10b981",
    STOP_LOSS: "#f43f5e",
    BREAKEVEN: "#f59e0b",
    TRAILING_STOP: "#00f2fe",
    MANUAL: "#38bdf8",
    SIGNAL: "#8b5cf6",
    OUTRO: "#64748b",
  };

  const legendHtml = items
    .map((it, idx) => {
      const share = Math.max(0.5, Number(it.share_pct || 0));
      const sliceAngle = (share / 100) * Math.PI * 2;
      const color = isCat ? PALETTE[idx % PALETTE.length] : reasonColors[it.reason] || PALETTE[idx % PALETTE.length];

      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAngle, startAngle + sliceAngle - 0.03);
      ctx.strokeStyle = color;
      ctx.lineWidth = thickness;
      ctx.lineCap = "butt";
      ctx.stroke();
      startAngle += sliceAngle;

      const pnl = Number(it.pnl_usd || 0);
      const pnlCls = pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "dim";
      const countNum = isCat ? it.trades : it.count;
      return `
        <div class="fin-legend-item">
          <div class="fin-legend-left">
            <span class="fin-legend-dot" style="background:${color};"></span>
            <span class="fin-legend-name">${escapeHtml(it.label || it.category || it.reason)}</span>
          </div>
          <div class="fin-legend-right">
            <span class="dim">${Number(it.share_pct || 0).toFixed(0)}% (${countNum})</span>
            <strong class="${pnlCls}">${fmtSignedUsd(pnl)}</strong>
          </div>
        </div>
      `;
    })
    .join("");

  legendEl.innerHTML = legendHtml;
}

function renderAssetBreakdown() {
  const container = document.getElementById("finAssetBars");
  const badge = document.getElementById("finAssetsCountBadge");
  if (!container) return;

  const assets = (financeState.data && financeState.data.asset_breakdown) || [];
  if (badge) badge.textContent = `${assets.length} ativo(s)`;

  if (!assets.length) {
    container.innerHTML = '<div class="empty-state">Nenhuma operação fechada no período selecionado.</div>';
    return;
  }

  const maxAbs = Math.max(1, ...assets.map((a) => Math.abs(Number(a.pnl_usd || 0))));
  container.innerHTML = assets
    .map((a) => {
      const pnl = Number(a.pnl_usd || 0);
      const pctW = Math.max(4, Math.min(100, (Math.abs(pnl) / maxAbs) * 100));
      const cls = pnl >= 0 ? "pos" : "neg";
      const txtCls = pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "dim";
      return `
        <div class="fin-asset-row">
          <div class="fin-asset-meta">
            <span class="fin-asset-sym">${escapeHtml(a.symbol)}</span>
            <span class="fin-asset-sub">${a.trades}x • ${Number(a.win_rate_pct || 0).toFixed(0)}% WR</span>
          </div>
          <div class="fin-asset-track">
            <div class="fin-asset-fill ${cls}" style="width: ${pctW.toFixed(1)}%;"></div>
          </div>
          <span class="fin-asset-pnl ${txtCls}">${fmtSignedUsd(pnl)}</span>
        </div>
      `;
    })
    .join("");
}

function renderCalendarHeatmap() {
  const grid = document.getElementById("finHeatmapGrid");
  if (!grid) return;

  const days = (financeState.data && financeState.data.calendar_35d) || [];
  if (!days.length) {
    grid.innerHTML = '<div class="empty-state">Calendário indisponível.</div>';
    return;
  }

  const maxAbs = Math.max(10, ...days.map((d) => Math.abs(Number(d.pnl_usd || 0))));
  grid.innerHTML = days
    .map((d) => {
      const pnl = Number(d.pnl_usd || 0);
      const trades = Number(d.trades || 0);
      let lvl = "";
      if (trades > 0) {
        const ratio = Math.abs(pnl) / maxAbs;
        if (pnl > 0) lvl = ratio > 0.65 ? "lvl-pos-3" : ratio > 0.28 ? "lvl-pos-2" : "lvl-pos-1";
        else if (pnl < 0) lvl = ratio > 0.65 ? "lvl-neg-3" : ratio > 0.28 ? "lvl-neg-2" : "lvl-neg-1";
      }
      const valCls = pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "dim";
      const title = `${d.date}: ${fmtSignedUsd(pnl)} (${trades} ops, ${d.wins || 0}W)`;
      return `
        <div class="fin-cal-cell ${lvl}" title="${escapeHtml(title)}">
          <span class="fin-cal-date">${escapeHtml(d.day_label)}</span>
          <span class="fin-cal-val ${valCls}">${trades > 0 ? fmtCompactUsd(pnl) : "—"}</span>
          <span class="fin-cal-trades">${trades > 0 ? `${trades} op` : ""}</span>
        </div>
      `;
    })
    .join("");
}

function renderLedgerTable() {
  const tbody = document.getElementById("finLedgerBody");
  const sub = document.getElementById("finLedgerSub");
  if (!tbody) return;

  const allRows = (financeState.data && financeState.data.ledger) || [];
  const q = financeState.ledgerQuery.trim().toLowerCase();
  const outcome = financeState.ledgerOutcome;

  const filtered = allRows.filter((row) => {
    if (outcome === "win" && Number(row.pnl_usd || 0) <= 0) return false;
    if (outcome === "loss" && Number(row.pnl_usd || 0) >= 0) return false;
    if (q) {
      const hay = `${row.symbol} ${row.source_label} ${row.reason} ${row.side}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (sub) {
    sub.textContent = `Exibindo ${filtered.length} de ${allRows.length} operação(ões) encerrada(s) no período`;
  }

  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-cell">Nenhuma operação encontrada com os filtros atuais.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered
    .map((r) => {
      const pnl = Number(r.pnl_usd || 0);
      const roi = Number(r.pnl_pct || 0);
      const pnlCls = pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "dim";
      const sideCls = r.side === "BUY" ? "text-up" : "text-down";
      const srcCls = r.source === "xtb" ? "xtb" : "bot";
      let rCls = "";
      if (r.reason === "TAKE_PROFIT") rCls = "tp";
      else if (r.reason === "STOP_LOSS") rCls = "sl";
      else if (r.reason === "BREAKEVEN") rCls = "be";

      return `
        <tr>
          <td class="mono-val dim">${escapeHtml((r.closed_at || "").replace("T", " ").slice(0, 16))}</td>
          <td><span class="fin-source-tag ${srcCls}">${escapeHtml(r.source_label || r.source)}</span></td>
          <td class="mono-val"><strong>${escapeHtml(r.symbol)}</strong></td>
          <td class="mono-val ${sideCls}"><strong>${escapeHtml(r.side)}</strong></td>
          <td class="mono-val dim">${escapeHtml(r.size_label || "--")}</td>
          <td class="mono-val">${escapeHtml(formatPrice(r.entry_price, r.symbol))}</td>
          <td class="mono-val">${escapeHtml(formatPrice(r.exit_price, r.symbol))}</td>
          <td><span class="fin-reason-tag ${rCls}">${escapeHtml(r.reason)}</span></td>
          <td class="mono-val ${pnlCls}"><strong>${fmtSignedUsd(pnl)}</strong></td>
          <td class="mono-val ${pnlCls}">${roi >= 0 ? "+" : ""}${roi.toFixed(2)}%</td>
        </tr>
      `;
    })
    .join("");
}

function bindCanvasTooltips() {
  const eqCanvas = document.getElementById("finEquityCanvas");
  const eqTip = document.getElementById("finEquityTooltip");
  if (eqCanvas && eqTip) {
    eqCanvas.addEventListener("mousemove", (ev) => {
      const series = (financeState.data && financeState.data.equity_curve) || [];
      if (series.length < 2) return;
      const rect = eqCanvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const padLeft = 74;
      const padRight = 20;
      const plotW = Math.max(40, rect.width - padLeft - padRight);
      const frac = Math.max(0, Math.min(1, (x - padLeft) / plotW));
      const idx = Math.round(frac * (series.length - 1));
      if (financeState.equityHoverIdx !== idx) {
        financeState.equityHoverIdx = idx;
        renderEquityChart();
      }
      const pt = series[idx];
      const pnlCls = Number(pt.trade_pnl || 0) >= 0 ? "text-up" : "text-down";
      eqTip.innerHTML = `
        <div class="tt-title">${escapeHtml(pt.label)} • ${escapeHtml(pt.symbol || "")}</div>
        <div class="tt-row"><span class="tt-label">Equity</span><strong>${fmtUsd(pt.equity)}</strong></div>
        <div class="tt-row"><span class="tt-label">PnL Acum.</span><strong class="${Number(pt.cumulative_pnl) >= 0 ? "text-up" : "text-down"}">${fmtSignedUsd(pt.cumulative_pnl)}</strong></div>
        <div class="tt-row"><span class="tt-label">Evento</span><strong class="${pnlCls}">${fmtSignedUsd(pt.trade_pnl)}</strong></div>
        <div class="tt-row"><span class="tt-label">Drawdown</span><strong class="text-down">${Number(pt.drawdown_pct || 0).toFixed(2)}%</strong></div>
      `;
      const leftPos = Math.min(rect.width - 190, Math.max(10, x + 14));
      eqTip.style.left = `${leftPos}px`;
      eqTip.style.top = "14px";
      eqTip.classList.add("visible");
    });

    eqCanvas.addEventListener("mouseleave", () => {
      financeState.equityHoverIdx = -1;
      eqTip.classList.remove("visible");
      renderEquityChart();
    });
  }

  const dayCanvas = document.getElementById("finDailyCanvas");
  const dayTip = document.getElementById("finDailyTooltip");
  if (dayCanvas && dayTip) {
    dayCanvas.addEventListener("mousemove", (ev) => {
      const daily = (financeState.data && financeState.data.daily_pnl) || [];
      if (!daily.length) return;
      const rect = dayCanvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const padLeft = 66;
      const padRight = 16;
      const plotW = Math.max(40, rect.width - padLeft - padRight);
      const frac = Math.max(0, Math.min(0.9999, (x - padLeft) / plotW));
      const idx = Math.floor(frac * daily.length);
      if (financeState.dailyHoverIdx !== idx) {
        financeState.dailyHoverIdx = idx;
        renderDailyPnlChart();
      }
      const d = daily[idx];
      const pnlCls = Number(d.pnl_usd || 0) >= 0 ? "text-up" : "text-down";
      dayTip.innerHTML = `
        <div class="tt-title">📅 ${escapeHtml(d.date)}</div>
        <div class="tt-row"><span class="tt-label">PnL do Dia</span><strong class="${pnlCls}">${fmtSignedUsd(d.pnl_usd)}</strong></div>
        <div class="tt-row"><span class="tt-label">Operações</span><strong>${d.trades} (${d.wins}W / ${d.losses}L)</strong></div>
      `;
      const leftPos = Math.min(rect.width - 185, Math.max(10, x + 12));
      dayTip.style.left = `${leftPos}px`;
      dayTip.style.top = "14px";
      dayTip.classList.add("visible");
    });

    dayCanvas.addEventListener("mouseleave", () => {
      financeState.dailyHoverIdx = -1;
      dayTip.classList.remove("visible");
      renderDailyPnlChart();
    });
  }
}

export function renderAllFinanceCharts() {
  if (!financeState.data) return;
  renderEquityChart();
  renderDailyPnlChart();
  renderDonutChart();
  renderAssetBreakdown();
  renderCalendarHeatmap();
  renderLedgerTable();
}

export async function fetchFinanceOverview(showSuccessToast = false) {
  try {
    const url = `/api/finance/overview?period=${encodeURIComponent(financeState.period)}&source=${encodeURIComponent(financeState.source)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    financeState.data = data;
    renderSummary(data.summary);
    renderAllFinanceCharts();
    if (showSuccessToast) {
      showToast("💰 Painel financeiro atualizado com dados reais.", "success");
    }
  } catch (err) {
    if (showSuccessToast) {
      showToast(`Erro ao carregar dados financeiros: ${err.message || err}`, "error");
    }
  }
}

export function initFinanceUI() {
  if (financeState.eventsBound) return;
  financeState.eventsBound = true;

  document.querySelectorAll("#financePeriodFilters .fin-pill[data-period]").forEach((btn) => {
    btn.addEventListener("click", () => {
      financeState.period = btn.dataset.period || "30d";
      document.querySelectorAll("#financePeriodFilters .fin-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      fetchFinanceOverview(false);
    });
  });

  document.querySelectorAll("#financeSourceFilters .fin-pill[data-source]").forEach((btn) => {
    btn.addEventListener("click", () => {
      financeState.source = btn.dataset.source || "all";
      document.querySelectorAll("#financeSourceFilters .fin-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      fetchFinanceOverview(false);
    });
  });

  document.querySelectorAll("#finEquityModeTabs .fin-pill[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      financeState.equityMode = btn.dataset.mode || "equity";
      document.querySelectorAll("#finEquityModeTabs .fin-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderEquityChart();
    });
  });

  document.querySelectorAll("#finDonutModeTabs .fin-pill[data-dmode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      financeState.donutMode = btn.dataset.dmode || "category";
      document.querySelectorAll("#finDonutModeTabs .fin-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderDonutChart();
    });
  });

  document.querySelectorAll("#finLedgerOutcomeFilters .fin-pill[data-outcome]").forEach((btn) => {
    btn.addEventListener("click", () => {
      financeState.ledgerOutcome = btn.dataset.outcome || "all";
      document.querySelectorAll("#finLedgerOutcomeFilters .fin-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderLedgerTable();
    });
  });

  const searchInp = document.getElementById("finLedgerSearch");
  if (searchInp) {
    searchInp.addEventListener("input", () => {
      financeState.ledgerQuery = searchInp.value || "";
      renderLedgerTable();
    });
  }

  const refreshBtn = document.getElementById("btnFinanceRefresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => fetchFinanceOverview(true));
  }

  const exportBtn = document.getElementById("btnFinanceExportCsv");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const dlUrl = `/api/finance/export.csv?period=${encodeURIComponent(financeState.period)}&source=${encodeURIComponent(financeState.source)}`;
      window.location.href = dlUrl;
      showToast("⬇️ Gerando extrato CSV financeiro...", "info");
    });
  }

  bindCanvasTooltips();

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const view = document.getElementById("view-finance");
      if (view && view.classList.contains("active")) {
        renderAllFinanceCharts();
      }
    }, 100);
  });
}
