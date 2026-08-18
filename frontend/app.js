const state = {
  accessToken: "",
  user: null,
  authMode: "login",
  sessions: [],
  sessionId: "",
  messages: [],
  isStreaming: false,
  healthOk: false,
  deepHealth: {},
  tasks: [],
  memoryStatus: {},
  tools: [],
  notifications: [],
  activePanel: "health",
  panelError: "",
  sidebarCollapsed: localStorage.getItem("nodeclaw.sidebarCollapsed") === "true",
  consoleCollapsed: localStorage.getItem("nodeclaw.consoleCollapsed") === "true",
  eventAbort: null,
};

const tabs = [
  { id: "health", label: "Health" },
  { id: "tasks", label: "Tasks" },
  { id: "memory", label: "Memory" },
  { id: "tools", label: "Tools" },
];

const $ = (id) => document.getElementById(id);
const els = {
  authScreen: $("auth-screen"), authForm: $("auth-form"), authSubtitle: $("auth-subtitle"),
  usernameField: $("username-field"), emailField: $("email-field"), username: $("auth-username"),
  email: $("auth-email"), login: $("auth-login"), password: $("auth-password"), authError: $("auth-error"),
  authSubmit: $("auth-submit"), authSwitch: $("auth-switch"), app: $("app"), brandStatus: $("brand-status"),
  sidebar: $("sidebar"), sidebarToggle: $("sidebar-toggle-btn"), closeSidebar: $("close-sidebar-btn"),
  mobileSidebar: $("mobile-sidebar-btn"), mobileConsole: $("mobile-console-btn"), drawerScrim: $("drawer-scrim"),
  sidebarMemory: $("sidebar-memory"), newChat: $("new-chat-btn"), sessionList: $("session-list"),
  accountName: $("account-name"), accountEmail: $("account-email"), logout: $("logout-btn"),
  deleteAccount: $("delete-account-btn"),
  sessionTitle: $("session-title"), messages: $("messages"), composer: $("composer"), input: $("message-input"),
  send: $("send-btn"), inspector: $("inspector"), rail: $("console-rail-btn"), collapse: $("collapse-console-btn"),
  refresh: $("refresh-console-btn"), panelSummary: $("panel-summary"), panelTabs: $("panel-tabs"),
  panelAlert: $("panel-alert"), panelContent: $("panel-content"),
};

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function renderText(value) {
  return escapeHtml(value).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
}

function authHeaders(json = true) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  if (state.accessToken) headers.Authorization = `Bearer ${state.accessToken}`;
  return headers;
}

async function refreshAccess() {
  const response = await fetch("/api/auth/refresh", { method: "POST", credentials: "same-origin" });
  if (!response.ok) return false;
  const data = await response.json();
  state.accessToken = data.access_token;
  state.user = data.user;
  return true;
}

async function api(url, options = {}, retry = true) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { ...authHeaders(options.body !== undefined), ...(options.headers || {}) },
  });
  if (response.status === 401 && retry && await refreshAccess()) return api(url, options, false);
  let data = {};
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function showAuth(error = "") {
  state.accessToken = ""; state.user = null;
  els.authScreen.classList.remove("hidden"); els.app.classList.add("hidden");
  els.authError.textContent = error; els.authError.classList.toggle("hidden", !error);
}

function showApp() {
  els.authScreen.classList.add("hidden"); els.app.classList.remove("hidden");
  els.accountName.textContent = state.user.username; els.accountEmail.textContent = state.user.email;
  renderAll();
}

function renderAuthMode() {
  const register = state.authMode === "register";
  els.usernameField.classList.toggle("hidden", !register); els.emailField.classList.toggle("hidden", !register);
  els.login.parentElement.classList.toggle("hidden", register);
  els.authSubtitle.textContent = register ? "创建你的 Nodeclaw 账号" : "登录后继续你的工作";
  els.authSubmit.textContent = register ? "注册" : "登录";
  els.authSwitch.textContent = register ? "已有账号，返回登录" : "创建账号";
  els.password.autocomplete = register ? "new-password" : "current-password";
}

async function submitAuth(event) {
  event.preventDefault(); els.authError.classList.add("hidden");
  const register = state.authMode === "register";
  const payload = register
    ? { username: els.username.value.trim(), email: els.email.value.trim(), password: els.password.value }
    : { login: els.login.value.trim(), password: els.password.value };
  try {
    const data = await api(register ? "/api/auth/register" : "/api/auth/login", {
      method: "POST", body: JSON.stringify(payload), headers: { Authorization: "" },
    }, false);
    state.accessToken = data.access_token; state.user = data.user; els.password.value = "";
    await initializeApp();
  } catch (error) {
    els.authError.textContent = error.message; els.authError.classList.remove("hidden");
  }
}

async function loadSessions(selectFirst = false) {
  const data = await api("/api/sessions"); state.sessions = data.sessions || [];
  if (!state.sessions.length) {
    const created = await api("/api/sessions", { method: "POST", body: JSON.stringify({ title: "新对话" }) });
    state.sessions = [created.session];
  }
  if (!state.sessionId || !state.sessions.some((row) => row.session_id === state.sessionId) || selectFirst) {
    state.sessionId = state.sessions[0].session_id;
  }
  await loadMessages();
}

async function createNewChat() {
  const data = await api("/api/sessions", { method: "POST", body: JSON.stringify({ title: "新对话" }) });
  state.sessions.unshift(data.session); state.sessionId = data.session.session_id; state.messages = [];
  closeMobileDrawers(); renderAll(); els.input.focus();
}

async function selectSession(sessionId) {
  if (state.isStreaming || sessionId === state.sessionId) return;
  state.sessionId = sessionId; await loadMessages(); closeMobileDrawers(); renderAll();
}

async function loadMessages() {
  if (!state.sessionId) return;
  const data = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/messages`);
  state.messages = (data.messages || []).map((row) => ({ role: row.role, content: row.content }));
}

function renderSessions() {
  els.sessionList.innerHTML = state.sessions.map((row) => `
    <div class="session-entry ${row.session_id === state.sessionId ? "active" : ""}">
      <button class="session-item" type="button" data-session="${row.session_id}">
        <span>${escapeHtml(row.title || "新对话")}</span>
        <small>${escapeHtml(row.memory_sync_status || "ready")}</small>
      </button>
      <div class="session-actions">
        <button type="button" data-rename-session="${row.session_id}" title="重命名">...</button>
        <button type="button" data-delete-session="${row.session_id}" title="删除">×</button>
      </div>
    </div>`).join("");
}

function renderMessages() {
  if (!state.messages.length) {
    els.messages.innerHTML = `<div class="empty-state"><div class="empty-bot" aria-hidden="true">🤖</div><h2>今天想做点什么？</h2><p>可以从一个问题、一项任务或一条提醒开始。</p></div>`;
    return;
  }
  els.messages.innerHTML = state.messages.map((row) => {
    const role = row.role === "user" ? "user" : "assistant";
    const content = row.kind === "tool" ? `<div class="tool-line">${escapeHtml(row.content)}</div>`
      : row.kind === "reminder" ? `<div class="reminder-line"><div class="reminder-label">Reminder</div>${renderText(row.content)}</div>`
      : `<div class="content">${renderText(row.content)}</div>`;
    const avatar = role === "user" ? "我" : "🤖";
    const label = role === "user" ? "你" : "Nodeclaw";
    return `<article class="message ${role}"><div class="avatar" aria-hidden="true">${avatar}</div><div class="message-body"><div class="message-label">${label}</div><div class="bubble">${content}</div></div></article>`;
  }).join("") + (state.isStreaming ? `<article class="message assistant"><div class="avatar" aria-hidden="true">🤖</div><div class="message-body"><div class="message-label">Nodeclaw</div><div class="bubble pending"><span></span><span></span><span></span></div></div></article>` : "");
  requestAnimationFrame(() => { els.messages.scrollTop = els.messages.scrollHeight; });
}

function renderShell() {
  const session = state.sessions.find((row) => row.session_id === state.sessionId);
  els.sessionTitle.textContent = session?.title || "Nodeclaw";
  els.brandStatus.textContent = state.healthOk ? "online" : "checking";
  els.brandStatus.classList.toggle("online", state.healthOk);
  els.sidebarMemory.textContent = state.memoryStatus.backend || "V3";
  els.send.textContent = state.isStreaming ? "…" : "↑";
  els.send.disabled = state.isStreaming || !state.sessionId || !els.input.value.trim();
  els.app.classList.toggle("console-collapsed", state.consoleCollapsed);
  els.app.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
  els.sidebarToggle.textContent = state.sidebarCollapsed ? "›" : "‹";
  els.sidebarToggle.title = state.sidebarCollapsed ? "展开侧栏" : "折叠侧栏";
  els.sidebarToggle.setAttribute("aria-label", els.sidebarToggle.title);
  els.collapse.textContent = state.consoleCollapsed ? "‹" : "›";
  els.rail.textContent = state.consoleCollapsed ? "›" : "‹";
}

function renderPanel() {
  els.panelTabs.innerHTML = tabs.map((tab) => `<button type="button" data-panel="${tab.id}" class="${state.activePanel === tab.id ? "active" : ""}">${tab.label}</button>`).join("");
  els.panelAlert.textContent = state.panelError; els.panelAlert.classList.toggle("hidden", !state.panelError);
  if (state.activePanel === "tasks") els.panelContent.innerHTML = tasksPanel();
  else if (state.activePanel === "memory") els.panelContent.innerHTML = memoryPanel();
  else if (state.activePanel === "tools") els.panelContent.innerHTML = toolsPanel();
  else els.panelContent.innerHTML = healthPanel();
  els.panelSummary.textContent = state.activePanel === "tasks" ? `${state.tasks.length} scheduled · ${state.notifications.length} unread`
    : state.activePanel === "memory" ? `${state.memoryStatus.object_count || 0} memories`
    : state.activePanel === "tools" ? `${state.tools.length} tools` : state.deepHealth.status || "checking";
}

function healthPanel() {
  return `<div class="check-list">${(state.deepHealth.checks || []).map((row) => `<div class="check-item"><div><strong>${escapeHtml(row.name)}</strong><p>${escapeHtml(row.message)}</p></div><span class="mini-pill ${row.status}">${row.status}</span></div>`).join("") || "Health data is loading."}</div>`;
}

function tasksPanel() {
  const notifications = state.notifications.map((row) => `<div class="notification-item"><div><strong>${escapeHtml(row.content)}</strong><p>${escapeHtml(row.created_at || "")}</p></div><button type="button" data-read-notification="${row.notification_id}" title="标为已读">✓</button></div>`).join("") || `<div class="empty-mini">暂无未读提醒</div>`;
  return `<form id="task-form" class="task-form"><label>时间<input name="target_time" placeholder="2026-08-18 09:00:00" required /></label><label>内容<textarea name="description" rows="2" placeholder="提醒我上课" required></textarea></label><div class="form-grid"><label>重复<select name="repeat"><option value="">once</option><option value="daily">daily</option><option value="weekly">weekly</option><option value="monthly">monthly</option></select></label><label>次数<input name="repeat_count" type="number" min="1" /></label></div><button class="primary-action" type="submit">添加提醒</button></form><div class="task-list">${state.tasks.map((row) => `<div class="task-item"><div><strong>${escapeHtml(row.description)}</strong><p>${escapeHtml(row.target_time)}</p><small>${escapeHtml(row.repeat || "once")}</small></div><button class="danger-button" data-delete-task="${row.id}" type="button">×</button></div>`).join("") || `<div class="empty-mini">暂无计划</div>`}</div><div class="subpanel-title notification-title">未读提醒</div><div class="notification-list">${notifications}</div>`;
}

function memoryPanel() {
  const last = state.memoryStatus.last_action;
  return `<div class="metric-grid"><div><span>Backend</span><strong>${escapeHtml(state.memoryStatus.backend || "v3")}</strong></div><div><span>Active</span><strong>${state.memoryStatus.object_count || 0}</strong></div><div><span>Pending</span><strong>${state.memoryStatus.pending_sessions || 0}</strong></div><div><span>Retrieval</span><strong>${escapeHtml(state.memoryStatus.retrieval_mode || "hybrid_rrf")}</strong></div></div><div class="subpanel"><div class="subpanel-title">最近动作</div><p class="muted-line">${last ? escapeHtml(`${last.action || "-"} · ${last.memory_id || ""}`) : "暂无"}</p></div><button class="primary-action" type="button" data-action="reindex">重建当前用户索引</button>`;
}

function toolsPanel() {
  return `<div class="tool-list">${state.tools.map((row) => `<div class="tool-item"><div class="hit-line"><strong>${escapeHtml(row.name)}</strong><span>${escapeHtml(row.source)}</span></div><p>${escapeHtml(row.description)}</p></div>`).join("")}</div>`;
}

function renderAll() { renderShell(); renderSessions(); renderMessages(); renderPanel(); }

async function refreshDashboard() {
  state.panelError = "";
  const results = await Promise.allSettled([
    api("/api/health/deep"), api("/api/tasks"), api("/api/memory/status"), api("/api/tools"), api("/api/notifications?unread_only=true"),
  ]);
  if (results[0].status === "fulfilled") state.deepHealth = results[0].value;
  if (results[1].status === "fulfilled") state.tasks = results[1].value.tasks || [];
  if (results[2].status === "fulfilled") state.memoryStatus = results[2].value;
  if (results[3].status === "fulfilled") state.tools = results[3].value.tools || [];
  if (results[4].status === "fulfilled") state.notifications = results[4].value.notifications || [];
  state.healthOk = state.deepHealth.status === "ok";
  const failed = results.find((row) => row.status === "rejected"); if (failed) state.panelError = failed.reason.message;
  renderAll();
}

async function sendMessage(event) {
  event.preventDefault(); const text = els.input.value.trim(); if (!text || state.isStreaming) return;
  const assistant = { role: "assistant", content: "" };
  state.messages.push({ role: "user", content: text }, assistant); els.input.value = ""; resizeComposer(); state.isStreaming = true; renderAll();
  try {
    let response = await fetch("/api/chat", { method: "POST", credentials: "same-origin", headers: authHeaders(), body: JSON.stringify({ message: text, session_id: state.sessionId }) });
    if (response.status === 401 && await refreshAccess()) response = await fetch("/api/chat", { method: "POST", credentials: "same-origin", headers: authHeaders(), body: JSON.stringify({ message: text, session_id: state.sessionId }) });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    await consumeSse(response, (payload) => {
      if (payload.startsWith("[TOOL_CALL]")) state.messages.splice(state.messages.length - 1, 0, { role: "assistant", kind: "tool", content: payload.replace("[TOOL_CALL]", "").trim() });
      else assistant.content += payload;
      renderMessages();
    });
  } catch (error) { assistant.content += `\n[ERROR] ${error.message}`; }
  state.isStreaming = false; await Promise.allSettled([loadSessions(), refreshDashboard()]); renderAll(); els.input.focus();
}

async function consumeSse(response, callback) {
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buffer += decoder.decode(value, { stream: true }); const chunks = buffer.split("\n\n"); buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      const payload = chunk.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.replace(/^data:\s?/, "")).join("\n");
      if (payload === "[DONE]") return; if (payload) callback(payload);
    }
  }
}

async function notificationStream() {
  if (state.eventAbort) state.eventAbort.abort(); state.eventAbort = new AbortController();
  try {
    const response = await fetch("/api/events", { headers: authHeaders(false), signal: state.eventAbort.signal });
    if (response.status === 401 && await refreshAccess()) return notificationStream();
    if (!response.ok) { setTimeout(notificationStream, 3000); return; }
    await consumeSse(response, (payload) => {
      try {
        const data = JSON.parse(payload); if (data.kind === "reminder") {
          state.notifications.unshift(data);
          state.messages.push({ role: "assistant", kind: "reminder", content: data.content }); renderMessages(); refreshDashboard();
        }
      } catch { /* keep-alive or malformed event */ }
    });
    if (!state.eventAbort.signal.aborted) setTimeout(notificationStream, 1000);
  } catch (error) { if (error.name !== "AbortError") setTimeout(notificationStream, 3000); }
}

async function createTaskFromForm(form) {
  const data = new FormData(form); const count = String(data.get("repeat_count") || "").trim();
  await api("/api/tasks", { method: "POST", body: JSON.stringify({ target_time: data.get("target_time"), description: data.get("description"), session_id: state.sessionId, repeat: data.get("repeat") || null, repeat_count: count ? Number(count) : null }) });
  form.reset(); await refreshDashboard();
}

async function initializeApp() {
  showApp(); await loadSessions(); await refreshDashboard(); notificationStream(); renderAll();
}

els.authForm.addEventListener("submit", submitAuth);
els.authSwitch.addEventListener("click", () => { state.authMode = state.authMode === "login" ? "register" : "login"; renderAuthMode(); });
els.newChat.addEventListener("click", createNewChat);
els.sidebarToggle.addEventListener("click", () => {
  state.sidebarCollapsed = !state.sidebarCollapsed;
  localStorage.setItem("nodeclaw.sidebarCollapsed", String(state.sidebarCollapsed));
  renderShell();
});
function syncDrawerScrim() {
  const open = els.sidebar.classList.contains("mobile-open") || els.inspector.classList.contains("mobile-open");
  els.drawerScrim.classList.toggle("visible", open);
}
function closeMobileDrawers() {
  els.sidebar.classList.remove("mobile-open"); els.inspector.classList.remove("mobile-open"); syncDrawerScrim();
}
els.mobileSidebar.addEventListener("click", () => {
  els.inspector.classList.remove("mobile-open"); els.sidebar.classList.add("mobile-open"); syncDrawerScrim();
});
els.closeSidebar.addEventListener("click", closeMobileDrawers);
els.drawerScrim.addEventListener("click", closeMobileDrawers);
els.mobileConsole.addEventListener("click", () => {
  state.consoleCollapsed = false; els.sidebar.classList.remove("mobile-open"); els.inspector.classList.toggle("mobile-open"); renderShell(); syncDrawerScrim();
});
els.logout.addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); if (state.eventAbort) state.eventAbort.abort(); showAuth(); });
els.deleteAccount.addEventListener("click", async () => {
  if (!confirm("确定永久删除账号及全部数据吗？此操作无法撤销。")) return;
  await api("/api/auth/account", { method: "DELETE" });
  if (state.eventAbort) state.eventAbort.abort(); showAuth("账号已删除");
});
els.composer.addEventListener("submit", sendMessage);
function resizeComposer() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 160)}px`;
}
els.input.addEventListener("input", () => { resizeComposer(); renderShell(); });
els.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); els.composer.requestSubmit(); }
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMobileDrawers(); });
els.sessionList.addEventListener("click", async (event) => {
  const renameButton = event.target.closest("[data-rename-session]");
  if (renameButton) {
    const row = state.sessions.find((item) => item.session_id === renameButton.dataset.renameSession);
    const title = prompt("会话名称", row?.title || "新对话");
    if (title?.trim()) {
      await api(`/api/sessions/${renameButton.dataset.renameSession}`, { method: "PATCH", body: JSON.stringify({ title: title.trim() }) });
      await loadSessions(); renderAll();
    }
    return;
  }
  const deleteButton = event.target.closest("[data-delete-session]");
  if (deleteButton) {
    if (state.isStreaming || !confirm("删除这个会话？共享的长期记忆会保留。")) return;
    await api(`/api/sessions/${deleteButton.dataset.deleteSession}`, { method: "DELETE" });
    if (state.sessionId === deleteButton.dataset.deleteSession) state.sessionId = "";
    await loadSessions(true); renderAll();
    return;
  }
  const button = event.target.closest("[data-session]"); if (button) selectSession(button.dataset.session);
});
els.panelTabs.addEventListener("click", (event) => { const button = event.target.closest("[data-panel]"); if (button) { state.activePanel = button.dataset.panel; renderPanel(); } });
els.panelContent.addEventListener("submit", (event) => { if (event.target.id === "task-form") { event.preventDefault(); createTaskFromForm(event.target).catch((error) => { state.panelError = error.message; renderPanel(); }); } });
els.panelContent.addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-task]");
  if (deleteButton) { await api(`/api/tasks/${deleteButton.dataset.deleteTask}`, { method: "DELETE" }); await refreshDashboard(); }
  const readButton = event.target.closest("[data-read-notification]");
  if (readButton) { await api(`/api/notifications/${readButton.dataset.readNotification}/read`, { method: "PATCH" }); await refreshDashboard(); }
  if (event.target.closest('[data-action="reindex"]')) { await api("/api/memory/reindex", { method: "POST" }); }
});
function toggleConsole() {
  if (window.innerWidth <= 780 && els.inspector.classList.contains("mobile-open")) {
    els.inspector.classList.remove("mobile-open"); syncDrawerScrim(); return;
  }
  state.consoleCollapsed = !state.consoleCollapsed; localStorage.setItem("nodeclaw.consoleCollapsed", String(state.consoleCollapsed)); renderShell();
}
els.collapse.addEventListener("click", toggleConsole); els.rail.addEventListener("click", toggleConsole); els.refresh.addEventListener("click", refreshDashboard);

async function bootstrap() {
  renderAuthMode();
  if (await refreshAccess()) await initializeApp(); else showAuth();
}
bootstrap();
