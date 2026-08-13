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
  mailFolders: [],
  mailMessages: [],
  selectedMailFolder: "",
  selectedMailMessage: null,
  mailPage: 1,
  mailPageSize: 30,
  mailHasMore: false,
  mailTotal: 0,
  mailArchiveTotal: 0,
  mailLoaded: false,
  mailSyncWasRunning: false,
  mailPollTimer: null,
  insightsLoaded: false,
};
const $ = (id) => document.getElementById(id);

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = {};
    }
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败：${response.status}`);
    error.status = response.status;
    throw error;
  }
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
  if (!["messages", "wiki", "mail", "insights"].includes(mode)) mode = "messages";
  state.mode = mode;
  const messages = mode === "messages";
  const wiki = mode === "wiki";
  const mail = mode === "mail";
  const insights = mode === "insights";
  $("message-sidebar").hidden = !messages;
  $("wiki-sidebar").hidden = !wiki;
  $("mail-sidebar").hidden = !mail;
  $("insights-sidebar").hidden = !insights;
  $("message-view").hidden = !messages;
  $("wiki-view").hidden = !wiki;
  $("mail-view").hidden = !mail;
  $("insights-view").hidden = !insights;
  $("mode-messages").classList.toggle("active", messages);
  $("mode-wiki").classList.toggle("active", wiki);
  $("mode-mail").classList.toggle("active", mail);
  $("mode-insights").classList.toggle("active", insights);
}

function yesterdayIso() {
  const value = new Date();
  value.setDate(value.getDate() - 1);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function renderInsightItems(rootId, items) {
  const root = $(rootId);
  root.replaceChildren();
  if (!Array.isArray(items) || !items.length) {
    const empty = document.createElement("p");
    empty.className = "insights-item";
    empty.textContent = "暂无可验证结论。";
    root.append(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("p");
    row.className = "insights-item";
    const summary = document.createElement("span");
    summary.textContent = item.summary || "";
    row.append(summary);
    const labels = [];
    const categoryLabels = {
      committed: "已承诺",
      project_followup: "项目跟进",
      ai_recommendation: "AI 建议",
      carryover: "累计待办",
    };
    const strengthLabels = {
      confirmed: "已确认机会",
      qualification: "待核实机会",
      weak: "弱信号",
    };
    if (categoryLabels[item.category]) labels.push(categoryLabels[item.category]);
    if (strengthLabels[item.strength]) labels.push(strengthLabels[item.strength]);
    if (item.status) labels.push(`状态：${item.status}`);
    if (labels.length) {
      const semantics = document.createElement("span");
      semantics.className = "insights-semantics";
      semantics.textContent = labels.join(" · ");
      row.append(semantics);
    }
    if (Array.isArray(item.evidence_gaps) && item.evidence_gaps.length) {
      const gaps = document.createElement("span");
      gaps.className = "insights-semantics";
      gaps.textContent = `证据缺口：${item.evidence_gaps.join("；")}`;
      row.append(gaps);
    }
    if (item.next_validation_step) {
      const next = document.createElement("span");
      next.className = "insights-semantics";
      next.textContent = `下一步核实：${item.next_validation_step}`;
      row.append(next);
    }
    const citations = Array.isArray(item.citations) ? item.citations.filter(Boolean) : [];
    if (citations.length) {
      const evidence = document.createElement("span");
      evidence.className = "insights-citations";
      evidence.textContent = `证据：${citations.join(" · ")}`;
      row.append(evidence);
    }
    root.append(row);
  });
}

async function loadInsights(updateHistory = true) {
  const reportDate = $("insights-date").value || yesterdayIso();
  $("insights-date").value = reportDate;
  $("insights-meta").textContent = "正在从本机洞察数据库读取…";
  try {
    const data = await request(`/api/insights/daily?date=${encodeURIComponent(reportDate)}`);
    const run = data.item || {};
    const report = run.report || run;
    const counts = report.coverage?.counts || {};
    const ledger = report.task_ledger || {};
    $("insights-title").textContent = `每日洞察 · ${report.report_date || reportDate}`;
    $("insights-meta").textContent = `${report.timezone || ""} · 模型 ${report.model || "未记录"} · 状态 ${report.model_status || run.status || "unknown"}`;
    const ledgerText = ledger.historical_backfill_complete
      ? `累计待办已回填：${ledger.coverage_start || ""} 至 ${ledger.coverage_end || ""}。`
      : `累计待办仅覆盖已成功日报；全历史回填尚未完成。`;
    $("insights-coverage").textContent = `覆盖：聊天 ${counts.chat || 0} 条；收到邮件 ${counts.mail_received || 0} 封；发出邮件 ${counts.mail_sent || 0} 封；知识库新增 ${counts.wiki_created || 0} 篇、编辑 ${counts.wiki_edited || 0} 篇。${ledgerText}`;
    renderInsightItems("insights-yesterday", report.yesterday_summary);
    renderInsightItems("insights-today", report.today_plan);
    renderInsightItems("insights-opportunities", report.commercial_opportunities);
    const publication = report.published === false ? "未发布（部分结果）" : "已发布";
    $("insights-status").textContent = `${publication}；当前显示 ${report.report_date || reportDate}`;
    state.insightsLoaded = true;
    if (updateHistory) {
      const url = new URL(window.location.href);
      url.search = "";
      url.searchParams.set("mode", "insights");
      url.searchParams.set("date", reportDate);
      history.pushState({}, "", url);
    }
  } catch (error) {
    const message = mailAccessMessage(error);
    $("insights-status").textContent = message;
    $("insights-meta").textContent = message;
    $("insights-coverage").textContent = "日报不可用；三条源档案不会因此受影响。";
    renderInsightItems("insights-yesterday", []);
    renderInsightItems("insights-today", []);
    renderInsightItems("insights-opportunities", []);
  }
}

function writeWikiLocation(nodeToken = null, replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "wiki");
  url.searchParams.delete("folder_id");
  url.searchParams.delete("message_id");
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
  url.searchParams.delete("folder_id");
  url.searchParams.delete("message_id");
  history.pushState({}, "", url);
}

function writeMailLocation(messageId = state.selectedMailMessage, replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "mail");
  url.searchParams.delete("space_id");
  url.searchParams.delete("node_token");
  if (state.selectedMailFolder) url.searchParams.set("folder_id", state.selectedMailFolder);
  else url.searchParams.delete("folder_id");
  if (messageId) url.searchParams.set("message_id", messageId);
  else url.searchParams.delete("message_id");
  history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function mailAccessMessage(error) {
  return error?.status === 401
    ? "邮箱尚未解锁。请运行 feishu-archive mail-reader-url --open 临时解锁，或使用 --permanent --open 永久解除本机锁定。"
    : error?.message || "邮件档案暂时不可用。";
}

function renderMailEmpty(root, title, detail) {
  root.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "mail-empty-state";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const description = document.createElement("span");
  description.textContent = detail;
  empty.append(heading, description);
  root.append(empty);
}

async function unlockMailSessionFromHash() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const unlockToken = fragment.get("mail-unlock");
  if (!unlockToken) return false;
  try {
    await request("/api/mail/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Feishu-Archive-Action": "mail-session",
      },
      body: JSON.stringify({ unlock_token: unlockToken }),
    });
    fragment.delete("mail-unlock");
    const url = new URL(window.location.href);
    url.hash = fragment.toString();
    url.searchParams.set("mode", "mail");
    history.replaceState({}, "", url);
    return true;
  } catch (error) {
    const message = mailAccessMessage(error);
    $("mail-status").textContent = message;
    $("mail-sync-status").textContent = message;
    return false;
  }
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

function mailFolderId(item) {
  return String(item?.folder_id ?? item?.id ?? "");
}

function mailMessageId(item) {
  return String(item?.id ?? item?.message_id ?? item?.provider_message_id ?? "");
}

function formatMailTime(value) {
  if (!value) return "时间未知";
  try {
    return typeof value === "number" || /^\d+$/.test(String(value))
      ? formatSourceTime(value)
      : formatTime(value);
  } catch {
    return String(value);
  }
}

function mailAddressText(value) {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(mailAddressText).filter(Boolean).join("、");
  if (typeof value !== "object") return String(value);
  const name = value.name || value.display_name || value.displayName || "";
  const address = value.address || value.email || value.mail_address || "";
  if (name && address && name !== address) return `${name} <${address}>`;
  return String(name || address || "");
}

function mailAddresses(item, role) {
  const candidates = [item?.[role], item?.[`head_${role}`], item?.[`${role}_addresses`]];
  if (role === "from") {
    candidates.push(item?.from_address);
    if (!candidates.some((value) => mailAddressText(value))) {
      candidates.push({
        name: item?.sender_name || item?.from_name,
        address: item?.sender_address,
      });
    }
  }
  const direct = candidates.map(mailAddressText).find(Boolean);
  if (direct) return direct;
  const recipients = Array.isArray(item?.recipients) ? item.recipients : [];
  return recipients
    .filter((recipient) => String(recipient.role || recipient.type || "").toLowerCase() === role)
    .map(mailAddressText)
    .filter(Boolean)
    .join("、");
}

function mailSenderLabel(item) {
  return item.sender_name || item.from_name || mailAddresses(item, "from") || "未知发件人";
}

function mailSentTime(item) {
  return item.sent_at || item.received_at || item.create_time || item.created_at || item.date;
}

function mailFolderName(item) {
  const labels = {
    inbox: "收件箱",
    sent: "已发送",
    drafts: "草稿",
    trash: "已删除",
    spam: "垃圾邮件",
    archive: "归档",
  };
  const type = String(item?.folder_type || item?.type || "").toLowerCase();
  return item?.name || item?.display_name || labels[type] || mailFolderId(item) || "未命名文件夹";
}

function renderMailFolderButton(root, id, name, meta) {
  const button = document.createElement("button");
  button.className = `mail-folder${id === state.selectedMailFolder ? " active" : ""}`;
  const title = document.createElement("span");
  title.textContent = name;
  const detail = document.createElement("small");
  detail.textContent = meta;
  button.append(title, detail);
  button.addEventListener("click", () => selectMailFolder(id));
  root.append(button);
}

function renderMailFolders() {
  const root = $("mail-folder-list");
  root.replaceChildren();
  renderMailFolderButton(root, "", "全部邮件", `${state.mailArchiveTotal || 0} 封本地邮件`);
  state.mailFolders.forEach((item) => {
    const count = item.message_count ?? item.total_count ?? item.messages ?? 0;
    const unread = item.unread_count ? ` · ${item.unread_count} 封未读` : "";
    renderMailFolderButton(root, mailFolderId(item), mailFolderName(item), `${count} 封${unread}`);
  });
}

async function loadMailStatus() {
  clearTimeout(state.mailPollTimer);
  try {
    const data = await request("/api/mail/status");
    const mailbox = data.mailbox || data.account || {};
    const address = mailbox.address || mailbox.email || data.mailbox_address || "";
    const messages = data.messages ?? data.message_count ?? data.total_messages ?? 0;
    const attachments = data.attachments ?? data.attachment_count ?? 0;
    const bytes = data.attachment_bytes ?? data.resource_bytes ?? data.blob_bytes ?? 0;
    const prefix = address ? `${address} · ` : "";
    state.mailArchiveTotal = Number(messages) || 0;
    $("mail-status").textContent = `${prefix}${messages} 封邮件 · ${attachments} 个附件 · 占用 ${formatBytes(bytes)}`;

    const job = data.latest_sync || data.job || data.sync || null;
    const running = job?.status === "running";
    const schedule = data.schedule?.description || data.schedule_description || "自动同步未配置";
    const button = $("mail-sync-now");
    button.disabled = running;
    button.textContent = running ? "同步中…" : "同步邮箱";
    if (!job) {
      $("mail-sync-status").textContent = schedule;
    } else if (running) {
      $("mail-sync-status").textContent = `邮件同步进行中 · ${schedule}`;
    } else {
      const labels = { success: "成功", partial: "部分完成", error: "失败" };
      const detail = job.error ? ` · ${String(job.error).split("\n")[0]}` : "";
      $("mail-sync-status").textContent = `上次同步${labels[job.status] || job.status}：${formatMailTime(job.finished_at)}${detail} · ${schedule}`;
    }
    if (state.mailSyncWasRunning && !running) {
      await loadMailFolders(true);
      if (state.mode === "mail") {
        await loadMailMessages({ selectFirst: true, updateHistory: false });
      }
    }
    state.mailSyncWasRunning = running;
    state.mailPollTimer = setTimeout(loadMailStatus, running ? 2000 : 60000);
  } catch (error) {
    const message = mailAccessMessage(error);
    $("mail-status").textContent = message;
    $("mail-sync-status").textContent = message;
    $("mail-sync-now").disabled = error.status === 401;
    state.mailLoaded = false;
    if (error.status === 401) {
      state.mailMessages = [];
      state.selectedMailMessage = null;
      renderMailEmpty($("mail-folder-list"), "邮箱已锁定", message);
      renderMailEmpty($("mail-message-list"), "邮箱已锁定", message);
      renderMailEmpty($("mail-detail"), "邮箱已锁定", message);
    }
    state.mailPollTimer = setTimeout(loadMailStatus, 60000);
  }
}

async function startMailSync() {
  const button = $("mail-sync-now");
  button.disabled = true;
  button.textContent = "正在启动…";
  $("mail-sync-status").textContent = "正在启动本机邮件同步任务…";
  try {
    await request("/api/mail/sync", {
      method: "POST",
      headers: { "X-Feishu-Archive-Action": "mail-sync" },
    });
    state.mailSyncWasRunning = true;
    await loadMailStatus();
  } catch (error) {
    $("mail-sync-status").textContent = mailAccessMessage(error);
    button.disabled = error.status === 401;
    button.textContent = "同步邮箱";
  }
}

async function loadMailFolders(keepSelection = false) {
  try {
    const data = await request("/api/mail/folders");
    state.mailFolders = Array.isArray(data.items) ? data.items : (Array.isArray(data.folders) ? data.folders : []);
    if (!keepSelection || !state.mailFolders.some((item) => mailFolderId(item) === state.selectedMailFolder)) {
      state.selectedMailFolder = "";
    }
    renderMailFolders();
    state.mailLoaded = true;
    return true;
  } catch (error) {
    const message = mailAccessMessage(error);
    const root = $("mail-folder-list");
    renderMailEmpty(root, error.status === 401 ? "邮箱已锁定" : "无法读取文件夹", message);
    $("mail-status").textContent = message;
    state.mailLoaded = false;
    return false;
  }
}

function renderMailMessages() {
  const root = $("mail-message-list");
  root.replaceChildren();
  if (!state.mailMessages.length) {
    renderMailEmpty(root, "没有符合条件的邮件", "这不代表飞书邮箱中不存在数据，也可能受授权范围或同步时间范围限制。");
    return;
  }
  state.mailMessages.forEach((item) => {
    const id = mailMessageId(item);
    const button = document.createElement("button");
    const unread = item.unread === true || item.is_read === false;
    button.className = `mail-message${unread ? " unread" : ""}${id === state.selectedMailMessage ? " active" : ""}`;
    const head = document.createElement("span");
    head.className = "mail-message-head";
    const sender = document.createElement("strong");
    sender.textContent = mailSenderLabel(item);
    const time = document.createElement("time");
    time.textContent = formatMailTime(mailSentTime(item));
    head.append(sender, time);
    const subject = document.createElement("span");
    subject.className = "mail-message-subject";
    subject.textContent = item.subject || "（无主题）";
    const excerpt = document.createElement("span");
    excerpt.className = "mail-message-excerpt";
    excerpt.textContent = String(item.snippet || item.body_preview || item.body_plain_text || "").replace(/\s+/g, " ").slice(0, 180);
    button.append(head, subject);
    if (excerpt.textContent) button.append(excerpt);
    const attachmentCount = item.attachment_count ?? (Array.isArray(item.attachments) ? item.attachments.length : 0);
    if (attachmentCount) {
      const badge = document.createElement("small");
      badge.className = "mail-message-attachment";
      badge.textContent = `${attachmentCount} 个附件`;
      button.append(badge);
    }
    button.addEventListener("click", () => selectMailMessage(id));
    root.append(button);
  });
}

async function loadMailMessages({ selectFirst = true, updateHistory = true } = {}) {
  const params = new URLSearchParams({
    page: String(state.mailPage),
    page_size: String(state.mailPageSize),
  });
  const query = $("mail-query").value.trim();
  if (query) params.set("q", query);
  if (state.selectedMailFolder) params.set("folder_id", state.selectedMailFolder);
  const root = $("mail-message-list");
  renderMailEmpty(root, "正在读取邮件…", "正在从本机邮件档案加载。" );
  try {
    const data = await request(`/api/mail/messages?${params}`);
    state.mailMessages = Array.isArray(data.items) ? data.items : (Array.isArray(data.messages) ? data.messages : []);
    state.mailPage = Number(data.page || data.pagination?.page || state.mailPage) || 1;
    const explicitTotal = data.total ?? data.total_count ?? data.pagination?.total;
    if (explicitTotal !== undefined) state.mailTotal = Number(explicitTotal) || 0;
    else state.mailTotal = (state.mailPage - 1) * state.mailPageSize + state.mailMessages.length;
    const explicitHasMore = data.has_more ?? data.pagination?.has_more;
    state.mailHasMore = typeof explicitHasMore === "boolean"
      ? explicitHasMore
      : explicitTotal !== undefined
        ? state.mailPage * state.mailPageSize < Number(explicitTotal)
        : state.mailMessages.length === state.mailPageSize;
    const shown = state.mailMessages.length;
    $("mail-result-count").textContent = explicitTotal !== undefined
      ? `共 ${state.mailTotal} 封邮件`
      : `本页 ${shown} 封邮件`;
    $("mail-page-label").textContent = `第 ${state.mailPage} 页`;
    $("mail-prev").disabled = state.mailPage <= 1;
    $("mail-next").disabled = !state.mailHasMore;
    renderMailFolders();
    renderMailMessages();
    if (!selectFirst || !state.mailMessages.length) {
      if (!state.mailMessages.length) {
        state.selectedMailMessage = null;
        renderMailEmpty($("mail-detail"), "请选择一封邮件", "邮件 HTML 不会在此处执行或渲染。");
        if (updateHistory) writeMailLocation(null);
      }
      return true;
    }
    const selected = state.mailMessages.find((item) => mailMessageId(item) === state.selectedMailMessage);
    await selectMailMessage(mailMessageId(selected || state.mailMessages[0]), updateHistory);
    return true;
  } catch (error) {
    const message = mailAccessMessage(error);
    if (error.status === 401) state.mailLoaded = false;
    renderMailEmpty(root, error.status === 401 ? "邮箱已锁定" : "邮件读取失败", message);
    renderMailEmpty($("mail-detail"), "无法显示邮件", message);
    $("mail-result-count").textContent = "0 封邮件";
    $("mail-prev").disabled = true;
    $("mail-next").disabled = true;
    return false;
  }
}

function appendMailAddressRow(root, label, value) {
  if (!value) return;
  const row = document.createElement("div");
  row.className = "mail-address-row";
  const name = document.createElement("strong");
  name.textContent = label;
  const address = document.createElement("span");
  address.textContent = value;
  row.append(name, address);
  root.append(row);
}

function renderMailDetail(item) {
  const root = $("mail-detail");
  root.replaceChildren();
  const header = document.createElement("header");
  header.className = "mail-detail-header";
  const heading = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = item.folder_name || item.folder || "本机邮件档案";
  const subject = document.createElement("h3");
  subject.textContent = item.subject || "（无主题）";
  heading.append(eyebrow, subject);
  const time = document.createElement("time");
  time.textContent = formatMailTime(mailSentTime(item));
  header.append(heading, time);
  root.append(header);

  const addresses = document.createElement("section");
  addresses.className = "mail-addresses";
  appendMailAddressRow(addresses, "发件人", mailAddresses(item, "from"));
  appendMailAddressRow(addresses, "收件人", mailAddresses(item, "to"));
  appendMailAddressRow(addresses, "抄送", mailAddresses(item, "cc"));
  appendMailAddressRow(addresses, "密送", mailAddresses(item, "bcc"));
  if (addresses.childElementCount) root.append(addresses);

  const bodySection = document.createElement("section");
  bodySection.className = "mail-body-section";
  const bodyHeading = document.createElement("h4");
  bodyHeading.textContent = "纯文本正文";
  const body = document.createElement("pre");
  body.className = "mail-plain-body";
  body.textContent = String(item.body_plain_text ?? item.body_text ?? item.text ?? "本地尚无可显示的纯文本正文。");
  bodySection.append(bodyHeading, body);
  root.append(bodySection);

  const attachments = Array.isArray(item.attachments) ? item.attachments : [];
  if (attachments.length) {
    const section = document.createElement("section");
    section.className = "mail-attachments";
    const attachmentHeading = document.createElement("h4");
    attachmentHeading.textContent = `附件（${attachments.length}）`;
    section.append(attachmentHeading);
    attachments.forEach((attachment) => {
      const id = attachment.id ?? attachment.attachment_id;
      const status = String(attachment.status || "").toLowerCase();
      const blocked = ["pending", "skipped", "error", "failed", "metadata_only"].includes(status);
      const quarantined = status === "quarantined";
      const element = document.createElement(id !== undefined && !blocked ? "a" : "span");
      element.className = "mail-attachment";
      if (quarantined) element.classList.add("quarantined");
      const filename = attachment.filename || attachment.name || "未命名附件";
      const size = attachment.byte_size ?? attachment.size;
      element.textContent = `${filename}${size ? ` · ${formatBytes(size)}` : ""}${blocked ? ` · ${status || "不可下载"}` : ""}${quarantined ? " · 风险格式，下载前需确认" : ""}`;
      if (element instanceof HTMLAnchorElement) {
        element.href = `/api/mail/attachments/${encodeURIComponent(id)}`;
        element.download = filename;
        if (quarantined) {
          element.addEventListener("click", (event) => {
            event.preventDefault();
            const accepted = window.confirm(
              `“${filename}”可能包含脚本、宏或其他主动内容。仅在信任发件人和文件来源时下载。是否继续？`,
            );
            if (!accepted) return;
            const download = document.createElement("a");
            download.href = `/api/mail/attachments/${encodeURIComponent(id)}?confirm=1`;
            download.download = filename;
            document.body.append(download);
            download.click();
            download.remove();
          });
        }
      }
      section.append(element);
    });
    root.append(section);
  }
}

async function selectMailMessage(messageId, updateHistory = true) {
  if (!messageId) return;
  state.selectedMailMessage = String(messageId);
  renderMailMessages();
  renderMailEmpty($("mail-detail"), "正在读取邮件…", "正在从本机邮件档案加载纯文本正文。" );
  try {
    const data = await request(`/api/mail/messages/${encodeURIComponent(messageId)}`);
    const item = data.item || data.message || data;
    renderMailDetail(item);
    $("mail-title").textContent = item.subject || "（无主题）";
    $("mail-view-meta").textContent = `${mailSenderLabel(item)} · ${formatMailTime(mailSentTime(item))} · 正文仅以纯文本显示`;
    if (updateHistory) writeMailLocation(messageId);
  } catch (error) {
    renderMailEmpty($("mail-detail"), error.status === 401 ? "邮箱已锁定" : "邮件读取失败", mailAccessMessage(error));
  }
}

async function selectMailFolder(folderId, updateHistory = true) {
  state.selectedMailFolder = String(folderId || "");
  state.selectedMailMessage = null;
  state.mailPage = 1;
  renderMailFolders();
  const folder = state.mailFolders.find((item) => mailFolderId(item) === state.selectedMailFolder);
  $("mail-title").textContent = folder ? mailFolderName(folder) : "全部邮件";
  $("mail-view-meta").textContent = "正文以纯文本显示，附件仅提供本地下载。";
  await loadMailMessages({ selectFirst: true, updateHistory });
}

async function ensureMailLoaded(updateHistory = true) {
  if (!state.mailLoaded) {
    const loaded = await loadMailFolders(true);
    if (!loaded) return false;
  }
  return loadMailMessages({ selectFirst: true, updateHistory });
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
$("mode-mail").addEventListener("click", async () => {
  setMode("mail");
  const loaded = await ensureMailLoaded(true);
  if (!loaded) writeMailLocation(null);
});
$("mode-insights").addEventListener("click", async () => {
  setMode("insights");
  if (!$("insights-date").value) $("insights-date").value = yesterdayIso();
  await loadInsights(true);
});
$("insights-load").addEventListener("click", () => loadInsights(true));
$("wiki-sync-now").addEventListener("click", startWikiSync);
$("wiki-search").addEventListener("click", searchWiki);
$("wiki-query").addEventListener("input", () => { if (state.wikiView === "nodes") renderWikiNodes(); });
$("wiki-query").addEventListener("keydown", (event) => { if (event.key === "Enter") searchWiki(); });
$("wiki-back").addEventListener("click", () => showWikiNodeList(true));
$("mail-sync-now").addEventListener("click", startMailSync);
$("mail-search").addEventListener("click", async () => {
  state.mailPage = 1;
  state.selectedMailMessage = null;
  await loadMailMessages({ selectFirst: true, updateHistory: true });
});
$("mail-query").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  state.mailPage = 1;
  state.selectedMailMessage = null;
  await loadMailMessages({ selectFirst: true, updateHistory: true });
});
$("mail-prev").addEventListener("click", async () => {
  if (state.mailPage <= 1) return;
  state.mailPage -= 1;
  state.selectedMailMessage = null;
  await loadMailMessages({ selectFirst: true, updateHistory: true });
});
$("mail-next").addEventListener("click", async () => {
  if (!state.mailHasMore) return;
  state.mailPage += 1;
  state.selectedMailMessage = null;
  await loadMailMessages({ selectFirst: true, updateHistory: true });
});

async function restoreLocation() {
  const params = new URLSearchParams(window.location.search);
  const mode = params.get("mode");
  if (mode === "insights") {
    setMode("insights");
    $("insights-date").value = params.get("date") || yesterdayIso();
    await loadInsights(false);
    return;
  }
  if (mode === "mail") {
    setMode("mail");
    if (!state.mailLoaded) {
      const loaded = await loadMailFolders(true);
      if (!loaded) return;
    }
    const requestedFolder = params.get("folder_id") || "";
    state.selectedMailFolder = state.mailFolders.some((item) => mailFolderId(item) === requestedFolder)
      ? requestedFolder
      : "";
    state.mailPage = 1;
    renderMailFolders();
    const folder = state.mailFolders.find((item) => mailFolderId(item) === state.selectedMailFolder);
    $("mail-title").textContent = folder ? mailFolderName(folder) : "全部邮件";
    const messageId = params.get("message_id");
    const loaded = await loadMailMessages({ selectFirst: !messageId, updateHistory: false });
    if (loaded && messageId) await selectMailMessage(messageId, false);
    return;
  }
  if (mode !== "wiki") {
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
  if (state.mode === "mail") $("mail-view-meta").textContent = mailAccessMessage(error);
  else $("wiki-document-meta").textContent = error.message;
}));

async function initialize() {
  await unlockMailSessionFromHash();
  await Promise.all([loadStatus(), loadConversations(), loadSyncStatus(), loadWikiStatus(), loadWikiSpaces(), loadMailStatus()]);
  await restoreLocation();
}

initialize().catch((error) => { $("archive-status").textContent = error.message; });
