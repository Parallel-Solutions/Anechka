(function () {
    'use strict';

    const SOURCE_LABELS = {
        deterministic: 'Правила',
        llm: 'LLM',
        hybrid: 'Правила + LLM',
        manual: 'Исправлено пользователем',
    };

    const SIGNAL_LABELS = {
        positive: 'Положительный',
        alternate_contact_requested: 'Другой контакт',
        callback_later_requested: 'Перезвон',
        no_answer: 'Не дозвонились',
        deal_not_found: 'Сделка не найдена',
        explicit_refusal: 'Отказ',
        hangup_without_result: 'Сброс трубки',
        replacement_contact_required: 'Перезвон на другой номер',
        needs_manual_review: 'Ручная проверка',
    };

    const METHOD_DESC = {
        'crm.timeline.comment.add': 'Комментарий в таймлайн сделки (отказ)',
        'crm.activity.todo.add': 'CRM-дело по положительному результату',
        'crm.contact.list': 'Поиск контакта по телефону',
        'crm.contact.add': 'Создание контакта',
        'crm.deal.contact.add': 'Привязка контакта к сделке',
        'retry_queue.add': 'Очередь повторных звонков',
        'contact_search.add': 'Требуется поиск нового контакта',
    };

    const MATCH_STATUS_LABELS = {
        matched: 'Сопоставлено',
        ambiguous: 'Неоднозначно',
        not_found: 'Не найдено',
        invalid: 'Некорректный телефон',
        conflict: 'Конфликт',
    };

    const ROW_FILTERS = [
        { id: 'all', label: 'Все' },
        { id: 'manual_review', label: 'Ручная проверка' },
        { id: 'manual_call', label: 'Ручной обзвон' },
        { id: 'auto_call', label: 'Автоматический обзвон' },
        { id: 'new_contacts', label: 'Новые контакты в битриксе' },
        { id: 'new_todos', label: 'Новые дела' },
        { id: 'new_comments', label: 'Новые комментарии' },
    ];

    const FILTER_ACTIONS = {
        manual_call: { type: 'export', label: 'Выгрузить' },
        auto_call: { type: 'export', label: 'Выгрузить' },
        new_contacts: { type: 'send', label: 'Отправить' },
        new_todos: { type: 'send', label: 'Отправить' },
        new_comments: { type: 'send', label: 'Отправить' },
    };

    const FILTER_SEND_METHODS = {
        new_contacts: ['crm.contact.list', 'crm.contact.add', 'crm.deal.contact.add'],
        new_todos: ['crm.activity.todo.add'],
        new_comments: ['crm.timeline.comment.add'],
    };

    const MANUAL_BITRIX_SEND_ACTIONS = new Set(['comment', 'todo', 'create_contact']);

    const ACTION_TO_FILTER = {
        comment: 'new_comments',
        todo: 'new_todos',
        create_contact: 'new_contacts',
    };

    const SEND_SUCCESS_MESSAGES = {
        comment: 'Комментарий добавлен в Bitrix24',
        todo: 'CRM-дело создано в Bitrix24',
        create_contact: 'Контакт создан в Bitrix24',
    };

    let selectedFile = null;
    let uploadInProgress = false;
    let pendingImportId = null;
    let pendingNeedsSheet = false;
    let importRowsCache = [];
    let importSummaryCache = null;
    let importActionsByRowId = {};
    let manualReviewIds = new Set();
    let hangupWithoutAnswersIds = new Set();
    let attemptHistoryCache = [];
    let retryQueueCache = [];
    let contactSearchCache = [];
    let activeFilterId = 'manual_review';
    let filterInitialized = false;
    let currentImportId = null;
    let rowViewModal = null;
    let filterSendModal = null;
    let filterSendState = { filterId: null, rowIds: [] };
    let currentViewRowId = null;
    let modalReviewState = {
        mode: 'idle',
        action: null,
        previewData: null,
        preparedRowId: null,
        preparedMessage: null,
        sendResult: null,
        resolveData: null,
    };
    let diagnosticsCache = null;
    let lastRowViewWheelNavAt = 0;
    let importPollTimer = null;
    let lastStatusSignature = null;

    function statusSignature(data) {
        const s = data.summary || {};
        return JSON.stringify({
            status: data.status,
            processed_at: data.processed_at,
            execute_status: s.execute_status,
            total_rows: s.total_rows,
            llm_completed: s.llm_completed,
            llm_pending: s.llm_pending,
            prepared_operations: s.prepared_operations,
            executed_operations: s.executed_operations,
            execution_errors: s.execution_errors,
        });
    }

    function normalizeLoadOptions(arg) {
        if (typeof arg === 'boolean') {
            return arg ? { pendingProcessing: true } : {};
        }
        return arg && typeof arg === 'object' ? arg : {};
    }

    function scheduleImportPoll(importId, statusData) {
        if (importPollTimer) {
            clearTimeout(importPollTimer);
            importPollTimer = null;
        }
        const status = statusData?.status;
        const executeStatus = statusData?.summary?.execute_status;
        if (status === 'processing' || status === 'uploaded') {
            importPollTimer = setTimeout(() => pollImportStatus(importId), 2000);
        } else if (status === 'ready' && executeStatus === 'executing') {
            importPollTimer = setTimeout(() => pollImportStatus(importId), 3000);
        }
    }

    async function pollImportStatus(importId) {
        try {
            const statusData = await fetchJson(`/api/call-results/imports/${importId}/status`);
            const sig = statusSignature(statusData);
            if (sig !== lastStatusSignature) {
                lastStatusSignature = sig;
                if (statusData.status === 'processing' || statusData.status === 'uploaded') {
                    applyImportMeta(statusData);
                    importSummaryCache = statusData.summary;
                    renderSummaryFilters(statusData.summary, activeFilterId);
                    renderFilteredList(activeFilterId, importId, true);
                } else {
                    await loadImportDetail(importId, { reloadQueues: false });
                }
            }
            scheduleImportPoll(importId, statusData);
        } catch (e) { /* ignore poll errors */ }
    }

    function setDropzoneBusy(busy) {
        const dropzone = document.getElementById('dropzone');
        if (!dropzone) return;
        dropzone.classList.toggle('uploading', busy);
        dropzone.style.pointerEvents = busy ? 'none' : '';
        dropzone.style.opacity = busy ? '0.7' : '';
    }

    function redirectToImportPage(importId) {
        window.location.href = `/call-results/imports/${importId}`;
    }

    function initCallResultsUploadPage() {
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file-input');
        if (!dropzone || !fileInput) return;

        dropzone.addEventListener('click', () => {
            if (uploadInProgress) return;
            fileInput.click();
        });
        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (uploadInProgress) return;
            if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
        });
        fileInput.addEventListener('change', () => {
            if (uploadInProgress) return;
            if (fileInput.files.length) setFile(fileInput.files[0]);
        });

        document.getElementById('btn-clear-file')?.addEventListener('click', clearFile);
        document.getElementById('btn-apply-mapping')?.addEventListener('click', applyConfigure);
        document.getElementById('btn-apply-sheet')?.addEventListener('click', applyConfigure);
    }

    function setFile(file) {
        if (uploadInProgress) return;
        selectedFile = file;
        const info = document.getElementById('file-info');
        const actions = document.getElementById('upload-actions');
        if (info) {
            info.classList.remove('d-none');
            info.innerHTML = `<strong>${escapeHtml(file.name)}</strong><br>Размер: ${(file.size / 1024).toFixed(1)} КБ`;
        }
        actions?.classList.remove('d-none');
        uploadFile(false);
    }

    function clearFile() {
        selectedFile = null;
        uploadInProgress = false;
        pendingImportId = null;
        setDropzoneBusy(false);
        document.getElementById('file-info')?.classList.add('d-none');
        document.getElementById('upload-actions')?.classList.add('d-none');
        document.getElementById('upload-progress')?.classList.add('d-none');
        document.getElementById('mapping-modal')?.classList.add('d-none');
        document.getElementById('sheet-modal')?.classList.add('d-none');
        const fi = document.getElementById('file-input');
        if (fi) fi.value = '';
    }

    function readMappingFromForm() {
        const mapping = {};
        document.querySelectorAll('.mapping-field').forEach((sel) => {
            const field = sel.dataset.field;
            if (field && sel.value) mapping[field] = sel.value;
        });
        return Object.keys(mapping).length ? mapping : null;
    }

    async function uploadFile(forceDuplicate) {
        if (!selectedFile || (uploadInProgress && !forceDuplicate)) return;
        uploadInProgress = true;
        setDropzoneBusy(true);

        const alertEl = document.getElementById('upload-alert');
        const progress = document.getElementById('upload-progress');
        progress?.classList.remove('d-none');

        const fd = new FormData();
        fd.append('file', selectedFile);
        if (forceDuplicate) fd.append('force_duplicate', 'true');

        try {
            const resp = await fetch('/api/call-results/imports', { method: 'POST', body: fd });
            const data = await resp.json();
            if (resp.status === 409 && data.duplicate) {
                uploadInProgress = false;
                setDropzoneBusy(false);
                const link = `/call-results/imports/${data.existing_import_id}`;
                let extra = data.resumable
                    ? ` <a href="${link}">Продолжить настройку</a>`
                    : ` <a href="${link}">Открыть предыдущий</a>`;
                if (!data.resumable) {
                    extra += ` <button type="button" class="btn btn-sm btn-warning ms-2" id="btn-force-duplicate">Загрузить как новый</button>`;
                }
                showAlert(alertEl, data.message + extra, 'warning');
                document.getElementById('btn-force-duplicate')?.addEventListener('click', () => uploadFile(true));
                progress?.classList.add('d-none');
                return;
            }
            if (!resp.ok) throw new Error(data.detail || data.error || 'Ошибка загрузки');

            if (data.needs_sheet) {
                uploadInProgress = false;
                setDropzoneBusy(false);
                pendingImportId = data.import_id;
                pendingNeedsSheet = true;
                showSheetUI(data);
                progress?.classList.add('d-none');
                return;
            }

            if (data.needs_column_mapping) {
                uploadInProgress = false;
                setDropzoneBusy(false);
                pendingImportId = data.import_id;
                pendingNeedsSheet = false;
                showMappingUI(data);
                progress?.classList.add('d-none');
                return;
            }

            redirectToImportPage(data.import_id);
        } catch (err) {
            uploadInProgress = false;
            setDropzoneBusy(false);
            showAlert(alertEl, err.message, 'danger');
            progress?.classList.add('d-none');
        }
    }

    async function applyConfigure() {
        if (!pendingImportId) return;
        const alertEl = document.getElementById('upload-alert');
        const body = {
            column_mapping: pendingNeedsSheet ? null : readMappingFromForm(),
            selected_sheet: pendingNeedsSheet ? (document.getElementById('sheet-select')?.value || null) : null,
        };
        if (!pendingNeedsSheet) {
            body.column_mapping = readMappingFromForm();
        }
        try {
            const resp = await fetch(`/api/call-results/imports/${pendingImportId}/configure`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Ошибка настройки');
            if (data.needs_column_mapping) {
                pendingNeedsSheet = false;
                showMappingUI(data);
                return;
            }
            document.getElementById('mapping-modal')?.classList.add('d-none');
            document.getElementById('sheet-modal')?.classList.add('d-none');
            redirectToImportPage(pendingImportId);
        } catch (err) {
            showAlert(alertEl, err.message, 'danger');
        }
    }

    function showSheetUI(data) {
        const modal = document.getElementById('sheet-modal');
        const sel = document.getElementById('sheet-select');
        if (!modal || !sel) return;
        modal.classList.remove('d-none');
        sel.innerHTML = (data.sheets || []).map((s) =>
            `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`
        ).join('');
    }

    function showMappingUI(data) {
        const modal = document.getElementById('mapping-modal');
        const form = document.getElementById('mapping-form');
        if (!modal || !form) return;
        modal.classList.remove('d-none');
        const fields = ['phone', 'comment', 'category', 'transcript', 'called_at', 'deal_id', 'callback_at', 'email', 'extension'];
        form.innerHTML = fields.map((f) => {
            const opts = (data.detected_columns || []).map((c) =>
                `<option value="${escapeHtml(c)}" ${data.suggested_mapping?.[f] === c ? 'selected' : ''}>${escapeHtml(c)}</option>`
            ).join('');
            return `<div class="mb-2"><label class="form-label">${f}</label><select class="form-select form-select-sm mapping-field" data-field="${f}"><option value="">—</option>${opts}</select></div>`;
        }).join('');
    }

    function initCallResultImportPage(importId) {
        currentImportId = importId;
        const viewModalEl = document.getElementById('row-view-modal');
        if (viewModalEl && window.bootstrap) {
            rowViewModal = new bootstrap.Modal(viewModalEl);
            viewModalEl.addEventListener('shown.bs.modal', () => {
                document.addEventListener('keydown', handleRowViewKeydown);
                viewModalEl.addEventListener('wheel', handleRowViewWheel, { passive: false });
            });
            viewModalEl.addEventListener('hidden.bs.modal', () => {
                document.removeEventListener('keydown', handleRowViewKeydown);
                viewModalEl.removeEventListener('wheel', handleRowViewWheel);
                currentViewRowId = null;
                renderFilteredList(activeFilterId, currentImportId);
            });
        }
        const hashFilter = getFilterFromHash();
        if (hashFilter && ROW_FILTERS.some((f) => f.id === hashFilter)) {
            activeFilterId = hashFilter;
        }
        loadImport(importId);
        document.getElementById('btn-delete-import')?.addEventListener('click', async () => {
            if (!confirm('Удалить импорт?')) return;
            await fetch(`/api/call-results/imports/${importId}`, { method: 'DELETE' });
            window.location.href = '/call-results';
        });
        document.getElementById('btn-restart')?.addEventListener('click', async () => {
            if (!confirm('Повторный парсинг удалит текущие строки и операции. Продолжить?')) return;
            const alertEl = document.getElementById('import-alert');
            const resp = await fetch(`/api/call-results/imports/${importId}/restart`, { method: 'POST' });
            if (resp.status === 409) {
                const data = await resp.json().catch(() => ({}));
                showAlert(alertEl, data.detail || 'Импорт уже обрабатывается', 'warning');
                return;
            }
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                showAlert(alertEl, data.detail || 'Не удалось запустить парсинг', 'danger');
                return;
            }
            loadImport(importId, { pendingProcessing: true });
        });
        const filterSendModalEl = document.getElementById('filter-send-modal');
        if (filterSendModalEl && window.bootstrap) {
            filterSendModal = new bootstrap.Modal(filterSendModalEl);
        }
        document.getElementById('btn-filter-send-confirm')?.addEventListener('click', () => {
            if (filterSendState.rowIds.length && currentImportId) {
                executeFilteredSend(currentImportId, filterSendState.rowIds);
            }
        });
    }

    function applyImportMeta(data) {
        const meta = document.getElementById('import-meta');
        if (meta && data.original_filename) {
            meta.textContent = data.original_filename;
        }
        const restartBtn = document.getElementById('btn-restart');
        if (restartBtn) restartBtn.disabled = data.status === 'processing';
    }

    async function loadImportDetail(importId, { reloadQueues = true } = {}) {
        const data = await fetchJson(`/api/call-results/imports/${importId}`);
        applyImportMeta(data);
        importSummaryCache = data.summary;
        importRowsCache = data.rows || [];
        buildActionsIndex(data.actions_by_method);
        manualReviewIds = new Set(data.manual_review_ids || []);
        hangupWithoutAnswersIds = new Set((data.hangup_rows || []).map((r) => r.id));
        attemptHistoryCache = data.attempt_history || [];
        if (reloadQueues) {
            await loadQueueCaches(importId);
        }
        await loadDiagnosticsCache();

        if (!filterInitialized) {
            const hashFilter = getFilterFromHash();
            if (hashFilter && ROW_FILTERS.some((f) => f.id === hashFilter)) {
                activeFilterId = hashFilter;
            } else {
                activeFilterId = 'manual_review';
            }
            filterInitialized = true;
        }

        renderSummaryFilters(importSummaryCache, activeFilterId);
        renderFilteredList(activeFilterId, importId);
        showLlmFailedAlert(importSummaryCache);
        lastStatusSignature = statusSignature(data);
    }

    function sleep(ms) {
        return new Promise((resolve) => { setTimeout(resolve, ms); });
    }

    async function loadDiagnosticsCache() {
        try {
            diagnosticsCache = await fetchJson('/api/call-results/diagnostics');
        } catch (e) {
            diagnosticsCache = null;
        }
        return diagnosticsCache;
    }

    function canSendToBitrix(resolveData) {
        const execEnabled = resolveData?.execution_enabled ?? diagnosticsCache?.execution_enabled;
        const webhookOk = diagnosticsCache?.bitrix_webhook_configured !== false;
        return !!execEnabled && webhookOk;
    }

    async function loadImport(importId, opts) {
        const options = normalizeLoadOptions(opts);
        currentImportId = importId;
        try {
            const statusData = await fetchJson(`/api/call-results/imports/${importId}/status`);
            applyImportMeta(statusData);
            lastStatusSignature = statusSignature(statusData);

            if (
                !options.fullReload
                && (
                    options.pendingProcessing
                    || statusData.status === 'processing'
                    || statusData.status === 'uploaded'
                )
            ) {
                importSummaryCache = statusData.summary;
                renderSummaryFilters(statusData.summary, activeFilterId);
                renderFilteredList(activeFilterId, importId, true);
                scheduleImportPoll(importId, statusData);
                return;
            }

            await loadImportDetail(importId, { reloadQueues: options.reloadQueues !== false });
            scheduleImportPoll(importId, statusData);
        } catch (e) { /* ignore poll errors */ }
    }

    function getFilterFromHash() {
        const m = window.location.hash.match(/^#filter=([a-z_]+)/);
        return m ? m[1] : null;
    }

    function buildActionsIndex(byMethod) {
        importActionsByRowId = {};
        Object.values(byMethod || {}).forEach((actions) => {
            (actions || []).forEach((a) => {
                if (!importActionsByRowId[a.import_row_id]) {
                    importActionsByRowId[a.import_row_id] = [];
                }
                importActionsByRowId[a.import_row_id].push(a);
            });
        });
    }

    async function loadQueueCaches(importId) {
        try {
            retryQueueCache = await fetchJson(`/api/call-results/retry-queue?import_id=${importId}`);
        } catch (e) {
            retryQueueCache = [];
        }
        try {
            contactSearchCache = await fetchJson(`/api/call-results/contact-search?import_id=${importId}`);
        } catch (e) {
            contactSearchCache = [];
        }
    }

    function getFilterContext() {
        return {
            manualReviewIds,
            hangupWithoutAnswersIds,
            importActionsByRowId,
        };
    }

    function getRowActions(row, ctx) {
        return (ctx?.importActionsByRowId || importActionsByRowId)[row.id] || [];
    }

    function isEnabledAction(action) {
        return action && action.is_enabled !== false;
    }

    function rowMatchesFilter(row, filterId, ctx) {
        const actions = getRowActions(row, ctx);
        const enabledActions = actions.filter(isEnabledAction);

        switch (filterId) {
        case 'all':
            return true;
        case 'manual_review':
        case 'manual_call':
        case 'auto_call':
            return row.ui_disposition === filterId;
        case 'new_contacts':
            return enabledActions.some((a) => a.method === 'crm.contact.add');
        case 'new_todos':
            return enabledActions.some((a) => a.method === 'crm.activity.todo.add');
        case 'new_comments':
            return enabledActions.some((a) => a.method === 'crm.timeline.comment.add');
        default:
            return false;
        }
    }

    function getFilteredRows(filterId) {
        const ctx = getFilterContext();
        return importRowsCache.filter((row) => rowMatchesFilter(row, filterId, ctx));
    }

    function getFilterHeaderAction(filterId) {
        return FILTER_ACTIONS[filterId] || null;
    }

    function renderFilterActionButton(filterId, rowCount) {
        const action = getFilterHeaderAction(filterId);
        if (!action || !rowCount) return '';
        const btnClass = action.type === 'export'
            ? 'btn-outline-primary btn-filter-export'
            : 'btn-success btn-filter-send';
        return `<button type="button" class="btn btn-sm ${btnClass}" data-filter-id="${escapeHtml(filterId)}">${escapeHtml(action.label)}</button>`;
    }

    function collectFilteredPhones(rows) {
        const seen = new Set();
        const phones = [];
        rows.forEach((row) => {
            const phone = row.normalized_phone || row.raw_phone;
            if (!phone || seen.has(phone)) return;
            seen.add(phone);
            phones.push(String(phone));
        });
        return phones;
    }

    function downloadTomoruPhonesCsv(phones, filename) {
        const content = `\uFEFFphone_number\r\n${phones.join('\r\n')}`;
        const blob = new Blob([content], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        link.click();
        URL.revokeObjectURL(url);
    }

    function exportFilteredList(filterId, importId) {
        const phones = collectFilteredPhones(getFilteredRows(filterId));
        if (!phones.length) return;
        downloadTomoruPhonesCsv(phones, `${filterId}_${importId}.csv`);
    }

    function getSendPreviewActions(row, filterId) {
        const methods = new Set(FILTER_SEND_METHODS[filterId] || []);
        return getRowActions(row).filter((a) => isEnabledAction(a) && methods.has(a.method));
    }

    function buildSendPreviewHtml(filterId, rows) {
        if (!rows.length) {
            return '<p class="text-muted mb-0">Нет строк для отправки.</p>';
        }
        return rows.map((row) => {
            const actions = getSendPreviewActions(row, filterId);
            const actionsHtml = actions.length
                ? actions.map((a) => `
                    <div class="filter-send-action mb-2">
                        <div class="small fw-semibold">${escapeHtml(METHOD_DESC[a.method] || a.method)}</div>
                        <div class="small text-muted">${escapeHtml(a.human_summary || '')}</div>
                        <pre class="filter-send-payload small mb-0">${escapeHtml(JSON.stringify(a.payload || {}, null, 2))}</pre>
                    </div>`).join('')
                : '<p class="small text-muted mb-0">Нет подготовленных запросов.</p>';
            return `
                <div class="card mb-2 filter-send-row">
                    <div class="card-header py-2 small">
                        <strong>Строка ${row.source_row_number}</strong>
                        <span class="text-muted ms-2">${escapeHtml(row.raw_phone || '—')}</span>
                        <span class="text-muted ms-2">${renderDealCell(row)}</span>
                    </div>
                    <div class="card-body py-2 filter-send-preview">${actionsHtml}</div>
                </div>`;
        }).join('');
    }

    async function openFilterSendModal(filterId, importId) {
        const filterDef = getFilterDef(filterId);
        const rows = getFilteredRows(filterId);
        filterSendState = { filterId, rowIds: rows.map((r) => r.id) };

        const titleEl = document.getElementById('filter-send-title');
        if (titleEl) titleEl.textContent = `Отправка в Битрикс24 — ${filterDef.label}`;

        const bodyEl = document.getElementById('filter-send-body');
        const confirmBtn = document.getElementById('btn-filter-send-confirm');
        const warningEl = document.getElementById('filter-send-warning');

        if (bodyEl) bodyEl.innerHTML = buildSendPreviewHtml(filterId, rows);

        const executionEnabled = !!diagnosticsCache?.execution_enabled;

        if (warningEl) {
            if (!executionEnabled) {
                warningEl.classList.remove('d-none');
                warningEl.textContent = 'Выполнение отключено (CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false).';
            } else {
                warningEl.classList.add('d-none');
                warningEl.textContent = '';
            }
        }
        if (confirmBtn) {
            confirmBtn.disabled = !executionEnabled || !rows.length;
        }

        filterSendModal?.show();
    }

    async function executeFilteredSend(importId, rowIds) {
        const alertEl = document.getElementById('import-alert');
        const confirmBtn = document.getElementById('btn-filter-send-confirm');
        try {
            if (confirmBtn) confirmBtn.disabled = true;
            const resp = await fetch(`/api/call-results/imports/${importId}/execute`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation_token: 'EXECUTE', row_ids: rowIds }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Execute недоступен');
            filterSendModal?.hide();
            showAlert(alertEl, data.message || 'Выполнение запущено', 'success');
            loadImport(importId, { fullReload: true });
        } catch (e) {
            showAlert(alertEl, e.message, 'danger');
        } finally {
            if (confirmBtn) confirmBtn.disabled = false;
        }
    }

    function bindFilterActionButton(block, filterId, importId) {
        block.querySelector('.btn-filter-export')?.addEventListener('click', () => {
            exportFilteredList(filterId, importId);
        });
        block.querySelector('.btn-filter-send')?.addEventListener('click', () => {
            openFilterSendModal(filterId, importId);
        });
    }

    function getAdjacentRowId(rowId, direction) {
        const rows = getFilteredRows(activeFilterId);
        const idx = rows.findIndex((r) => r.id === rowId);
        if (idx < 0) return null;
        const next = rows[idx + direction];
        return next ? next.id : null;
    }

    function isRowViewNavBlocked(e) {
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
        return !currentViewRowId || !currentImportId;
    }

    function navigateRowView(direction) {
        if (!currentViewRowId || !currentImportId) return false;
        const nextId = getAdjacentRowId(currentViewRowId, direction);
        if (nextId == null) return false;
        openRowViewer(currentImportId, nextId, { keepOpen: true });
        return true;
    }

    function getRowViewScrollContainer() {
        return document.getElementById('row-view-body');
    }

    function isRowViewScrolledToTop(el) {
        return !el || el.scrollTop <= 0;
    }

    function isRowViewScrolledToBottom(el) {
        if (!el) return true;
        return el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
    }

    function handleRowViewKeydown(e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        if (isRowViewNavBlocked(e)) return;

        const direction = e.key === 'ArrowLeft' ? -1 : 1;
        e.preventDefault();
        navigateRowView(direction);
    }

    function handleRowViewWheel(e) {
        if (isRowViewNavBlocked(e)) return;

        const deltaY = e.deltaY;
        if (deltaY === 0) return;

        const direction = deltaY > 0 ? 1 : -1;
        const body = getRowViewScrollContainer();
        const atBoundary = direction > 0
            ? isRowViewScrolledToBottom(body)
            : isRowViewScrolledToTop(body);
        if (!atBoundary) return;

        const nextId = getAdjacentRowId(currentViewRowId, direction);
        if (nextId == null) return;

        const now = Date.now();
        if (now - lastRowViewWheelNavAt < 300) return;

        e.preventDefault();
        if (navigateRowView(direction)) {
            lastRowViewWheelNavAt = now;
        }
    }

    function getFilterDef(filterId) {
        return ROW_FILTERS.find((f) => f.id === filterId) || ROW_FILTERS[0];
    }

    function setActiveFilter(filterId, updateHash) {
        if (updateHash !== false) {
            window.location.hash = `filter=${filterId}`;
        }
        activeFilterId = filterId;
        renderSummaryFilters(importSummaryCache, filterId);
        renderFilteredList(filterId, currentImportId);
    }

    function renderSummaryFilters(_s, activeId) {
        const el = document.getElementById('summary-cards');
        if (!el) return;
        el.innerHTML = ROW_FILTERS.map((f) => {
            const count = getFilteredRows(f.id).length;
            const isActive = f.id === activeId;
            const zeroClass = count === 0 ? ' summary-filter-card--zero' : '';
            return `<div class="col-6 col-md-4 col-lg">
                <button type="button" class="card summary-filter-card h-100 w-100 text-start${isActive ? ' active' : ''}${zeroClass}"
                    data-filter-id="${f.id}" aria-pressed="${isActive}">
                    <div class="card-body py-2">
                        <div class="text-muted small">${escapeHtml(f.label)}</div>
                        <div class="fs-6 fw-semibold">${count}</div>
                    </div>
                </button>
            </div>`;
        }).join('');
        el.querySelectorAll('.summary-filter-card').forEach((btn) => {
            btn.addEventListener('click', () => setActiveFilter(btn.dataset.filterId));
        });
    }

    function showLlmFailedAlert(s) {
        const alertEl = document.getElementById('import-alert');
        if (alertEl && (s.llm_failed || 0) > 0) {
            const n = s.llm_failed;
            const configFailed = (s.llm_failed_config || 0) > 0;
            const msg = configFailed
                ? `LLM не обработала ${n} строк(и). Проверьте OPENAI_API_KEY в Настройках или нажмите «Прогнать через ИИ».`
                : `ИИ ответила, но ${n} строк(и) не прошли проверку ответа. Для части из них применён fallback по расшифровке. Откройте строку для деталей или нажмите «Прогнать через ИИ».`;
            showAlert(alertEl, msg, 'warning');
        }
    }

    function getDealTitle(row) {
        const actions = importActionsByRowId[row.id] || [];
        const fromAction = actions.find((a) => a.deal_title);
        if (fromAction) return fromAction.deal_title;
        const match = (row.candidate_matches || []).find(
            (c) => c.local_id === row.matched_deal_local_id,
        );
        if (match) return match.title;
        if (row.matched_deal_id) return `Сделка #${row.matched_deal_id}`;
        return '—';
    }

    function bitrixLink(url, label) {
        if (!url) return escapeHtml(label);
        return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
    }

    function renderDealCell(row) {
        const title = getDealTitle(row);
        if (title === '—') return '—';
        return bitrixLink(row.matched_deal_bitrix_url, title);
    }

    function renderFilteredList(filterId, importId, processing) {
        const block = document.getElementById('filtered-list-block');
        if (!block) return;
        const filterDef = getFilterDef(filterId);

        if (processing) {
            block.innerHTML = `<div class="card">
                <div class="card-header d-flex align-items-center gap-2">
                    <span>${escapeHtml(filterDef.label)}</span>
                    <span class="badge bg-secondary">…</span>
                </div>
                <div class="card-body text-muted py-4 text-center">Импорт обрабатывается…</div>
            </div>`;
            return;
        }

        const rows = getFilteredRows(filterId);

        if (!rows.length) {
            block.innerHTML = `<div class="card filtered-list-card">
                <div class="card-header d-flex align-items-center gap-2">
                    <span class="fw-semibold">${escapeHtml(filterDef.label)}</span>
                    <span class="badge bg-primary">0</span>
                </div>
                <div class="card-body text-muted py-4 text-center">Нет записей в этой категории</div>
            </div>`;
            return;
        }

        block.innerHTML = `<div class="card filtered-list-card">
            <div class="card-header d-flex align-items-center gap-2">
                <span class="fw-semibold">${escapeHtml(filterDef.label)}</span>
                <span class="badge bg-primary">${rows.length}</span>
                <div class="ms-auto">${renderFilterActionButton(filterId, rows.length)}</div>
            </div>
            <div class="table-responsive">
                <table class="table table-sm table-hover mb-0 filtered-list-table">
                    <thead><tr>
                        <th>Строка</th><th>Телефон</th><th>Сделка</th><th>Сигналы</th><th></th>
                    </tr></thead>
                    <tbody>${rows.map((r) => `<tr data-row-id="${r.id}"${r.id === currentViewRowId ? ' class="table-active"' : ''}>
                        <td>${r.source_row_number}</td>
                        <td>${escapeHtml(r.raw_phone || '')}</td>
                        <td class="text-truncate" style="max-width:180px" title="${escapeHtml(getDealTitle(r))}">${renderDealCell(r)}</td>
                        <td>${signalBadges(r.business_signals, r)}</td>
                        <td><button type="button" class="btn btn-sm btn-primary btn-view-row" data-row-id="${r.id}">Просмотреть</button></td>
                    </tr>`).join('')}</tbody>
                </table>
            </div>
        </div>`;

        bindFilterActionButton(block, filterId, importId);
        block.querySelectorAll('.btn-view-row').forEach((btn) => {
            btn.addEventListener('click', () => openRowViewer(importId, parseInt(btn.dataset.rowId, 10)));
        });
    }

    function formatConversationTranscript(row) {
        const events = row.scenario_events || [];
        if (events.length) {
            return events
                .map((ev) => {
                    const text = (ev.transcription || ev.match || '').trim();
                    if (!text) return '';
                    const label = (ev.field || '').trim();
                    return label ? `${label}: ${text}` : text;
                })
                .filter(Boolean)
                .join('\n\n');
        }
        const sig = row.business_signals || {};
        const ext = row.extracted_data || {};
        return (ext.summary || sig.summary || row.comment || '').trim() || '—';
    }

    function renderSectionHeading(title) {
        return `<h6 class="fw-semibold border-bottom pb-1">${escapeHtml(title)}</h6>`;
    }

    function renderTranscriptBlock(row) {
        return `<div class="row-view-section">
            ${renderSectionHeading('Транскрибация разговора')}
            <div class="border rounded p-3 bg-light small manual-review-transcript">${escapeHtml(formatConversationTranscript(row))}</div>
        </div>`;
    }

    function renderContextBlock(row, extraRowsHtml = '') {
        return `<div class="row-view-section">
            ${renderSectionHeading('Контекст')}
            <dl class="row small mb-0">
                <dt class="col-sm-4">Телефон</dt><dd class="col-sm-8">${escapeHtml(row.raw_phone || '—')}</dd>
                <dt class="col-sm-4">Сделка</dt><dd class="col-sm-8">${renderDealCell(row)}</dd>
                ${row.manual_review_reason ? `<dt class="col-sm-4">Причина проверки</dt><dd class="col-sm-8 text-warning">${escapeHtml(row.manual_review_reason)}</dd>` : ''}
                ${extraRowsHtml}
            </dl>
        </div>`;
    }

    function renderManualReviewBody(row) {
        return renderTranscriptBlock(row) + renderContextBlock(row);
    }

    function renderManualReviewFooter(rowId) {
        return `
            <button type="button" class="btn btn-outline-secondary btn-sm btn-manual-resolve" data-action="comment" data-row-id="${rowId}">Комментарий</button>
            <button type="button" class="btn btn-outline-primary btn-sm btn-manual-resolve" data-action="todo" data-row-id="${rowId}">Дело</button>
            <button type="button" class="btn btn-outline-success btn-sm btn-manual-resolve" data-action="create_contact" data-row-id="${rowId}">Завести контакт в битриксе</button>
            <button type="button" class="btn btn-primary btn-sm btn-manual-resolve" data-action="find_contact" data-row-id="${rowId}">Найти другой контакт</button>
        `;
    }

    function renderPreviewConfirmFooter(rowId) {
        return `
            <button type="button" class="btn btn-outline-secondary btn-sm btn-manual-cancel" data-row-id="${rowId}">Отмена</button>
            <button type="button" class="btn btn-primary btn-sm btn-manual-confirm" data-row-id="${rowId}">Подтвердить</button>
        `;
    }

    function renderManualPreviewHints(action) {
        if (!MANUAL_BITRIX_SEND_ACTIONS.has(action)) return '';
        const hints = [
            '<div class="alert alert-info small mb-3 py-2">Шаг 1 из 2: проверьте данные и нажмите «Подтвердить». Затем отправьте действие в Bitrix24.</div>',
        ];
        const diag = diagnosticsCache || {};
        if (!diag.execution_enabled) {
            hints.push('<div class="alert alert-warning small mb-3 py-2">Отправка в Bitrix отключена (CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false). Действие будет только подготовлено.</div>');
        } else if (!diag.bitrix_webhook_configured) {
            hints.push('<div class="alert alert-warning small mb-3 py-2">Webhook Bitrix24 не настроен — отправка недоступна.</div>');
        }
        return hints.join('');
    }

    function renderPreparedBody(row, action, message, sendResult) {
        const filterId = ACTION_TO_FILTER[action];
        const previewHtml = filterId ? buildSendPreviewHtml(filterId, [row]) : '';
        let resultHtml = '';
        if (sendResult) {
            if (sendResult.ok) {
                resultHtml = `<div class="alert alert-success small mt-3 mb-0">${escapeHtml(SEND_SUCCESS_MESSAGES[action] || 'Успешно отправлено в Bitrix24')}</div>`;
            } else {
                resultHtml = `<div class="alert alert-danger small mt-3 mb-0">${escapeHtml(sendResult.message || 'Ошибка отправки в Bitrix24')}</div>`;
            }
        }
        const sendWarning = !sendResult && !canSendToBitrix()
            ? '<div class="alert alert-warning small mb-2 py-2">Отправка в Bitrix недоступна. Проверьте CALL_RESULTS_BITRIX_EXECUTION_ENABLED и webhook.</div>'
            : '';
        return `${renderTranscriptBlock(row)}${renderContextBlock(row)}
            <div class="row-view-section manual-prepared-section border border-success rounded p-3 bg-white mt-3">
                ${renderSectionHeading('Шаг 2: отправка в Bitrix24')}
                <p class="small text-muted mb-2">${escapeHtml(message || '')}</p>
                ${sendWarning}
                ${previewHtml}
                ${resultHtml}
            </div>`;
    }

    function renderPreparedFooter(rowId, { canSend, sendDone }) {
        let sendBtn = '';
        if (!sendDone) {
            if (canSend) {
                sendBtn = `<button type="button" class="btn btn-success btn-sm btn-manual-send-bitrix" data-row-id="${rowId}">Отправить в Bitrix24</button>`;
            } else {
                sendBtn = '<button type="button" class="btn btn-success btn-sm" disabled title="Отправка недоступна">Отправить в Bitrix24</button>';
            }
        }
        const nextClass = sendDone ? 'btn-primary' : 'btn-outline-primary';
        return `
            ${sendBtn}
            <button type="button" class="btn ${nextClass} btn-sm btn-manual-next-row" data-row-id="${rowId}">Следующая строка</button>
            <button type="button" class="btn btn-outline-secondary btn-sm btn-manual-close" data-row-id="${rowId}">Закрыть</button>
        `;
    }

    function searchMethodLabel(method) {
        if (method === 'ai_keywords') return 'Поиск по ключевым словам (ИИ)';
        if (method === 'lpr_fallback') return 'Поиск по правилам ЛПР';
        return method || '—';
    }

    function renderPreviewSection(action, previewData) {
        const heading = {
            comment: 'Текст комментария',
            todo: 'CRM-дело',
            create_contact: 'Новый контакт',
            find_contact: 'Найденный контакт',
        }[action] || 'Превью';

        let fieldsHtml = '';
        if (action === 'comment') {
            fieldsHtml = `<textarea class="form-control form-control-sm manual-preview-comment" rows="8">${escapeHtml(previewData.preview_text || '')}</textarea>`;
        } else if (action === 'todo') {
            fieldsHtml = `
                <label class="form-label small mb-1">Заголовок</label>
                <input type="text" class="form-control form-control-sm manual-preview-todo-title mb-2" value="${escapeHtml(previewData.todo_title || '')}">
                <label class="form-label small mb-1">Описание</label>
                <textarea class="form-control form-control-sm manual-preview-todo-description" rows="8">${escapeHtml(previewData.preview_text || '')}</textarea>`;
        } else if (action === 'create_contact') {
            const c = previewData.contact_data || {};
            fieldsHtml = `
                <dl class="row small mb-2">
                    <dt class="col-sm-4">Имя</dt><dd class="col-sm-8"><input type="text" class="form-control form-control-sm manual-preview-contact-name" value="${escapeHtml(c.name || '')}"></dd>
                    <dt class="col-sm-4">Телефон</dt><dd class="col-sm-8"><input type="text" class="form-control form-control-sm manual-preview-contact-phone" value="${escapeHtml(c.phone || '')}"></dd>
                    <dt class="col-sm-4">Должность</dt><dd class="col-sm-8"><input type="text" class="form-control form-control-sm manual-preview-contact-position" value="${escapeHtml(c.position || '')}"></dd>
                    <dt class="col-sm-4">Email</dt><dd class="col-sm-8"><input type="text" class="form-control form-control-sm manual-preview-contact-email" value="${escapeHtml(c.email || '')}"></dd>
                    <dt class="col-sm-4">Добавочный</dt><dd class="col-sm-8"><input type="text" class="form-control form-control-sm manual-preview-contact-extension" value="${escapeHtml(c.extension || '')}"></dd>
                </dl>`;
        } else if (action === 'find_contact') {
            const fc = previewData.found_contact || {};
            const keywords = (previewData.ai_keywords || []).join(', ') || '—';
            fieldsHtml = `
                <dl class="row small mb-0">
                    <dt class="col-sm-4">Контакт</dt><dd class="col-sm-8">${escapeHtml(fc.contact_name || `#${fc.contact_id || '—'}`)}</dd>
                    <dt class="col-sm-4">Телефон</dt><dd class="col-sm-8">${escapeHtml(fc.phone || '—')}</dd>
                    <dt class="col-sm-4">Метод</dt><dd class="col-sm-8">${escapeHtml(searchMethodLabel(previewData.search_method))}</dd>
                    <dt class="col-sm-4">Ключевые слова</dt><dd class="col-sm-8">${escapeHtml(keywords)}</dd>
                    <dt class="col-sm-4">Причина</dt><dd class="col-sm-8">${escapeHtml(fc.reason || '—')}</dd>
                </dl>`;
        }

        return `<div class="row-view-section manual-preview-section border border-primary rounded p-3 bg-white">
            ${renderManualPreviewHints(action)}
            ${renderSectionHeading(heading)}
            ${fieldsHtml}
        </div>`;
    }

    function renderPreviewBody(row, action, previewData) {
        return renderManualReviewBody(row) + renderPreviewSection(action, previewData);
    }

    function collectPreviewConfirmPayload(action, previewData) {
        if (action === 'comment') {
            return { preview_text: document.querySelector('.manual-preview-comment')?.value || previewData.preview_text || '' };
        }
        if (action === 'todo') {
            return {
                todo_title: document.querySelector('.manual-preview-todo-title')?.value || previewData.todo_title || '',
                todo_description: document.querySelector('.manual-preview-todo-description')?.value || previewData.preview_text || '',
            };
        }
        if (action === 'create_contact') {
            return {
                contact_data: {
                    name: document.querySelector('.manual-preview-contact-name')?.value || previewData.contact_data?.name || null,
                    phone: document.querySelector('.manual-preview-contact-phone')?.value || previewData.contact_data?.phone || null,
                    position: document.querySelector('.manual-preview-contact-position')?.value || previewData.contact_data?.position || null,
                    email: document.querySelector('.manual-preview-contact-email')?.value || previewData.contact_data?.email || null,
                    extension: document.querySelector('.manual-preview-contact-extension')?.value || previewData.contact_data?.extension || null,
                },
            };
        }
        if (action === 'find_contact') {
            const fc = previewData.found_contact || {};
            return {
                found_contact_id: fc.contact_id || null,
                found_phone: fc.phone || null,
            };
        }
        return {};
    }

    function resetManualReviewModal(importId, rowId) {
        modalReviewState = {
            mode: 'idle',
            action: null,
            previewData: null,
            preparedRowId: null,
            preparedMessage: null,
            sendResult: null,
            resolveData: null,
        };
        const row = importRowsCache.find((r) => r.id === rowId);
        if (!row) return;
        document.getElementById('row-view-body').innerHTML = renderManualReviewBody(row);
        const footer = document.getElementById('row-view-footer');
        if (footer) {
            footer.innerHTML = renderManualReviewFooter(rowId);
            bindManualReviewActions(importId, rowId);
        }
    }

    async function pollRowExecutionStatus(importId, rowId, filterId, { timeoutMs = 30000, intervalMs = 1000 } = {}) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const data = await fetchJson(`/api/call-results/imports/${importId}`);
            buildActionsIndex(data.actions_by_method);
            const freshRow = (data.rows || []).find((r) => r.id === rowId);
            if (freshRow) {
                const idx = importRowsCache.findIndex((r) => r.id === rowId);
                if (idx >= 0) importRowsCache[idx] = freshRow;
            }
            const row = importRowsCache.find((r) => r.id === rowId) || freshRow;
            if (!row) {
                await sleep(intervalMs);
                continue;
            }
            const actions = getSendPreviewActions(row, filterId);
            if (!actions.length) {
                await sleep(intervalMs);
                continue;
            }
            const terminal = actions.every((a) => ['succeeded', 'failed', 'skipped'].includes(a.execution_status));
            if (terminal) {
                const failed = actions.filter((a) => a.execution_status === 'failed');
                if (failed.length) {
                    const errMsg = failed.map((a) => a.last_error).filter(Boolean).join('; ')
                        || 'Ошибка отправки в Bitrix24';
                    return { ok: false, message: errMsg };
                }
                return { ok: true };
            }
            await sleep(intervalMs);
        }
        return { ok: false, message: 'Превышено время ожидания ответа Bitrix24' };
    }

    async function showPreparedState(importId, rowId, action, resolveData) {
        modalReviewState = {
            mode: 'prepared',
            action,
            previewData: null,
            preparedRowId: rowId,
            preparedMessage: resolveData.message || '',
            sendResult: null,
            resolveData,
        };
        const row = importRowsCache.find((r) => r.id === rowId);
        const body = document.getElementById('row-view-body');
        const footer = document.getElementById('row-view-footer');
        if (body && row) {
            body.innerHTML = renderPreparedBody(row, action, resolveData.message, null);
            body.scrollTop = 0;
        }
        if (footer) {
            footer.innerHTML = renderPreparedFooter(rowId, {
                canSend: canSendToBitrix(resolveData),
                sendDone: false,
            });
            bindPreparedFooterActions(importId, rowId, action, resolveData);
        }
    }

    async function executeRowSendFromModal(importId, rowId, action, resolveData) {
        const alertEl = document.getElementById('import-alert');
        const filterId = ACTION_TO_FILTER[action];
        const sendBtn = document.querySelector('.btn-manual-send-bitrix');
        const statusEl = document.querySelector('.manual-prepared-section');
        try {
            if (sendBtn) sendBtn.disabled = true;
            if (statusEl) {
                statusEl.insertAdjacentHTML('beforeend', '<div class="manual-send-progress text-center py-2"><div class="spinner-border spinner-border-sm"></div> Отправка в Bitrix24…</div>');
            }
            const resp = await fetch(`/api/call-results/imports/${importId}/execute`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation_token: 'EXECUTE', row_ids: [rowId] }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Execute недоступен');
            await loadImport(importId, { fullReload: true });
            const pollResult = await pollRowExecutionStatus(importId, rowId, filterId);
            modalReviewState.sendResult = pollResult;
            const row = importRowsCache.find((r) => r.id === rowId);
            const body = document.getElementById('row-view-body');
            const footer = document.getElementById('row-view-footer');
            if (body && row) {
                body.innerHTML = renderPreparedBody(row, action, resolveData.message, pollResult);
                body.scrollTop = 0;
            }
            if (footer) {
                footer.innerHTML = renderPreparedFooter(rowId, {
                    canSend: false,
                    sendDone: pollResult.ok,
                });
                bindPreparedFooterActions(importId, rowId, action, resolveData);
            }
            if (pollResult.ok) {
                showAlert(alertEl, SEND_SUCCESS_MESSAGES[action] || 'Отправлено в Bitrix24', 'success');
            } else {
                showAlert(alertEl, pollResult.message || 'Ошибка отправки', 'danger');
            }
        } catch (e) {
            document.querySelector('.manual-send-progress')?.remove();
            showAlert(alertEl, e.message, 'danger');
            if (sendBtn) sendBtn.disabled = false;
        }
    }

    function bindPreparedFooterActions(importId, rowId, action, resolveData) {
        document.querySelector('.btn-manual-send-bitrix')?.addEventListener('click', () => {
            executeRowSendFromModal(importId, rowId, action, resolveData);
        });
        document.querySelector('.btn-manual-next-row')?.addEventListener('click', () => {
            modalReviewState = {
                mode: 'idle',
                action: null,
                previewData: null,
                preparedRowId: null,
                preparedMessage: null,
                sendResult: null,
            };
            openNextFilteredRow(importId, rowId);
        });
        document.querySelector('.btn-manual-close')?.addEventListener('click', () => {
            modalReviewState = {
                mode: 'idle',
                action: null,
                previewData: null,
                preparedRowId: null,
                preparedMessage: null,
                sendResult: null,
            };
            rowViewModal?.hide();
        });
    }

    async function previewManualReview(importId, rowId, action) {
        const alertEl = document.getElementById('import-alert');
        const body = document.getElementById('row-view-body');
        const footer = document.getElementById('row-view-footer');
        if (MANUAL_BITRIX_SEND_ACTIONS.has(action) && !diagnosticsCache) {
            await loadDiagnosticsCache();
        }
        footer?.querySelectorAll('.btn-manual-resolve').forEach((btn) => { btn.disabled = true; });
        if (body) {
            body.innerHTML = `${renderManualReviewBody(importRowsCache.find((r) => r.id === rowId) || {})}
                <div class="text-center py-3 manual-preview-loading"><div class="spinner-border spinner-border-sm"></div> Формирование данных…</div>`;
        }
        try {
            const resp = await fetch(`/api/call-results/imports/${importId}/rows/${rowId}/manual-preview`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                const detail = data.detail;
                const msg = typeof detail === 'string' ? detail : (Array.isArray(detail) ? detail.map((d) => d.msg || d).join('; ') : 'Ошибка превью');
                throw new Error(msg);
            }
            modalReviewState = { mode: 'preview', action, previewData: data, preparedRowId: null, preparedMessage: null, sendResult: null };
            const row = importRowsCache.find((r) => r.id === rowId);
            if (body && row) body.innerHTML = renderPreviewBody(row, action, data);
            if (footer) {
                footer.innerHTML = renderPreviewConfirmFooter(rowId);
                bindPreviewConfirmActions(importId, rowId);
            }
        } catch (e) {
            showAlert(alertEl, e.message, 'danger');
            resetManualReviewModal(importId, rowId);
        }
    }

    async function resolveManualReview(importId, rowId, action, previewData) {
        const alertEl = document.getElementById('import-alert');
        const footer = document.getElementById('row-view-footer');
        footer?.querySelectorAll('.btn-manual-confirm, .btn-manual-cancel').forEach((btn) => { btn.disabled = true; });
        const confirmPayload = collectPreviewConfirmPayload(action, previewData);
        try {
            const resp = await fetch(`/api/call-results/imports/${importId}/rows/${rowId}/manual-resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action, confirmed: true, ...confirmPayload }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                const detail = data.detail;
                const msg = typeof detail === 'string' ? detail : (Array.isArray(detail) ? detail.map((d) => d.msg || d).join('; ') : 'Ошибка действия');
                throw new Error(msg);
            }
            if (MANUAL_BITRIX_SEND_ACTIONS.has(action)) {
                showAlert(alertEl, data.message || 'Подготовлено', 'success');
                await loadImport(importId, { fullReload: true });
                await showPreparedState(importId, rowId, action, data);
            } else {
                modalReviewState = {
                    mode: 'idle',
                    action: null,
                    previewData: null,
                    preparedRowId: null,
                    preparedMessage: null,
                    sendResult: null,
                };
                showAlert(alertEl, data.message || 'Готово', 'success');
                await loadImport(importId, { fullReload: true });
                openNextFilteredRow(importId, rowId);
            }
        } catch (e) {
            showAlert(alertEl, e.message, 'danger');
            footer?.querySelectorAll('.btn-manual-confirm, .btn-manual-cancel').forEach((btn) => { btn.disabled = false; });
        }
    }

    function openNextFilteredRow(importId, currentRowId) {
        const remaining = getFilteredRows(activeFilterId).filter((r) => r.id !== currentRowId);
        if (remaining.length) {
            openRowViewer(importId, remaining[0].id, { keepOpen: true });
            return;
        }
        rowViewModal?.hide();
    }

    function bindManualReviewActions(importId, rowId) {
        document.getElementById('row-view-footer')?.querySelectorAll('.btn-manual-resolve').forEach((btn) => {
            btn.addEventListener('click', () => {
                previewManualReview(importId, rowId, btn.dataset.action);
            });
        });
    }

    function bindPreviewConfirmActions(importId, rowId) {
        document.getElementById('row-view-footer')?.querySelector('.btn-manual-cancel')?.addEventListener('click', () => {
            resetManualReviewModal(importId, rowId);
        });
        document.getElementById('row-view-footer')?.querySelector('.btn-manual-confirm')?.addEventListener('click', () => {
            const { action, previewData } = modalReviewState;
            if (!action || !previewData) return;
            resolveManualReview(importId, rowId, action, previewData);
        });
    }

    function openRowViewer(importId, rowId, options = {}) {
        const { keepOpen = false } = options;
        const row = importRowsCache.find((r) => r.id === rowId);
        if (!row) return;
        currentViewRowId = rowId;
        if (modalReviewState.mode !== 'prepared' || modalReviewState.preparedRowId !== rowId) {
            modalReviewState = {
                mode: 'idle',
                action: null,
                previewData: null,
                preparedRowId: null,
                preparedMessage: null,
                sendResult: null,
            };
        }

        const filteredRows = getFilteredRows(activeFilterId);
        const rowIndex = filteredRows.findIndex((r) => r.id === rowId);
        const positionSuffix = rowIndex >= 0 ? ` (${rowIndex + 1} / ${filteredRows.length})` : '';
        document.getElementById('row-view-title').textContent =
            `Строка #${row.source_row_number} · ${row.raw_phone || '—'}${positionSuffix}`;

        const footer = document.getElementById('row-view-footer');
        const bodyEl = document.getElementById('row-view-body');
        if (modalReviewState.mode === 'prepared' && modalReviewState.preparedRowId === rowId) {
            if (bodyEl) {
                bodyEl.innerHTML = renderPreparedBody(
                    row,
                    modalReviewState.action,
                    modalReviewState.preparedMessage,
                    modalReviewState.sendResult,
                );
            }
            if (footer) {
                footer.innerHTML = renderPreparedFooter(rowId, {
                    canSend: canSendToBitrix(modalReviewState.resolveData),
                    sendDone: !!modalReviewState.sendResult?.ok,
                });
                bindPreparedFooterActions(importId, rowId, modalReviewState.action, modalReviewState.resolveData);
            }
        } else {
            if (bodyEl) bodyEl.innerHTML = renderManualReviewBody(row);
            if (footer) {
                footer.innerHTML = renderManualReviewFooter(rowId);
                bindManualReviewActions(importId, rowId);
            }
        }

        renderFilteredList(activeFilterId, importId);

        if (keepOpen) {
            const body = document.getElementById('row-view-body');
            if (body) body.scrollTop = 0;
        } else {
            rowViewModal?.show();
        }
    }

    function signalBadges(signals, row) {
        const tooltip = signalTooltip(row);
        const titleAttr = tooltip ? ` title="${escapeHtml(tooltip)}"` : '';
        if (!signals) {
            return `<span class="text-muted"${titleAttr}>—</span>`;
        }
        const badges = Object.entries(SIGNAL_LABELS)
            .filter(([k]) => signals[k])
            .map(([k, label]) => {
                const cls = k === 'needs_manual_review' ? 'bg-warning text-dark' : 'bg-primary';
                return `<span class="badge ${cls} me-1">${label}</span>`;
            })
            .join('');
        if (badges) return badges;
        return `<span class="text-muted"${titleAttr}>—</span>`;
    }

    function signalTooltip(row) {
        if (!row) return '';
        const parts = [];
        if (row.manual_review_reason) parts.push(row.manual_review_reason);
        if (row.llm_validation_errors?.length) parts.push(row.llm_validation_errors[0]);
        if (row.deterministic_reason && !row.business_signals) parts.push(row.deterministic_reason);
        return parts.join('; ');
    }

    window.initCallResultsUploadPage = initCallResultsUploadPage;
    window.initCallResultImportPage = initCallResultImportPage;
})();
