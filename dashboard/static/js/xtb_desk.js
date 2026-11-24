/**
 * Watchlist Multi-Ativos e Mesa de Operações XTB (FyNex Quant AI)
 */

import { hooks, state } from "./state.js";
import { csrfHeaders, escapeHtml, fmtSignedUsd, fmtUsd, formatPrice, getInstrumentMeta, showToast } from "./utils.js";

let xtbOrderInFlight = false;
const xtbClosingIds = new Set();

export async function fetchInstrumentsCatalog() {
  try {
    const res = await fetch("/api/market/instruments");
    if (!res.ok) return;
    const data = await res.json();
    const map = {};
    for (const inst of data.instruments || []) {
      map[inst.symbol] = inst;
    }
    state.instrumentsMap = map;
  } catch (_) {
    // fallback silencioso
  }
}

export async function fetchWatchlist() {
  state.lastWatchlistFetchAt = Date.now();
  try {
    const res = await fetch("/api/market/watchlist?category=all");
    if (!res.ok) return;
    const data = await res.json();
    state.watchlistData = data.items || [];
    renderWatchlist();
  } catch (_) {
    // silencioso
  }
}

export function renderWatchlist() {
  const container = document.getElementById("watchlistContainer");
  if (!container) return;

  const qInput = document.getElementById("watchlistSearch");
  const query = (qInput ? qInput.value : "").trim().toLowerCase();

  const filtered = state.watchlistData.filter((item) => {
    if (state.watchlistCategory === "commodities") {
      if (item.category !== "commodities" && item.category !== "indices") return false;
    } else if (state.watchlistCategory !== "all" && item.category !== state.watchlistCategory) {
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
      const isAct = item.symbol === state.activeSymbol;
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
      if (sym && sym !== state.activeSymbol && typeof hooks.onSymbolChange === "function") {
        hooks.onSymbolChange(sym);
      }
    });
  });
}

export function updateXtbMarginEstimate() {
  const lotsEl = document.getElementById("xtbLotsInput");
  const levEl = document.getElementById("xtbLeverageSelect");
  const marginEl = document.getElementById("xtbEstMargin");
  if (!lotsEl || !levEl || !marginEl) return;

  const lots = parseFloat(lotsEl.value) || 0;
  const lev = Math.max(1, parseInt(levEl.value, 10) || 1);
  const price = state.currentAskPrice || state.lastKnownPrice || 0;
  const meta = getInstrumentMeta(state.activeSymbol);
  const contractSize = Number(meta.contract_size || 1);

  if (lots <= 0 || price <= 0) {
    marginEl.textContent = "$0.00";
    return;
  }

  const [base, quote] = state.activeSymbol.split("/");
  const units = lots * contractSize;
  let notional = units * price;
  if (quote !== "USD" && quote !== "USDT") {
    notional = base === "USD" ? units : units * 1.08;
  }
  const margin = notional / lev;
  marginEl.textContent = fmtUsd(margin);
}

export async function submitXtbOrder(side) {
  if (xtbOrderInFlight) return;
  const lots = parseFloat(document.getElementById("xtbLotsInput")?.value || "0");
  const leverage = parseInt(document.getElementById("xtbLeverageSelect")?.value || "10", 10);
  const slRaw = document.getElementById("xtbSlInput")?.value?.trim();
  const tpRaw = document.getElementById("xtbTpInput")?.value?.trim();

  if (!Number.isFinite(lots) || lots <= 0) {
    showToast("Informe um volume (lotes) válido.", "error");
    return;
  }

  const payload = {
    symbol: state.activeSymbol,
    side,
    lots,
    leverage,
    sl_price: slRaw ? parseFloat(slRaw) : null,
    tp_price: tpRaw ? parseFloat(tpRaw) : null,
  };

  xtbOrderInFlight = true;
  try {
    const res = await fetch("/api/xtb/order", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
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
  } finally {
    xtbOrderInFlight = false;
  }
}

export async function closeXtbPosition(posId) {
  if (xtbClosingIds.has(posId)) return;
  xtbClosingIds.add(posId);
  try {
    const res = await fetch(`/api/xtb/close/${posId}`, {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || "Erro ao fechar posição.", "error");
      return;
    }
    showToast(`💰 ${data.message}`, "success");
    await fetchXtbAccount();
  } catch (err) {
    showToast(`Erro ao fechar posição: ${err}`, "error");
  } finally {
    xtbClosingIds.delete(posId);
  }
}

export async function fetchXtbAccount() {
  state.lastXtbFetchAt = Date.now();
  try {
    const res = await fetch("/api/xtb/account");
    if (!res.ok) return;
    const acc = await res.json();

    state.xtbFreeMarginUsd = Number(acc.free_margin_usd || 0);
    const positions = acc.open_positions || [];
    const trades = acc.recent_trades || [];
    const floatPnl = Number(acc.floating_pnl_usd || 0);

    const set = (id, text, cls = null) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      if (cls !== null) el.className = cls;
    };

    set("sideXtbEquity", fmtUsd(acc.equity_usd));
    set("sideXtbFloating", fmtSignedUsd(floatPnl), `mono-val ${floatPnl >= 0 ? "text-up" : "text-down"}`);
    set("sideXtbFreeMargin", fmtUsd(acc.free_margin_usd));
    set("navOpenPosBadge", String(positions.length));
    set("xtbFreeMarginTicket", fmtUsd(acc.free_margin_usd));
    set("openPositionsCount", `${positions.length} aberta(s)`);
    set("terminalFloatingPnl", `PnL Aberto: ${fmtSignedUsd(floatPnl)}`, `mono-val ${floatPnl >= 0 ? "text-up" : "text-down"}`);

    set("portXtbBalance", fmtUsd(acc.balance_usd));
    set("portXtbRealized", `Realizado: ${fmtSignedUsd(acc.realized_pnl_usd)}`);
    set("portXtbEquity", fmtUsd(acc.equity_usd));
    set("portXtbFloating", `PnL Flutuante: ${fmtSignedUsd(floatPnl)}`);
    set("portXtbMargin", fmtUsd(acc.used_margin_usd));
    set("portXtbFreeMargin", `Livre: ${fmtUsd(acc.free_margin_usd)}`);

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

    const histBody = document.getElementById("xtbTradesHistoryBody");
    set("xtbTradesCount", `${trades.length} encerrada(s)`);
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
