function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
}

function numberText(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
}

function signedNumberText(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return `${number >= 0 ? '+' : ''}${number.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function dateText(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-TW', { hour12: false });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  return data;
}

function renderCards(items) {
  if (!items || !items.length) return '<p class="muted">此時間點無可靠排名。</p>';
  return items.map((item) => `<article class="card"><div class="card-head"><div><h3>${escapeHtml(item.contract)}</h3><span class="tag ${item.direction}">${escapeHtml(item.direction)}</span><span class="tag">${escapeHtml(item.market_state)}</span></div><div class="score">${numberText(item.ranking_score, 1)}</div></div><p>把握程度 ${numberText(item.confidence, 1)}／100，資料完整度 ${numberText(item.data_completeness_pct, 1)}%</p><p>${escapeHtml((item.reasons || []).join('、') || '沒有可用主要原因')}</p><small>風險：${escapeHtml((item.risk_flags || []).join('、') || 'none')}</small></article>`).join('');
}

async function loadRankings() {
  const holder = document.querySelector('#ranking-cards');
  if (!holder) return;
  try {
    const type = holder.dataset.rankingType;
    const data = await fetchJson(type ? `/api/rankings/${type}` : '/api/rankings');
    holder.innerHTML = renderCards(type ? data.items : data.combined);
  } catch (error) {
    holder.innerHTML = `<p class="muted">排名載入失敗：${escapeHtml(error.message)}</p>`;
  }
}

async function loadStatus() {
  try {
    const data = await fetchJson('/api/status');
    const latest = data.latest_scan || {};
    const scheduler = data.scheduler || {};
    const latestHolder = document.querySelector('#latest-scan');
    const qualifiedHolder = document.querySelector('#qualified-count');
    const schedulerHolder = document.querySelector('#scheduler-status');
    const discordHolder = document.querySelector('#discord-status');
    if (latestHolder) latestHolder.textContent = dateText(latest.finished_at) || '尚未掃描';
    if (qualifiedHolder) qualifiedHolder.textContent = String((latest.rankings?.combined || []).length);
    if (schedulerHolder) schedulerHolder.textContent = scheduler.running ? `運作中｜下次 ${dateText(scheduler.next_scan_at)}` : '未運作';
    if (discordHolder) discordHolder.textContent = data.discord_enabled ? '已啟用' : '未啟用';
    const holder = document.querySelector('#status-data');
    if (holder) holder.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    const holder = document.querySelector('#status-data');
    if (holder) holder.textContent = `狀態載入失敗：${error.message}`;
  }
}

function authHeaders() {
  const token = document.querySelector('#admin-token')?.value || localStorage.getItem('gate-admin-token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function renderAccount(account, summary) {
  const holder = document.querySelector('#account-cards');
  if (!holder) return;
  const cards = [
    ['總餘額', account.total_balance, 'USDT'],
    ['可用餘額', account.available_balance, 'USDT'],
    ['未實現損益', account.unrealised_pnl, 'USDT'],
    ['持倉初始保證金', account.position_initial_margin, 'USDT'],
    ['維持保證金', account.maintenance_margin, 'USDT'],
    ['掛單保證金', account.order_margin, 'USDT'],
    ['目前持倉', summary.position_count, '個'],
    ['保護單', summary.protection_order_count, '張'],
  ];
  holder.innerHTML = cards.map(([label, value, suffix]) => `<div class="account-card"><small>${label}</small><strong>${numberText(value)} ${suffix}</strong></div>`).join('');
}

function renderPositions(overview) {
  const holder = document.querySelector('#positions-table');
  if (!holder) return;
  if (!overview.positions?.length) {
    holder.innerHTML = '<p class="muted">Gate 目前沒有持倉。</p>';
    return;
  }
  const protectionByContract = {};
  const protectionDetailByContract = {};
  (overview.protection_orders || []).forEach((order) => {
    const contract = order.contract || order.initial?.contract || '';
    protectionByContract[contract] = (protectionByContract[contract] || 0) + 1;
    const type = String(order.order_type || '').includes('plan-close') ? 'TP' : String(order.order_type || '').includes('close-') ? 'SL' : '保護';
    const trigger = order.trigger?.price || '?';
    protectionDetailByContract[contract] = protectionDetailByContract[contract] || [];
    protectionDetailByContract[contract].push(`${type}@${trigger}`);
  });
  const rows = overview.positions.map((position) => {
    const pnl = Number(position.unrealised_pnl || 0);
    const pnlClass = pnl >= 0 ? 'profit' : 'loss';
    const marginMode = position.margin_mode === 'cross' ? '全倉' : position.margin_mode === 'isolated' ? '逐倉' : '未知';
    const protectionCount = protectionByContract[position.contract] || 0;
    const protectionDetail = (protectionDetailByContract[position.contract] || []).join('、');
    return `<tr><td>${escapeHtml(position.contract)}</td><td class="${position.side === 'LONG' ? 'profit' : 'loss'}">${position.side}</td><td>${escapeHtml(marginMode)}</td><td>${escapeHtml(position.position_mode || '未知')}</td><td>${numberText(position.size, 4)}</td><td>${numberText(position.entry_price, 6)}</td><td>${numberText(position.mark_price, 6)}</td><td>${numberText(position.leverage, 1)}x</td><td class="${pnlClass}">${signedNumberText(position.unrealised_pnl)} USDT</td><td>${signedNumberText(position.pnl_percent)}%</td><td>${numberText(position.liquidation_price, 6)}</td><td>${numberText(position.margin)} USDT</td><td title="${escapeHtml(protectionDetail)}">${protectionCount} 張</td></tr>`;
  }).join('');
  holder.innerHTML = `<table class="data-table"><thead><tr><th>合約</th><th>方向</th><th>保證金模式</th><th>持倉模式</th><th>數量</th><th>進場價</th><th>標記價</th><th>槓桿</th><th>未實現損益</th><th>損益%</th><th>清算價</th><th>保證金</th><th>保護單</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function loadOverview() {
  const message = document.querySelector('#overview-message');
  const positionsSummary = document.querySelector('#positions-summary');
  if (!document.querySelector('#account-cards') && !positionsSummary) return;
  const headers = authHeaders();
  if (!headers.Authorization) {
    if (message) message.textContent = '請輸入管理 Token 查看 Gate 帳戶與持倉。';
    if (positionsSummary) positionsSummary.textContent = '請輸入管理 Token 查看持倉。';
    return;
  }
  try {
    const data = await fetchJson('/api/trading/overview', { headers });
    if (data.account) renderAccount(data.account, data.summary || {});
    renderPositions(data);
    if (positionsSummary) positionsSummary.textContent = `Gate 持倉 ${data.summary?.position_count || 0} 個（全倉 ${data.summary?.cross_position_count || 0}／逐倉 ${data.summary?.isolated_position_count || 0}）；未實現損益 ${signedNumberText(data.summary?.unrealised_pnl)} USDT；保護單 ${data.summary?.protection_order_count || 0} 張`;
    if (message) {
      if (data.gate_status === 'not_configured') {
        message.textContent = '後端尚未設定 Gate API 金鑰。';
      } else if (data.gate_status === 'partial_error') {
        message.textContent = 'Gate 已連線，但部分帳戶資料暫時無法取得，系統會自動重試。';
      } else {
        message.textContent = `Gate 已連線｜更新於 ${dateText(new Date().toISOString())}`;
      }
    }
  } catch (error) {
    if (message) message.textContent = `帳戶資料載入失敗：${error.message}`;
    if (positionsSummary) positionsSummary.textContent = `持倉載入失敗：${error.message}`;
  }
}

async function loadTrading() {
  const status = document.querySelector('#trading-status');
  if (!status) return;
  try {
    const data = await fetchJson('/api/trading/status');
    status.textContent = data.paused ? '已暫停新下單' : data.auto_order_enabled ? '自動下單運作中' : '自動下單未啟用';
    status.className = `status-pill ${data.paused ? 'paused' : data.auto_order_enabled ? 'active' : 'disabled'}`;
  } catch (error) {
    status.textContent = '交易狀態不可用';
    status.className = 'status-pill paused';
  }
  await loadOverview();
}

async function tradingAction(path) {
  const message = document.querySelector('#trading-message');
  const token = document.querySelector('#admin-token')?.value || localStorage.getItem('gate-admin-token') || '';
  if (token) localStorage.setItem('gate-admin-token', token);
  try {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: path.endsWith('/pause') ? JSON.stringify({ reason: 'dashboard manual pause' }) : '{}' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.message || 'request failed');
    if (message) message.textContent = path.endsWith('/pause') ? '已暫停新下單；持倉管理仍持續。' : '已恢復新下單。';
    await loadTrading();
  } catch (error) {
    if (message) message.textContent = `操作失敗：${error.message}`;
  }
}

document.querySelector('#pause-trading')?.addEventListener('click', () => tradingAction('/api/trading/pause'));
document.querySelector('#resume-trading')?.addEventListener('click', () => tradingAction('/api/trading/resume'));
document.querySelector('#refresh-overview')?.addEventListener('click', loadOverview);
const tokenInput = document.querySelector('#admin-token');
if (tokenInput) tokenInput.value = localStorage.getItem('gate-admin-token') || '';
loadRankings();
loadStatus();
loadTrading();
setInterval(loadStatus, 10000);
setInterval(loadTrading, 10000);
setInterval(loadRankings, 30000);
