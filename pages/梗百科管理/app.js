(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const state = {
    query: "",
    page: 1,
    pages: 1,
    total: 0,
    selected: "",
    originalTitle: "",
    polling: null,
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[char]));
  }

  function getBridge() {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    try {
      if (window.parent && window.parent !== window) return window.parent.AstrBotPluginPage || null;
    } catch (_) { return null; }
    return null;
  }

  async function bridge() {
    for (let index = 0; index < 150; index += 1) {
      const value = getBridge();
      if (value?.apiGet && value?.apiPost) {
        if (typeof value.ready === "function") await value.ready();
        return value;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("请从 AstrBot 插件管理页面打开此页面");
  }

  async function api(method, path, body = {}) {
    const client = await bridge();
    setConnection("ok", "已连接");
    const url = new URL(String(path || ""), "https://astrbot-plugin-page.local/");
    const endpoint = `page/${url.pathname.replace(/^\/+/, "")}`;
    const params = Object.fromEntries(url.searchParams.entries());
    const result = method === "GET"
      ? await client.apiGet(endpoint, Object.keys(params).length ? params : undefined)
      : await client.apiPost(endpoint, body);
    if (result?.status === "error") throw new Error(result.message || "请求失败");
    return result?.data ?? result;
  }

  function toast(message, kind = "success") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toastRegion").append(node);
    window.setTimeout(() => node.remove(), 3200);
  }

  function setConnection(kind, text) {
    const node = $("#connectionState");
    node.className = `status-chip ${kind}`;
    node.textContent = text;
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "—";
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function renderSync(sync) {
    const panel = $("#syncPanel");
    const current = sync || {};
    const visible = current.state && current.state !== "idle";
    panel.hidden = !visible;
    if (!visible) return;

    $("#syncPhase").textContent = current.phase || "准备更新";
    const badge = $("#syncBadge");
    badge.className = `sync-badge ${current.state}`;
    badge.textContent = current.state === "running" ? "运行中" : current.state === "done" ? "已完成" : "失败";
    let summary = `已检查 ${Number(current.scanned || 0)} 条最新词条`;
    let progress = current.state === "done" ? 100 : 8;
    if (current.pending > 0) {
      summary = `发现 ${current.pending} 条新增，已下载 ${current.fetched || 0} 条`;
      progress = Math.max(12, Math.min(92, (Number(current.fetched || 0) / current.pending) * 80 + 12));
    }
    if (current.state === "done") summary = `新增 ${Number(current.added || 0)} 条，本地修改均已保留`;
    if (current.state === "error") summary = current.error || "更新失败";
    $("#syncSummary").textContent = summary;
    $("#syncProgress").style.width = `${progress}%`;
    $("#syncLogs").innerHTML = (current.logs || []).map((line) => `<div>${escapeHtml(line)}</div>`).join("");
    $("#syncButton").disabled = current.state === "running";
    $("#syncButton").textContent = current.state === "running" ? "正在更新…" : "一键更新词库";
  }

  async function loadState() {
    const data = await api("GET", "state");
    $("#totalCount").textContent = Number(data.total || 0).toLocaleString("zh-CN");
    $("#builtAt").textContent = data.built_at || "未知";
    $("#dataSource").textContent = data.source || "cnmeme.wiki";
    $("#databaseSize").textContent = formatBytes(data.database_size);
    renderSync(data.sync);
    if (data.sync?.state === "running") startPolling();
    return data;
  }

  function entryPreview(item) {
    return item.summary || item.tags || item.aliases || "暂无摘要";
  }

  function renderEntries(data) {
    state.total = Number(data.total || 0);
    state.page = Number(data.page || 1);
    state.pages = Number(data.pages || 1);
    $("#resultSummary").textContent = state.query ? `找到 ${state.total} 条相关词条` : `共 ${state.total} 条，按名称排列`;
    $("#pageLabel").textContent = `${state.page} / ${state.pages}`;
    $("#previousPage").disabled = state.page <= 1;
    $("#nextPage").disabled = state.page >= state.pages;
    const list = $("#entryList");
    if (!data.items?.length) {
      list.innerHTML = `<div class="list-empty">没有匹配的词条<br />可以新建一条本地内容</div>`;
      return;
    }
    list.innerHTML = data.items.map((item) => `
      <button class="entry-item ${item.title === state.selected ? "active" : ""}" type="button" data-title="${escapeHtml(item.title)}" role="option" aria-selected="${item.title === state.selected}">
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(entryPreview(item))}</p>
        ${item.aliases ? `<small>别名：${escapeHtml(item.aliases)}</small>` : ""}
      </button>
    `).join("");
    list.querySelectorAll("[data-title]").forEach((button) => {
      button.addEventListener("click", () => selectEntry(button.dataset.title));
    });
  }

  async function loadEntries() {
    const params = new URLSearchParams({ query: state.query, page: String(state.page), page_size: "30" });
    renderEntries(await api("GET", `entries?${params}`));
  }

  function showEditor(entry, isNew = false) {
    $("#emptyEditor").hidden = true;
    $("#entryForm").hidden = false;
    state.originalTitle = isNew ? "" : entry.title;
    state.selected = isNew ? "" : entry.title;
    $("#editorMode").textContent = isNew ? "新建词条" : "编辑词条";
    $("#editorTitle").textContent = entry.title || "未命名词条";
    $("#deleteButton").hidden = isNew;
    for (const key of ["Title", "Slug", "Tags", "Aliases", "Summary", "Usage", "Origin", "Other"]) {
      $(`#field${key}`).value = entry[key.toLowerCase()] || "";
    }
    $("#fieldTitle").focus();
    loadEntries().catch(() => {});
  }

  function emptyEntry() {
    return { title: "", slug: "", tags: "", aliases: "", summary: "", usage: "", origin: "", other: "" };
  }

  async function selectEntry(title) {
    try {
      const data = await api("GET", `entry?title=${encodeURIComponent(title)}`);
      showEditor(data.entry, false);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function collectEntry() {
    const result = {};
    for (const key of ["Title", "Slug", "Tags", "Aliases", "Summary", "Usage", "Origin", "Other"]) {
      result[key.toLowerCase()] = $(`#field${key}`).value.trim();
    }
    return result;
  }

  async function saveEntry(event) {
    event.preventDefault();
    const button = $("#saveButton");
    button.disabled = true;
    try {
      const data = await api("POST", "entry/save", { original_title: state.originalTitle, entry: collectEntry() });
      state.selected = data.entry.title;
      state.originalTitle = data.entry.title;
      $("#editorMode").textContent = "编辑词条";
      $("#editorTitle").textContent = data.entry.title;
      $("#deleteButton").hidden = false;
      toast(data.created ? "词条已新建" : "修改已保存");
      await Promise.all([loadEntries(), loadState()]);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function deleteEntry() {
    if (!state.originalTitle) return;
    if (!window.confirm(`确定删除「${state.originalTitle}」吗？此操作不会自动恢复。`)) return;
    try {
      await api("POST", "entry/delete", { title: state.originalTitle });
      toast("词条已删除");
      state.selected = "";
      state.originalTitle = "";
      $("#entryForm").hidden = true;
      $("#emptyEditor").hidden = false;
      await Promise.all([loadEntries(), loadState()]);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function startPolling() {
    if (state.polling) return;
    state.polling = window.setInterval(async () => {
      try {
        const data = await loadState();
        if (data.sync?.state !== "running") {
          window.clearInterval(state.polling);
          state.polling = null;
          await loadEntries();
          if (data.sync?.state === "done") toast(`词库更新完成，新增 ${data.sync.added || 0} 条`);
        }
      } catch (_) { /* 下一轮继续 */ }
    }, 1500);
  }

  async function startSync() {
    const button = $("#syncButton");
    button.disabled = true;
    try {
      const data = await api("POST", "sync/start");
      renderSync(data.sync);
      startPolling();
      toast("已开始检查社区词库更新");
    } catch (error) {
      button.disabled = false;
      toast(error.message, "error");
    }
  }

  function bindEvents() {
    let timer = null;
    $("#searchInput").addEventListener("input", (event) => {
      state.query = event.target.value.trim();
      state.page = 1;
      $("#clearSearch").hidden = !state.query;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => loadEntries().catch((error) => toast(error.message, "error")), 250);
    });
    $("#clearSearch").addEventListener("click", () => {
      $("#searchInput").value = "";
      state.query = "";
      state.page = 1;
      $("#clearSearch").hidden = true;
      loadEntries().catch((error) => toast(error.message, "error"));
    });
    $("#refreshButton").addEventListener("click", () => Promise.all([loadState(), loadEntries()]).catch((error) => toast(error.message, "error")));
    $("#previousPage").addEventListener("click", () => { state.page -= 1; loadEntries(); });
    $("#nextPage").addEventListener("click", () => { state.page += 1; loadEntries(); });
    $("#newButton").addEventListener("click", () => showEditor(emptyEntry(), true));
    $("#emptyNewButton").addEventListener("click", () => showEditor(emptyEntry(), true));
    $("#entryForm").addEventListener("submit", saveEntry);
    $("#deleteButton").addEventListener("click", deleteEntry);
    $("#syncButton").addEventListener("click", startSync);
    $("#fieldTitle").addEventListener("input", (event) => { $("#editorTitle").textContent = event.target.value.trim() || "未命名词条"; });
    window.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && !$("#entryForm").hidden) {
        event.preventDefault();
        $("#entryForm").requestSubmit();
      }
    });
  }

  async function init() {
    bindEvents();
    try {
      await bridge();
      setConnection("ok", "已连接");
      const results = await Promise.allSettled([loadState(), loadEntries()]);
      results.forEach((result) => {
        if (result.status === "rejected") toast(result.reason?.message || "页面数据载入失败", "error");
      });
    } catch (error) {
      setConnection("error", "连接失败");
      toast(error.message, "error");
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
