/**
 * Copiloto XTB (xStation 5) e Configuração de Alertas (Telegram & WhatsApp CallMeBot)
 */

import { csrfHeaders, escapeHtml, formatPrice, showToast } from "./utils.js";

let copilotSoundEnabled = true;
let lastKnownCopilotSignalId = 0;
let sharedCopilotAudioCtx = null;

export function toggleCopilotSound() {
  copilotSoundEnabled = !copilotSoundEnabled;
  const btn = document.getElementById("btnCopilotSound");
  if (btn) btn.textContent = copilotSoundEnabled ? "🔊 Som: ON" : "🔇 Som: OFF";
  if (copilotSoundEnabled) playCopilotChime();
}

export function playCopilotChime() {
  if (!copilotSoundEnabled) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    if (!sharedCopilotAudioCtx || sharedCopilotAudioCtx.state === "closed") {
      sharedCopilotAudioCtx = new AudioCtx();
    }
    const ctx = sharedCopilotAudioCtx;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(1174.66, ctx.currentTime + 0.14);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.36);
  } catch (_) {
    // autoplay bloqueado antes de interação
  }
}

export function applyWhatsAppConfigToUI(wa) {
  if (!wa) return;
  const statusEl = document.getElementById("waConfigStatus");
  const phoneInp = document.getElementById("waPhoneInput");
  const hourSel = document.getElementById("waDailyHourSelect");
  const enChk = document.getElementById("waEnabledCheck");
  const btnToggle = document.getElementById("btnToggleWhatsAppBox");

  if (phoneInp && !phoneInp.value && wa.phone) phoneInp.value = wa.phone;
  if (hourSel && wa.daily_report_hour !== undefined && !hourSel.dataset.userEdited) {
    hourSel.value = String(wa.daily_report_hour);
  }
  if (enChk && !enChk.dataset.userEdited) enChk.checked = Boolean(wa.enabled);
  if (btnToggle) btnToggle.textContent = wa.active ? "💬 WhatsApp: ON" : "💬 WhatsApp";

  if (statusEl) {
    if (wa.active) {
      statusEl.textContent = `✅ WhatsApp CallMeBot ATIVO (${wa.phone}) • API Key ${wa.apikey_masked} • Resumo diário às ${String(wa.daily_report_hour ?? 21).padStart(2, "0")}:00 (Brasília).`;
    } else if (wa.configured && !wa.enabled) {
      statusEl.textContent = `⏸️ WhatsApp configurado (${wa.phone}), mas pausado. Marque "Ativo" e salve para reativar.`;
    } else if (wa.phone) {
      statusEl.textContent = `📱 Número ${wa.phone} salvo! Falta apenas colar a API Key recebida do CallMeBot e clicar em "Salvar & Testar".`;
    } else {
      statusEl.textContent = "Receba no WhatsApp: entradas, saídas (lucro/prejuízo), falhas operacionais e resumo diário automático.";
    }
  }
}

export async function fetchWhatsAppConfigUI() {
  try {
    const res = await fetch("/api/alerts/whatsapp");
    if (!res.ok) return;
    applyWhatsAppConfigToUI(await res.json());
  } catch (_) {
    // silencioso
  }
}

export async function updateCopilotSignalStatus(signalId, status) {
  try {
    const res = await fetch(`/api/xtb/copilot/signals/${signalId}/status`, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ status }),
    });
    if (!res.ok) return;
    showToast(status === "EXECUTED_XTB" ? "✅ Marcado como executado na XTB!" : "Sinal dispensado.", "info");
    await fetchXtbCopilotSignals();
  } catch (_) {
    // silencioso
  }
}

export function renderXtbCopilotTable(signals) {
  const tbody = document.getElementById("xtbCopilotBody");
  if (!tbody) return;
  if (!signals || !signals.length) {
    tbody.innerHTML =
      '<tr><td colspan="10" class="empty-cell">Nenhum sinal XTB gerado ainda. Clique em "Escanear XTB Agora" quando o mercado global estiver aberto.</td></tr>';
    return;
  }

  tbody.innerHTML = signals
    .map((s) => {
      const sideCls = s.side === "BUY" ? "buy" : "sell";
      const isDone = s.status === "EXECUTED_XTB";
      const isDismissed = s.status === "DISMISSED";
      const statusHtml = isDone
        ? '<span class="copilot-mini-btn done">✅ Na XTB</span>'
        : isDismissed
          ? '<span class="dim">Dispensado</span>'
          : `
          <button type="button" class="copilot-mini-btn" data-cop-copy="${escapeHtml(s.id)}">📋 Copiar</button>
          <button type="button" class="copilot-mini-btn" data-cop-exec="${escapeHtml(s.id)}">✅ Executei</button>
          <button type="button" class="copilot-mini-btn" data-cop-dis="${escapeHtml(s.id)}">✖️</button>
        `;
      return `
        <tr style="${isDismissed ? "opacity:0.48;" : ""}">
          <td class="dim">${escapeHtml(String(s.created_at || "").replace(" UTC", ""))}</td>
          <td><strong>${escapeHtml(s.xtb_symbol)}</strong> <span class="dim">(${escapeHtml(s.symbol)})</span></td>
          <td><span class="side-pill ${sideCls}">${escapeHtml(s.side)}</span></td>
          <td><strong>${Number(s.score || 0).toFixed(0)}/100</strong></td>
          <td class="mono-val">${escapeHtml(formatPrice(s.entry_price, s.symbol))}</td>
          <td class="mono-val text-down">${s.sl_price ? escapeHtml(formatPrice(s.sl_price, s.symbol)) : "--"}</td>
          <td class="mono-val text-up">${s.tp_price ? escapeHtml(formatPrice(s.tp_price, s.symbol)) : "--"}</td>
          <td class="mono-val">${escapeHtml(s.suggested_lots)} (1:${escapeHtml(s.leverage)})</td>
          <td class="dim">${escapeHtml(s.reason || "")}</td>
          <td><div class="copilot-btn-row">${statusHtml}</div></td>
        </tr>
      `;
    })
    .join("");

  const byId = {};
  for (const s of signals) byId[String(s.id)] = s;

  tbody.querySelectorAll("[data-cop-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sig = byId[btn.dataset.copCopy];
      if (!sig) return;
      try {
        await navigator.clipboard.writeText(sig.copy_text || `${sig.xtb_symbol} ${sig.side} @ ${sig.entry_price}`);
        showToast(`📋 Ordem ${sig.xtb_symbol} copiada para a xStation 5!`, "success");
      } catch (_) {
        showToast("Não foi possível copiar automaticamente.", "error");
      }
    });
  });

  tbody.querySelectorAll("[data-cop-exec]").forEach((btn) => {
    btn.addEventListener("click", () => updateCopilotSignalStatus(Number(btn.dataset.copExec), "EXECUTED_XTB"));
  });

  tbody.querySelectorAll("[data-cop-dis]").forEach((btn) => {
    btn.addEventListener("click", () => updateCopilotSignalStatus(Number(btn.dataset.copDis), "DISMISSED"));
  });
}

export async function fetchXtbCopilotSignals() {
  try {
    const res = await fetch("/api/xtb/copilot/signals?limit=25");
    if (!res.ok) return;
    const data = await res.json();

    const statusEl = document.getElementById("copilotMarketStatus");
    const badgeEl = document.getElementById("copilotModeBadge");
    if (statusEl) statusEl.textContent = `Binance: Cripto Automático • XTB: ${data.status_message || "Monitorando"}`;
    if (badgeEl) {
      badgeEl.textContent = data.market_open
        ? `🟢 XTB ABERTO • ${data.new_count || 0} novo(s)`
        : "⏸️ XTB FECHADO (FDS)";
    }

    const tgStatus = document.getElementById("tgConfigStatus");
    const tgChatInp = document.getElementById("tgChatIdInput");
    if (data.telegram) {
      if (tgStatus) {
        tgStatus.textContent = data.telegram.configured
          ? `✅ Telegram conectado (Chat ${data.telegram.chat_id}) — alertas ativos!`
          : "Opcional: informe Bot Token e Chat ID para receber alertas no celular.";
      }
      if (tgChatInp && !tgChatInp.value && data.telegram.chat_id) tgChatInp.value = data.telegram.chat_id;
    }

    if (data.whatsapp) applyWhatsAppConfigToUI(data.whatsapp);

    const signals = data.signals || [];
    if (signals.length > 0) {
      const topId = Number(signals[0].id || 0);
      if (lastKnownCopilotSignalId > 0 && topId > lastKnownCopilotSignalId && signals[0].status === "NEW") {
        playCopilotChime();
        showToast(`🧭 Novo sinal XTB: ${signals[0].xtb_symbol} ${signals[0].side} @ ${signals[0].entry_price}`, "success");
      }
      lastKnownCopilotSignalId = Math.max(lastKnownCopilotSignalId, topId);
    }

    renderXtbCopilotTable(signals);
  } catch (_) {
    // silencioso
  }
}

export async function scanXtbCopilotNow() {
  const btn = document.getElementById("btnScanXtbNow");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Escaneando...";
  }
  try {
    const res = await fetch("/api/xtb/copilot/scan", { method: "POST", headers: csrfHeaders() });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao escanear mercados XTB.", "error");
      return;
    }
    if (!data.market_open) {
      showToast(`⏸️ ${data.status_message}`, "info");
    } else if (data.last_action) {
      playCopilotChime();
      showToast(`🧭 ${data.last_action}`, "success");
    } else {
      const top = (data.opportunities || [])[0];
      const msg = top
        ? `Varredura concluída. Melhor score atual: ${top.symbol} (${top.side} ${Number(top.score || 0).toFixed(0)}/100) — aguardando >= 72.`
        : "Varredura concluída. Nenhuma entrada forte acima de 72/100 agora.";
      showToast(msg, "info");
    }
    await fetchXtbCopilotSignals();
  } catch (err) {
    showToast(`Erro no scanner XTB: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🔎 Escanear XTB Agora";
    }
  }
}

export async function saveTelegramConfigUI() {
  const token = document.getElementById("tgBotTokenInput")?.value?.trim() || "";
  const chatId = document.getElementById("tgChatIdInput")?.value?.trim() || "";
  if (!chatId) {
    showToast("Informe o seu Chat ID do Telegram.", "error");
    return;
  }
  const btn = document.getElementById("btnSaveTelegram");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Testando...";
  }
  try {
    const res = await fetch("/api/xtb/copilot/telegram", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ bot_token: token, chat_id: chatId, send_test: true }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao salvar Telegram.", "error");
      return;
    }
    if (data.test_result && data.test_result.sent) {
      showToast("✅ Telegram conectado! Mensagem de teste enviada.", "success");
    } else {
      showToast(`⚠️ Salvo, mas o teste falhou: ${(data.test_result && data.test_result.error) || "verifique token/chat_id"}`, "error");
    }
    await fetchXtbCopilotSignals();
  } catch (err) {
    showToast(`Erro ao salvar Telegram: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "💾 Salvar & Testar";
    }
  }
}

export async function saveWhatsAppConfigUI() {
  const phone = document.getElementById("waPhoneInput")?.value?.trim() || "";
  const apikey = document.getElementById("waApiKeyInput")?.value?.trim() || "";
  const dailyHour = Number(document.getElementById("waDailyHourSelect")?.value || 21);
  const enabled = Boolean(document.getElementById("waEnabledCheck")?.checked ?? true);

  if (!phone) {
    showToast("Informe o seu número de WhatsApp com DDD (ex.: 62986290913).", "error");
    return;
  }
  const btn = document.getElementById("btnSaveWhatsApp");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Testando...";
  }
  try {
    const res = await fetch("/api/alerts/whatsapp", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ phone, apikey, enabled, daily_report_hour: dailyHour, send_test: enabled }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao salvar configuração do WhatsApp.", "error");
      return;
    }
    const hourSel = document.getElementById("waDailyHourSelect");
    const enChk = document.getElementById("waEnabledCheck");
    if (hourSel) delete hourSel.dataset.userEdited;
    if (enChk) delete enChk.dataset.userEdited;
    if (data.whatsapp) applyWhatsAppConfigToUI(data.whatsapp);
    if (!enabled) {
      showToast("⏸️ Alertas do WhatsApp pausados e salvos com sucesso.", "info");
    } else if (data.test_result && data.test_result.sent) {
      showToast("✅ WhatsApp conectado! Mensagem de teste enviada para o seu celular.", "success");
    } else {
      showToast(
        `⚠️ Config salva, mas o envio de teste falhou: ${(data.test_result && data.test_result.error) || "verifique a API Key do CallMeBot"}`,
        "error"
      );
    }
  } catch (err) {
    showToast(`Erro ao salvar WhatsApp: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "💾 Salvar & Testar";
    }
  }
}

export async function sendDailySummaryWhatsAppUI() {
  const btn = document.getElementById("btnSendDailySummaryWa");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Enviando...";
  }
  try {
    const res = await fetch("/api/alerts/whatsapp/daily-summary", { method: "POST", headers: csrfHeaders() });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Falha ao enviar resumo diário.", "error");
      return;
    }
    if (data.send_result && data.send_result.sent) {
      showToast("📊 Resumo do dia enviado com sucesso para o seu WhatsApp!", "success");
    } else {
      showToast(
        `⚠️ Não foi possível enviar o resumo: ${(data.send_result && data.send_result.error) || "configure a API Key do CallMeBot"}`,
        "error"
      );
    }
  } catch (err) {
    showToast(`Erro ao enviar resumo diário: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "📊 Resumo Hoje";
    }
  }
}
