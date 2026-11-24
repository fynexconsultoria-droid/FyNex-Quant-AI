/**
 * Utilitários de Formatação, Segurança XSS, CSRF e Toast (FyNex Quant AI)
 */

import { state } from "./state.js";

export function getInstrumentMeta(symbol = state.activeSymbol) {
  return (
    state.instrumentsMap[symbol] || {
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

export function isActiveForex() {
  return getInstrumentMeta(state.activeSymbol).category !== "crypto";
}

export function formatPrice(val, symbol = state.activeSymbol) {
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

export function formatCompactUsd(val) {
  const num = Number(val || 0);
  if (!Number.isFinite(num) || num <= 0) return "--";
  if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
  if (num >= 1e6) return `$${(num / 1e6).toFixed(2)}M`;
  if (num >= 1e3) return `$${(num / 1e3).toFixed(1)}K`;
  return `$${num.toFixed(0)}`;
}

export function fmtUsd(v) {
  return `$${Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function fmtSignedUsd(v) {
  return `${Number(v || 0) >= 0 ? "+$" : "-$"}${Math.abs(Number(v || 0)).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function showToast(message, type = "info") {
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

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

export function csrfHeaders(extra = {}) {
  return {
    "X-CSRF-Token": getCsrfToken(),
    ...extra,
  };
}
