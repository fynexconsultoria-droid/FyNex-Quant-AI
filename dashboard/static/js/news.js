/**
 * Feed de Notícias Financeiras ao Vivo (FyNex Quant AI)
 */

import { state } from "./state.js";
import { escapeHtml } from "./utils.js";

export function sentimentBadgeHtml(sent) {
  if (sent === "positivo") return '<span class="sent-chip" style="color:#10b981;">🟢 Alta</span>';
  if (sent === "negativo") return '<span class="sent-chip" style="color:#f43f5e;">🔴 Baixa</span>';
  return '<span class="sent-chip" style="color:#94a3b8;">⚪ Neutro</span>';
}

export function renderMiniNews(items) {
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

export function renderFullNews(items) {
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

let newsFetchSeq = 0;

export async function fetchNewsFeed(forceRefresh = false) {
  const seq = ++newsFetchSeq;
  state.lastNewsFetchAt = Date.now();
  try {
    const qInput = document.getElementById("newsSearchInput");
    const qVal = qInput ? qInput.value.trim() : "";
    const params = new URLSearchParams({
      category: state.newsCategory,
      q: qVal,
      limit: "36",
      refresh: forceRefresh ? "true" : "false",
    });
    const res = await fetch(`/api/news?${params.toString()}`);
    if (!res.ok || seq !== newsFetchSeq) return;
    const data = await res.json();
    if (seq !== newsFetchSeq) return;
    state.newsCacheItems = data.items || [];

    const updEl = document.getElementById("newsLastUpdated");
    if (updEl && data.updated_at) updEl.textContent = `Atualizado ${data.updated_at}`;

    renderMiniNews(state.newsCacheItems.slice(0, 4));
    renderFullNews(state.newsCacheItems);
  } catch (_) {
    // silencioso
  }
}
