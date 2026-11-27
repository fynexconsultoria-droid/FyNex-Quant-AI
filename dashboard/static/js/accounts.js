/**
 * Divisão de Contas Estilo Corretora Internacional (Conta Demo / Treino IA vs Conta Real / Live)
 * Gerencia alternância de contas (/demo e /live), tema visual, controle paralelo de robôs,
 * governança de promoção do Modelo Campeão e checklist de prontidão para dinheiro real.
 */

import { hooks, state } from "./state.js";
import { csrfHeaders, escapeHtml, fmtUsd, formatPrice, isActiveForex, showToast } from "./utils.js";

const STORAGE_KEY = "fynex_active_account";

export function detectInitialAccount() {
  const path = (window.location.pathname || "").toLowerCase();
  if (path === "/live" || path.startsWith("/live/")) return "live";
  if (path === "/demo" || path.startsWith("/demo/")) return "demo";
  const meta = document.querySelector('meta[name="initial-account"]');
  if (meta && (meta.content === "live" || meta.content === "demo")) {
    return meta.content;
  }
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "live" || saved === "demo") return saved;
  } catch (_) {
    // silencioso
  }
  return "demo";
}

export function applyAccountTheme(account) {
  const acc = account === "live" ? "live" : "demo";
  state.activeAccount = acc;
  document.body.dataset.account = acc;

  document.title =
    acc === "live"
      ? "FyNex Quant AI — CONTA REAL (Dinheiro Real • Binance Spot)"
      : "FyNex Quant AI — CONTA DEMO (Treinamento & Simulação IA)";

  const btnDemo = document.getElementById("btnSwitchDemo");
  const btnLive = document.getElementById("btnSwitchLive");
  if (btnDemo) btnDemo.classList.toggle("active", acc === "demo");
  if (btnLive) btnLive.classList.toggle("active", acc === "live");

  const pill = document.getElementById("accBannerPill");
  const title = document.getElementById("accBannerTitle");
  const desc = document.getElementById("accBannerDesc");
  const btnQuickSwitch = document.getElementById("btnQuickSwitchAccount");

  if (acc === "live") {
    if (pill) {
      pill.textContent = "💎 CONTA REAL (LIVE)";
      pill.className = "acc-banner-pill live";
    }
    if (title) title.textContent = "Ambiente de Operação Real — Binance Spot (Dinheiro Real)";
    if (desc) {
      desc.textContent =
        "Modo Sniper (só alto potencial ≥78 pts) em gráficos 1h+, pesquisa pré-ordem (Bolsas + Notícias), Demo treinando 24/7 e aprovação obrigatória do admin.";
    }
    if (btnQuickSwitch) {
      btnQuickSwitch.textContent = "🧪 Ir para Conta Demo (Treino)";
      btnQuickSwitch.className = "btn btn-secondary";
    }
  } else {
    if (pill) {
      pill.textContent = "🧪 CONTA DEMO • TREINO IA";
      pill.className = "acc-banner-pill demo";
    }
    if (title) title.textContent = "Laboratório de Treinamento & Simulação Realista da IA";
    if (desc) {
      desc.textContent =
        "Simula entradas reais com taxas, slippage, SL/TP e auto-análise de erros. Promova o melhor modelo para a Conta Real quando estiver pronto.";
    }
    if (btnQuickSwitch) {
      btnQuickSwitch.textContent = "💎 Ir para Conta Real →";
      btnQuickSwitch.className = "btn btn-acc-gold";
    }
  }

  // Atualiza títulos sensíveis à conta
  const capTitle = document.getElementById("kpiCapitalTitle");
  if (capTitle) {
    capTitle.textContent = acc === "live" ? "Capital Real (Binance Spot)" : "Patrimônio Simulado (Demo)";
  }
  const finSub = document.getElementById("financeAccountPill");
  if (finSub) {
    finSub.textContent = acc === "live" ? "💎 CONTA REAL (DINHEIRO REAL)" : "🧪 CONTA DEMO (SIMULAÇÃO IA)";
    finSub.className = `acc-banner-pill ${acc}`;
  }
  const xtbSrcBtn = document.querySelector('#financeSourceFilters .fin-pill[data-source="xtb"]');
  if (xtbSrcBtn) {
    xtbSrcBtn.style.display = acc === "live" ? "none" : "";
    if (acc === "live" && xtbSrcBtn.classList.contains("active")) {
      xtbSrcBtn.classList.remove("active");
      const botSrcBtn = document.querySelector('#financeSourceFilters .fin-pill[data-source="bot"]')
        || document.querySelector('#financeSourceFilters .fin-pill[data-source="all"]');
      if (botSrcBtn) botSrcBtn.classList.add("active");
    }
  }
  const consoleTitle = document.getElementById("consoleLogTitle");
  const activeLogAcc = state.logAccountFilter || acc;
  if (consoleTitle) {
    consoleTitle.textContent =
      activeLogAcc === "live"
        ? "Console Log ao Vivo — Conta Real (logs/trading_bot_live.log)"
        : "Console Log ao Vivo — Conta Demo (logs/trading_bot_demo.log)";
  }
  document.querySelectorAll("#consoleAccountTabs .fin-pill[data-log-acc]").forEach((b) => {
    b.classList.toggle("active", b.dataset.logAcc === activeLogAcc);
  });
}

export async function switchAccount(targetAccount, { pushUrl = true, refresh = true } = {}) {
  const acc = targetAccount === "live" ? "live" : "demo";
  const changed = state.activeAccount !== acc;
  state.logAccountFilter = acc;
  applyAccountTheme(acc);

  try {
    window.localStorage.setItem(STORAGE_KEY, acc);
  } catch (_) {
    // silencioso
  }

  if (pushUrl && window.history && typeof window.history.pushState === "function") {
    const targetPath = acc === "live" ? "/live" : "/demo";
    if (window.location.pathname !== targetPath) {
      window.history.pushState({ account: acc }, "", targetPath);
    }
  }

  if (refresh && typeof hooks.onAccountChange === "function") {
    await hooks.onAccountChange(acc, changed);
  }
  if (changed) {
    showToast(
      acc === "live"
        ? "💎 Alternado para CONTA REAL (Dinheiro Real • Modelo Campeão)."
        : "🧪 Alternado para CONTA DEMO (Laboratório de Treinamento & Simulação IA).",
      acc === "live" ? "warning" : "info"
    );
  }
}

function setText(id, text, cls = null) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  if (cls !== null) el.className = cls;
}

export function syncPromoteButtonLabel(demoModelOverride = null) {
  const btnPromote = document.getElementById("btnPromoteModelToLive");
  if (!btnPromote) return;
  const verSel = document.getElementById("govPromoteVersionSelect");
  const selectedVer = verSel && verSel.value ? String(verSel.value) : "";
  const demoModel = demoModelOverride || (state.accountsOverview && state.accountsOverview.demo && state.accountsOverview.demo.active_model) || null;
  const targetVer = selectedVer || (demoModel && demoModel.version ? String(demoModel.version) : "");
  btnPromote.disabled = !targetVer;
  btnPromote.textContent = targetVer
    ? `🚀 Promover v${targetVer} para Conta Real`
    : "Treine um modelo na Demo primeiro";
}

export function renderAccountsOverview(data) {
  if (!data) return;
  state.accountsOverview = data;

  const demo = data.demo || {};
  const live = data.live || {};
  const policy = data.policy || {};
  const demoModel = demo.active_model || null;
  const liveModel = live.champion_model || null;
  const readiness = live.readiness || {};

  // 1. Sidebar switcher balances & parallel running dots
  const demoDot = document.getElementById("accDotDemo");
  const liveDot = document.getElementById("accDotLive");
  if (demoDot) demoDot.className = `acc-mini-dot demo ${demo.bot_online ? "online" : ""}`;
  if (liveDot) liveDot.className = `acc-mini-dot live ${live.bot_online ? "online" : ""}`;

  setText("accBalDemo", fmtUsd(demo.simulated_capital_usd ?? 1000));
  if (state.liveUsdtFree !== null && state.liveUsdtFree !== undefined) {
    setText("accBalLive", fmtUsd(state.liveUsdtFree));
  } else {
    setText("accBalLive", live.has_keys ? "Conectada" : "Sem chaves");
  }

  setText(
    "parallelDemoStatus",
    demo.bot_online ? `Demo: ON (#${demo.bot_pid})` : "Demo: OFF",
    `parallel-bot-chip ${demo.bot_online ? "text-up" : "dim"}`
  );
  setText(
    "parallelLiveStatus",
    live.bot_online ? `Real: ON (#${live.bot_pid})` : "Real: OFF",
    `parallel-bot-chip ${live.bot_online ? "text-up" : "dim"}`
  );

  // 2. Banner global no topo
  const activeIsLive = state.activeAccount === "live";
  const bannerModel = activeIsLive ? liveModel : demoModel;
  const bannerModelEl = document.getElementById("accBannerModelChip");
  if (bannerModelEl) {
    if (bannerModel) {
      const tag = activeIsLive
        ? live.has_explicit_champion
          ? "🏆 Campeão Real"
          : "⚠️ Fallback Latest"
        : "🧠 Modelo Demo";
      bannerModelEl.textContent = `${tag}: v${bannerModel.version} (${Number(bannerModel.accuracy_pct || 0).toFixed(1)}%)`;
    } else {
      bannerModelEl.textContent = activeIsLive ? "🏆 Sem modelo campeão ainda" : "🧠 Sem modelo treinado";
    }
  }

  const upgradeChip = document.getElementById("accBannerUpgradeBtn");
  if (upgradeChip) {
    if (live.has_newer_demo_model && demoModel) {
      upgradeChip.style.display = "inline-flex";
      upgradeChip.textContent = live.has_explicit_champion
        ? `✨ Promover v${demoModel.version} → Real`
        : `🚀 Promover v${demoModel.version} p/ Conta Real`;
    } else {
      upgradeChip.style.display = "none";
    }
  }

  const pendingBtn = document.getElementById("accBannerPendingBtn");
  const pendingCnt = Number(live.pending_approvals_count || 0);
  if (pendingBtn) {
    if (pendingCnt > 0) {
      pendingBtn.style.display = "inline-flex";
      pendingBtn.textContent = `🔐 ${pendingCnt} ordem(ns) aguardando aprovação`;
    } else {
      pendingBtn.style.display = "none";
    }
  }

  // 3. Card de Governança (Demo -> Ponte -> Real) na área Robô IA
  setText(
    "govDemoBotBadge",
    demo.bot_online ? `🟢 ONLINE (PID ${demo.bot_pid})` : "⚪ OFFLINE",
    `badge ${demo.bot_online ? "model-ver" : "badge-subtle"}`
  );
  setText("govDemoModelVer", demoModel ? `v${demoModel.version} (${String(demoModel.model_type || "ensemble").toUpperCase()})` : "Não treinado");
  setText("govDemoModelAcc", demoModel ? `${Number(demoModel.precision_buy_pct || demoModel.accuracy_pct || 0).toFixed(1)}%` : "--%");
  const dStats = demo.trade_stats || {};
  setText("govDemoWinRate", `${Number(dStats.win_rate || 0).toFixed(1)}% (${dStats.total_trades || 0} tr.)`);
  const dPnl = Number(dStats.total_pnl_usd || 0);
  setText("govDemoPnl", `${dPnl >= 0 ? "+$" : "-$"}${Math.abs(dPnl).toFixed(2)}`, `gov-metric-val ${dPnl >= 0 ? "text-up" : "text-down"}`);

  const btnDemoBot = document.getElementById("btnGovToggleDemoBot");
  if (btnDemoBot) {
    btnDemoBot.textContent = demo.bot_online ? "⏹ Parar Robô Demo" : "▶ Iniciar Robô Demo";
    btnDemoBot.className = demo.bot_online ? "btn btn-danger" : "btn btn-primary";
  }

  // Coluna Central: Ponte de Promoção
  const syncBadge = document.getElementById("govBridgeSyncBadge");
  if (syncBadge) {
    if (!demoModel) {
      syncBadge.textContent = "⏳ Aguardando treino na Demo";
      syncBadge.className = "badge badge-subtle";
    } else if (live.has_newer_demo_model) {
      syncBadge.textContent = live.has_explicit_champion
        ? `✨ Novo modelo v${demoModel.version} pronto!`
        : `🚀 v${demoModel.version} pronto para promoção`;
      syncBadge.className = "xtb-tag";
    } else {
      syncBadge.textContent = `✅ Sincronizado (v${liveModel ? liveModel.version : demoModel.version})`;
      syncBadge.className = "badge model-ver";
    }
  }

  const verSel = document.getElementById("govPromoteVersionSelect");
  if (verSel) {
    const versions = Array.isArray(data.available_versions) ? data.available_versions : [];
    const currentSelected = verSel.value;
    if (!versions.length) {
      verSel.innerHTML = '<option value="">Nenhum modelo em disco</option>';
    } else {
      verSel.innerHTML = versions
        .map((v) => {
          const flags = [];
          if (v.is_demo_latest) flags.push("Mais recente Demo");
          if (v.is_live_champion) flags.push("Atual Campeão Real");
          const suffix = flags.length ? ` — ${flags.join(" • ")}` : "";
          return `<option value="${escapeHtml(v.version)}">v${escapeHtml(v.version)}${escapeHtml(suffix)}</option>`;
        })
        .join("");
      if (currentSelected && versions.some((v) => String(v.version) === String(currentSelected))) {
        verSel.value = currentSelected;
      }
    }
  }

  const autoCheck = document.getElementById("chkAutoPromoteLive");
  if (autoCheck) autoCheck.checked = Boolean(policy.auto_promote_to_live);

  const demoAlwaysChk = document.getElementById("chkDemoAlwaysOn");
  if (demoAlwaysChk && policy.demo_always_on_training !== undefined) {
    demoAlwaysChk.checked = Boolean(policy.demo_always_on_training);
  }
  const liveHighChk = document.getElementById("chkLiveHighPotentialOnly");
  if (liveHighChk && policy.live_high_potential_only !== undefined) {
    liveHighChk.checked = Boolean(policy.live_high_potential_only);
  }
  const liveConfSel = document.getElementById("liveMinConfSelect");
  if (liveConfSel && policy.live_min_confidence_pct) {
    const cv = String(policy.live_min_confidence_pct);
    if (Array.from(liveConfSel.options).some((o) => o.value === cv)) liveConfSel.value = cv;
    liveConfSel.disabled = liveHighChk ? !liveHighChk.checked : false;
  }
  const liveScoreSel = document.getElementById("liveMinScoreSelect");
  if (liveScoreSel && policy.live_min_potential_score) {
    const sv = String(policy.live_min_potential_score);
    if (Array.from(liveScoreSel.options).some((o) => o.value === sv)) liveScoreSel.value = sv;
    liveScoreSel.disabled = liveHighChk ? !liveHighChk.checked : false;
  }
  const microChk = document.getElementById("chkLiveMicroCompound");
  if (microChk && policy.live_micro_compound_enabled !== undefined) {
    microChk.checked = Boolean(policy.live_micro_compound_enabled);
  }
  const allocSel = document.getElementById("liveCompoundPctSelect");
  if (allocSel && policy.live_compound_alloc_pct) {
    const av = String(policy.live_compound_alloc_pct);
    if (Array.from(allocSel.options).some((o) => o.value === av)) allocSel.value = av;
    allocSel.disabled = microChk ? !microChk.checked : false;
  }

  syncPromoteButtonLabel(demoModel);

  // Coluna Direita: Conta Real
  setText(
    "govLiveBotBadge",
    live.bot_online ? `🟢 ONLINE (PID ${live.bot_pid})` : "⚪ OFFLINE",
    `badge ${live.bot_online ? "model-ver" : "badge-subtle"}`
  );
  setText(
    "govLiveModelVer",
    liveModel
      ? `v${liveModel.version}${live.has_explicit_champion ? " 🏆" : " (Fallback)"}`
      : "Nenhum"
  );
  setText("govLiveModelAcc", liveModel ? `${Number(liveModel.precision_buy_pct || liveModel.accuracy_pct || 0).toFixed(1)}%` : "--%");
  const lStats = live.trade_stats || {};
  setText("govLiveWinRate", `${Number(lStats.win_rate || 0).toFixed(1)}% (${lStats.total_trades || 0} tr.)`);
  const lPnl = Number(lStats.total_pnl_usd || 0);
  setText("govLivePnl", `${lPnl >= 0 ? "+$" : "-$"}${Math.abs(lPnl).toFixed(2)}`, `gov-metric-val ${lPnl >= 0 ? "text-up" : "text-down"}`);

  const btnLiveBot = document.getElementById("btnGovToggleLiveBot");
  if (btnLiveBot) {
    btnLiveBot.textContent = live.bot_online ? "⏹ Parar Robô REAL" : "💎 Iniciar Robô REAL";
    btnLiveBot.className = live.bot_online ? "btn btn-danger" : "btn btn-acc-live";
  }

  // Checklist de Prontidão Live
  const checklistEl = document.getElementById("govLiveReadinessList");
  if (checklistEl && Array.isArray(readiness.checks)) {
    checklistEl.innerHTML = readiness.checks
      .map(
        (c) => `
          <div class="readiness-item ${c.ok ? "ok" : "warn"}">
            <span>${c.ok ? "✅" : "⚠️"}</span>
            <div class="readiness-item-text">
              <strong>${escapeHtml(c.label)}</strong>
              <span>${escapeHtml(c.detail)}</span>
            </div>
          </div>
        `
      )
      .join("");
  }

  // Histórico de promoções
  const promoBody = document.getElementById("govPromotionsHistoryBody");
  if (promoBody) {
    const promos = Array.isArray(data.promotions) ? data.promotions : [];
    if (!promos.length) {
      promoBody.innerHTML =
        '<tr><td colspan="6" class="empty-cell">Nenhuma promoção registrada ainda. Treine e valide a IA na Conta Demo e clique em "Promover para Conta Real".</td></tr>';
    } else {
      promoBody.innerHTML = promos
        .map(
          (p) => `
            <tr>
              <td class="dim">${escapeHtml(p.promoted_at || "")}</td>
              <td><strong>${escapeHtml(p.symbol)}</strong> <span class="dim">${escapeHtml(p.timeframe)}</span></td>
              <td><span class="xtb-tag">v${escapeHtml(p.model_version)}</span></td>
              <td class="mono-val text-up">${(Number(p.precision_buy || p.accuracy || 0) * 100).toFixed(1)}%</td>
              <td>${p.promoted_by === "auto" ? "⚡ Auto" : "👤 Manual"}</td>
              <td class="dim">${escapeHtml(p.notes || "Promovido para Conta Real")}</td>
            </tr>
          `
        )
        .join("");
    }
  }
}

export async function fetchAccountsOverview() {
  try {
    const sym = isActiveForex() ? "BTC/USDT" : state.activeSymbol || "BTC/USDT";
    const tf = state.activeTimeframe || "1h";
    const q = new URLSearchParams({ symbol: sym, timeframe: tf });
    const res = await fetch(`/api/accounts/overview?${q.toString()}`);
    if (!res.ok) return null;
    const data = await res.json();
    renderAccountsOverview(data);
    return data;
  } catch (_) {
    return null;
  }
}

export async function promoteModelToLive(explicitVersion = null) {
  const sym = isActiveForex() ? "BTC/USDT" : state.activeSymbol || "BTC/USDT";
  const tf = state.activeTimeframe || "1h";
  const verSel = document.getElementById("govPromoteVersionSelect");
  const verVal = explicitVersion ?? (verSel && verSel.value ? Number(verSel.value) : null);
  const verLabel = verVal ? `v${verVal}` : "mais recente";

  const ok = window.confirm(
    `Confirmar promoção do modelo ${verLabel} (${sym} ${tf}) da Conta Demo para Campeão oficial da CONTA REAL (Dinheiro Real)?`
  );
  if (!ok) return;

  const btn = document.getElementById("btnPromoteModelToLive");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/accounts/promote-model", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        symbol: sym,
        timeframe: tf,
        version: verVal,
        notes: `Promovido da Conta Demo para Conta Real via dashboard (${sym} ${tf})`,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || `Falha ao promover modelo (HTTP ${res.status}).`, "error");
      return;
    }
    showToast(`🏆 ${data.message}`, "success");
    await fetchAccountsOverview();
    if (typeof hooks.onRefreshStatus === "function") await hooks.onRefreshStatus();
  } catch (err) {
    showToast(`Erro ao promover modelo: ${err}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function saveAccountPoliciesFromUI() {
  try {
    const sym = isActiveForex() ? "BTC/USDT" : state.activeSymbol || "BTC/USDT";
    const tf = state.activeTimeframe || "1h";
    const autoPromote = Boolean(document.getElementById("chkAutoPromoteLive")?.checked ?? true);
    const demoAlwaysOn = Boolean(document.getElementById("chkDemoAlwaysOn")?.checked ?? true);
    const liveHighOnly = Boolean(document.getElementById("chkLiveHighPotentialOnly")?.checked ?? true);
    const liveMinConf = Number(document.getElementById("liveMinConfSelect")?.value || 72);
    const liveMinScore = Number(document.getElementById("liveMinScoreSelect")?.value || 78);
    const liveMicroCompound = Boolean(document.getElementById("chkLiveMicroCompound")?.checked ?? true);
    const liveCompoundPct = Number(document.getElementById("liveCompoundPctSelect")?.value || 40);

    const res = await fetch("/api/accounts/policy", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        auto_promote_to_live: autoPromote,
        demo_always_on_training: demoAlwaysOn,
        live_high_potential_only: liveHighOnly,
        live_min_confidence_pct: liveMinConf,
        live_min_potential_score: liveMinScore,
        live_micro_compound_enabled: liveMicroCompound,
        live_compound_alloc_pct: liveCompoundPct,
        symbol: sym,
        timeframe: tf,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || "Falha ao atualizar política das contas.", "error");
      return;
    }
    showToast(data.message || "Política de Demo e Conta Real atualizada.", "success");
    await fetchAccountsOverview();
    if (typeof hooks.onRefreshStatus === "function") await hooks.onRefreshStatus();
  } catch (err) {
    showToast(`Erro ao salvar política: ${err}`, "error");
  }
}

export async function updateAutoPromotePolicy(enabled) {
  const autoCheck = document.getElementById("chkAutoPromoteLive");
  if (autoCheck) autoCheck.checked = Boolean(enabled);
  await saveAccountPoliciesFromUI();
}

export async function controlAccountBot(targetAccount) {
  const acc = targetAccount === "live" ? "live" : "demo";
  const ov = state.accountsOverview || {};
  const isOnline = acc === "live" ? Boolean(ov.live && ov.live.bot_online) : Boolean(ov.demo && ov.demo.bot_online);

  if (!isOnline && acc === "live") {
    const ok = window.confirm(
      "⚠️ ATENÇÃO — DINHEIRO REAL!\n\nDeseja iniciar o robô na CONTA REAL (Binance Spot Live) usando o Modelo Campeão promovido?"
    );
    if (!ok) return;
  }

  try {
    let res;
    if (isOnline) {
      res = await fetch(`/api/bot/stop?account=${acc}`, { method: "POST", headers: csrfHeaders() });
    } else {
      const targetSymbol = isActiveForex() ? "BTC/USDT" : state.activeSymbol || "BTC/USDT";
      const q = new URLSearchParams({
        symbol: targetSymbol,
        timeframe: state.activeTimeframe || "1h",
        account: acc,
        trading_mode: acc === "live" ? "live" : "paper",
      });
      res = await fetch(`/api/bot/start?${q.toString()}`, { method: "POST", headers: csrfHeaders() });
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || `Falha ao controlar robô ${acc.toUpperCase()} (HTTP ${res.status}).`, "error");
    } else {
      showToast(data.message || `Comando enviado ao robô ${acc.toUpperCase()}.`, "success");
    }
    await fetchAccountsOverview();
    if (typeof hooks.onRefreshStatus === "function") await hooks.onRefreshStatus();
  } catch (err) {
    showToast(`Erro ao comunicar com servidor: ${err}`, "error");
  }
}

export async function approveLiveOrder(orderId) {
  const ok = window.confirm(
    `⚠️ CONFIRMAR EXECUÇÃO COM DINHEIRO REAL\n\nAprovar e enviar a ordem #${orderId} para a Binance Spot Live com proteção OCO?`
  );
  if (!ok) return;
  try {
    const res = await fetch(`/api/live/approvals/${orderId}/approve`, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ note: "Aprovada pelo administrador no painel" }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || `Não foi possível executar a ordem #${orderId}.`, "error");
    } else {
      showToast(`✅ ${data.message}`, "success");
    }
    await Promise.all([fetchLiveApprovals(), fetchAccountsOverview()]);
    if (typeof hooks.onRefreshStatus === "function") await hooks.onRefreshStatus();
  } catch (err) {
    showToast(`Erro ao aprovar ordem #${orderId}: ${err}`, "error");
  }
}

export async function rejectLiveOrder(orderId) {
  try {
    const res = await fetch(`/api/live/approvals/${orderId}/reject`, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ note: "Rejeitada pelo administrador no painel" }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || `Não foi possível rejeitar a ordem #${orderId}.`, "error");
    } else {
      showToast(`❌ ${data.message}`, "info");
    }
    await Promise.all([fetchLiveApprovals(), fetchAccountsOverview()]);
  } catch (err) {
    showToast(`Erro ao rejeitar ordem #${orderId}: ${err}`, "error");
  }
}

export async function pollTelegramApprovalsNow() {
  const btn = document.getElementById("btnPollTelegramApprovals");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/live/approvals/poll-telegram", {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || "Falha ao sincronizar respostas do Telegram.", "error");
      return;
    }
    const n = Number(data.processed_count || 0);
    showToast(
      n > 0
        ? `📲 ${n} decisão(ões) sincronizada(s) do Telegram!`
        : "📲 Telegram sincronizado — nenhuma nova resposta pendente.",
      n > 0 ? "success" : "info"
    );
    await Promise.all([fetchLiveApprovals(), fetchAccountsOverview()]);
  } catch (err) {
    showToast(`Erro ao consultar Telegram: ${err}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function fetchLiveApprovals() {
  try {
    const res = await fetch("/api/live/approvals?limit=25");
    if (!res.ok) return null;
    const data = await res.json();

    const cnt = Number(data.pending_count || 0);
    setText(
      "liveApprovalsCountBadge",
      cnt > 0 ? `🔐 ${cnt} pendente(s)` : "0 pendente(s)",
      cnt > 0 ? "xtb-tag" : "badge badge-subtle"
    );
    const tgCfg = data.telegram || {};
    const waCfg = data.whatsapp || {};
    setText("approvalTgStatusBadge", tgCfg.configured ? "📲 Telegram: ATIVO" : "📲 Telegram: OFF");
    setText("approvalWaStatusBadge", waCfg.active ? "💬 WhatsApp: ATIVO" : "💬 WhatsApp: OFF");

    const tbody = document.getElementById("liveApprovalsTableBody");
    if (!tbody) return data;
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      tbody.innerHTML =
        '<tr><td colspan="10" class="empty-cell">Nenhuma ordem da Conta Real aguardando aprovação. Quando a IA encontrar uma entrada em 1h+, ela pesquisará bolsas globais e notícias e enviará aqui e no seu Telegram/WhatsApp.</td></tr>';
      return data;
    }

    const statusBadge = (st) => {
      if (st === "PENDING") return '<span class="xtb-tag">⏳ AGUARDANDO</span>';
      if (st === "EXECUTED") return '<span class="eval-badge correct">✅ EXECUTADA</span>';
      if (st === "REJECTED") return '<span class="eval-badge wrong">❌ REJEITADA</span>';
      if (st === "EXPIRED") return '<span class="dim">⌛ EXPIRADA</span>';
      if (st === "FAILED") return '<span class="eval-badge wrong">⚠️ FALHOU</span>';
      return `<span class="dim">${escapeHtml(st)}</span>`;
    };

    tbody.innerHTML = items
      .map((o) => {
        const isPending = o.status === "PENDING";
        const phoneChannels = [
          o.notified_telegram ? "📲 TG" : "",
          o.notified_whatsapp ? "💬 WA" : "",
        ]
          .filter(Boolean)
          .join(" • ") || '<span class="dim">—</span>';
        const intel = o.pre_order_intel || null;
        let intelHtml = '<span class="dim">Sem dossiê</span>';
        if (intel) {
          const hl = intel.top_headline ? `<div class="dim" style="font-size:0.68rem;">📰 ${escapeHtml(String(intel.top_headline).slice(0, 72))}</div>` : "";
          intelHtml = `
            <div><strong>${escapeHtml(intel.verdict_badge || "Pesquisado")}</strong></div>
            <div class="dim" style="font-size:0.7rem;">🏛️ ${escapeHtml(intel.equities_line || "")}</div>
            ${hl}
          `;
        }
        const actionsHtml = isPending
          ? `<div class="approval-action-row">
              <button type="button" class="btn-approve-mini" data-approve-id="${escapeHtml(o.id)}">✅ Aprovar</button>
              <button type="button" class="btn-reject-mini" data-reject-id="${escapeHtml(o.id)}">❌ Rejeitar</button>
            </div>`
          : `<span class="dim">${escapeHtml(o.execution_error || o.decision_note || o.decided_by || "—")}</span>`;

        return `
          <tr>
            <td class="mono-val"><strong>#${escapeHtml(o.id)}</strong></td>
            <td class="dim">${escapeHtml(o.created_at || "")}</td>
            <td><strong>${escapeHtml(o.symbol)}</strong> <span class="badge badge-subtle">${escapeHtml(o.timeframe)}</span></td>
            <td>v${escapeHtml(o.model_version)} • <strong>${(Number(o.prob_up || 0) * 100).toFixed(1)}%</strong></td>
            <td class="mono-val">${escapeHtml(formatPrice(o.entry_price, o.symbol))}<br><span class="dim">$${Number(o.amount_usdt || 0).toFixed(2)}</span></td>
            <td class="mono-val"><span class="text-up">${escapeHtml(formatPrice(o.tp_price, o.symbol))}</span><br><span class="text-down">${escapeHtml(formatPrice(o.sl_price, o.symbol))}</span></td>
            <td>${intelHtml}</td>
            <td>${phoneChannels}</td>
            <td>${statusBadge(o.status)}</td>
            <td>${actionsHtml}</td>
          </tr>
        `;
      })
      .join("");

    tbody.querySelectorAll("[data-approve-id]").forEach((btn) => {
      btn.addEventListener("click", () => approveLiveOrder(Number(btn.dataset.approveId)));
    });
    tbody.querySelectorAll("[data-reject-id]").forEach((btn) => {
      btn.addEventListener("click", () => rejectLiveOrder(Number(btn.dataset.rejectId)));
    });
    return data;
  } catch (_) {
    return null;
  }
}
