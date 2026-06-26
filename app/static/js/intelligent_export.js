(function () {
  "use strict";
  const root = document.getElementById("ie-app");
  if (!root) return;
  const API = root.dataset.api;
  const state = {
    conversationId: null,
    planVersionId: null,
    preview: null,
    health: null,
    chatPrompts: [],
    jsonVisible: false,
  };

  const $ = (id) => document.getElementById(id);

  let planModal = null;
  let previewModal = null;

  const ERROR_MESSAGES = {
    NO_DATA: "В подключённой базе нет данных CRM. Сначала выполните импорт — затем повторите.",
    CATALOG_EMPTY: "Каталог полей пуст — нечего выгружать. Проверьте импорт метаданных CRM.",
    IMPORT_STALE: "Данные импорта устарели — полная выгрузка заблокирована. Обновите импорт.",
    AI_UNAVAILABLE: "AI планировщик недоступен или не ответил вовремя. Попробуйте позже или уточните запрос.",
    PLAN_INVALID: "План не прошёл проверку. См. список ошибок ниже.",
    PLAN_VERSION_CONFLICT: "Версия плана изменилась (конфликт). Обновите страницу.",
    QUERY_TIMEOUT: "Запрос превысил лимит времени. Упростите план или уменьшите объём данных.",
    PREVIEW_FAILED: "Не удалось построить предпросмотр. Проверьте план и данные.",
    EXPORT_NOT_READY: "Файл ещё не готов — дождитесь завершения выгрузки.",
    CONVERSATION_NOT_FOUND: "Диалог не найден. Обновите страницу.",
    PLAN_NOT_FOUND: "План или шаблон не найден.",
    PROMPT_NOT_FOUND: "Готовый промпт не найден.",
    RUN_NOT_FOUND: "Выгрузка не найдена.",
    ACCESS_DENIED: "Недостаточно прав для этого действия.",
  };

  function humanError(e) {
    if (e && e.detail && e.detail.code && ERROR_MESSAGES[e.detail.code]) {
      return ERROR_MESSAGES[e.detail.code];
    }
    return (e && (e.message || (e.detail && e.detail.message))) || "Произошла ошибка";
  }

  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : { code: res.status, message: "Ошибка" };
      throw Object.assign(new Error(detail.message || "Ошибка"), { detail, status: res.status });
    }
    return body;
  }

  function isExportBlocked() {
    const h = state.health;
    if (!h || !h.has_data) return true;
    return !(h.sync_state && h.sync_state.export_allowed);
  }

  function exportBlockedTip() {
    const h = state.health;
    if (!h || !h.has_data) return "Нет данных для выгрузки — сначала импортируйте CRM";
    if (!(h.sync_state && h.sync_state.export_allowed)) return "Выгрузка заблокирована: импорт устарел";
    return "";
  }

  // --- conversations ---
  async function loadChatPrompts() {
    try {
      const data = await api("/chat-prompts");
      state.chatPrompts = data.prompts || [];
    } catch (e) {
      state.chatPrompts = [];
    }
  }

  function promptTooltip(p) {
    const checks = (p.preview_checks || []).join("; ");
    return (p.purpose || "") + (checks ? " Проверить в preview: " + checks + "." : "");
  }

  function appendConvItem(list, text, opts) {
    const li = document.createElement("li");
    let cls = "ie-conv-item";
    if (opts.active) cls += " active";
    if (opts.starter) cls += " ie-prompt-starter";
    li.className = cls;
    li.textContent = text;
    if (opts.title) li.title = opts.title;
    li.addEventListener("click", opts.onClick);
    list.appendChild(li);
  }

  async function loadConversations() {
    const convData = await api("/conversations");
    const list = $("ie-conv-list");
    list.innerHTML = "";

    if (!convData.conversations.length) {
      const empty = document.createElement("li");
      empty.className = "ie-conv-empty";
      empty.textContent = "Нет диалогов";
      list.appendChild(empty);
      return convData;
    }

    convData.conversations.forEach((c) => {
      appendConvItem(list, c.title, {
        active: c.id === state.conversationId,
        onClick: () => selectConversation(c.id),
      });
    });
    return convData;
  }

  async function startFromPrompt(promptId) {
    try {
      const resp = await api("/conversations/from-prompt/" + encodeURIComponent(promptId), { method: "POST" });
      state.conversationId = resp.conversation_id;
      await loadConversations();
      applyChatResponse(resp);
      await loadMessages();
      if (resp.follow_up_prompt) {
        const input = $("ie-chat-input");
        input.value = resp.follow_up_prompt;
        input.placeholder = "Шаг 2 — отправьте уточнение";
        resizeComposer();
        input.focus();
      }
    } catch (e) {
      alert(humanError(e));
    }
  }

  $("ie-new-conv").addEventListener("click", async () => {
    const c = await api("/conversations", { method: "POST", body: JSON.stringify({ title: "Новый диалог" }) });
    state.conversationId = c.id;
    await loadConversations();
    await selectConversation(c.id);
  });

  async function selectConversation(id) {
    state.conversationId = id;
    await loadConversations();
    await loadMessages();
    const conv = await api("/conversations/" + id);
    if (conv.current_plan_version_id) {
      await loadPlanVersion(conv.current_plan_version_id);
    } else {
      state.planVersionId = null;
    }
  }

  function messagesBox() {
    return $("ie-messages");
  }

  function showEmptyState() {
    const box = messagesBox();
    let html = '<div class="ie-empty-state"><div>Опишите нужную выгрузку<br>или выберите шаблон</div>';
    if (state.chatPrompts && state.chatPrompts.length) {
      html += '<div class="ie-starter-list">' + state.chatPrompts.map((p) =>
        '<button type="button" class="ie-starter-btn" data-prompt-id="' + escapeHtml(p.id) + '" title="' + escapeHtml(promptTooltip(p)) + '">' + escapeHtml(p.title) + "</button>"
      ).join("") + "</div>";
    }
    html += "</div>";
    box.innerHTML = html;
    box.querySelectorAll(".ie-starter-btn").forEach((btn) => {
      btn.addEventListener("click", () => startFromPrompt(btn.dataset.promptId));
    });
  }

  function wrapMsgContent(row, bubble) {
    const content = document.createElement("div");
    content.className = "ie-msg-content";
    content.appendChild(bubble);
    row.appendChild(content);
    return row;
  }

  async function loadMessages() {
    const scrollEl = $("ie-messages");
    if (!state.conversationId) {
      showEmptyState();
      return;
    }
    const data = await api("/conversations/" + state.conversationId + "/messages");
    const box = messagesBox();
    box.innerHTML = "";
    if (!data.messages.length) {
      showEmptyState();
      return;
    }
    data.messages.forEach((m) => {
      box.appendChild(renderMessage(m));
    });
    scrollEl.scrollTop = scrollEl.scrollHeight;
  }

  function renderMessage(m) {
    if (m.role === "user") return renderUserBubble(m);
    return renderAssistantBubble(m);
  }

  function renderUserBubble(m) {
    const row = document.createElement("div");
    row.className = "ie-msg-row ie-msg-user";
    const bubble = document.createElement("div");
    bubble.className = "ie-msg-bubble";
    bubble.textContent = m.content || "";
    return wrapMsgContent(row, bubble);
  }

  function renderAssistantBubble(m) {
    const row = document.createElement("div");
    row.className = "ie-msg-row ie-msg-assistant";

    const bubble = document.createElement("div");
    bubble.className = "ie-msg-bubble";
    bubble.innerHTML = escapeHtml(m.content || "").replace(/\n/g, "<br>");

    if (m.metadata && m.metadata.clarifying_questions && m.metadata.clarifying_questions.length) {
      bubble.innerHTML += '<ul class="mb-0 mt-2">' + m.metadata.clarifying_questions.map((q) => "<li>" + escapeHtml(q) + "</li>").join("") + "</ul>";
    }
    if (m.metadata && m.metadata.fix_suggestions && m.metadata.fix_suggestions.length) {
      bubble.innerHTML += '<ul class="mb-0 mt-2 text-danger">' + m.metadata.fix_suggestions.map((s) => "<li>" + escapeHtml(s) + "</li>").join("") + "</ul>";
    }

    const meta = m.metadata || {};
    if (meta.status === "validated" && meta.plan_version_id) {
      const actions = document.createElement("div");
      actions.className = "ie-msg-actions";

      const btnPlan = document.createElement("button");
      btnPlan.type = "button";
      btnPlan.className = "btn btn-sm btn-outline-secondary";
      btnPlan.textContent = "Посмотреть план";
      btnPlan.addEventListener("click", () => openPlanModal(meta.plan_version_id, meta.plan_summary));

      const btnPreview = document.createElement("button");
      btnPreview.type = "button";
      btnPreview.className = "btn btn-sm btn-outline-primary";
      btnPreview.textContent = "Предпросмотр";
      btnPreview.addEventListener("click", () => openPreviewModal(meta.plan_version_id));

      const btnRun = document.createElement("button");
      btnRun.type = "button";
      btnRun.className = "btn btn-sm btn-warning";
      btnRun.textContent = "Выгрузить";
      const blocked = isExportBlocked();
      if (blocked) {
        btnRun.disabled = true;
        btnRun.title = exportBlockedTip();
      }
      btnRun.addEventListener("click", () => runExport(meta.plan_version_id, actions));

      actions.appendChild(btnPlan);
      actions.appendChild(btnPreview);
      actions.appendChild(btnRun);
      bubble.appendChild(actions);
    }

    return wrapMsgContent(row, bubble);
  }

  function renderPlanSummaryHtml(summary, asBlock) {
    if (!summary) return "";
    const parts = [];
    if (summary.title) parts.push("<strong>" + escapeHtml(summary.title) + "</strong>");
    if (summary.entities && summary.entities.length) {
      parts.push("<div><em>Сущности:</em> " + escapeHtml(summary.entities.join(", ")) + "</div>");
    }
    if (summary.relations && summary.relations.length) {
      parts.push("<div><em>Связи:</em> " + escapeHtml(summary.relations.join(", ")) + "</div>");
    }
    if (summary.columns && summary.columns.length) {
      const cols = summary.columns.map((c) => {
        let s = c.header;
        if (c.source) s += " — " + c.source;
        if (c.transforms && c.transforms.length) s += " [" + c.transforms.join(", ") + "]";
        return escapeHtml(s);
      }).join("; ");
      parts.push("<div><em>Колонки:</em> " + cols + "</div>");
    }
    if (summary.filters && summary.filters.length) {
      const fs = summary.filters.map((f) => {
        const val = f.value != null ? " «" + f.value + "»" : "";
        return escapeHtml(f.field + " " + f.op + val);
      }).join("; ");
      parts.push("<div><em>Фильтры:</em> " + fs + "</div>");
    }
    if (summary.limit) parts.push("<div><em>Лимит:</em> " + escapeHtml(String(summary.limit)) + " строк</div>");
    (summary.assumptions || []).forEach((a) => parts.push("<div class=\"text-muted\">" + escapeHtml(a) + "</div>"));
    const inner = parts.join("");
    return asBlock ? inner : '<div class="small">' + inner + "</div>";
  }

  function renderPlanSummary(summary) {
    const box = $("ie-plan-summary");
    if (!box) return;
    if (!summary) {
      box.style.display = "none";
      box.innerHTML = "";
      return;
    }
    box.style.display = "block";
    box.innerHTML = renderPlanSummaryHtml(summary, true);
  }

  function setJsonVisible(visible) {
    state.jsonVisible = visible;
    const jsonEl = $("ie-plan-json");
    const btn = $("ie-toggle-json");
    if (jsonEl) jsonEl.style.display = visible ? "block" : "none";
    if (btn) btn.textContent = visible ? "Скрыть JSON" : "Показать JSON";
  }

  if ($("ie-toggle-json")) {
    $("ie-toggle-json").addEventListener("click", () => setJsonVisible(!state.jsonVisible));
  }

  async function openPlanModal(versionId, summaryHint) {
    try {
      const data = await api("/plans/" + versionId);
      state.planVersionId = versionId;
      $("ie-plan-editor").value = JSON.stringify(data.plan, null, 2);
      renderPlanSummary(summaryHint || buildSummaryFromPlan(data.plan));
      $("ie-plan-json").textContent = JSON.stringify(data.plan, null, 2);
      setJsonVisible(false);
      renderValidation(data.validation);
      if (!planModal) planModal = new bootstrap.Modal($("ie-plan-modal"));
      planModal.show();
    } catch (e) {
      alert(humanError(e));
    }
  }

  function buildSummaryFromPlan(plan) {
    if (!plan) return null;
    const summary = { title: plan.title || "План выгрузки" };
    const entityIds = new Set();
    const filters = [];
    let limit = null;
    (plan.datasets || []).forEach((ds) => {
      if (ds.limit) limit = ds.limit;
      (ds.sources || []).forEach((s) => { if (s.entity_type_id != null) entityIds.add(s.entity_type_id); });
      (ds.filters || []).forEach((f) => {
        filters.push({ field: f.field || f.field_code || "?", op: f.op, value: f.value });
      });
    });
    if (entityIds.size) summary.entities = [...entityIds].map(String);
    if (filters.length) summary.filters = filters;
    if (limit) summary.limit = limit;
    const columns = [];
    ((plan.workbook && plan.workbook.sheets) || []).forEach((sheet) => {
      if (sheet.mode && sheet.mode !== "rows") return;
      (sheet.columns || []).forEach((col) => {
        columns.push({
          header: col.header,
          source: col.value && col.value.field_code ? col.value.field_code : "",
          transforms: (col.transforms || []).map((t) => t.op),
        });
      });
    });
    if (columns.length) summary.columns = columns;
    return summary;
  }

  async function openPreviewModal(versionId) {
    state.planVersionId = versionId;
    try {
      await triggerPreview();
      if (!previewModal) previewModal = new bootstrap.Modal($("ie-preview-modal"));
      previewModal.show();
    } catch (e) {
      showPlanError(e);
    }
  }

  async function runExport(versionId, actionsEl) {
    let statusEl = actionsEl.querySelector(".ie-msg-run-status");
    if (!statusEl) {
      statusEl = document.createElement("div");
      statusEl.className = "ie-msg-run-status";
      actionsEl.appendChild(statusEl);
    }
    try {
      const r = await api("/plans/" + versionId + "/run", { method: "POST" });
      statusEl.textContent = "Запущена выгрузка #" + r.run_id + "…";
      pollRun(r.run_id, statusEl);
    } catch (e) {
      statusEl.textContent = humanError(e);
      showPlanError(e);
    }
  }

  async function applyChatResponse(resp) {
    if (resp.plan) { $("ie-plan-editor").value = JSON.stringify(resp.plan, null, 2); }
    if (resp.version) { state.planVersionId = resp.version.id; }
  }

  // --- chat ---
  const chatInput = $("ie-chat-input");

  function resizeComposer() {
    if (!chatInput) return;
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + "px";
  }

  if (chatInput) {
    chatInput.addEventListener("input", resizeComposer);
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
  }

  $("ie-chat-send").addEventListener("click", sendChat);

  async function sendChat() {
    const input = chatInput;
    const msg = input.value.trim();
    if (!msg) return;
    if (!state.conversationId) { await $("ie-new-conv").click(); }
    input.value = "";
    input.placeholder = "Опишите нужную выгрузку...";
    resizeComposer();
    try {
      const resp = await api("/conversations/" + state.conversationId + "/chat", { method: "POST", body: JSON.stringify({ message: msg }) });
      await loadMessages();
      await applyChatResponse(resp);
    } catch (e) {
      alert(humanError(e));
    }
  }

  async function loadPlanVersion(versionId) {
    const data = await api("/plans/" + versionId);
    state.planVersionId = versionId;
    $("ie-plan-editor").value = JSON.stringify(data.plan, null, 2);
  }

  function renderValidation(v) {
    const box = $("ie-validation");
    if (!box) return;
    if (!v) { box.innerHTML = ""; return; }
    if (v.valid) { box.innerHTML = '<span class="badge bg-success">План валиден</span>'; return; }
    const issues = (v.issues || []).map((i) => '<li><code>' + escapeHtml(i.code) + "</code> " + escapeHtml(i.message) + (i.path ? ' <span class="text-muted">(' + escapeHtml(i.path) + ")</span>" : "") + "</li>").join("");
    box.innerHTML = '<span class="badge bg-danger">Ошибки</span><ul class="mb-0">' + issues + "</ul>";
  }

  async function triggerPreview() {
    if (!state.planVersionId) throw new Error("План не выбран");
    const p = await api("/plans/" + state.planVersionId + "/preview", { method: "POST" });
    state.preview = p;
    renderSync(p.sync_state);
    $("ie-warnings").innerHTML = (p.warnings || []).map(escapeHtml).join("<br>");
    renderPreview(p);
    return p;
  }

  function showPlanError(e) {
    if (e.detail && e.detail.issues) renderValidation({ valid: false, issues: e.detail.issues });
    else if (e.detail && e.detail.code === "PREVIEW_FAILED" && e.detail.message) {
      renderValidation({ valid: false, issues: [{ code: e.detail.code, message: e.detail.message }] });
    } else alert(humanError(e));
  }

  function renderSync(s) {
    if (!s) { $("ie-sync").innerHTML = ""; return; }
    const cls = s.state === "normal" ? "success" : (s.state === "warning" ? "warning" : "danger");
    $("ie-sync").innerHTML = '<span class="badge bg-' + cls + '">Импорт: ' + s.state + "</span> " + (s.last_successful_sync_at || "нет данных");
  }

  function renderPreview(p) {
    const tabs = $("ie-preview-tabs");
    const body = $("ie-preview-body");
    const hint = $("ie-preview-hint");
    tabs.innerHTML = "";
    body.innerHTML = "";
    const hasErrors = (p.sheets || []).some((s) => s.mode === "errors");
    if (hint) {
      hint.textContent = hasErrors
        ? "Строки с проблемами данных (пустой или некорректный телефон и т.п.) не попали в основную вкладку."
        : "";
    }
    p.sheets.forEach((sheet, idx) => {
      const li = document.createElement("li");
      li.className = "nav-item";
      const a = document.createElement("a");
      a.className = "nav-link" + (idx === 0 ? " active" : "");
      a.href = "#";
      a.textContent = sheet.name + " (" + sheet.total_count + ")";
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        document.querySelectorAll("#ie-preview-tabs .nav-link").forEach((n) => n.classList.remove("active"));
        a.classList.add("active");
        drawSheet(sheet);
      });
      li.appendChild(a);
      tabs.appendChild(li);
    });
    if (p.sheets[0]) drawSheet(p.sheets[0]);
  }

  function drawSheet(sheet) {
    const body = $("ie-preview-body");
    const cols = sheet.columns || [];
    let html = '<table class="table table-sm table-bordered"><thead><tr>';
    cols.forEach((c) => { html += "<th>" + escapeHtml(c.header) + "</th>"; });
    if (sheet.mode === "errors") html += "<th>Ошибки</th>";
    html += "</tr></thead><tbody>";
    (sheet.rows || []).forEach((row) => {
      html += "<tr>";
      cols.forEach((c) => { html += "<td>" + escapeHtml(fmt(row[c.id])) + "</td>"; });
      if (sheet.mode === "errors") html += "<td>" + escapeHtml(fmt(row._errors)) + "</td>";
      html += "</tr>";
    });
    html += "</tbody></table>";
    if (sheet.validation_summary && sheet.validation_summary.error_count) {
      html += '<div class="text-danger">Ошибок: ' + sheet.validation_summary.error_count + ", строк с ошибками: " + (sheet.validation_summary.error_rows || 0) + "</div>";
    }
    body.innerHTML = html;
  }

  function pollRun(runId, statusEl) {
    const timer = setInterval(async () => {
      try {
        const r = await api("/runs/" + runId);
        const job = r.job || {};
        if (r.status === "completed") {
          clearInterval(timer);
          statusEl.innerHTML = 'Выгрузка #' + runId + ' завершена. <a href="' + API + "/runs/" + runId + '/download" class="btn btn-sm btn-outline-success ms-1">Скачать</a>';
          return;
        }
        if (r.status === "failed" || r.status === "cancelled") {
          clearInterval(timer);
          statusEl.textContent = "Выгрузка #" + runId + ": " + r.status;
          return;
        }
        statusEl.textContent = "Выгрузка #" + runId + ": " + r.status + " " + (job.progress_percent || 0) + "% " + (job.current_step || "");
      } catch (e) {
        clearInterval(timer);
      }
    }, 1500);
  }

  function fmt(v) { return v === null || v === undefined ? "" : String(v); }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // --- readiness banner ---
  async function loadHealth() {
    try {
      const h = await api("/health");
      state.health = h;
      renderBanner(h);
    } catch (e) {
      state.health = null;
      $("ie-banner").innerHTML = '<div class="alert alert-secondary mb-0">Не удалось проверить готовность данных</div>';
    }
  }

  function renderBanner(h) {
    const box = $("ie-banner");
    if (!h) { box.innerHTML = ""; return; }
    if (!h.has_data) {
      box.innerHTML = '<div class="alert alert-danger mb-0">База пуста — выполните импорт CRM для портала <code>' +
        escapeHtml(h.portal_id) + '</code></div>';
      return;
    }
    const sync = h.sync_state || {};
    if (!sync.export_allowed) {
      box.innerHTML = '<div class="alert alert-warning mb-0">Импорт устарел — полная выгрузка заблокирована, предпросмотр доступен (' +
        h.crm_entities + ' записей)</div>';
      return;
    }
    box.innerHTML = "";
  }

  init().catch((e) => alert(humanError(e)));

  async function init() {
    await loadHealth();
    await loadChatPrompts();
    const convData = await loadConversations();
    if (!state.conversationId && convData.conversations.length) {
      await selectConversation(convData.conversations[0].id);
    } else if (!state.conversationId) {
      showEmptyState();
    }
  }
})();
