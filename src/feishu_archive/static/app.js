const state = {
  conversations: [],
  selectedChat: null,
  syncWasRunning: false,
  syncPollTimer: null,
};
const $ = (id) => document.getElementById(id);

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatTime(value) {
  if (!value) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

async function loadStatus() {
  const data = await request("/api/status");
  $("archive-status").textContent = `${data.conversations} 个会话 · ${data.messages} 条消息 · 附件 ${formatBytes(data.attachment_bytes)}`;
}

async function loadSyncStatus() {
  clearTimeout(state.syncPollTimer);
  try {
    const data = await request("/api/sync/status");
    const job = data.job;
    const running = job?.status === "running";
    const schedule = data.schedule?.description || "自动同步未配置";
    const button = $("sync-now");
    button.disabled = running;
    button.textContent = running ? "同步中…" : "立即同步";
    if (!job) {
      $("sync-status").textContent = schedule;
    } else if (running) {
      const trigger = job.trigger === "manual" ? "手工" : "自动";
      $("sync-status").textContent = `${trigger}同步进行中 · ${schedule}`;
    } else {
      const labels = { success: "成功", partial: "部分完成", error: "失败" };
      $("sync-status").textContent = `上次同步${labels[job.status] || job.status}：${formatTime(job.finished_at)} · ${schedule}`;
    }
    if (state.syncWasRunning && !running) {
      await loadStatus();
      await loadConversations();
      if (state.selectedChat) await selectConversation(state.selectedChat);
    }
    state.syncWasRunning = running;
    state.syncPollTimer = setTimeout(loadSyncStatus, running ? 2000 : 60000);
  } catch (error) {
    $("sync-status").textContent = error.message;
    $("sync-now").disabled = false;
    state.syncPollTimer = setTimeout(loadSyncStatus, 60000);
  }
}

async function startSync() {
  const button = $("sync-now");
  button.disabled = true;
  button.textContent = "正在启动…";
  $("sync-status").textContent = "正在启动本机同步任务…";
  try {
    await request("/api/sync", {
      method: "POST",
      headers: { "X-Feishu-Archive-Action": "sync" },
    });
    state.syncWasRunning = true;
    await loadSyncStatus();
  } catch (error) {
    $("sync-status").textContent = error.message;
    button.disabled = false;
    button.textContent = "立即同步";
  }
}

async function loadConversations() {
  const data = await request("/api/conversations");
  state.conversations = data.items;
  renderConversations();
  if (!state.selectedChat && data.items.length) await selectConversation(data.items[0].chat_id);
}

function renderConversations() {
  const needle = $("chat-search").value.trim().toLowerCase();
  const root = $("conversation-list");
  root.replaceChildren();
  state.conversations.filter((item) => (item.name || item.chat_id).toLowerCase().includes(needle)).forEach((item) => {
    const button = document.createElement("button");
    button.className = `conversation${item.chat_id === state.selectedChat ? " active" : ""}`;
    const name = document.createElement("span");
    name.textContent = item.name || item.chat_id;
    const meta = document.createElement("small");
    meta.textContent = `${item.message_count} 条消息${item.external ? " · 外部群" : ""}`;
    button.append(name, meta);
    button.addEventListener("click", () => selectConversation(item.chat_id));
    root.append(button);
  });
}

async function selectConversation(chatId) {
  state.selectedChat = chatId;
  const item = state.conversations.find((row) => row.chat_id === chatId);
  $("chat-title").textContent = item?.name || chatId;
  $("export-html").disabled = false;
  $("export-json").disabled = false;
  renderConversations();
  const senders = await request(`/api/senders?chat_id=${encodeURIComponent(chatId)}`);
  const select = $("sender");
  select.replaceChildren(new Option("全部人员", ""));
  senders.items.forEach((name) => select.add(new Option(name, name)));
  await loadMessages();
}

async function loadMessages() {
  if (!state.selectedChat) return;
  const params = new URLSearchParams({ chat_id: state.selectedChat, limit: "500" });
  const values = { q: "query", sender: "sender", type: "message-type", date_from: "date-from", date_to: "date-to" };
  Object.entries(values).forEach(([key, id]) => { if ($(id).value) params.set(key, $(id).value); });
  const root = $("messages");
  root.innerHTML = '<div class="empty-state"><span>正在从本机档案读取…</span></div>';
  try {
    const data = await request(`/api/messages?${params}`);
    $("result-count").textContent = `${data.items.length} 条消息`;
    root.replaceChildren();
    if (!data.items.length) {
      root.innerHTML = '<div class="empty-state"><strong>没有符合条件的消息</strong><span>这不代表飞书端不存在数据，也可能受授权或历史可见性限制。</span></div>';
      return;
    }
    data.items.forEach((item) => root.append(renderMessage(item)));
  } catch (error) {
    root.innerHTML = `<div class="empty-state"><strong>读取失败</strong><span></span></div>`;
    root.querySelector("span").textContent = error.message;
  }
}

function renderMessage(item) {
  const article = document.createElement("article");
  article.className = "message";
  const head = document.createElement("div"); head.className = "message-head";
  const sender = document.createElement("strong"); sender.textContent = item.sender_name || "未知发送者";
  const time = document.createElement("time"); time.textContent = formatTime(item.created_at);
  head.append(sender, time);
  const body = document.createElement("div"); body.className = "message-body";
  body.textContent = item.body_text || `[${item.message_type}]`;
  const meta = document.createElement("div"); meta.className = "message-meta";
  [item.message_type, item.thread_id ? "话题" : null, item.deleted ? "已删除" : null, item.recalled ? "已撤回" : null, item.attachment_count ? `${item.attachment_count} 个附件` : null]
    .filter(Boolean).forEach((value) => { const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = value; meta.append(badge); });
  article.append(head, body, meta);
  (item.attachments || []).forEach((attachment) => {
    const link = document.createElement(attachment.status === "downloaded" ? "a" : "span");
    link.className = "attachment-link";
    link.textContent = attachment.status === "downloaded"
      ? `打开附件：${attachment.filename || attachment.file_key}`
      : `附件未归档：${attachment.filename || attachment.file_key}（${attachment.status}）`;
    if (attachment.status === "downloaded") link.href = `/api/attachments/${attachment.id}`;
    article.append(link);
  });
  return article;
}

function exportConversation(format) {
  if (!state.selectedChat) return;
  window.location.href = `/api/export?chat_id=${encodeURIComponent(state.selectedChat)}&format=${format}`;
}

$("chat-search").addEventListener("input", renderConversations);
$("search").addEventListener("click", loadMessages);
$("query").addEventListener("keydown", (event) => { if (event.key === "Enter") loadMessages(); });
$("export-html").addEventListener("click", () => exportConversation("html"));
$("export-json").addEventListener("click", () => exportConversation("json"));
$("sync-now").addEventListener("click", startSync);

Promise.all([loadStatus(), loadConversations(), loadSyncStatus()]).catch((error) => {
  $("archive-status").textContent = error.message;
});
