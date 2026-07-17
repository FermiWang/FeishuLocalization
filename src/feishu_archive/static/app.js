const state = {
  conversations: [],
  selectedChat: null,
  syncWasRunning: false,
  syncPollTimer: null,
  mode: "messages",
  wikiSpaces: [],
  wikiNodes: [],
  selectedWikiSpace: null,
  selectedWikiNode: null,
  wikiSyncWasRunning: false,
  wikiPollTimer: null,
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

function formatSourceTime(value) {
  if (!value) return "时间未知";
  const milliseconds = Number(value) < 1e12 ? Number(value) * 1000 : Number(value);
  return formatTime(milliseconds);
}

function setMode(mode) {
  state.mode = mode;
  const wiki = mode === "wiki";
  $("message-sidebar").hidden = wiki;
  $("wiki-sidebar").hidden = !wiki;
  $("message-view").hidden = wiki;
  $("wiki-view").hidden = !wiki;
  $("mode-messages").classList.toggle("active", !wiki);
  $("mode-wiki").classList.toggle("active", wiki);
}

async function loadStatus() {
  const data = await request("/api/status");
  $("archive-status").textContent = `${data.conversations} 个会话 · ${data.messages} 条消息 · 图片 ${data.images} 张 · 附件 ${data.attachments} 个 · 占用 ${formatBytes(data.resource_bytes)}`;
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

async function loadWikiStatus() {
  clearTimeout(state.wikiPollTimer);
  try {
    const data = await request("/api/wiki/status");
    $("wiki-status").textContent = `${data.spaces} 个空间 · ${data.nodes} 个节点 · ${data.synced_documents} 篇正文 · 附件 ${data.assets} 个 · 占用 ${formatBytes(data.asset_bytes)}`;
    const job = data.latest_sync;
    const running = job?.status === "running";
    const schedule = data.schedule?.description || "自动同步未配置";
    const button = $("wiki-sync-now");
    button.disabled = running;
    button.textContent = running ? "同步中…" : "同步知识库";
    if (!job) {
      $("wiki-sync-status").textContent = schedule;
    } else if (running) {
      $("wiki-sync-status").textContent = `知识库同步进行中 · ${schedule}`;
    } else {
      const labels = { success: "成功", partial: "部分完成", error: "失败" };
      const detail = job.error ? ` · ${String(job.error).split("\n")[0]}` : "";
      $("wiki-sync-status").textContent = `上次同步${labels[job.status] || job.status}：${formatTime(job.finished_at)}${detail} · ${schedule}`;
    }
    if (state.wikiSyncWasRunning && !running) {
      await loadWikiSpaces(true);
      if (state.selectedWikiSpace) await selectWikiSpace(state.selectedWikiSpace, false);
      if (state.selectedWikiNode) await selectWikiNode(state.selectedWikiNode);
    }
    state.wikiSyncWasRunning = running;
    state.wikiPollTimer = setTimeout(loadWikiStatus, running ? 2000 : 60000);
  } catch (error) {
    $("wiki-sync-status").textContent = error.message;
    $("wiki-sync-now").disabled = false;
    state.wikiPollTimer = setTimeout(loadWikiStatus, 60000);
  }
}

async function startWikiSync() {
  const button = $("wiki-sync-now");
  button.disabled = true;
  button.textContent = "正在启动…";
  $("wiki-sync-status").textContent = "正在启动本机知识库同步任务…";
  try {
    await request("/api/wiki/sync", {
      method: "POST",
      headers: { "X-Feishu-Archive-Action": "wiki-sync" },
    });
    state.wikiSyncWasRunning = true;
    await loadWikiStatus();
  } catch (error) {
    $("wiki-sync-status").textContent = error.message;
    button.disabled = false;
    button.textContent = "同步知识库";
  }
}

async function loadWikiSpaces(keepSelection = false) {
  const data = await request("/api/wiki/spaces");
  state.wikiSpaces = data.items;
  renderWikiSpaces();
  if (!keepSelection && !state.selectedWikiSpace && data.items.length) {
    await selectWikiSpace(data.items[0].space_id);
  }
}

function renderWikiSpaces() {
  const root = $("wiki-space-list");
  root.replaceChildren();
  state.wikiSpaces.forEach((item) => {
    const button = document.createElement("button");
    button.className = `wiki-space${item.space_id === state.selectedWikiSpace ? " active" : ""}`;
    const name = document.createElement("span");
    name.textContent = item.name || item.space_id;
    const meta = document.createElement("small");
    meta.textContent = `${item.node_count || 0} 个节点`;
    button.append(name, meta);
    button.addEventListener("click", () => selectWikiSpace(item.space_id));
    root.append(button);
  });
}

async function selectWikiSpace(spaceId, resetNode = true) {
  state.selectedWikiSpace = spaceId;
  if (resetNode) state.selectedWikiNode = null;
  renderWikiSpaces();
  const space = state.wikiSpaces.find((item) => item.space_id === spaceId);
  $("wiki-title").textContent = space?.name || spaceId;
  $("wiki-eyebrow").textContent = "知识空间目录";
  $("wiki-document-meta").textContent = space?.description || "选择左侧文档开始离线阅读。";
  const data = await request(`/api/wiki/nodes?space_id=${encodeURIComponent(spaceId)}`);
  state.wikiNodes = data.items;
  renderWikiNodes();
  if (resetNode) {
    $("wiki-results").hidden = true;
    $("wiki-document").innerHTML = '<div class="empty-state"><strong>选择左侧文档开始阅读</strong><span>目录与正文均来自本机档案。</span></div>';
  }
}

function renderWikiNodes() {
  const needle = $("wiki-query").value.trim().toLowerCase();
  const root = $("wiki-node-list");
  root.replaceChildren();
  state.wikiNodes
    .filter((item) => !needle || `${item.title || ""} ${item.path || ""}`.toLowerCase().includes(needle))
    .forEach((item) => {
      const button = document.createElement("button");
      button.className = `wiki-node${item.node_token === state.selectedWikiNode ? " active" : ""}`;
      const depth = Math.max(0, String(item.path || "").split("/").length - 1);
      button.style.paddingLeft = `${12 + Math.min(depth, 8) * 14}px`;
      const title = document.createElement("span");
      title.textContent = item.title || item.obj_token;
      const meta = document.createElement("small");
      const labels = { synced: "已离线", metadata_only: "仅目录", error: "同步失败", syncing: "同步中" };
      meta.textContent = `${item.obj_type || "unknown"} · ${labels[item.document_status] || "待同步"}`;
      button.append(title, meta);
      button.addEventListener("click", () => selectWikiNode(item.node_token));
      root.append(button);
    });
}

async function selectWikiNode(nodeToken) {
  state.selectedWikiNode = nodeToken;
  renderWikiNodes();
  const root = $("wiki-document");
  root.innerHTML = '<div class="empty-state"><span>正在从本机档案读取…</span></div>';
  try {
    const item = await request(`/api/wiki/document?node_token=${encodeURIComponent(nodeToken)}`);
    $("wiki-title").textContent = item.title || item.obj_token || "未命名文档";
    $("wiki-eyebrow").textContent = item.path || "知识库离线阅读";
    const labels = { synced: "正文已离线", metadata_only: "已保存目录元数据", error: "上次同步失败", syncing: "正在同步" };
    $("wiki-document-meta").textContent = `${labels[item.status] || item.status || "待同步"} · 源文档更新 ${formatSourceTime(item.source_edit_time || item.obj_edit_time)} · 本机同步 ${formatTime(item.last_synced_at)}`;
    root.replaceChildren();
    if (item.error) {
      const warning = document.createElement("div");
      warning.className = "wiki-warning";
      warning.textContent = item.error;
      root.append(warning);
    }
    const body = document.createElement("div");
    body.className = "wiki-document-body";
    if (item.rendered_html) {
      body.innerHTML = item.rendered_html;
    } else {
      const pre = document.createElement("pre");
      pre.textContent = item.content_text || "本地尚无可显示的正文。";
      body.append(pre);
    }
    root.append(body);
  } catch (error) {
    root.innerHTML = '<div class="empty-state"><strong>读取失败</strong><span></span></div>';
    root.querySelector("span").textContent = error.message;
  }
}

async function searchWiki() {
  const query = $("wiki-query").value.trim();
  if (!query) {
    $("wiki-results").hidden = true;
    renderWikiNodes();
    return;
  }
  const params = new URLSearchParams({ q: query, limit: "100" });
  const data = await request(`/api/wiki/search?${params}`);
  const root = $("wiki-results");
  root.replaceChildren();
  const summary = document.createElement("strong");
  summary.textContent = `找到 ${data.items.length} 篇本地文档`;
  root.append(summary);
  data.items.forEach((item) => {
    const button = document.createElement("button");
    button.className = "wiki-search-result";
    const title = document.createElement("strong");
    title.textContent = item.title || item.obj_token;
    const path = document.createElement("small");
    path.textContent = item.path || item.space_id;
    const excerpt = document.createElement("span");
    excerpt.textContent = String(item.excerpt || "").replace(/\s+/g, " ").slice(0, 180);
    button.append(title, path, excerpt);
    button.addEventListener("click", async () => {
      if (state.selectedWikiSpace !== item.space_id) await selectWikiSpace(item.space_id, false);
      await selectWikiNode(item.node_token);
    });
    root.append(button);
  });
  root.hidden = false;
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
  const resources = item.resources || [];
  const images = resources.filter((resource) => resource.resource_type === "image");
  const attachments = resources.filter((resource) => resource.resource_type === "file");
  const article = document.createElement("article");
  article.className = "message";
  const head = document.createElement("div"); head.className = "message-head";
  const sender = document.createElement("strong"); sender.textContent = item.sender_name || "未知发送者";
  const time = document.createElement("time"); time.textContent = formatTime(item.created_at);
  head.append(sender, time);
  const body = document.createElement("div"); body.className = "message-body";
  const bodyText = (item.body_text || "").trim();
  const imageOnlyPlaceholder = item.message_type === "image" && /^\[图片\](\s*图片资源示例)?$/.test(bodyText);
  body.textContent = imageOnlyPlaceholder && images.length ? "" : (bodyText || `[${item.message_type}]`);
  const meta = document.createElement("div"); meta.className = "message-meta";
  [item.message_type, item.thread_id ? "话题" : null, item.deleted ? "已删除" : null, item.recalled ? "已撤回" : null, item.image_count ? `${item.image_count} 张图片` : null, item.attachment_count ? `${item.attachment_count} 个附件` : null]
    .filter(Boolean).forEach((value) => { const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = value; meta.append(badge); });
  article.append(head);
  if (body.textContent) article.append(body);
  if (images.length) {
    const gallery = document.createElement("div");
    gallery.className = "message-images";
    images.forEach((image) => {
      if (image.status === "downloaded") {
        const link = document.createElement("a");
        link.className = "message-image-link";
        link.href = `/api/images/${image.id}`;
        link.target = "_blank";
        link.rel = "noopener";
        const element = document.createElement("img");
        element.className = "message-image";
        element.src = `/api/images/${image.id}`;
        element.loading = "lazy";
        element.decoding = "async";
        element.alt = `${item.sender_name || "未知发送者"}发送的图片`;
        link.append(element);
        gallery.append(link);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "image-placeholder";
        placeholder.textContent = `图片未归档（${image.status}）`;
        gallery.append(placeholder);
      }
    });
    article.append(gallery);
  }
  article.append(meta);
  attachments.forEach((attachment) => {
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
$("mode-messages").addEventListener("click", () => setMode("messages"));
$("mode-wiki").addEventListener("click", () => setMode("wiki"));
$("wiki-sync-now").addEventListener("click", startWikiSync);
$("wiki-search").addEventListener("click", searchWiki);
$("wiki-query").addEventListener("input", renderWikiNodes);
$("wiki-query").addEventListener("keydown", (event) => { if (event.key === "Enter") searchWiki(); });

Promise.all([loadStatus(), loadConversations(), loadSyncStatus(), loadWikiStatus(), loadWikiSpaces()]).catch((error) => {
  $("archive-status").textContent = error.message;
});
