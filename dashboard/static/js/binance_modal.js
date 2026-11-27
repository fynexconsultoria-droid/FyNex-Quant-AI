/**
 * Controlador do Modal de Conexão e Credenciais Binance (FyNex Quant AI)
 */

import { hooks } from "./state.js";
import { csrfHeaders, escapeHtml, showToast } from "./utils.js";

let currentSelectedEnvIsTestnet = false;
let savedApiKeyMasked = "";

export function updateBinanceStatusUI(binance) {
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

export async function openBinanceModal() {
  const modal = document.getElementById("binanceModalOverlay");
  if (modal) {
    modal.classList.add("active");
    await loadBinanceModalData();
  }
}

export function closeBinanceModal() {
  const modal = document.getElementById("binanceModalOverlay");
  if (modal) modal.classList.remove("active");
}

export function closeBinanceModalOnOutsideClick(e) {
  if (e.target && e.target.id === "binanceModalOverlay") {
    closeBinanceModal();
  }
}

export function selectEnvironment(isTestnet) {
  currentSelectedEnvIsTestnet = isTestnet;
  const btnTestnet = document.getElementById("segTestnet");
  const btnLive = document.getElementById("segLive");
  if (btnTestnet && btnLive) {
    btnTestnet.classList.toggle("active", Boolean(isTestnet));
    btnLive.classList.toggle("active", !isTestnet);
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

export function bindTradingModeListener() {
  document.addEventListener("change", (e) => {
    if (e.target && e.target.name === "tradingMode") {
      if (e.target.value === "testnet") selectEnvironment(true);
      if (e.target.value === "live") selectEnvironment(false);
    }
  });
}

export async function pasteApiKey() {
  try {
    const text = await navigator.clipboard.readText();
    const inp = document.getElementById("inputApiKey");
    if (text && inp) {
      inp.value = text.trim();
      showToast("📋 API Key colada da área de transferência.", "info");
    }
  } catch (_) {
    showToast("Não foi possível ler a área de transferência. Cole manualmente com Ctrl+V.", "error");
  }
}

export function toggleSecretVisibility() {
  const secretInput = document.getElementById("inputApiSecret");
  const btn = document.getElementById("btnToggleSecret");
  if (!secretInput || !btn) return;
  if (secretInput.type === "password") {
    secretInput.type = "text";
    btn.textContent = "🔒";
  } else {
    secretInput.type = "password";
    btn.textContent = "👁️";
  }
}

export function setQuickAmount(val) {
  const amountInput = document.getElementById("inputTradeAmount");
  if (amountInput) amountInput.value = Number(val).toFixed(2);
}

export async function loadBinanceModalData() {
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
      await testBinanceConnectionModal(true);
    } else {
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
  } catch (err) {
    console.error("Erro ao carregar configurações da Binance:", err);
  }
}

export async function testBinanceConnectionModal(silent = false) {
  const resultBox = document.getElementById("testResultBox");
  const spinner = document.getElementById("modalTestSpinner");
  const btnText = document.getElementById("btnModalTestText");

  if (!silent) {
    if (spinner) spinner.style.display = "inline";
    if (btnText) btnText.textContent = "Testando...";
  }

  const apiKeyInput = (document.getElementById("inputApiKey")?.value || "").trim();
  const apiSecretInput = (document.getElementById("inputApiSecret")?.value || "").trim();

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
        <div style="font-size: 0.725rem; color: var(--text-dim); display: flex; gap: 0.75rem; flex-wrap: wrap;">
          <span>Trading Spot: ${data.can_trade ? "✅ Ativo" : "❌ Inativo"}</span>
          <span>Saques: ${data.can_withdraw ? "⚠️ Permitido" : "🔒 Bloqueado (Segurança Máxima)"}</span>
          ${
            data.clock_drift_ms !== null && data.clock_drift_ms !== undefined
              ? `<span>Clock Drift: ${escapeHtml(String(data.clock_drift_ms))} ms</span>`
              : ""
          }
          ${
            data.active_mirror
              ? `<span>Endpoint: ${escapeHtml(String(data.active_mirror).replace(/^https?:\/\//, ""))}</span>`
              : ""
          }
        </div>
      `;

      const kpiBalance = document.getElementById("kpiBinanceBalance");
      const kpiBtc = document.getElementById("kpiBinanceBtc");
      if (kpiBalance) {
        kpiBalance.innerHTML = `$${escapeHtml(
          Number(data.usdt_free || 0).toFixed(2)
        )} <span class="kpi-currency">USDT</span>`;
      }
      if (kpiBtc) kpiBtc.textContent = `${Number(data.btc_total || 0).toFixed(6)} BTC`;
    } else {
      resultBox.className = "test-result-box error";
      resultBox.innerHTML = `
        <div class="test-result-title"><span>❌</span> Falha de Conexão com a Binance</div>
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
      if (spinner) spinner.style.display = "none";
      if (btnText) btnText.textContent = "🔍 Testar Conexão";
    }
  }
}

export async function saveBinanceConnectionModal() {
  const apiKeyInput = (document.getElementById("inputApiKey")?.value || "").trim();
  const apiSecretInput = (document.getElementById("inputApiSecret")?.value || "").trim();
  const amountVal = parseFloat(document.getElementById("inputTradeAmount")?.value || "50") || 50.0;

  const modeRadio = document.querySelector('input[name="tradingMode"]:checked');
  const tradingMode = modeRadio ? modeRadio.value : "paper";

  const finalKey = apiKeyInput === savedApiKeyMasked ? "" : apiKeyInput;
  const finalSecret =
    apiSecretInput.startsWith("•") || apiSecretInput.startsWith("*") ? "" : apiSecretInput;

  const saveBtn = document.getElementById("btnModalSave");
  const saveBtnText = document.getElementById("btnModalSaveText");
  if (saveBtn) saveBtn.disabled = true;
  if (saveBtnText) saveBtnText.textContent = "Salvando...";

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

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(`Falha ao salvar: ${data.detail || "Erro no servidor"}`, "error");
      return;
    }

    if (data.test_result && data.test_result.success) {
      showToast(
        `✅ ${data.message} • ${data.test_result.environment} • Saldo USDT: $${(
          data.test_result.usdt_free || 0
        ).toFixed(2)}`,
        "success"
      );
    } else {
      showToast(
        `⚠️ ${data.message} • ${(data.test_result && data.test_result.message) || "Verifique suas chaves."}`,
        "warning"
      );
    }

    closeBinanceModal();
    if (typeof hooks.onRefreshAll === "function") await hooks.onRefreshAll();
  } catch (err) {
    showToast(`Erro ao salvar dados da Binance: ${err}`, "error");
  } finally {
    if (saveBtn) saveBtn.disabled = false;
    if (saveBtnText) saveBtnText.textContent = "💾 Salvar e Aplicar";
  }
}
