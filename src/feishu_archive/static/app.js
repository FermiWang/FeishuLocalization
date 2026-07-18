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
  wikiView: "nodes",
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

function writeWikiLocation(nodeToken = null, replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "wiki");
  if (state.selectedWikiSpace) url.searchParams.set("space_id", state.selectedWikiSpace);
  else url.searchParams.delete("space_id");
  if (nodeToken) url.searchParams.set("node_token", nodeToken);
  else url.searchParams.delete("node_token");
  history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function writeMessageLocation() {
  const url = new URL(window.location.href);
  url.searchParams.delete("mode");
  url.searchParams.delete("space_id");
  url.searchParams.delete("node_token");
  history.pushState({}, "", url);
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
      const selectedNode = state.selectedWikiNode;
      if (state.selectedWikiSpace) await selectWikiSpace(state.selectedWikiSpace, false, false);
      if (selectedNode) await selectWikiNode(selectedNode, false);
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
  if (!keepSelection && state.selectedWikiSpace && !data.items.some((item) => item.space_id === state.selectedWikiSpace)) {
    state.selectedWikiSpace = null;
    state.selectedWikiNode = null;
  }
  renderWikiSpaces();
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

async function selectWikiSpace(spaceId, resetNode = true, updateHistory = true) {
  state.selectedWikiSpace = spaceId;
  if (resetNode) state.selectedWikiNode = null;
  renderWikiSpaces();
  $("wiki-node-list").innerHTML = '<div class="empty-state"><span>正在从本机档案读取节点…</span></div>';
  const data = await request(`/api/wiki/nodes?space_id=${encodeURIComponent(spaceId)}`);
  state.wikiNodes = data.items;
  showWikiNodeList(updateHistory);
}

function renderWikiNodes() {
  const needle = $("wiki-query").value.trim().toLowerCase();
  const root = $("wiki-node-list");
  root.replaceChildren();
  const ordered = orderWikiNodes(state.wikiNodes);
  const visible = ordered.filter(({ item }) => !needle || `${item.title || ""} ${item.path || ""}`.toLowerCase().includes(needle));
  $("wiki-node-count").textContent = needle ? `${visible.length} / ${ordered.length} 个节点` : `${ordered.length} 个节点`;
  if (!visible.length) {
    root.innerHTML = '<div class="empty-state"><strong>没有匹配的节点</strong><span>可清除搜索词查看空间中的全部节点。</span></div>';
    return;
  }
  visible.forEach(({ item, depth }) => {
      const button = document.createElement("button");
      button.className = `wiki-node${item.node_token === state.selectedWikiNode ? " active" : ""}`;
      button.style.paddingLeft = `${15 + Math.min(depth, 6) * 22}px`;
      const title = document.createElement("span");
      title.className = "wiki-node-title";
      title.textContent = item.title || item.obj_token;
      const path = document.createElement("small");
      path.className = "wiki-node-path";
      path.textContent = item.path || item.title || item.obj_token;
      const status = document.createElement("span");
      const labels = { synced: "已离线", metadata_only: "仅目录", error: "同步失败", syncing: "同步中" };
      const statusName = item.document_status || "pending";
      status.className = `wiki-node-status ${statusName}`;
      status.textContent = `${item.obj_type || "unknown"} · ${labels[statusName] || "待同步"}`;
      button.append(title, path, status);
      button.addEventListener("click", () => selectWikiNode(item.node_token));
      root.append(button);
    });
}

function orderWikiNodes(items) {
  const byParent = new Map();
  const known = new Set(items.map((item) => item.node_token));
  items.forEach((item) => {
    const parent = item.parent_node_token && known.has(item.parent_node_token) ? item.parent_node_token : "";
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(item);
  });
  byParent.forEach((children) => children.sort((left, right) => {
    const position = Number(left.position || 0) - Number(right.position || 0);
    return position || String(left.title || "").localeCompare(String(right.title || ""), "zh-CN");
  }));
  const result = [];
  const visited = new Set();
  function visit(parent, depth) {
    (byParent.get(parent) || []).forEach((item) => {
      if (visited.has(item.node_token)) return;
      visited.add(item.node_token);
      result.push({ item, depth });
      visit(item.node_token, depth + 1);
    });
  }
  visit("", 0);
  items.forEach((item) => {
    if (!visited.has(item.node_token)) result.push({ item, depth: 0 });
  });
  return result;
}

function showWikiNodeList(updateHistory = true) {
  state.wikiView = "nodes";
  state.selectedWikiNode = null;
  const space = state.wikiSpaces.find((item) => item.space_id === state.selectedWikiSpace);
  $("wiki-title").textContent = space?.name || state.selectedWikiSpace || "请选择知识空间";
  $("wiki-node-heading").textContent = space?.name || state.selectedWikiSpace || "请选择左侧知识空间";
  $("wiki-eyebrow").textContent = "知识空间目录";
  $("wiki-document-meta").textContent = space?.description || "点击下方节点，在右侧区域阅读本地正文。";
  $("wiki-results").hidden = true;
  $("wiki-node-view").hidden = false;
  $("wiki-document").hidden = true;
  $("wiki-back").hidden = true;
  renderWikiNodes();
  if (updateHistory) writeWikiLocation();
}

async function selectWikiNode(nodeToken, updateHistory = true) {
  state.selectedWikiNode = nodeToken;
  state.wikiView = "document";
  renderWikiNodes();
  const root = $("wiki-document");
  $("wiki-results").hidden = true;
  $("wiki-node-view").hidden = true;
  root.hidden = false;
  $("wiki-back").hidden = false;
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
    if (updateHistory) writeWikiLocation(nodeToken);
  } catch (error) {
    root.innerHTML = '<div class="empty-state"><strong>读取失败</strong><span></span></div>';
    root.querySelector("span").textContent = error.message;
  }
}

async function searchWiki() {
  const query = $("wiki-query").value.trim();
  if (!query) {
    if (state.selectedWikiSpace) showWikiNodeList(false);
    return;
  }
  const params = new URLSearchParams({ q: query, limit: "100" });
  const data = await request(`/api/wiki/search?${params}`);
  const root = $("wiki-results");
  state.wikiView = "search";
  $("wiki-title").textContent = `搜索：${query}`;
  $("wiki-eyebrow").textContent = "本地知识库搜索";
  $("wiki-document-meta").textContent = "搜索结果来自已离线保存的标题和正文。";
  $("wiki-node-view").hidden = true;
  $("wiki-document").hidden = true;
  $("wiki-back").hidden = !state.selectedWikiSpace;
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
      if (state.selectedWikiSpace !== item.space_id) await selectWikiSpace(item.space_id, true, false);
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
$("mode-messages").addEventListener("click", () => { setMode("messages"); writeMessageLocation(); });
$("mode-wiki").addEventListener("click", async () => {
  setMode("wiki");
  if (state.selectedWikiSpace) showWikiNodeList(true);
  else if (state.wikiSpaces.length) await selectWikiSpace(state.wikiSpaces[0].space_id);
});
$("wiki-sync-now").addEventListener("click", startWikiSync);
$("wiki-search").addEventListener("click", searchWiki);
$("wiki-query").addEventListener("input", () => { if (state.wikiView === "nodes") renderWikiNodes(); });
$("wiki-query").addEventListener("keydown", (event) => { if (event.key === "Enter") searchWiki(); });
$("wiki-back").addEventListener("click", () => showWikiNodeList(true));

async function restoreLocation() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("mode") !== "wiki") {
    setMode("messages");
    return;
  }
  setMode("wiki");
  const requestedSpace = params.get("space_id");
  const spaceId = state.wikiSpaces.some((item) => item.space_id === requestedSpace)
    ? requestedSpace
    : state.wikiSpaces[0]?.space_id;
  if (!spaceId) return;
  await selectWikiSpace(spaceId, true, false);
  const nodeToken = params.get("node_token");
  if (nodeToken && state.wikiNodes.some((item) => item.node_token === nodeToken)) {
    await selectWikiNode(nodeToken, false);
  }
}

window.addEventListener("popstate", () => restoreLocation().catch((error) => {
  $("wiki-document-meta").textContent = error.message;
}));

async function initialize() {
  await Promise.all([loadStatus(), loadConversations(), loadSyncStatus(), loadWikiStatus(), loadWikiSpaces()]);
  await restoreLocation();
}

initialize().catch((error) => { $("archive-status").textContent = error.message; });
