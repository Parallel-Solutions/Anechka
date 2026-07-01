/* Bitrix24 Export — frontend helpers */

function showAlert(container, message, type = 'danger') {
    container.innerHTML = `<div class="alert alert-${type}">${message}</div>`;
}

async function postJson(url, data) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(data),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(body.detail || body.message || 'Ошибка запроса');
    }
    return body;
}

async function fetchJson(url) {
    const resp = await fetch(url, { headers: { Accept: 'application/json' } });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(body.detail || body.message || 'Ошибка запроса');
    }
    return body;
}

async function putJson(url, data) {
    const resp = await fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(data),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(body.detail || body.message || 'Ошибка запроса');
    }
    return body;
}

async function deleteJson(url) {
    const resp = await fetch(url, {
        method: 'DELETE',
        headers: { Accept: 'application/json' },
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(body.detail || body.message || 'Ошибка запроса');
    }
    return body;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formToObject(form) {
    const data = {};
    const fd = new FormData(form);
    for (const [key, value] of fd.entries()) {
        if (key.endsWith('_ids')) continue;
        if (data[key] !== undefined) {
            if (!Array.isArray(data[key])) data[key] = [data[key]];
            data[key].push(value);
        } else {
            data[key] = value;
        }
    }
    form.querySelectorAll('input[type=checkbox]').forEach((el) => {
        data[el.name] = el.checked;
    });
    return data;
}

function initIndexPage() {
    const regionForm = document.getElementById('region-form');
    const stageForm = document.getElementById('stage-form');
    const searchBtn = document.getElementById('btn-search-region');
    const resultBox = document.getElementById('region-search-result');
    const regionIdInput = document.getElementById('region_id');

    if (searchBtn) {
        searchBtn.addEventListener('click', async () => {
            const name = document.getElementById('region_name').value.trim();
            const iblockId = document.getElementById('iblock_id').value;
            if (!name) {
                showAlert(resultBox, 'Введите название региона');
                return;
            }
            resultBox.innerHTML = '<div class="text-muted">Поиск...</div>';
            try {
                const resp = await fetch(`/api/regions/search?name=${encodeURIComponent(name)}&iblock_id=${iblockId}`);
                const data = await resp.json();
                if (!resp.ok) {
                    showAlert(resultBox, data.detail || 'Регион не найден', 'warning');
                    regionIdInput.value = '';
                    return;
                }
                if (data.length === 1) {
                    regionIdInput.value = data[0].id;
                    resultBox.innerHTML = `<div class="alert alert-success">Найден регион: <strong>${data[0].name}</strong> (ID: ${data[0].id})</div>`;
                } else {
                    resultBox.innerHTML = '<div class="alert alert-warning">Найдено несколько регионов. Выберите нужный:</div>';
                    const list = document.createElement('div');
                    list.className = 'list-group';
                    data.forEach((r) => {
                        const item = document.createElement('button');
                        item.type = 'button';
                        item.className = 'list-group-item list-group-item-action region-option';
                        item.textContent = `${r.name} (ID: ${r.id})`;
                        item.addEventListener('click', () => {
                            regionIdInput.value = r.id;
                            resultBox.innerHTML = `<div class="alert alert-success">Выбран регион: <strong>${r.name}</strong> (ID: ${r.id})</div>`;
                        });
                        list.appendChild(item);
                    });
                    resultBox.appendChild(list);
                }
            } catch (e) {
                showAlert(resultBox, e.message);
            }
        });
    }

    if (regionForm) {
        regionForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!regionIdInput.value) {
                alert('Сначала найдите и выберите регион');
                return;
            }
            const payload = formToObject(regionForm);
            payload.region_id = parseInt(regionIdInput.value, 10);
            payload.category_id = parseInt(payload.category_id, 10);
            payload.iblock_id = parseInt(payload.iblock_id, 10);
            payload.limit = parseInt(payload.limit, 10);
            try {
                const res = await postJson('/exports/region', payload);
                window.location.href = `/exports/${res.job_id}`;
            } catch (err) {
                alert(err.message);
            }
        });
    }

    const categorySelect = document.getElementById('stage_category_id');
    const stageSelect = document.getElementById('stage_id');
    const usersSelect = document.getElementById('excluded_users');
    const fullCategorySelect = document.getElementById('full_category_id');
    const fullUsersSelect = document.getElementById('full_excluded_users');
    const categoryFullForm = document.getElementById('category-full-form');

    async function loadCategories() {
        if (!categorySelect && !fullCategorySelect) return;
        try {
            const cats = await fetch('/api/categories').then((r) => r.json());
            const options = cats.map((c) => `<option value="${c.id}">${c.name}</option>`).join('');
            if (categorySelect) {
                categorySelect.innerHTML = options;
                if (cats.length) loadStages(cats[0].id);
            }
            if (fullCategorySelect) {
                fullCategorySelect.innerHTML = options;
            }
        } catch (e) {
            if (categorySelect) categorySelect.innerHTML = '<option>Ошибка загрузки</option>';
            if (fullCategorySelect) fullCategorySelect.innerHTML = '<option>Ошибка загрузки</option>';
        }
    }

    async function loadStages(categoryId) {
        if (!stageSelect) return;
        stageSelect.disabled = true;
        stageSelect.innerHTML = '<option>Загрузка...</option>';
        try {
            const stages = await fetch(`/api/categories/${categoryId}/stages`).then((r) => r.json());
            stageSelect.innerHTML = stages.map((s) => `<option value="${s.id}">${s.name}</option>`).join('');
            stageSelect.disabled = stages.length === 0;
        } catch (e) {
            stageSelect.innerHTML = '<option>Ошибка</option>';
        }
    }

    async function loadUsers() {
        if (!usersSelect && !fullUsersSelect) return;
        try {
            const users = await fetch('/api/users').then((r) => r.json());
            const options = users.map((u) => `<option value="${u.id}">${u.name}</option>`).join('');
            if (usersSelect) usersSelect.innerHTML = options;
            if (fullUsersSelect) fullUsersSelect.innerHTML = options;
        } catch (e) {
            if (usersSelect) usersSelect.innerHTML = '<option>Ошибка загрузки</option>';
            if (fullUsersSelect) fullUsersSelect.innerHTML = '<option>Ошибка загрузки</option>';
        }
    }

    if (categorySelect || fullCategorySelect) {
        if (categorySelect) {
            categorySelect.addEventListener('change', () => loadStages(categorySelect.value));
        }
        loadCategories();
        loadUsers();
    }

    if (stageForm) {
        stageForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const payload = formToObject(stageForm);
            payload.category_id = parseInt(payload.category_id, 10);
            payload.limit = parseInt(payload.limit, 10);
            const excluded = Array.from(usersSelect.selectedOptions).map((o) => parseInt(o.value, 10));
            payload.excluded_user_ids = excluded;
            try {
                const res = await postJson('/exports/stage', payload);
                window.location.href = `/exports/${res.job_id}`;
            } catch (err) {
                alert(err.message);
            }
        });
    }

    if (categoryFullForm) {
        categoryFullForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const payload = formToObject(categoryFullForm);
            payload.category_id = parseInt(payload.category_id, 10);
            payload.limit = parseInt(payload.limit, 10);
            const excluded = fullUsersSelect
                ? Array.from(fullUsersSelect.selectedOptions).map((o) => parseInt(o.value, 10))
                : [];
            payload.excluded_user_ids = excluded;
            try {
                const res = await postJson('/exports/category-full', payload);
                window.location.href = `/exports/${res.job_id}`;
            } catch (err) {
                alert(err.message);
            }
        });
    }
}

function initSettingsPage() {
    const btn = document.getElementById('btn-test-connection');
    const result = document.getElementById('connection-result');
    if (btn) {
        btn.addEventListener('click', async () => {
            result.innerHTML = '<div class="text-muted">Проверка...</div>';
            try {
                const data = await postJson('/api/connection/test', {});
                showAlert(result, data.message, data.ok ? 'success' : 'danger');
            } catch (e) {
                showAlert(result, e.message);
            }
        });
    }

    initLprConfig();
}

function initLprConfig() {
    const keywordsEl = document.getElementById('lpr-keywords');
    const fieldsEl = document.getElementById('lpr-fields');
    const stopwordsEl = document.getElementById('lpr-stopwords');
    const saveBtn = document.getElementById('btn-save-lpr');
    const resultEl = document.getElementById('lpr-save-result');
    if (!keywordsEl || !fieldsEl || !saveBtn) return;

    const toLines = (arr) => (Array.isArray(arr) ? arr.join('\n') : '');
    const fromLines = (text) => text
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line.length > 0);

    saveBtn.addEventListener('click', async () => {
        resultEl.textContent = 'Сохранение…';
        resultEl.className = 'small text-muted';
        try {
            const data = await putJson('/api/ai/lpr-config', {
                keywords: fromLines(keywordsEl.value),
                fields: fromLines(fieldsEl.value),
                stopwords: stopwordsEl ? fromLines(stopwordsEl.value) : [],
            });
            keywordsEl.value = toLines(data.keywords);
            fieldsEl.value = toLines(data.fields);
            if (stopwordsEl) stopwordsEl.value = toLines(data.stopwords);
            resultEl.textContent = 'Сохранено';
            resultEl.className = 'small text-success';
        } catch (e) {
            resultEl.textContent = `Ошибка: ${e.message}`;
            resultEl.className = 'small text-danger';
        }
    });
}

function initExportDetail() {
    const jobId = window.EXPORT_JOB_ID;
    if (!jobId) return;

    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const currentStep = document.getElementById('current-step');
    const eventLog = document.getElementById('event-log');
    const errorMessage = document.getElementById('error-message');

    const dealsState = {
        source: 'filter',
        offset: 0,
        limit: 50,
        total: 0,
        loading: false,
    };

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function fmtDealCell(value) {
        return value === null || value === undefined || value === '' ? '—' : escapeHtml(value);
    }

    function bitrixLink(url, label) {
        if (!url) return '—';
        return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
    }

    function setDealsLoading(isLoading) {
        dealsState.loading = isLoading;
        document.getElementById('deals-loading').classList.toggle('d-none', !isLoading);
    }

    function renderDeals(data) {
        const noteEl = document.getElementById('deals-note');
        const emptyEl = document.getElementById('deals-empty');
        const tableWrap = document.getElementById('deals-table-wrap');
        const tbody = document.getElementById('deals-tbody');
        const pager = document.getElementById('deals-pager');
        const totalEl = document.getElementById('deals-total');
        const pageInfo = document.getElementById('deals-page-info');

        dealsState.total = data.total || 0;
        noteEl.textContent = data.note || '';
        noteEl.classList.toggle('text-warning', Boolean(data.note && !data.available));
        totalEl.textContent = data.available ? `Всего: ${dealsState.total}` : '';

        if (!data.available) {
            emptyEl.textContent = data.note || 'Список недоступен';
            emptyEl.classList.remove('d-none');
            tableWrap.classList.add('d-none');
            pager.classList.add('d-none');
            tbody.innerHTML = '';
            return;
        }

        if (!data.deals || data.deals.length === 0) {
            emptyEl.textContent = 'Сделки не найдены';
            emptyEl.classList.remove('d-none');
            tableWrap.classList.add('d-none');
            pager.classList.add('d-none');
            tbody.innerHTML = '';
            return;
        }

        emptyEl.classList.add('d-none');
        tableWrap.classList.remove('d-none');
        tbody.innerHTML = data.deals.map((deal) => {
            const idCell = deal.bitrix_url
                ? bitrixLink(deal.bitrix_url, deal.deal_id)
                : fmtDealCell(deal.deal_id);
            const titleCell = deal.bitrix_url
                ? bitrixLink(deal.bitrix_url, deal.title || deal.deal_id)
                : fmtDealCell(deal.title);
            return (
                '<tr>'
                + `<td>${bitrixLink(deal.bitrix_url, 'Открыть')}</td>`
                + `<td>${idCell}</td>`
                + `<td>${titleCell}</td>`
                + `<td>${fmtDealCell(deal.stage_name || deal.stage_id)}</td>`
                + `<td>${fmtDealCell(deal.category_id)}</td>`
                + `<td>${fmtDealCell(deal.created_time)}</td>`
                + '</tr>'
            );
        }).join('');

        const start = dealsState.offset + 1;
        const end = dealsState.offset + data.deals.length;
        pageInfo.textContent = `${start}–${end} из ${dealsState.total}`;
        pager.classList.toggle('d-none', dealsState.total <= dealsState.limit);
        document.getElementById('deals-prev').disabled = dealsState.offset <= 0;
        document.getElementById('deals-next').disabled = dealsState.offset + dealsState.limit >= dealsState.total;
    }

    async function loadDeals() {
        if (dealsState.loading) return;
        setDealsLoading(true);
        try {
            const params = new URLSearchParams({
                source: dealsState.source,
                offset: String(dealsState.offset),
                limit: String(dealsState.limit),
            });
            const resp = await fetch(`/api/exports/${jobId}/deals?${params}`);
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                renderDeals({
                    available: false,
                    total: 0,
                    deals: [],
                    note: err.detail || 'Не удалось загрузить список сделок',
                });
                return;
            }
            renderDeals(await resp.json());
        } catch (_) {
            renderDeals({
                available: false,
                total: 0,
                deals: [],
                note: 'Ошибка сети при загрузке сделок',
            });
        } finally {
            setDealsLoading(false);
        }
    }

    function activateDealsTab(source) {
        dealsState.source = source;
        dealsState.offset = 0;
        document.querySelectorAll('#deals-tabs .nav-link').forEach((btn) => {
            const active = btn.dataset.dealsSource === source;
            btn.classList.toggle('active', active);
        });
        loadDeals();
    }

    function enableFileDealsTab() {
        const fileTab = document.getElementById('deals-tab-file');
        if (!fileTab) return;
        fileTab.classList.remove('disabled');
        fileTab.disabled = false;
    }

    document.getElementById('deals-tab-filter')?.addEventListener('click', () => {
        activateDealsTab('filter');
    });
    document.getElementById('deals-tab-file')?.addEventListener('click', () => {
        if (window.EXPORT_JOB_STATUS !== 'completed') return;
        activateDealsTab('file');
    });
    document.getElementById('deals-prev')?.addEventListener('click', () => {
        dealsState.offset = Math.max(0, dealsState.offset - dealsState.limit);
        loadDeals();
    });
    document.getElementById('deals-next')?.addEventListener('click', () => {
        if (dealsState.offset + dealsState.limit < dealsState.total) {
            dealsState.offset += dealsState.limit;
            loadDeals();
        }
    });

    if (document.getElementById('deals-card')) {
        loadDeals();
    }

    function updateUI(data) {
        const pct = data.progress_percent || 0;
        progressBar.style.width = `${pct}%`;
        progressBar.textContent = `${pct}%`;
        progressText.textContent = `${data.progress_current} / ${data.progress_total} (${pct}%)`;
        currentStep.textContent = data.current_step || '—';
        if (data.statistics) {
            document.getElementById('stat-contacts').textContent = data.statistics.contacts_found ?? '—';
            document.getElementById('stat-phones').textContent = data.statistics.phones_found ?? '—';
            document.getElementById('stat-errors').textContent = data.statistics.errors ?? '—';
            document.getElementById('stat-skipped').textContent = data.statistics.skipped ?? '—';
        }
        if (data.event_log) {
            eventLog.textContent = data.event_log.join('\n');
        }
        if (data.error_message) {
            errorMessage.textContent = data.error_message;
            errorMessage.classList.remove('d-none');
        }
        if (data.status === 'completed') {
            window.EXPORT_JOB_STATUS = 'completed';
            enableFileDealsTab();
        }
        if (data.status === 'completed' && !document.getElementById('btn-download')) {
            location.reload();
        }
    }

    if (typeof EventSource !== 'undefined') {
        const es = new EventSource(`/api/exports/${jobId}/status`, { withCredentials: false });
        es.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data);
                updateUI(data);
                if (['completed', 'failed', 'cancelled'].includes(data.status)) {
                    es.close();
                }
            } catch (_) { /* ignore */ }
        };
        es.onerror = () => {
            es.close();
            pollStatus();
        };
    } else {
        pollStatus();
    }

    async function pollStatus() {
        const resp = await fetch(`/api/exports/${jobId}/status`, { headers: { Accept: 'application/json' } });
        if (!resp.ok) return;
        const data = await resp.json();
        updateUI(data);
        if (!['completed', 'failed', 'cancelled'].includes(data.status)) {
            setTimeout(pollStatus, 1500);
        }
    }

    document.getElementById('btn-cancel')?.addEventListener('click', async () => {
        await postJson(`/api/exports/${jobId}/cancel`, {});
    });

    document.getElementById('btn-retry')?.addEventListener('click', async () => {
        const res = await postJson(`/api/exports/${jobId}/retry`, {});
        window.location.href = `/exports/${res.job_id}`;
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initIndexPage();
    initSettingsPage();
    initAIPrompts();
    initAIPage();
});

function initAIPrompts() {
    const listEl = document.getElementById('prompt-list');
    if (!listEl) return;

    const chatInput = document.getElementById('chat-input');
    const addBtn = document.getElementById('btn-add-prompt');
    const modalEl = document.getElementById('prompt-modal');
    const modalForm = document.getElementById('prompt-form');
    const modalTitle = document.getElementById('prompt-modal-label');
    const editIdInput = document.getElementById('prompt-edit-id');
    const titleInput = document.getElementById('prompt-title');
    const textInput = document.getElementById('prompt-text');
    const formError = document.getElementById('prompt-form-error');
    const modal = modalEl ? bootstrap.Modal.getOrCreateInstance(modalEl) : null;

    let prompts = [];
    let activePromptId = null;

    function openModal(mode, item) {
        if (!modal) return;
        formError.classList.add('d-none');
        formError.textContent = '';
        if (mode === 'edit' && item) {
            modalTitle.textContent = 'Редактировать промпт';
            editIdInput.value = String(item.id);
            titleInput.value = item.title;
            textInput.value = item.prompt;
        } else {
            modalTitle.textContent = 'Добавить промпт';
            editIdInput.value = '';
            titleInput.value = '';
            textInput.value = '';
        }
        modal.show();
        titleInput.focus();
    }

    function setActivePrompt(id) {
        activePromptId = id;
        listEl.querySelectorAll('.ai-prompt-item').forEach((el) => {
            el.classList.toggle('active', el.dataset.id === String(id));
        });
    }

    function renderPrompts() {
        if (!prompts.length) {
            listEl.innerHTML = '<div class="list-group-item text-muted small">Нет промптов. Добавьте первый.</div>';
            return;
        }
        listEl.innerHTML = prompts.map((item) => `
            <div class="list-group-item ai-prompt-item${activePromptId === item.id ? ' active' : ''}" data-id="${item.id}">
                <div class="ai-prompt-item-body">
                    <button type="button" class="ai-prompt-select btn btn-link p-0 text-start text-decoration-none"
                            title="${escapeHtml(item.prompt)}">
                        ${escapeHtml(item.title)}
                    </button>
                    <div class="ai-prompt-actions">
                        <button type="button" class="btn btn-sm btn-link ai-prompt-edit p-0" data-id="${item.id}" title="Редактировать">✎</button>
                        <button type="button" class="btn btn-sm btn-link text-danger ai-prompt-delete p-0" data-id="${item.id}" title="Удалить">🗑</button>
                    </div>
                </div>
            </div>
        `).join('');

        listEl.querySelectorAll('.ai-prompt-select').forEach((btn) => {
            btn.addEventListener('click', () => {
                const parent = btn.closest('.ai-prompt-item');
                const id = Number(parent.dataset.id);
                const item = prompts.find((p) => p.id === id);
                if (!item || !chatInput) return;
                chatInput.value = item.prompt;
                chatInput.focus();
                setActivePrompt(id);
            });
        });

        listEl.querySelectorAll('.ai-prompt-edit').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = Number(btn.dataset.id);
                const item = prompts.find((p) => p.id === id);
                if (item) openModal('edit', item);
            });
        });

        listEl.querySelectorAll('.ai-prompt-delete').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = Number(btn.dataset.id);
                const item = prompts.find((p) => p.id === id);
                if (!item) return;
                if (!confirm(`Удалить промпт «${item.title}»?`)) return;
                try {
                    await deleteJson(`/api/ai/prompts/${id}`);
                    if (activePromptId === id) activePromptId = null;
                    await loadPrompts();
                } catch (err) {
                    alert(err.message);
                }
            });
        });
    }

    async function loadPrompts() {
        try {
            prompts = await fetchJson('/api/ai/prompts');
            renderPrompts();
        } catch (err) {
            listEl.innerHTML = `<div class="list-group-item text-danger small">${escapeHtml(err.message)}</div>`;
        }
    }

    if (addBtn) {
        addBtn.addEventListener('click', () => openModal('add'));
    }

    if (modalForm) {
        modalForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            formError.classList.add('d-none');
            const title = titleInput.value.trim();
            const prompt = textInput.value.trim();
            const editId = editIdInput.value;
            try {
                if (editId) {
                    await putJson(`/api/ai/prompts/${editId}`, { title, prompt });
                } else {
                    await postJson('/api/ai/prompts', { title, prompt });
                }
                modal.hide();
                await loadPrompts();
            } catch (err) {
                formError.textContent = err.message;
                formError.classList.remove('d-none');
            }
        });
    }

    loadPrompts();
}

function initAIPage() {
    const form = document.getElementById('chat-form');
    if (!form) return;

    const input = document.getElementById('chat-input');
    const messagesEl = document.getElementById('chat-messages');
    const statusEl = document.getElementById('chat-status');
    const tableArea = document.getElementById('chat-table-area');
    const tableEl = document.getElementById('chat-table');
    const downloadBtn = document.getElementById('btn-download-excel');
    const downloadJsonBtn = document.getElementById('btn-download-json');
    const sendBtn = document.getElementById('btn-send-chat');

    const history = [];

    function appendMessage(role, text) {
        const wrap = document.createElement('div');
        wrap.className = `mb-2 ${role === 'user' ? 'text-end' : ''}`;
        const bubble = document.createElement('div');
        bubble.className = `d-inline-block p-2 rounded ${role === 'user' ? 'bg-primary text-white' : 'bg-white border'}`;
        bubble.style.maxWidth = '85%';
        bubble.style.whiteSpace = 'pre-wrap';
        bubble.textContent = text;
        wrap.appendChild(bubble);
        messagesEl.appendChild(wrap);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderTable(table, resultToken, downloadUrl, downloadLabel) {
        if (!table || !table.columns || !table.columns.length) {
            tableArea.classList.add('d-none');
            downloadBtn.classList.add('d-none');
            if (downloadJsonBtn) downloadJsonBtn.classList.add('d-none');
            return;
        }
        tableArea.classList.remove('d-none');
        const thead = tableEl.querySelector('thead');
        const tbody = tableEl.querySelector('tbody');
        thead.innerHTML = `<tr>${table.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr>`;
        tbody.innerHTML = table.rows.map((row) =>
            `<tr>${table.columns.map((c) => `<td>${escapeHtml(row[c] ?? '')}</td>`).join('')}</tr>`
        ).join('');
        if (downloadUrl) {
            downloadBtn.href = downloadUrl;
            downloadBtn.textContent = downloadLabel || 'Скачать Excel';
            downloadBtn.classList.remove('d-none');
            if (downloadJsonBtn) downloadJsonBtn.classList.add('d-none');
        } else if (resultToken) {
            downloadBtn.href = `/api/ai/result/${resultToken}/download`;
            downloadBtn.textContent = 'Скачать Excel';
            downloadBtn.classList.remove('d-none');
            if (downloadJsonBtn) {
                downloadJsonBtn.href = `/api/ai/result/${resultToken}/download/json`;
                downloadJsonBtn.classList.remove('d-none');
            }
        } else {
            downloadBtn.classList.add('d-none');
            if (downloadJsonBtn) downloadJsonBtn.classList.add('d-none');
        }
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;

        appendMessage('user', text);
        history.push({ role: 'user', content: text });
        input.value = '';
        input.disabled = true;
        sendBtn.disabled = true;
        statusEl.textContent = 'Думаю…';

        try {
            const data = await postJson('/api/ai/chat', { messages: history });
            appendMessage('assistant', data.reply || '(пустой ответ)');
            history.push({ role: 'assistant', content: data.reply || '' });
            renderTable(data.table, data.result_token, data.download_url, data.download_label);
            statusEl.textContent = '';
        } catch (err) {
            appendMessage('assistant', `Ошибка: ${err.message}`);
            statusEl.textContent = '';
        } finally {
            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();
        }
    });
}
