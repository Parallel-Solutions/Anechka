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
        hangup_during_robocall: 'Бросил без разговора',
        replacement_contact_required: 'Перезвон на другой номер',
        needs_manual_review: 'Ручная проверка',
    };

    const SIGNAL_BADGE_CLASSES = {
        positive: 'bg-success',
        alternate_contact_requested: 'bg-info text-dark',
        callback_later_requested: 'bg-primary',
        replacement_contact_required: 'bg-primary',
        no_answer: 'bg-secondary',
        deal_not_found: 'bg-danger',
        explicit_refusal: 'bg-danger',
        hangup_without_result: 'bg-warning text-dark',
        hangup_during_robocall: 'bg-secondary',
        needs_manual_review: 'bg-warning text-dark',
    };

    const BUSINESS_GROUP_BADGE_CLASSES = {
        conversation_yes: 'bg-success',
        conversation_no: 'bg-danger',
        callback_same: 'bg-primary',
        callback_other: 'bg-info text-dark',
        conversation_unclear: 'bg-warning text-dark',
        no_answer: 'bg-secondary',
        other: 'bg-dark',
    };

    const HANGUP_REPLACEMENT_SIGNAL_KEYS = ['hangup_without_result', 'replacement_contact_required'];
    const HANGUP_REPLACEMENT_BADGE = {
        label: 'Сброс трубки',
        cls: 'bg-warning text-dark',
    };

    const METHOD_DESC = {
        'crm.timeline.comment.add': 'Комментарий в таймлайн сделки (отказ)',
        'crm.activity.todo.add': 'CRM-дело по положительному результату',
        'tasks.task.add': 'Задача по положительному результату (привязка к сделке)',
        'crm.contact.list': 'Поиск контакта по телефону',
        'crm.contact.add': 'Создание контакта',
        'crm.deal.contact.add': 'Привязка контакта к сделке',
        'retry_queue.add': 'Очередь повторных звонков',
        'contact_search.add': 'Требуется поиск нового контакта',
    };

    const EXECUTION_STATUS_LABELS = {
        prepared: 'Ожидает',
        executing: 'Выполняется',
        succeeded: 'Успех',
        failed: 'Ошибка',
        skipped: 'Пропущено',
    };

    const EXECUTION_STATUS_BADGE = {
        prepared: 'bg-secondary',
        executing: 'bg-primary',
        succeeded: 'bg-success',
        failed: 'bg-danger',
        skipped: 'bg-warning text-dark',
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
        { id: 'new_todos', label: 'Новые задачи' },
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
        new_todos: ['tasks.task.add', 'crm.activity.todo.add'],
        new_comments: ['crm.timeline.comment.add'],
    };

    const FILTER_MATCHERS = {
        all: () => true,
        manual_review: (row) => row.row_filter === 'manual_review',
        manual_call: (row) =>
            row.row_filter === 'manual_call' || row.ui_disposition === 'manual_call',
        auto_call: (row) => row.row_filter === 'auto_call',
        new_contacts: (row) => row.row_filter === 'new_contacts',
        new_todos: (row) => row.row_filter === 'new_todos',
        new_comments: (row) => row.row_filter === 'new_comments',
    };

    const MANUAL_BITRIX_SEND_ACTIONS = new Set(['comment', 'todo', 'create_contact']);

    const ACTION_TO_FILTER = {
        comment: 'new_comments',
        todo: 'new_todos',
        create_contact: 'new_contacts',
        find_contact: 'auto_call',
    };

    const MANUAL_ACTION_BUTTONS = {
        comment: (rowId) => `<button type="button" class="btn btn-outline-secondary btn-sm btn-manual-resolve" data-action="comment" data-row-id="${rowId}">Комментарий</button>`,
        todo: (rowId) => `<button type="button" class="btn btn-outline-primary btn-sm btn-manual-resolve" data-action="todo" data-row-id="${rowId}">Дело</button>`,
        create_contact: (rowId) => `<button type="button" class="btn btn-outline-success btn-sm btn-manual-resolve" data-action="create_contact" data-row-id="${rowId}">Завести контакт в битриксе</button>`,
        find_contact: (rowId) => `<button type="button" class="btn btn-primary btn-sm btn-manual-resolve" data-action="find_contact" data-row-id="${rowId}">Найти другой контакт</button>`,
    };

    function parseApiErrorDetail(detail, fallback) {
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) {
            return detail.map((d) => d.msg || d.message || d).join('; ');
        }
        if (detail && typeof detail === 'object') {
            return detail.message || detail.msg || fallback;
        }
        return fallback;
    }

    function showRowViewError(row, message) {
        const body = document.getElementById('row-view-body');
        if (!body) return;
        body.innerHTML = `${renderManualReviewBody(row)}<div class="alert alert-danger small mt-3 mb-0">${escapeHtml(message)}</div>`;
    }

    const SEND_SUCCESS_MESSAGES = {
        comment: 'Комментарий добавлен в Bitrix24',
        todo: 'Задача создана в Bitrix24',
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
    let retryQueueLoaded = false;
    let contactSearchLoaded = false;
    const FILTERS_NEEDING_RETRY_QUEUE = new Set(['manual_call']);
    const FILTERS_NEEDING_CONTACT_SEARCH = new Set(['new_contacts']);
    let activeFilterId = 'manual_review';
    let filterInitialized = false;
    let currentImportId = null;
    let rowViewModal = null;
    let filterSendModal = null;
    let filterSendState = { filterId: null, rowIds: [], sending: false, sendDone: false };
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
    let currentUserCache = null;
    let lastRowViewWheelNavAt = 0;
    let importPollTimer = null;
    let lastStatusSignature = null;
    let currentImportStatus = null;
    let lastLlmCompletedForStall = null;
    let stallPollCount = 0;
    const STALL_POLL_THRESHOLD = 30;

    function resetStallTracking() {
        lastLlmCompletedForStall = null;
        stallPollCount = 0;
    }

    function updateRetryLlmUi(statusData) {
        const alertEl = document.getElementById('import-alert');
        const retryBtn = document.getElementById('btn-retry-llm');
        if (!retryBtn) return;

        const status = statusData.status;
        const summary = statusData.summary || {};
        const llmSent = summary.llm_sent ?? 0;
        const llmCompleted = summary.llm_completed ?? 0;
        const hasLlmWork = llmSent > llmCompleted;
        const showRetry = hasLlmWork && (status === 'processing' || status === 'uploaded' || status === 'failed');

        if (!showRetry) {
            retryBtn.classList.add('d-none');
            retryBtn.disabled = true;
            if (status === 'ready') resetStallTracking();
            return;
        }

        retryBtn.classList.remove('d-none');

        if (status === 'failed') {
            retryBtn.disabled = false;
            if (alertEl) {
                showAlert(
                    alertEl,
                    'Обработка прервана. Нажмите «Повторить ИИ», чтобы продолжить классификацию строк.',
                    'warning',
                );
            }
            return;
        }

        if (lastLlmCompletedForStall === llmCompleted) {
            stallPollCount += 1;
        } else {
            stallPollCount = 0;
            lastLlmCompletedForStall = llmCompleted;
        }

        const stalled = stallPollCount >= STALL_POLL_THRESHOLD;
        retryBtn.disabled = !stalled;
        if (stalled && alertEl) {
            showAlert(
                alertEl,
                'Обработка, похоже, зависла. Нажмите «Повторить ИИ», чтобы продолжить классификацию строк.',
                'warning',
            );
        } else if (alertEl && alertEl.querySelector('.alert-warning')) {
            alertEl.innerHTML = '';
        }
    }

    function statusSignature(data) {
        const s = data.summary || {};
        return JSON.stringify({
            status: data.status,
            processed_at: data.processed_at,
            execute_status: s.execute_status,
            total_rows: s.total_rows,
            llm_sent: s.llm_sent,
            llm_completed: s.llm_completed,
            llm_pending: s.llm_pending,
            prepared_operations: s.prepared_operations,
            executed_operations: s.executed_operations,
            execution_errors: s.execution_errors,
        });
    }

    function renderImportProgress(statusData) {
        const container = document.getElementById('import-progress');
        const labelEl = document.getElementById('import-progress-label');
        const textEl = document.getElementById('import-progress-text');
        const barEl = document.getElementById('import-progress-bar');
        if (!container || !labelEl || !textEl || !barEl) return;

        const status = statusData?.status;
        const summary = statusData?.summary || {};
        const processing = status === 'processing' || status === 'uploaded';
        if (!processing) {
            container.classList.add('d-none');
            return;
        }

        container.classList.remove('d-none');
        const totalRows = summary.total_rows ?? 0;
        const llmSent = summary.llm_sent ?? 0;
        const llmCompleted = summary.llm_completed ?? 0;

        if (totalRows === 0 || llmSent === 0) {
            labelEl.textContent = 'Читаем файл и сопоставляем телефоны…';
            textEl.textContent = '';
            barEl.className = 'progress-bar progress-bar-striped progress-bar-animated';
            barEl.style.width = '100%';
            barEl.textContent = '';
            barEl.removeAttribute('aria-valuenow');
            barEl.removeAttribute('aria-valuemin');
            barEl.removeAttribute('aria-valuemax');
            return;
        }

        const pct = Math.min(100, Math.round((llmCompleted / llmSent) * 100));
        labelEl.textContent = 'Классификация ИИ';
        textEl.textContent = `${llmCompleted} из ${llmSent}`;
        barEl.className = 'progress-bar';
        barEl.style.width = `${pct}%`;
        barEl.textContent = `${pct}%`;
        barEl.setAttribute('aria-valuenow', String(pct));
        barEl.setAttribute('aria-valuemin', '0');
        barEl.setAttribute('aria-valuemax', '100');
    }

    function showIndeterminateProgress(label) {
        const container = document.getElementById('import-progress');
        const labelEl = document.getElementById('import-progress-label');
        const textEl = document.getElementById('import-progress-text');
        const barEl = document.getElementById('import-progress-bar');
        if (!container || !labelEl || !textEl || !barEl) return;
        container.classList.remove('d-none');
        labelEl.textContent = label || 'Читаем файл и сопоставляем телефоны…';
        textEl.textContent = '';
        barEl.className = 'progress-bar progress-bar-striped progress-bar-animated';
        barEl.style.width = '100%';
        barEl.textContent = '';
        barEl.removeAttribute('aria-valuenow');
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
                    renderImportProgress(statusData);
                    renderSummaryFilters(statusData.summary, activeFilterId);
                    renderFilteredList(activeFilterId, importId, true);
                    updateSendAndExportButton();
                } else {
                    await loadImportDetail(importId, { reloadQueues: false });
                }
            }
            scheduleImportPoll(importId, statusData);
            updateRetryLlmUi(statusData);
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
        activeFilterId = resolveInitialFilter();
        window.addEventListener('hashchange', () => {
            const hashFilter = getFilterFromHash();
            if (hashFilter && ROW_FILTERS.some((f) => f.id === hashFilter) && hashFilter !== activeFilterId) {
                setActiveFilter(hashFilter, false);
            }
        });
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
            resetStallTracking();
            showIndeterminateProgress('Читаем файл и сопоставляем телефоны…');
            loadImport(importId, { pendingProcessing: true });
        });
        document.getElementById('btn-retry-llm')?.addEventListener('click', async () => {
            const alertEl = document.getElementById('import-alert');
            const retryBtn = document.getElementById('btn-retry-llm');
            if (retryBtn) retryBtn.disabled = true;
            const resp = await fetch(`/api/call-results/imports/${importId}/retry-llm`, { method: 'POST' });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                showAlert(alertEl, data.detail || 'Не удалось запустить повтор ИИ', 'danger');
                if (retryBtn) retryBtn.disabled = false;
                return;
            }
            resetStallTracking();
            if (alertEl) alertEl.innerHTML = '';
            loadImport(importId, { pendingProcessing: true });
        });
        const filterSendModalEl = document.getElementById('filter-send-modal');
        if (filterSendModalEl && window.bootstrap) {
            filterSendModal = new bootstrap.Modal(filterSendModalEl);
        }
        document.getElementById('btn-filter-send-confirm')?.addEventListener('click', () => {
            if (filterSendState.sendDone) {
                filterSendModal?.hide();
                return;
            }
            if (filterSendState.rowIds.length && currentImportId && !filterSendState.sending) {
                executeFilteredSend(currentImportId, filterSendState.rowIds);
            }
        });
        document.getElementById('btn-send-and-export')?.addEventListener('click', () => {
            if (currentImportId) runSendAndExport(currentImportId);
        });
    }

    function applyImportMeta(data) {
        currentImportStatus = data.status ?? currentImportStatus;
        const meta = document.getElementById('import-meta');
        if (meta && data.original_filename) {
            meta.textContent = data.campaign_name
                ? data.campaign_name + ' — ' + data.original_filename
                : data.original_filename;
        }
        const restartBtn = document.getElementById('btn-restart');
        if (restartBtn) restartBtn.disabled = data.status === 'processing';
        renderImportProgress(data);
        updateRetryLlmUi(data);
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
        resetQueueCacheState();

        if (!filterInitialized) {
            activeFilterId = resolveInitialFilter();
            filterInitialized = true;
        }

        renderSummaryFilters(importSummaryCache, activeFilterId);
        renderFilteredList(activeFilterId, importId);
        showLlmFailedAlert(importSummaryCache);
        lastStatusSignature = statusSignature(data);

        const parallelTasks = [
            loadDiagnosticsCache(),
            loadCurrentUserCache(),
        ];
        if (reloadQueues && filterNeedsQueueCaches(activeFilterId)) {
            parallelTasks.push(ensureQueueCachesForFilter(importId, activeFilterId));
        }
        void Promise.all(parallelTasks).then(() => {
            updateSendAndExportButton();
        });
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

    async function loadCurrentUserCache() {
        try {
            const data = await fetchJson('/auth/me');
            currentUserCache = data.user || null;
        } catch (e) {
            currentUserCache = null;
        }
        return currentUserCache;
    }

    function currentUserBitrixId() {
        const id = currentUserCache?.crm_user_external_id;
        return id != null ? id : null;
    }

    function taskResponsibleUserId(action) {
        const operatorId = currentUserBitrixId();
        if (operatorId != null) return operatorId;
        if (action?.task_responsible_user_id != null) return action.task_responsible_user_id;
        const serviceId = diagnosticsCache?.bitrix_service_user_id;
        return serviceId > 0 ? serviceId : null;
    }

    function applyPreviewResponsibleUser(payload, method, action) {
        const copy = JSON.parse(JSON.stringify(payload || {}));
        if (method === 'crm.activity.todo.add') {
            const userId = taskResponsibleUserId(action);
            if (userId != null) copy.responsibleId = userId;
        } else if (method === 'tasks.task.add') {
            const userId = taskResponsibleUserId(action);
            if (userId != null) {
                const fields = { ...(copy.fields || copy) };
                fields.RESPONSIBLE_ID = userId;
                fields.CREATED_BY = userId;
                copy.fields = fields;
            }
        }
        return copy;
    }

    function canSendToBitrix(resolveData, preparedActions) {
        const execEnabled = resolveData?.execution_enabled ?? diagnosticsCache?.execution_enabled;
        const webhookOk = diagnosticsCache?.bitrix_webhook_configured !== false;
        if (!execEnabled || !webhookOk) return false;
        const actions = preparedActions || [];
        const needsResponsible = actions.some(
            (a) => (a.method === 'crm.activity.todo.add' || a.method === 'tasks.task.add') && a.is_enabled !== false,
        );
        if (needsResponsible) {
            return actions.some((a) => taskResponsibleUserId(a) != null);
        }
        return true;
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
                renderImportProgress(statusData);
                renderSummaryFilters(statusData.summary, activeFilterId);
                renderFilteredList(activeFilterId, importId, true);
                updateSendAndExportButton();
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

    function resolveInitialFilter() {
        const hashFilter = getFilterFromHash();
        if (hashFilter && ROW_FILTERS.some((f) => f.id === hashFilter)) return hashFilter;
        return 'manual_review';
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

    function resetQueueCacheState() {
        retryQueueCache = [];
        contactSearchCache = [];
        retryQueueLoaded = false;
        contactSearchLoaded = false;
    }

    function filterNeedsQueueCaches(filterId) {
        return FILTERS_NEEDING_RETRY_QUEUE.has(filterId)
            || FILTERS_NEEDING_CONTACT_SEARCH.has(filterId);
    }

    async function loadQueueCaches(importId, { retry = true, contact = true } = {}) {
        const tasks = [];
        if (retry && !retryQueueLoaded) {
            tasks.push(
                fetchJson(`/api/call-results/retry-queue?import_id=${importId}`)
                    .then((data) => { retryQueueCache = data; })
                    .catch(() => { retryQueueCache = []; })
                    .finally(() => { retryQueueLoaded = true; }),
            );
        }
        if (contact && !contactSearchLoaded) {
            tasks.push(
                fetchJson(`/api/call-results/contact-search?import_id=${importId}`)
                    .then((data) => { contactSearchCache = data; })
                    .catch(() => { contactSearchCache = []; })
                    .finally(() => { contactSearchLoaded = true; }),
            );
        }
        if (tasks.length) {
            await Promise.all(tasks);
        }
    }

    async function ensureQueueCachesForFilter(importId, filterId) {
        const needRetry = FILTERS_NEEDING_RETRY_QUEUE.has(filterId) && !retryQueueLoaded;
        const needContact = FILTERS_NEEDING_CONTACT_SEARCH.has(filterId) && !contactSearchLoaded;
        if (!needRetry && !needContact) return;
        await loadQueueCaches(importId, { retry: needRetry, contact: needContact });
    }

    function getRowActions(row) {
        return importActionsByRowId[row.id] || [];
    }

    function isEnabledAction(action) {
        return action && action.is_enabled !== false;
    }

    function rowMatchesFilter(row, filterId) {
        const match = FILTER_MATCHERS[filterId];
        if (!match) return false;
        return match(row);
    }

    function getFilteredRows(filterId) {
        return importRowsCache.filter((row) => rowMatchesFilter(row, filterId));
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
            const phone = row.dial_phone || row.normalized_phone || row.raw_phone;
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

    const BITRIX_SEND_FILTERS = ['new_comments', 'new_contacts', 'new_todos'];

    function collectBitrixSendRowIds() {
        const seen = new Set();
        const ids = [];
        BITRIX_SEND_FILTERS.forEach((filterId) => {
            getFilteredRows(filterId).forEach((row) => {
                if (seen.has(row.id)) return;
                seen.add(row.id);
                ids.push(row.id);
            });
        });
        return ids;
    }

    function updateSendAndExportButton() {
        const btn = document.getElementById('btn-send-and-export');
        if (!btn) return;
        const processing = currentImportStatus === 'processing' || currentImportStatus === 'uploaded';
        const executing = importSummaryCache?.execute_status === 'executing';
        const sendCount = collectBitrixSendRowIds().length;
        const autoCount = collectFilteredPhones(getFilteredRows('auto_call')).length;
        btn.disabled = processing || executing || (sendCount === 0 && autoCount === 0);
    }

    async function runSendAndExport(importId) {
        const alertEl = document.getElementById('import-alert');
        const btn = document.getElementById('btn-send-and-export');
        const sendRowIds = collectBitrixSendRowIds();
        const autoPhones = collectFilteredPhones(getFilteredRows('auto_call'));
        const messages = [];
        let sendStarted = false;

        try {
            if (btn) btn.disabled = true;

            if (sendRowIds.length > 0) {
                if (!diagnosticsCache) await loadDiagnosticsCache();
                if (!diagnosticsCache?.execution_enabled) {
                    messages.push(
                        'Отправка в Bitrix24 пропущена: выполнение отключено (CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false).',
                    );
                } else {
                    const resp = await fetch(`/api/call-results/imports/${importId}/execute`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ confirmation_token: 'EXECUTE', row_ids: sendRowIds }),
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok) throw new Error(data.detail || 'Execute недоступен');
                    messages.push(data.message || `Отправка в Bitrix24 запущена (${sendRowIds.length} строк).`);
                    sendStarted = true;
                }
            }

            if (autoPhones.length > 0) {
                exportFilteredList('auto_call', importId);
                messages.push(`Скачан список автобзвона: ${autoPhones.length} телефонов.`);
            }

            if (!messages.length) {
                showAlert(alertEl, 'Нечего отправлять или выгружать.', 'warning');
                updateSendAndExportButton();
                return;
            }

            const hasSendSkip = sendRowIds.length > 0 && !sendStarted;
            showAlert(alertEl, messages.join(' '), hasSendSkip ? 'warning' : 'success');

            if (sendStarted) {
                loadImport(importId, { fullReload: true });
            } else {
                updateSendAndExportButton();
            }
        } catch (e) {
            showAlert(alertEl, e.message, 'danger');
            updateSendAndExportButton();
        }
    }

    function getSendPreviewActions(row, filterId) {
        const methods = new Set(FILTER_SEND_METHODS[filterId] || []);
        return getRowActions(row).filter((a) => isEnabledAction(a) && methods.has(a.method));
    }

    function renderExecutionStatusBadge(status) {
        const s = status || 'prepared';
        const cls = EXECUTION_STATUS_BADGE[s] || 'bg-secondary';
        const label = EXECUTION_STATUS_LABELS[s] || s;
        return `<span class="badge ${cls} filter-send-status">${escapeHtml(label)}</span>`;
    }

    function resetFilterSendModalUi() {
        const progressEl = document.getElementById('filter-send-progress');
        const confirmBtn = document.getElementById('btn-filter-send-confirm');
        if (progressEl) {
            progressEl.classList.add('d-none');
            progressEl.innerHTML = '';
        }
        if (confirmBtn) {
            confirmBtn.textContent = 'Отправить в Битрикс24';
            confirmBtn.classList.remove('btn-outline-secondary');
            confirmBtn.classList.add('btn-success');
            confirmBtn.disabled = false;
        }
        filterSendState.sending = false;
        filterSendState.sendDone = false;
    }

    function renderFilterSendProgress(data) {
        const items = data?.items || [];
        const total = items.length;
        if (!total) {
            return '<div class="d-flex align-items-center gap-2"><div class="spinner-border spinner-border-sm"></div><span>Отправка в Bitrix24…</span></div>';
        }
        const done = items.filter((i) => ['succeeded', 'failed', 'skipped'].includes(i.execution_status)).length;
        const failed = items.filter((i) => i.execution_status === 'failed').length;
        const executing = data?.execute_status === 'executing' || done < total;
        if (executing) {
            return `<div class="d-flex align-items-center gap-2"><div class="spinner-border spinner-border-sm"></div><span>Отправка… ${done} из ${total}</span></div>`;
        }
        const cls = failed ? 'alert-warning' : 'alert-success';
        const succeeded = done - failed;
        return `<div class="alert ${cls} small mb-0 py-2">Готово: ${succeeded} успешно, ${failed} с ошибкой</div>`;
    }

    function formatBitrixExternalId(externalId) {
        if (!externalId) return '';
        const raw = String(externalId).trim();
        if (/^\d+$/.test(raw)) return raw;
        const match = raw.match(/['"]?id['"]?\s*[:=]\s*(\d+)/i);
        return match ? match[1] : raw;
    }

    function renderBitrixExternalId(item) {
        if (!item.external_id) return '';
        const label = formatBitrixExternalId(item.external_id);
        const url = item.bitrix_external_url;
        const idHtml = url
            ? bitrixLink(url, label)
            : `<code>${escapeHtml(label)}</code>`;
        return `<div class="small text-muted mb-1">ID в Bitrix: ${idHtml}</div>`;
    }

    function buildSendLogHtml(filterId, rows, logItems) {
        if (!rows.length) {
            return '<p class="text-muted mb-0">Нет строк для отправки.</p>';
        }
        const itemsByRow = {};
        (logItems || []).forEach((item) => {
            if (!itemsByRow[item.import_row_id]) itemsByRow[item.import_row_id] = [];
            itemsByRow[item.import_row_id].push(item);
        });
        return rows.map((row) => {
            const items = itemsByRow[row.id] || [];
            const previewActions = getSendPreviewActions(row, filterId);
            const previewById = {};
            previewActions.forEach((a) => { previewById[a.id] = a; });
            const actionsHtml = items.length
                ? items.map((item) => {
                    const previewAction = previewById[item.id];
                    const requestDetails = previewAction
                        ? `<details class="mb-2"><summary class="small text-muted">Запрос</summary><pre class="filter-send-payload small mb-0 mt-1">${escapeHtml(JSON.stringify(applyPreviewResponsibleUser(previewAction.payload, previewAction.method, previewAction), null, 2))}</pre></details>`
                        : '';
                    const errHtml = item.last_error
                        ? `<div class="small text-danger mb-1">${escapeHtml(item.last_error)}</div>`
                        : '';
                    const extHtml = renderBitrixExternalId(item);
                    const terminal = ['succeeded', 'failed', 'skipped'].includes(item.execution_status);
                    const respJson = item.response_payload != null
                        ? JSON.stringify(item.response_payload, null, 2)
                        : (terminal ? '{}' : '—');
                    return `
                    <div class="filter-send-action mb-2">
                        <div class="d-flex align-items-center gap-2 mb-1 flex-wrap">
                            <span class="small fw-semibold">${escapeHtml(METHOD_DESC[item.method] || item.method)}</span>
                            ${renderExecutionStatusBadge(item.execution_status)}
                        </div>
                        <div class="small text-muted mb-1">${escapeHtml(item.human_summary || '')}</div>
                        ${errHtml}
                        ${extHtml}
                        ${requestDetails}
                        <div class="small fw-semibold mt-1">Ответ Bitrix24</div>
                        <pre class="filter-send-response small mb-0">${escapeHtml(respJson)}</pre>
                    </div>`;
                }).join('')
                : '<p class="small text-muted mb-0">Ожидание действий…</p>';
            return `
                <div class="card mb-2 filter-send-row">
                    <div class="card-header py-2 small">
                        <strong>Строка ${row.source_row_number}</strong>
                        <span class="text-muted ms-2">${renderPhoneLink(row.raw_phone)}</span>
                        <span class="text-muted ms-2">${renderDealCell(row)}</span>
                    </div>
                    <div class="card-body py-2 filter-send-preview">${actionsHtml}</div>
                </div>`;
        }).join('');
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
                        <pre class="filter-send-payload small mb-0">${escapeHtml(JSON.stringify(applyPreviewResponsibleUser(a.payload, a.method, a), null, 2))}</pre>
                    </div>`).join('')
                : '<p class="small text-muted mb-0">Нет подготовленных запросов.</p>';
            return `
                <div class="card mb-2 filter-send-row">
                    <div class="card-header py-2 small">
                        <strong>Строка ${row.source_row_number}</strong>
                        <span class="text-muted ms-2">${renderPhoneLink(row.raw_phone)}</span>
                        <span class="text-muted ms-2">${renderDealCell(row)}</span>
                    </div>
                    <div class="card-body py-2 filter-send-preview">${actionsHtml}</div>
                </div>`;
        }).join('');
    }

    async function pollFilterSendStatus(importId, rowIds, { timeoutMs = 60000, intervalMs = 1000, onUpdate } = {}) {
        const rowIdsParam = rowIds.join(',');
        const deadline = Date.now() + timeoutMs;
        let lastData = null;
        while (Date.now() < deadline) {
            const data = await fetchJson(`/api/call-results/imports/${importId}/execute/status?row_ids=${rowIdsParam}`);
            lastData = data;
            if (onUpdate) onUpdate(data);
            const items = data.items || [];
            if (items.length) {
                const terminal = items.every((i) => ['succeeded', 'failed', 'skipped'].includes(i.execution_status));
                if (terminal) {
                    const failed = items.some((i) => i.execution_status === 'failed');
                    return { ok: !failed, data };
                }
            }
            if (['completed', 'partial'].includes(data.execute_status) && items.length) {
                const terminal = items.every((i) => ['succeeded', 'failed', 'skipped'].includes(i.execution_status));
                if (terminal) {
                    const failed = items.some((i) => i.execution_status === 'failed');
                    return { ok: !failed, data };
                }
            }
            await sleep(intervalMs);
        }
        const failed = (lastData?.items || []).some((i) => i.execution_status === 'failed');
        return {
            ok: false,
            data: lastData,
            message: failed ? 'Отправка завершена с ошибками' : 'Превышено время ожидания ответа Bitrix24',
        };
    }

    async function openFilterSendModal(filterId, importId) {
        resetFilterSendModalUi();
        const filterDef = getFilterDef(filterId);
        const rows = getFilteredRows(filterId);
        filterSendState = { filterId, rowIds: rows.map((r) => r.id), sending: false, sendDone: false };

        const titleEl = document.getElementById('filter-send-title');
        if (titleEl) titleEl.textContent = `Отправка в Битрикс24 — ${filterDef.label}`;

        const bodyEl = document.getElementById('filter-send-body');
        const confirmBtn = document.getElementById('btn-filter-send-confirm');
        const warningEl = document.getElementById('filter-send-warning');

        if (bodyEl) bodyEl.innerHTML = buildSendPreviewHtml(filterId, rows);

        const executionEnabled = !!diagnosticsCache?.execution_enabled;
        const bitrixId = currentUserBitrixId();

        if (warningEl) {
            if (!executionEnabled) {
                warningEl.classList.remove('d-none');
                warningEl.textContent = 'Выполнение отключено (CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false).';
            } else if (bitrixId == null) {
                warningEl.classList.remove('d-none');
                warningEl.textContent = 'У текущего пользователя не указан ID в Bitrix — укажите его в разделе «Пользователи».';
            } else {
                warningEl.classList.add('d-none');
                warningEl.textContent = '';
            }
        }
        if (confirmBtn) {
            confirmBtn.disabled = !executionEnabled || bitrixId == null || !rows.length;
        }

        filterSendModal?.show();
    }

    async function executeFilteredSend(importId, rowIds) {
        const alertEl = document.getElementById('import-alert');
        const confirmBtn = document.getElementById('btn-filter-send-confirm');
        const bodyEl = document.getElementById('filter-send-body');
        const progressEl = document.getElementById('filter-send-progress');
        const filterId = filterSendState.filterId;
        const rows = importRowsCache.filter((r) => rowIds.includes(r.id));
        try {
            filterSendState.sending = true;
            if (confirmBtn) confirmBtn.disabled = true;
            if (progressEl) {
                progressEl.classList.remove('d-none');
                progressEl.innerHTML = '<div class="d-flex align-items-center gap-2"><div class="spinner-border spinner-border-sm"></div><span>Запуск отправки…</span></div>';
            }
            const resp = await fetch(`/api/call-results/imports/${importId}/execute`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation_token: 'EXECUTE', row_ids: rowIds }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Execute недоступен');
            const updateLog = (statusData) => {
                if (bodyEl) bodyEl.innerHTML = buildSendLogHtml(filterId, rows, statusData.items || []);
                if (progressEl) progressEl.innerHTML = renderFilterSendProgress(statusData);
            };
            updateLog({ items: [], execute_status: 'executing' });
            const pollResult = await pollFilterSendStatus(importId, rowIds, { onUpdate: updateLog });
            if (pollResult.data) updateLog(pollResult.data);
            filterSendState.sending = false;
            filterSendState.sendDone = true;
            if (confirmBtn) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Закрыть';
                confirmBtn.classList.remove('btn-success');
                confirmBtn.classList.add('btn-outline-secondary');
            }
            loadImport(importId, { fullReload: true });
            const pageMsg = pollResult.ok
                ? 'Отправка в Bitrix24 завершена'
                : (pollResult.message || 'Отправка завершена с ошибками');
            showAlert(alertEl, pageMsg, pollResult.ok ? 'success' : (pollResult.data ? 'warning' : 'danger'));
        } catch (e) {
            filterSendState.sending = false;
            showAlert(alertEl, e.message, 'danger');
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
        if (currentImportId && filterNeedsQueueCaches(filterId)) {
            void ensureQueueCachesForFilter(currentImportId, filterId).then(() => {
                updateSendAndExportButton();
            });
        }
    }

    function getFilterCount(filterId, summary) {
        if (filterId === 'all') return importRowsCache.length;
        if (filterId === 'manual_call' && summary?.manual_call_inclusive != null) {
            return summary.manual_call_inclusive;
        }
        const counts = summary?.filter_counts;
        if (counts && Object.prototype.hasOwnProperty.call(counts, filterId)) {
            return counts[filterId];
        }
        return getFilteredRows(filterId).length;
    }

    function renderSummaryFilters(summary, activeId) {
        const el = document.getElementById('summary-cards');
        if (!el) return;
        el.innerHTML = ROW_FILTERS.map((f) => {
            const count = getFilterCount(f.id, summary);
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
                <div class="card-body text-muted py-4 text-center">Данные появятся после обработки</div>
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
                        <th>Строка</th><th>Телефон</th><th>Сделка</th><th>Бизнес-группа</th><th>Сигналы</th><th></th>
                    </tr></thead>
                    <tbody>${rows.map((r) => `<tr data-row-id="${r.id}"${r.id === currentViewRowId ? ' class="table-active"' : ''}>
                        <td>${r.source_row_number}</td>
                        <td>${renderPhoneLink(r.raw_phone)}</td>
                        <td class="text-truncate" style="max-width:180px" title="${escapeHtml(getDealTitle(r))}">${renderDealCell(r)}</td>
                        <td>${businessGroupBadge(r)}</td>
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

    function formatCalledAt(value) {
        if (!value) return '—';
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return '—';
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'Europe/Moscow',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false,
        }).formatToParts(d);
        const get = (type) => parts.find((p) => p.type === type)?.value || '00';
        return `${get('day')}.${get('month')}.${get('year')} ${get('hour')}:${get('minute')}:${get('second')}`;
    }

    function renderContextBlock(row, extraRowsHtml = '') {
        return `<div class="row-view-section">
            ${renderSectionHeading('Контекст')}
            <dl class="row small mb-0">
                <dt class="col-sm-4">Телефон</dt><dd class="col-sm-8">${renderPhoneLink(row.raw_phone)}</dd>
                <dt class="col-sm-4">Время звонка</dt><dd class="col-sm-8">${escapeHtml(formatCalledAt(row.called_at))}</dd>
                <dt class="col-sm-4">Сделка</dt><dd class="col-sm-8">${renderDealCell(row)}</dd>
                <dt class="col-sm-4">Бизнес-группа</dt><dd class="col-sm-8">${businessGroupBadge(row)}</dd>
                ${row.manual_review_reason ? `<dt class="col-sm-4">Причина проверки</dt><dd class="col-sm-8 text-warning">${escapeHtml(row.manual_review_reason)}</dd>` : ''}
                ${extraRowsHtml}
            </dl>
        </div>`;
    }

    function renderManualReviewBody(row) {
        return renderTranscriptBlock(row) + renderContextBlock(row);
    }

    function renderManualReviewFooter(row) {
        const rowId = row.id;
        const actions = row.available_manual_actions || [];
        if (!actions.length) {
            return `<span class="text-muted small me-2">Нет доступных действий</span>
                <button type="button" class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Закрыть</button>`;
        }
        return actions.map((action) => {
            const render = MANUAL_ACTION_BUTTONS[action];
            return render ? render(rowId) : '';
        }).join('');
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
        } else if (currentUserBitrixId() == null && (diag.bitrix_service_user_id || 0) <= 0) {
            hints.push('<div class="alert alert-warning small mb-3 py-2">Для задач и CRM-дел укажите ответственного по сделке или BITRIX_SERVICE_USER_ID.</div>');
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
            ? '<div class="alert alert-warning small mb-2 py-2">Отправка в Bitrix недоступна. Проверьте CALL_RESULTS_BITRIX_EXECUTION_ENABLED, webhook и ответственного по сделке (или BITRIX_SERVICE_USER_ID).</div>'
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
            todo: 'Задача',
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
                    <dt class="col-sm-4">Телефон</dt><dd class="col-sm-8">${renderPhoneLink(fc.phone)}</dd>
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
            if (row.manual_review_pending) {
                footer.innerHTML = renderManualReviewFooter(row);
                bindManualReviewActions(importId, rowId);
            } else {
                footer.innerHTML = '<button type="button" class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Закрыть</button>';
            }
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
            await loadCurrentUserCache();
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
                throw new Error(parseApiErrorDetail(data.detail, 'Ошибка превью'));
            }
            modalReviewState = { mode: 'preview', action, previewData: data, preparedRowId: null, preparedMessage: null, sendResult: null };
            const row = importRowsCache.find((r) => r.id === rowId);
            if (body && row) body.innerHTML = renderPreviewBody(row, action, data);
            if (footer) {
                footer.innerHTML = renderPreviewConfirmFooter(rowId);
                bindPreviewConfirmActions(importId, rowId);
            }
        } catch (e) {
            const row = importRowsCache.find((r) => r.id === rowId);
            if (row) showRowViewError(row, e.message);
            showAlert(alertEl, e.message, 'danger');
            if (footer && row?.manual_review_pending) {
                footer.innerHTML = renderManualReviewFooter(row);
                bindManualReviewActions(importId, rowId);
            }
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
                throw new Error(parseApiErrorDetail(data.detail, 'Ошибка действия'));
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
                if (action === 'find_contact') {
                    openNextFilteredRow(importId, rowId, 'manual_review');
                } else {
                    const targetFilter = ACTION_TO_FILTER[action];
                    if (targetFilter) setActiveFilter(targetFilter);
                    openNextFilteredRow(importId, rowId);
                }
            }
        } catch (e) {
            const row = importRowsCache.find((r) => r.id === rowId);
            if (row) showRowViewError(row, e.message);
            showAlert(alertEl, e.message, 'danger');
            footer?.querySelectorAll('.btn-manual-confirm, .btn-manual-cancel').forEach((btn) => { btn.disabled = false; });
        }
    }

    function openNextFilteredRow(importId, currentRowId, filterId = activeFilterId) {
        const remaining = getFilteredRows(filterId).filter((r) => r.id !== currentRowId);
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
        const titleEl = document.getElementById('row-view-title');
        if (titleEl) {
            titleEl.innerHTML =
                `Строка #${row.source_row_number} · ${renderPhoneLink(row.raw_phone)}${positionSuffix}`;
        }

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
                if (row.manual_review_pending) {
                    footer.innerHTML = renderManualReviewFooter(row);
                    bindManualReviewActions(importId, rowId);
                } else {
                    footer.innerHTML = '<button type="button" class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Закрыть</button>';
                }
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

    function businessGroupBadge(row) {
        const code = row?.business_group || 'other';
        const label = row?.business_group_label || 'ИНОЕ';
        const cls = BUSINESS_GROUP_BADGE_CLASSES[code] || 'bg-dark';
        return '<span class="badge ' + cls + ' text-wrap text-start">' + escapeHtml(label) + '</span>';
    }

    function signalBadges(signals, row) {
        const tooltip = signalTooltip(row);
        const titleAttr = tooltip ? ` title="${escapeHtml(tooltip)}"` : '';
        if (!signals) {
            return `<span class="text-muted"${titleAttr}>—</span>`;
        }
        const showHangupReplacement =
            signals.hangup_without_result && signals.replacement_contact_required;
        const skipKeys = showHangupReplacement ? new Set(HANGUP_REPLACEMENT_SIGNAL_KEYS) : new Set();
        const parts = [];
        if (showHangupReplacement) {
            parts.push(
                `<span class="badge ${HANGUP_REPLACEMENT_BADGE.cls} me-1">${HANGUP_REPLACEMENT_BADGE.label}</span>`,
            );
        }
        Object.entries(SIGNAL_LABELS)
            .filter(([k]) => signals[k] && !skipKeys.has(k))
            .forEach(([k, label]) => {
                const cls = SIGNAL_BADGE_CLASSES[k] || 'bg-primary';
                parts.push(`<span class="badge ${cls} me-1">${label}</span>`);
            });
        const badges = parts.join('');
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
