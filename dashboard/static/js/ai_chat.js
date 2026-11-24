/**
 * Drawer Flutuante do Chat Quantitativo da IA (Evolução & Resultados 24h / 7d / 30d)
 */

import { state } from "./state.js";
import { csrfHeaders, escapeHtml, fmtSignedUsd, isActiveForex } from "./utils.js";

let aiChatInitialized = false;
let aiChatDrawerOpen = false;

export function formatChatMarkdownSafe(text) {
  let safe = escapeHtml(text || "");
  safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  safe = safe.replace(/(^|\s)_([^_\n]+?)_(?=\s|[.,!?;:]|$)/g, "$1<em>$2</em>");
  return safe;
}

export function appendAiChatBubble(role, text) {
  const feed = document.getElementById("aiChatMessages");
  if (!feed) return;
  const wrap = document.createElement("div");
  wrap.className = `chat-msg ${role === "user" ? "user" : "ai"}`;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.innerHTML = formatChatMarkdownSafe(text);
  wrap.appendChild(bubble);
  feed.appendChild(wrap);
  feed.scrollTop = feed.scrollHeight;
}

export function renderAiPerformanceBoxes(summary) {
  if (!summary || !summary.periods) return;
  const map = [
    ["24h", "perf24hPnl", "perf24hSub"],
    ["7d", "perf7dPnl", "perf7dSub"],
    ["30d", "perf30dPnl", "perf30dSub"],
  ];
  for (const [key, pnlId, subId] of map) {
    const p = summary.periods[key] || {};
    const pnl = Number(p.total_pnl_usd || 0);
    const pnlEl = document.getElementById(pnlId);
    const subEl = document.getElementById(subId);
    if (pnlEl) {
      pnlEl.textContent = fmtSignedUsd(pnl);
      pnlEl.className = `mono-val ${pnl >= 0 ? "text-up" : "text-down"}`;
    }
    if (subEl) {
      const accStr = p.predictions_evaluated > 0 ? `${Number(p.accuracy_pct || 0).toFixed(1)}%` : "--%";
      subEl.textContent = `${p.total_trades || 0} trade(s) • WR ${Number(p.win_rate_pct || 0).toFixed(0)}% • Acurácia: ${accStr}`;
    }
  }
}

export async function sendAiChatMessage(presetText = null, silentUserBubble = false) {
  const inp = document.getElementById("aiChatInput");
  const text = (presetText !== null ? presetText : inp ? inp.value : "").trim();
  if (!text) return;

  if (!silentUserBubble) appendAiChatBubble("user", text);
  if (inp && presetText === null) inp.value = "";

  const btn = document.getElementById("btnSendAiChat");
  if (btn) btn.disabled = true;

  try {
    const sym = isActiveForex() ? "BTC/USDT" : state.activeSymbol;
    const res = await fetch("/api/ai/chat", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ message: text, symbol: sym, timeframe: state.activeTimeframe }),
    });
    const data = await res.json();
    if (!res.ok) {
      appendAiChatBubble("ai", `⚠️ ${data.detail || "Não foi possível consultar a IA agora."}`);
      return;
    }
    if (data.summary) renderAiPerformanceBoxes(data.summary);
    appendAiChatBubble("ai", data.reply || "Sem resposta.");
  } catch (err) {
    appendAiChatBubble("ai", `⚠️ Erro ao falar com a IA: ${err}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function fetchAiPerformanceAndWelcome() {
  const sym = isActiveForex() ? "BTC/USDT" : state.activeSymbol;
  try {
    const res = await fetch(
      `/api/ai/performance?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(state.activeTimeframe)}`
    );
    if (res.ok) renderAiPerformanceBoxes(await res.json());
  } catch (_) {
    // silencioso
  }

  if (!aiChatInitialized) {
    aiChatInitialized = true;
    const feed = document.getElementById("aiChatMessages");
    if (feed) feed.innerHTML = "";
    await sendAiChatMessage("Resumo geral da sua evolução e resultados", true);
  }
}

export function toggleAiChatDrawer(forceState = null) {
  const drawer = document.getElementById("aiChatDrawer");
  if (!drawer) return;
  aiChatDrawerOpen = forceState !== null ? Boolean(forceState) : !aiChatDrawerOpen;
  drawer.classList.toggle("open", aiChatDrawerOpen);
  drawer.setAttribute("aria-hidden", aiChatDrawerOpen ? "false" : "true");
  if (aiChatDrawerOpen) {
    fetchAiPerformanceAndWelcome();
    setTimeout(() => {
      const inp = document.getElementById("aiChatInput");
      if (inp) inp.focus();
    }, 80);
  }
}
