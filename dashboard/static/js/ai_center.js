/**
 * Centro de Treinamento da IA, Macro Inteligência, Evolução e Auto-Análise de Erros
 */

import { fetchAccountsOverview } from "./accounts.js";
import { hooks, state } from "./state.js";
import { csrfHeaders, escapeHtml, isActiveForex, showToast } from "./utils.js";

let aiTrainPollTimer = null;
let autoTrainTargetEpochMs = 0;
let autoTrainEnabledState = true;
let autoTrainRunningState = false;
let autoTrainCountdownTimer = null;

function formatCountdownSeconds(sec) {
  const s = Math.max(0, Math.floor(sec));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (hrs > 0) {
    return `${hrs}h ${String(mins).padStart(2, "0")}m ${String(rem).padStart(2, "0")}s`;
  }
  return `${String(mins).padStart(2, "0")}m ${String(rem).padStart(2, "0")}s`;
}

function tickAutoTrainCountdownUI() {
  const el = document.getElementById("aiAutoTrainCountdown");
  if (!el) return;
  if (autoTrainRunningState) {
    el.textContent = "⏳ Treinando agora em segundo plano...";
    return;
  }
  if (!autoTrainEnabledState || !autoTrainTargetEpochMs) {
    el.textContent = "Agendamento pausado";
    return;
  }
  const remSec = Math.max(0, Math.round((autoTrainTargetEpochMs - Date.now()) / 1000));
  if (remSec <= 0) {
    el.textContent = "⏰ Iniciando novo ciclo automático...";
  } else {
    el.textContent = `⏰ Próximo auto-treino em: ${formatCountdownSeconds(remSec)}`;
  }
}

export function renderAiTrainScheduleUI(sched, isRunning = false) {
  if (!sched) return;
  autoTrainEnabledState = Boolean(sched.enabled);
  autoTrainRunningState = Boolean(isRunning);

  const badge = document.getElementById("aiAutoTrainStatusBadge");
  const meta = document.getElementById("aiAutoTrainMeta");
  const chk = document.getElementById("chkAiAutoTrainEnabled");
  const intSel = document.getElementById("aiAutoTrainIntervalSelect");
  const tfSel = document.getElementById("aiTrainTimeframe");
  const cdlSel = document.getElementById("aiTrainCandles");

  const hrs = Math.max(1, Math.round(Number(sched.interval_minutes || 60) / 60));
  if (badge) {
    if (isRunning) {
      badge.textContent = "🔄 Treinando IA agora";
      badge.className = "xtb-tag";
    } else if (sched.enabled) {
      badge.textContent = `🟢 Auto-Treino ${hrs}h ATIVO`;
      badge.className = "xtb-tag";
    } else {
      badge.textContent = "⏸️ Auto-Treino PAUSADO";
      badge.className = "badge badge-subtle";
    }
  }
  if (chk) chk.checked = Boolean(sched.enabled);
  if (intSel && sched.interval_minutes) {
    const valStr = String(sched.interval_minutes);
    if (Array.from(intSel.options).some((o) => o.value === valStr)) {
      intSel.value = valStr;
    }
  }
  if (tfSel && sched.timeframe && !tfSel.dataset.userEdited) {
    if (Array.from(tfSel.options).some((o) => o.value === sched.timeframe)) {
      tfSel.value = sched.timeframe;
    }
  }
  if (cdlSel && sched.candles && !cdlSel.dataset.userEdited) {
    const cStr = String(sched.candles);
    if (Array.from(cdlSel.options).some((o) => o.value === cStr)) {
      cdlSel.value = cStr;
    }
  }
  if (meta) {
    const lastStr = sched.last_run_at ? `Último: ${sched.last_run_at} UTC` : "Aguardando 1º ciclo";
    const runs = Number(sched.total_runs || 0);
    meta.textContent = `${sched.symbol || "BTC/USDT"} (${sched.timeframe || "1h"} • ${Number(sched.candles || 3000).toLocaleString("pt-BR")} candles) • ${lastStr} • ${runs} ciclo(s) auto`;
  }

  if (sched.enabled && typeof sched.seconds_until_next === "number") {
    autoTrainTargetEpochMs = Date.now() + Math.max(0, sched.seconds_until_next) * 1000;
  } else {
    autoTrainTargetEpochMs = 0;
  }
  tickAutoTrainCountdownUI();
  if (!autoTrainCountdownTimer) {
    autoTrainCountdownTimer = setInterval(tickAutoTrainCountdownUI, 1000);
  }
}

export async function fetchAiTrainSchedule() {
  try {
    const res = await fetch("/api/ai/train/schedule");
    if (!res.ok) return null;
    const sched = await res.json();
    renderAiTrainScheduleUI(sched, autoTrainRunningState);
    return sched;
  } catch (_) {
    return null;
  }
}

export async function saveAiTrainScheduleUI() {
  const enabled = Boolean(document.getElementById("chkAiAutoTrainEnabled")?.checked ?? true);
  const intervalMinutes = Number(document.getElementById("aiAutoTrainIntervalSelect")?.value || 60);
  const timeframe = document.getElementById("aiTrainTimeframe")?.value || "1h";
  const candles = Number(document.getElementById("aiTrainCandles")?.value || 3000);
  const symbol = state.activeSymbol || "BTC/USDT";

  try {
    const res = await fetch("/api/ai/train/schedule", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        enabled,
        interval_minutes: intervalMinutes,
        symbol,
        timeframe,
        candles,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || "Falha ao salvar agendamento de treino automático.", "error");
      return;
    }
    if (data.schedule) renderAiTrainScheduleUI(data.schedule, autoTrainRunningState);
    showToast(`⚡ ${data.message || "Agendamento de auto-treino atualizado!"}`, enabled ? "success" : "info");
  } catch (err) {
    showToast(`Erro ao salvar agendamento de auto-treino: ${err}`, "error");
  }
}

function setText(id, text, cls = null) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  if (cls !== null) el.className = cls;
}

export async function startAiTraining() {
  const targetSymbol = state.activeSymbol || "BTC/USDT";
  const btn = document.getElementById("btnStartAiTrain");
  const stageEl = document.getElementById("aiTrainStage");
  const progEl = document.getElementById("aiTrainProgress");
  const trainTf = document.getElementById("aiTrainTimeframe")?.value || "1h";
  const candles = document.getElementById("aiTrainCandles")?.value || "3000";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Iniciando...";
  }
  if (stageEl) stageEl.textContent = `Iniciando download e treino de ${targetSymbol} (${trainTf}, ${candles} candles)...`;
  if (progEl) progEl.style.width = "5%";

  try {
    const q = new URLSearchParams({
      symbol: targetSymbol,
      timeframe: trainTf,
      candles: String(candles),
    });
    const res = await fetch(`/api/ai/train?${q.toString()}`, {
      method: "POST",
      headers: csrfHeaders(),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const errMsg = data.detail || `Falha ao iniciar treinamento (HTTP ${res.status}).`;
      if (stageEl) stageEl.textContent = `❌ ${errMsg}`;
      showToast(errMsg, "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🧠 Treinar IA Agora";
      }
      return;
    }
    showToast(`🧠 ${data.message}`, "success");
    pollAiTrainStatus();
    if (aiTrainPollTimer) clearInterval(aiTrainPollTimer);
    aiTrainPollTimer = setInterval(pollAiTrainStatus, 2000);
  } catch (err) {
    if (stageEl) stageEl.textContent = `❌ Erro ao iniciar treino: ${err}`;
    showToast(`Erro ao iniciar treino: ${err}`, "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🧠 Treinar IA Agora";
    }
  }
}

export async function pollAiTrainStatus() {
  try {
    const res = await fetch("/api/ai/train/status");
    if (!res.ok) return;
    const st = await res.json();

    const stageEl = document.getElementById("aiTrainStage");
    const progEl = document.getElementById("aiTrainProgress");
    const btn = document.getElementById("btnStartAiTrain");
    const tbody = document.getElementById("aiTrainResultsBody");

    if (stageEl) stageEl.textContent = st.error ? `Erro: ${st.error}` : st.stage || "Pronto";
    if (progEl) progEl.style.width = `${Math.max(0, Math.min(100, Number(st.progress_pct || 0)))}%`;
    if (btn) {
      btn.disabled = Boolean(st.running);
      btn.textContent = st.running ? "⏳ Treinando..." : "🧠 Treinar IA Agora";
    }

    if (st.schedule) {
      renderAiTrainScheduleUI(st.schedule, Boolean(st.running));
    }

    if (st.running && !aiTrainPollTimer) {
      aiTrainPollTimer = setInterval(pollAiTrainStatus, 2000);
    }

    if (!st.running && aiTrainPollTimer) {
      clearInterval(aiTrainPollTimer);
      aiTrainPollTimer = null;
      if (st.error) {
        showToast(`❌ ${st.error}`, "error");
      } else if (st.last_result) {
        showToast(`✅ Treinamento concluído! ${st.stage}`, "success");
        if (typeof hooks.onRefreshStatus === "function") hooks.onRefreshStatus();
        fetchAccountsOverview();
      }
    }

    if (tbody && st.last_result && Array.isArray(st.last_result.candidates)) {
      const winnerType = st.last_result.winner?.model_type || "";
      tbody.innerHTML = st.last_result.candidates
        .map((c) => {
          const isWin = c.model_type === winnerType;
          const pnl = Number(c.sim_pnl_pct || 0);
          return `
            <tr>
              <td><strong>${escapeHtml(String(c.model_type).toUpperCase())}</strong></td>
              <td>${isWin ? '<span class="xtb-tag">🏆 CAMPEÃO</span>' : '<span class="dim">Candidato</span>'}</td>
              <td>${(Number(c.precision_buy || 0) * 100).toFixed(1)}%</td>
              <td>${Number(c.profit_factor || 0).toFixed(2)}</td>
              <td>${Number(c.roc_auc || 0.5).toFixed(3)}</td>
              <td class="${pnl >= 0 ? "text-up" : "text-down"}"><strong>${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%</strong></td>
              <td>${(Number(c.optimal_threshold || 0.55) * 100).toFixed(0)}%</td>
              <td>${escapeHtml(c.buy_signals)}</td>
            </tr>
          `;
        })
        .join("");
    }
  } catch (_) {
    // silencioso
  }
}

export async function fetchMacroIntelligence(forceRefresh = false) {
  try {
    const res = await fetch(`/api/macro/live?refresh=${forceRefresh ? "true" : "false"}`);
    if (!res.ok) return;
    const m = await res.json();

    setText("macroRegimeBadge", `${m.badge || "⚖️"} ${m.regime || "MACRO NEUTRO"}`);
    setText("macroRegimeDesc", m.description || "Contexto macro atualizado.");

    setText("macroDxyVal", m.dxy_price > 0 ? Number(m.dxy_price).toFixed(2) : "--");
    const dxyChg = Number(m.dxy_change_pct || 0);
    setText("macroDxyChg", `${dxyChg >= 0 ? "+" : ""}${dxyChg.toFixed(2)}% (hoje)`, `market-metric-sub ${dxyChg <= 0 ? "text-up" : "text-down"}`);

    setText("macroUs10yVal", m.us10y_yield > 0 ? `${Number(m.us10y_yield).toFixed(2)}%` : "--");
    const yChg = Number(m.us10y_change_pct || 0);
    setText("macroUs10yChg", `${yChg >= 0 ? "+" : ""}${yChg.toFixed(2)}%`, `market-metric-sub ${yChg <= 0 ? "text-up" : "text-down"}`);

    setText("macroSp500Val", m.sp500_price > 0 ? Number(m.sp500_price).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--");
    const spChg = Number(m.sp500_change_pct || 0);
    setText("macroSp500Chg", `${spChg >= 0 ? "+" : ""}${spChg.toFixed(2)}%`, `market-metric-sub ${spChg >= 0 ? "text-up" : "text-down"}`);

    setText("macroFngVal", m.fear_greed_value !== null && m.fear_greed_value !== undefined ? `${m.fear_greed_value}/100` : "--");
    setText("macroFngLabel", m.fear_greed_label || "N/D");

    const ns = Number(m.news_sentiment_score || 0);
    setText("macroNewsScore", `${ns >= 0 ? "+" : ""}${(ns * 100).toFixed(0)}%`);
    const fb = Number(m.inflation_fed_bias || 0);
    setText("macroFedBias", `Fed/Inflação: ${fb >= 0 ? "+" : ""}${(fb * 100).toFixed(0)}% (${m.inflation_fed_mentions || 0}x)`);

    const sc = Number(m.macro_score || 0);
    setText("macroTotalScore", `${sc >= 0 ? "+" : ""}${sc.toFixed(1)}`, `market-metric-value ${sc >= 0 ? "text-up" : "text-down"}`);
    const adj = Math.round(Number(m.confidence_adjustment || 0) * 100);
    setText("macroAdjFilter", `Ajuste filtro: ${adj >= 0 ? "+" : ""}${adj}%`);

    // Bolsas de Valores Globais & VIX
    setText("macroEquitiesBadge", `🏛️ ${m.equities_regime || "BOLSAS ESTÁVEIS"}`);

    const ndxChg = Number(m.nasdaq_change_pct || 0);
    setText("macroNasdaqVal", m.nasdaq_price > 0 ? Number(m.nasdaq_price).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--");
    setText("macroNasdaqChg", `${ndxChg >= 0 ? "+" : ""}${ndxChg.toFixed(2)}%`, `market-metric-sub ${ndxChg >= 0 ? "text-up" : "text-down"}`);

    const djiChg = Number(m.dowjones_change_pct || 0);
    setText("macroDowVal", m.dowjones_price > 0 ? Number(m.dowjones_price).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--");
    setText("macroDowChg", `${djiChg >= 0 ? "+" : ""}${djiChg.toFixed(2)}%`, `market-metric-sub ${djiChg >= 0 ? "text-up" : "text-down"}`);

    const vixVal = Number(m.vix_price || 0);
    const vixChg = Number(m.vix_change_pct || 0);
    setText("macroVixVal", vixVal > 0 ? vixVal.toFixed(2) : "--", `market-metric-value ${vixVal >= 25 ? "text-down" : "text-up"}`);
    setText("macroVixChg", `${vixChg >= 0 ? "+" : ""}${vixChg.toFixed(2)}%`, `market-metric-sub ${vixChg <= 0 ? "text-up" : "text-down"}`);

    const ibovChg = Number(m.ibovespa_change_pct || 0);
    setText("macroIbovVal", m.ibovespa_price > 0 ? Number(m.ibovespa_price).toLocaleString("pt-BR", { maximumFractionDigits: 0 }) : "--");
    setText("macroIbovChg", `${ibovChg >= 0 ? "+" : ""}${ibovChg.toFixed(2)}%`, `market-metric-sub ${ibovChg >= 0 ? "text-up" : "text-down"}`);

    const daxChg = Number(m.dax_change_pct || 0);
    setText("macroDaxVal", m.dax_price > 0 ? Number(m.dax_price).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "--");
    setText("macroDaxChg", `${daxChg >= 0 ? "+" : ""}${daxChg.toFixed(2)}%`, `market-metric-sub ${daxChg >= 0 ? "text-up" : "text-down"}`);

    const eqAvg = Number(m.equities_avg_change_pct || 0);
    const ftseChg = Number(m.ftse_change_pct || 0);
    const nikChg = Number(m.nikkei_change_pct || 0);
    setText("macroGlobalEqVal", `${eqAvg >= 0 ? "+" : ""}${eqAvg.toFixed(2)}% méd.`, `market-metric-value ${eqAvg >= 0 ? "text-up" : "text-down"}`);
    setText("macroGlobalEqSub", `FTSE ${ftseChg >= 0 ? "+" : ""}${ftseChg.toFixed(1)}% • Nikkei ${nikChg >= 0 ? "+" : ""}${nikChg.toFixed(1)}%`);
  } catch (_) {
    // silencioso
  }
}

export async function fetchAiEvolution() {
  try {
    const q = new URLSearchParams({
      symbol: isActiveForex() ? "BTC/USDT" : state.activeSymbol,
      timeframe: state.activeTimeframe || "1h",
    });
    const res = await fetch(`/api/ai/evolution?${q.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    const evo = data.evolution || {};
    const m = data.macro || {};
    const p = evo.pillars || {};

    const pct = Math.max(0, Math.min(100, Number(evo.progress_pct || 0)));

    setText("sideEvoPct", `${pct.toFixed(0)}%`);
    setText("sideEvoLevel", `Nível ${evo.level || 1}`);
    setText("sideEvoStatus", evo.adaptation_tone === "warning" ? "Adaptando..." : "🔄 Auto");
    const sideBar = document.getElementById("sideEvoBar");
    if (sideBar) sideBar.style.width = `${pct}%`;

    const mainBar = document.getElementById("evoMainBar");
    if (mainBar) mainBar.style.width = `${pct}%`;
    setText("evoLevelBadge", `Nível ${evo.level || 1} • ${evo.stage_name || ""} (${pct.toFixed(0)}%)`);
    setText("evoAdaptationBadge", evo.adaptation_status || "🟢 Sincronizada");
    setText("evoNextMilestone", `Próximo marco: ${evo.next_milestone || "Evolução contínua"}`);

    setText("evoCandlesVal", `${Number(p.candles_count || 0).toLocaleString("pt-BR")} / ${Number(p.candles_target || 3000).toLocaleString("pt-BR")}`);
    setText("evoMacroVal", `${p.macro_snapshots || 0} / ${p.macro_target || 100}`);
    setText("evoCyclesVal", `${p.learning_cycles || 0} ciclo(s) (v${p.model_version || 1})`);
    setText("evoAccVal", p.eval_total > 0 ? `${Number(p.eval_accuracy_pct || 0).toFixed(1)}% (${p.eval_total})` : "Coletando...");

    const oilChg = Number(m.oil_change_pct || 0);
    setText("globOilVal", m.oil_price > 0 ? `$${Number(m.oil_price).toFixed(2)}` : "--");
    setText("globOilChg", `${oilChg >= 0 ? "+" : ""}${oilChg.toFixed(2)}%`, `market-metric-sub ${oilChg <= 2.0 ? "text-up" : "text-down"}`);

    const goldChg = Number(m.gold_change_pct || 0);
    const silChg = Number(m.silver_change_pct || 0);
    setText("globGoldVal", m.gold_price > 0 ? `$${Number(m.gold_price).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "--");
    setText("globSilverChg", `Ouro ${goldChg >= 0 ? "+" : ""}${goldChg.toFixed(1)}% • Prata ${silChg >= 0 ? "+" : ""}${silChg.toFixed(1)}%`);

    const reChg = Number(m.rare_earth_change_pct || 0);
    setText("globRareVal", m.rare_earth_price > 0 ? `$${Number(m.rare_earth_price).toFixed(2)}` : "--");
    setText("globRareChg", `${reChg >= 0 ? "+" : ""}${reChg.toFixed(2)}%`, `market-metric-sub ${reChg >= 0 ? "text-up" : "text-down"}`);

    const copChg = Number(m.copper_change_pct || 0);
    setText("globCopperVal", m.copper_price > 0 ? `$${Number(m.copper_price).toFixed(2)}` : "--");
    setText("globCopperChg", `${copChg >= 0 ? "+" : ""}${copChg.toFixed(2)}%`, `market-metric-sub ${copChg >= 0 ? "text-up" : "text-down"}`);

    const warM = Number(m.war_geopolitics_mentions || 0);
    setText("globWarVal", `${warM} notícia(s)`, `market-metric-value ${warM >= 3 ? "text-down" : "text-up"}`);
    setText("globWarNote", warM >= 3 ? "Atenção geopolítica elevada" : "Estável nas manchetes");

    const cliM = Number(m.climate_energy_mentions || 0);
    const minM = Number(m.rare_earth_minerals_mentions || 0);
    setText("globClimateVal", `${cliM} clima/energia`);
    setText("globMineralsNote", `Minérios/Terras Raras: ${minM}`);
  } catch (_) {
    // silencioso
  }
}

export async function fetchAiErrorLessons() {
  try {
    const sym = state.activeSymbol || "BTC/USDT";
    const tf = isActiveForex() ? "1h" : state.activeTimeframe;
    const q = new URLSearchParams({ symbol: sym, timeframe: tf, limit: "8" });
    const res = await fetch(`/api/ai/error-lessons?${q.toString()}`);
    if (!res.ok) return;
    const rep = await res.json();

    const countEl = document.getElementById("errLearnCountBadge");
    const rulesEl = document.getElementById("errLearnRulesBadge");
    const subEl = document.getElementById("errLearnSummarySub");
    const chipsEl = document.getElementById("errLearnRulesList");
    const tbody = document.getElementById("errLearnTableBody");

    const total14 = Number(rep.total_errors_14d || 0);
    const rules = rep.active_rules || [];
    if (countEl) countEl.textContent = `${total14} erro(s) (14d)`;
    if (rulesEl) rulesEl.textContent = `${rules.length} trava(s) ativa(s)`;
    if (subEl) {
      const slBoost = Number(rep.sl_atr_boost || 1.0);
      const pen = Number(rep.base_confidence_penalty_pct || 0);
      subEl.textContent =
        rules.length > 0
          ? `Adaptação ativa: ${rules.length} padrão(ões) de erro protegido(s) • Ajuste SL/ATR: ${slBoost.toFixed(2)}x • Penalidade extra: +${pen.toFixed(1)}%`
          : "Monitorando cada previsão e trade: se houver erro, todos os parâmetros são analisados automaticamente.";
    }
    if (chipsEl) {
      chipsEl.innerHTML = rules
        .map(
          (r) =>
            `<span class="sent-chip negativo" title="${escapeHtml(r.advice || "")}">🛡️ ${escapeHtml(r.label)} (${escapeHtml(r.count)}x • ${escapeHtml(r.action)})</span>`
        )
        .join("");
    }
    if (tbody) {
      const items = rep.recent_errors || [];
      if (!items.length) {
        tbody.innerHTML =
          '<tr><td colspan="5" class="empty-cell">Nenhum erro analisado ainda neste par/timeframe. A auto-análise roda automaticamente a cada falha.</td></tr>';
      } else {
        tbody.innerHTML = items
          .map((e) => {
            const isTradeLoss = e.source === "TRADE_LOSS" || e.source === "XTB_TRADE_LOSS";
            const srcLabel = e.source === "XTB_TRADE_LOSS" ? "XTB (Prejuízo)" : isTradeLoss ? "Trade (Prejuízo)" : "Previsão (Falso +)";
            const metric = isTradeLoss
              ? `$${Number(e.pnl_usd || 0).toFixed(2)} (${Number(e.pnl_pct || 0).toFixed(2)}%)`
              : `Prob. ${(Number(e.prob_up || 0) * 100).toFixed(1)}%`;
            return `
              <tr>
                <td class="dim">${escapeHtml(e.created_at || "")}</td>
                <td><span class="side-pill sell">${escapeHtml(srcLabel)}</span></td>
                <td><strong>${escapeHtml(e.primary_label || e.primary_cause)}</strong></td>
                <td class="mono-val text-down">${escapeHtml(metric)}</td>
                <td class="dim">${escapeHtml(e.lesson_text || "")}</td>
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
