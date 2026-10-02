'use strict';
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat('vi-VN').format(value || 0);
let needsSetup = false;
let currentStatus;
let popup;
let dirty = false;

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
  $('notice').hidden = !message;
}
async function api(path, body) {
  const response = await fetch('/admin/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Graphiti-Admin': '1' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = typeof result.detail === 'string' ? result.detail : 'Thông tin nhập chưa hợp lệ.';
    if (response.status === 401) {
      $('dashboard').hidden = true;
      $('authPanel').hidden = false;
    }
    throw new Error(detail);
  }
  return result;
}
function option(select, value, label) {
  const item = document.createElement('option');
  item.value = value;
  item.textContent = label;
  select.append(item);
}
function chart(days) {
  $('chart').replaceChildren();
  if (!days.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'Chưa có usage. Thống kê sẽ xuất hiện sau yêu cầu đầu tiên.';
    $('chart').append(empty);
    return;
  }
  const peak = Math.max(1, ...days.map(d => d.input_tokens + d.output_tokens));
  for (const day of days) {
    const column = document.createElement('div');
    column.className = 'barColumn';
    column.title = `${day.day}: input ${number(day.input_tokens)}, output ${number(day.output_tokens)}`;
    for (const kind of ['output', 'input']) {
      const bar = document.createElement('div');
      bar.className = kind;
      bar.style.height = `${100 * day[kind + '_tokens'] / peak}%`;
      column.append(bar);
    }
    const label = document.createElement('span');
    label.textContent = day.day.slice(5);
    column.append(label);
    $('chart').append(column);
  }
}
function providerControls() {
  const custom = $('llmProvider').value === 'custom';
  $('customLlmFields').hidden = !custom;
  $('llmBaseUrl').required = custom;
  $('oauthAccount').hidden = custom;
  $('manualCallback').hidden = custom;
  $('oauthCallback').hidden = custom;
  $('welcome').hidden = custom || !currentStatus?.active_account || !!localStorage.getItem('graphiti-plan-welcome');
  $('llmDescription').textContent = custom
    ? 'Chọn model từ LLM server local. Embeddings và reranker tiếp tục dùng Infinity bên dưới.'
    : 'Chọn model OpenAI từ danh sách của tài khoản đã đăng nhập. Graphiti không cần API key.';
}
async function refresh() {
  const status = await api('status');
  currentStatus = status;
  $('dashboard').hidden = false;
  $('authPanel').hidden = true;
  $('adminLogout').hidden = false;
  const account = status.accounts.find(a => a.id === status.active_account);
  $('accountName').textContent = account ? account.email || account.id : 'Chưa kết nối OpenAI';
  $('disconnect').hidden = !account;
  $('connect').textContent = account ? 'Thêm tài khoản ChatGPT' : 'Continue with ChatGPT';
  $('accounts').replaceChildren();
  option($('accounts'), '', 'Chọn tài khoản đã lưu');
  for (const account of status.accounts) option($('accounts'), account.id, account.email || account.id);
  $('accounts').value = status.active_account || '';
  $('accounts').hidden = !status.accounts.length;
  if (account && !localStorage.getItem('graphiti-plan-welcome')) $('welcome').hidden = false;
  const totals = status.usage.totals;
  for (const [id, field] of Object.entries({ totalTokens: 'total_tokens', inputTokens: 'input_tokens', outputTokens: 'output_tokens', requests: 'requests' })) $(id).textContent = number(totals[field]);
  $('cachedTokens').textContent = number(totals.cached_tokens) + ' token cache trong input';
  $('failures').textContent = number(totals.failures) + ' yêu cầu lỗi';
  chart(status.usage.daily);
  $('modelUsage').replaceChildren();
  for (const row of status.usage.providers) {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    name.textContent = row.model;
    const provider = document.createElement('small');
    provider.textContent = row.provider === 'llm' ? 'OpenAI · gói ChatGPT'
      : row.provider === 'llm_custom' ? 'LLM · OpenAI-compatible URL' : 'Infinity · ' + row.provider;
    name.append(provider);
    tr.append(name);
    for (const key of ['requests', 'input_tokens', 'output_tokens']) {
      const td = document.createElement('td');
      td.textContent = number(row[key]);
      tr.append(td);
    }
    $('modelUsage').append(tr);
  }
  if (!dirty) {
    $('llmProvider').value = status.connections.llm_provider;
    $('llmBaseUrl').value = status.connections.llm_base_url;
    for (const [id, field] of [['model', 'model'], ['smallModel', 'small_model']]) {
      const selected = status.connections[field];
      if (selected && !Array.from($(id).options).some(item => item.value === selected)) option($(id), selected, selected);
      $(id).value = selected || '';
    }
    $('localUrl').value = status.connections.local_model_url;
    $('embeddingModel').value = status.connections.embedding_model;
    $('rerankerModel').value = status.connections.reranker_model;
  }
  providerControls();
  $('callback').textContent = status.callback_uri;
  $('mcpEndpoint').textContent = new URL('/mcp/', window.location.origin).href;
}
async function loadModels() {
  const query = new URLSearchParams({ llm_provider: $('llmProvider').value });
  if ($('llmProvider').value === 'custom' && $('llmBaseUrl').value) query.set('llm_base_url', $('llmBaseUrl').value);
  const models = await api('models?' + query.toString());
  const config = currentStatus.connections;
  for (const [id, field] of [['model', 'model'], ['smallModel', 'small_model']]) {
    const selected = dirty ? $(id).value : config[field];
    $(id).replaceChildren();
    option($(id), '', models.llm.length ? 'Chọn model' : 'Chưa tải được danh sách model');
    for (const model of models.llm) option($(id), model.id, model.name);
    $(id).value = selected || '';
  }
  for (const [id, capability] of [['embeddingOptions', 'embed'], ['rerankerOptions', 'rerank']]) {
    $(id).replaceChildren();
    for (const model of models.local.filter(m => m.capabilities.includes(capability))) option($(id), model.id, model.id);
  }
  if (!$('rerankerModel').value) {
    const rerankers = models.local.filter(m => m.capabilities.includes('rerank'));
    if (rerankers.length === 1) {
      $('rerankerModel').value = rerankers[0].id;
      dirty = true;
    }
  }
  notice(models.errors.join(' '));
}
function action(id, callback) {
  $(id).addEventListener('click', async () => {
    $(id).disabled = true;
    try { await callback(); } catch (error) { notice(error.message, true); }
    finally { $(id).disabled = false; }
  });
}
async function connect(accountId = null) {
  popup = window.open('about:blank', '_blank');
  try {
    const result = await api('openai/login', { account_id: accountId });
    if (popup) popup.location.href = result.url;
    else window.location.href = result.url;
    notice('Hoàn tất đăng nhập trong cửa sổ OpenAI. Nếu callback 127.0.0.1 không mở được, dán URL của cửa sổ đó vào phần Đăng nhập Docker / LXC bên dưới.');
  } catch (error) { if (popup) popup.close(); throw error; }
}
$('passwordForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api(needsSetup ? 'setup' : 'login', { password: $('password').value });
    $('password').value = '';
    notice('');
    await refresh();
    await loadModels();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$('callbackForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  const callbackUrl = $('callbackUrl').value;
  $('callbackUrl').value = '';
  button.disabled = true;
  try {
    await api('openai/callback', { callback_url: callbackUrl });
    $('manualCallback').open = false;
    await refresh();
    await loadModels();
    notice('Đã kết nối OpenAI. Chọn model và lưu kết nối để sử dụng.');
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$('connectionForm').addEventListener('input', () => { dirty = true; });
$('connectionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api('connections', { llm_provider: $('llmProvider').value, llm_base_url: $('llmBaseUrl').value || null,
      model: $('model').value, small_model: $('smallModel').value,
      local_model_url: $('localUrl').value, embedding_model: $('embeddingModel').value,
      reranker_model: $('rerankerModel').value });
    dirty = false;
    $('savedStatus').textContent = 'Đã lưu';
    notice('Kết nối đã được lưu. Các yêu cầu tiếp theo sẽ dùng model đã chọn.');
    await refresh();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$('llmProvider').addEventListener('change', async () => {
  dirty = true;
  providerControls();
  try { await loadModels(); } catch (error) { notice(error.message, true); }
});
$('accounts').addEventListener('change', async () => {
  const id = $('accounts').value;
  if (!id) return;
  try {
    const account = currentStatus.accounts.find(a => a.id === id);
    if (!account.connected) return await connect(id);
    await api('openai/select', { account_id: id });
    await refresh();
    await loadModels();
  } catch (error) { notice(error.message, true); }
});
action('connect', () => connect());
action('disconnect', async () => {
  const result = await api('openai/logout', {});
  notice(result.revoked ? 'Đã ngắt kết nối OpenAI.' : 'Đã xóa token tại Graphiti, nhưng chưa xác nhận được thu hồi ở OpenAI. Bạn có thể ngắt ứng dụng trong ChatGPT Settings.', !result.revoked);
  await refresh();
  await loadModels();
});
action('refresh', refresh);
action('loadModels', loadModels);
action('adminLogout', async () => { await api('session/logout', {}); window.location.reload(); });
action('dismissWelcome', async () => { localStorage.setItem('graphiti-plan-welcome', '1'); $('welcome').hidden = true; });
setInterval(async () => {
  if ($('dashboard').hidden || document.hidden) return;
  try {
    const oldAccount = currentStatus?.active_account;
    await refresh();
    if (oldAccount !== currentStatus.active_account) await loadModels();
  } catch (error) { notice(error.message, true); }
}, 15000);
(async () => {
  try {
    $('manualCallback').open = !['127.0.0.1', 'localhost'].includes(window.location.hostname);
    const session = await api('session');
    needsSetup = session.needs_setup;
    if (session.authenticated) { await refresh(); await loadModels(); }
    else {
      $('authPanel').hidden = false;
      if (needsSetup) {
        $('authTitle').textContent = 'Tạo mật khẩu quản trị';
        $('authDescription').textContent = 'Lần chạy đầu tiên: tạo mật khẩu tối thiểu 10 ký tự để bảo vệ tài khoản OpenAI và cấu hình.';
        $('password').autocomplete = 'new-password';
      }
    }
  } catch (error) { notice(error.message, true); }
})();
