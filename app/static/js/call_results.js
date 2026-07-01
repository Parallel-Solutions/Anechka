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
        hangup_without_result: 'Бросили трубку',
        replacement_contact_required: 'Перезвон на другой номер',
        needs_manual_review: 'Ручная проверка',
    };

    const LLM_STATUS_LABELS = {
        completed: 'OK',
        failed: 'Ошибка',
        invalid: 'Невалид',
        pending: 'Ожидание',
        processing: 'Обработка',
        not_required: '—',
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

    const CATEGORIES = ['hot_lead', 'manager_callback', 'robot_callback', 'refusal', 'unknown'];

    let selectedFile = null;
    let pendingImportId = null;
    let pendingNeedsSheet = false;
    let importRowsCache = [];
    let rowEditorModal = null;
    let rowRawModal = null;
    let rowLlmModal = null;

    function initCallResultsUploadPage() {
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file-input');
        if (!dropzone || !fileInput) return;

        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
        });
        fileInput.addEventListener('change', () => { if (fileInput.files.length) setFile(fileInput.files[0]); });

        document.getElementById('btn-clear-file')?.addEventListener('click', clearFile);
        document.getElementById('btn-upload')?.addEventListener('click', () => uploadFile(false));
        document.getElementById('btn-apply-mapping')?.addEventListener('click', applyConfigure);
        document.getElementById('btn-apply-sheet')?.addEventListener('click', applyConfigure);
    }

    function setFile(file) {
        selectedFile = file;
        const info = document.getElementById('file-info');
        const actions = document.getElementById('upload-actions');
        if (info) {
            info.classList.remove('d-none');
            info.innerHTML = `<strong>${escapeHtml(file.name)}</strong><br>Размер: ${(file.size / 1024).toFixed(1)} КБ`;
        }
        actions?.classList.remove('d-none');
    }

    function clearFile() {
        selectedFile = null;
        pendingImportId = null;
        document.getElementById('file-info')?.classList.add('d-none');
        document.getElementById('upload-actions')?.classList.add('d-none');
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
        if (!selectedFile) return;
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

            if (data.source_format === 'tomoru_csv') {
                showAlert(alertEl, data.message, 'success');
                pollUntilReady(data.import_id, alertEl);
                return;
            }

            if (data.needs_sheet) {
                pendingImportId = data.import_id;
                pendingNeedsSheet = true;
                showSheetUI(data);
                progress?.classList.add('d-none');
                return;
            }

            if (data.needs_column_mapping) {
                pendingImportId = data.import_id;
                pendingNeedsSheet = false;
                showMappingUI(data);
                progress?.classList.add('d-none');
                return;
            }

            pollUntilReady(data.import_id, alertEl);
        } catch (err) {
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
            pollUntilReady(pendingImportId, alertEl);
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

    async function pollUntilReady(importId, alertEl) {
        const progress = document.getElementById('upload-progress');
        const interval = setInterval(async () => {
            try {
                const data = await fetchJson(`/api/call-results/imports/${importId}/status`);
                if (data.status === 'ready') {
                    clearInterval(interval);
                    progress?.classList.add('d-none');
                    window.location.href = `/call-results/imports/${importId}`;
                } else if (data.status === 'failed') {
                    clearInterval(interval);
                    showAlert(alertEl, data.error_message || 'Ошибка обработки', 'danger');
                    progress?.classList.add('d-none');
                }
            } catch (e) { /* keep polling */ }
        }, 2000);
    }

    function initCallResultImportPage(importId) {
        const modalEl = document.getElementById('row-editor-modal');
        if (modalEl && window.bootstrap) {
            rowEditorModal = new bootstrap.Modal(modalEl);
        }
        const rawModalEl = document.getElementById('row-raw-modal');
        if (rawModalEl && window.bootstrap) {
            rowRawModal = new bootstrap.Modal(rawModalEl);
        }
        const llmModalEl = document.getElementById('row-llm-modal');
        if (llmModalEl && window.bootstrap) {
            rowLlmModal = new bootstrap.Modal(llmModalEl);
        }
        document.getElementById('row-editor-save')?.addEventListener('click', () => saveRowEditor(importId));
        loadImport(importId);
        setInterval(() => loadImport(importId), 5000);
        document.getElementById('btn-delete-import')?.addEventListener('click', async () => {
            if (!confirm('Удалить импорт?')) return;
            await fetch(`/api/call-results/imports/${importId}`, { method: 'DELETE' });
            window.location.href = '/call-results';
        });
        document.getElementById('btn-rebuild')?.addEventListener('click', async () => {
            await fetch(`/api/call-results/imports/${importId}/rebuild`, { method: 'POST' });
            loadImport(importId, true);
        });
        document.getElementById('btn-retry-llm')?.addEventListener('click', async () => {
            const alertEl = document.getElementById('import-alert');
            const btn = document.getElementById('btn-retry-llm');
            try {
                btn.disabled = true;
                const resp = await fetch(`/api/call-results/imports/${importId}/retry-llm`, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Не удалось запустить классификацию');
                showAlert(alertEl, data.message || 'Прогон через ИИ запущен', 'success');
                loadImport(importId, true);
            } catch (e) {
                showAlert(alertEl, e.message, 'danger');
            } finally {
                btn.disabled = false;
            }
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
            loadImport(importId, true);
        });
        document.getElementById('btn-execute')?.addEventListener('click', async () => {
            const alertEl = document.getElementById('import-alert');
            try {
                const detail = await fetchJson(`/api/call-results/imports/${importId}`);
                const s = detail.summary || {};
                const msg = `Создать: CRM-дела ${s.todos || 0}, комментарии ${s.comments || 0}, `
                    + `подготовлено ${s.prepared_operations || 0}. Пропущено проверок: ${s.manual_review || 0}. Продолжить?`;
                if (!confirm(msg)) return;
                const resp = await fetch(`/api/call-results/imports/${importId}/execute`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirmation_token: 'EXECUTE' }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Execute недоступен');
                showAlert(alertEl, data.message || 'Выполнение запущено', 'success');
                loadImport(importId, true);
            } catch (e) {
                showAlert(alertEl, e.message, 'danger');
            }
        });
    }

    function applyImportMeta(data) {
        const meta = document.getElementById('import-meta');
        if (meta) {
            const parts = [data.original_filename];
            if (data.source_format) parts.push(`Формат: ${data.source_format}`);
            if (data.batch_id) parts.push(`Batch: ${data.batch_id}`);
            if (data.exported_at) parts.push(`Выгрузка: ${data.exported_at}`);
            meta.textContent = parts.join(' · ');
        }
        const statusEl = document.getElementById('import-status');
        if (statusEl) statusEl.textContent = data.status;
        const restartBtn = document.getElementById('btn-restart');
        if (restartBtn) restartBtn.disabled = data.status === 'processing';
        const retryLlmBtn = document.getElementById('btn-retry-llm');
        if (retryLlmBtn) retryLlmBtn.disabled = data.status === 'processing';
    }

    async function loadImport(importId, forceProcessing) {
        try {
            const statusData = await fetchJson(`/api/call-results/imports/${importId}/status`);
            applyImportMeta(statusData);
            if (forceProcessing || statusData.status === 'processing' || statusData.status === 'uploaded') {
                renderSummary(statusData.summary);
                return;
            }
            const data = await fetchJson(`/api/call-results/imports/${importId}`);
            applyImportMeta(data);
            renderSummary(data.summary);
            importRowsCache = data.rows || [];
            renderHangupWithoutAnswers(data.hangup_rows || [], importId);
            renderRowsTable(importRowsCache, importId);
            renderQueues(importId);
            renderMethodBlocks(data.actions_by_method, importId);
            renderAttemptHistory(data.attempt_history);
            renderTomoruRows(data.rows);
        } catch (e) { /* ignore poll errors */ }
    }

    function renderSummary(s) {
        const el = document.getElementById('summary-cards');
        if (!el || !s) return;
        const cards = [
            ['Строк', s.total_rows], ['Положительные', s.positive], ['Другой контакт', s.alternate_contact],
            ['Перезвон', s.callback_later], ['Не дозвонились', s.no_answer], ['Отказы', s.refusal],
            ['Hangup', s.hangup], ['Hangup без ответов', s.hangup_without_answers],
            ['Ручная проверка', s.manual_review], ['Подготовлено', s.prepared_operations],
            ['Выполнено', s.executed_operations], ['Ошибки', s.execution_errors],
            ['Комментарии', s.comments], ['CRM-дела', s.todos],
            ['Сопоставлено', s.matched_rows], ['LLM OK', s.llm_completed],
        ];
        if ((s.llm_failed || 0) > 0) {
            cards.push(['LLM ошибки', s.llm_failed]);
        }
        if ((s.llm_pending || 0) > 0) {
            cards.push(['LLM в очереди', s.llm_pending]);
        }
        el.innerHTML = cards.map(([t, v]) =>
            `<div class="col-6 col-md-3 col-lg-2"><div class="card h-100"><div class="card-body py-2"><div class="text-muted small">${t}</div><div class="fs-6 fw-semibold">${v ?? 0}</div></div></div></div>`
        ).join('');
        const alertEl = document.getElementById('import-alert');
        if (alertEl && (s.llm_failed || 0) > 0) {
            showAlert(
                alertEl,
                `LLM не обработала ${s.llm_failed} строк(и). Для части из них применён fallback по расшифровке. Проверьте OPENAI_API_KEY или нажмите «Прогнать через ИИ».`,
                'warning',
            );
        }
    }

    function renderMethodBlocks(byMethod, importId) {
        const el = document.getElementById('method-blocks');
        if (!el) return;
        const order = [
            'crm.activity.todo.add', 'crm.timeline.comment.add', 'crm.contact.list',
            'crm.contact.add', 'crm.deal.contact.add', 'retry_queue.add', 'contact_search.add',
        ];
        el.innerHTML = order.map((method) => {
            const rows = byMethod[method] || [];
            return `<div class="card mb-4">
                <div class="card-header"><strong>${method}</strong> <span class="badge bg-secondary">${rows.length}</span>
                <div class="small text-muted fw-normal">${METHOD_DESC[method] || ''}</div></div>
                <div class="table-responsive"><table class="table table-sm table-striped mb-0">
                <thead><tr><th></th><th>Строка</th><th>Телефон</th><th>Сделка</th><th>Bitrix ID</th><th>Категория</th><th>Статус</th><th></th></tr></thead>
                <tbody>${rows.map((a) => actionRow(a, importId)).join('')}</tbody></table></div></div>`;
        }).join('');
        el.querySelectorAll('.action-toggle').forEach((cb) => {
            cb.addEventListener('change', async (e) => {
                const id = e.target.dataset.actionId;
                await fetch(`/api/call-results/imports/${importId}/actions/${id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ is_enabled: e.target.checked }),
                });
            });
        });
    }

    function actionRow(a, importId) {
        const payloadId = `payload-${a.id}`;
        const modBadge = a.user_modified ? ' <span class="badge bg-info">Изменено</span>' : '';
        return `<tr>
            <td><input type="checkbox" class="action-toggle" data-action-id="${a.id}" ${a.is_enabled ? 'checked' : ''}></td>
            <td>${a.source_row_number ?? ''}</td>
            <td>${escapeHtml(a.phone || '')}</td>
            <td>${escapeHtml(a.deal_title || '')}</td>
            <td>${a.bitrix_deal_id ?? ''}</td>
            <td>${escapeHtml(a.final_category || '')}</td>
            <td><span class="badge ${a.validation_status === 'valid' ? 'bg-success' : 'bg-warning'}">${a.validation_status}</span>${modBadge}</td>
            <td><button class="btn btn-link btn-sm p-0" type="button" data-bs-toggle="collapse" data-bs-target="#${payloadId}">Payload</button>
            <div class="collapse" id="${payloadId}"><pre class="small mt-1">${escapeHtml(JSON.stringify(a.payload, null, 2))}</pre></div></td>
        </tr>`;
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

    function llmStatusBadge(status) {
        const label = LLM_STATUS_LABELS[status] || status || '—';
        let cls = 'bg-secondary';
        if (status === 'completed') cls = 'bg-success';
        else if (status === 'failed' || status === 'invalid') cls = 'bg-danger';
        else if (status === 'pending' || status === 'processing') cls = 'bg-warning text-dark';
        return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
    }

    function renderRowsTable(rows, importId) {
        const block = document.getElementById('rows-table-block');
        if (!block || !rows?.length) { block?.classList.add('d-none'); return; }
        block.classList.remove('d-none');
        block.innerHTML = `<div class="card mb-4"><div class="card-header">Строки импорта</div>
        <div class="table-responsive"><table class="table table-sm mb-0"><thead><tr>
        <th>Строка</th><th>Телефон</th><th>Итог</th><th>Сигналы</th><th>LLM</th><th>Перезвон</th><th>Статус</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
            <td>${r.source_row_number}</td>
            <td>${escapeHtml(r.raw_phone || '')}</td>
            <td>${escapeHtml(r.primary_outcome || r.final_category || '')}</td>
            <td>${signalBadges(r.business_signals, r)}</td>
            <td>${llmStatusBadge(r.llm_status)}</td>
            <td>${escapeHtml(r.callback_at || '')}</td>
            <td>${escapeHtml(r.execution_status || '')}</td>
            <td><div class="d-flex gap-1 flex-nowrap">
                <button type="button" class="btn btn-sm btn-outline-primary btn-view-raw" data-row-id="${r.id}">Оригинал</button>
                <button type="button" class="btn btn-sm btn-outline-info btn-view-llm" data-row-id="${r.id}">ИИ</button>
                <button type="button" class="btn btn-sm btn-outline-secondary btn-edit-row" data-row-id="${r.id}">Изменить</button>
            </div></td>
        </tr>`).join('')}</tbody></table></div></div>`;
        block.querySelectorAll('.btn-view-raw').forEach((btn) => {
            btn.addEventListener('click', () => openRowRawViewer(parseInt(btn.dataset.rowId, 10)));
        });
        block.querySelectorAll('.btn-view-llm').forEach((btn) => {
            btn.addEventListener('click', () => openRowLlmViewer(importId, parseInt(btn.dataset.rowId, 10)));
        });
        block.querySelectorAll('.btn-edit-row').forEach((btn) => {
            btn.addEventListener('click', () => openRowEditor(parseInt(btn.dataset.rowId, 10)));
        });
    }

    function formatRawDataTable(rawData) {
        if (!rawData || !Object.keys(rawData).length) {
            return '<p class="text-muted mb-0">Нет исходных данных</p>';
        }
        const rows = Object.entries(rawData).map(([key, val]) => {
            const display = val === null || val === undefined || val === '' ? '—' : escapeHtml(String(val));
            return `<tr><th class="text-nowrap align-top" style="width:35%">${escapeHtml(key)}</th><td class="text-break">${display}</td></tr>`;
        }).join('');
        return `<div class="table-responsive"><table class="table table-sm table-bordered mb-0"><tbody>${rows}</tbody></table></div>`;
    }

    function openRowRawViewer(rowId) {
        const row = importRowsCache.find((r) => r.id === rowId);
        if (!row) return;
        document.getElementById('row-raw-title').textContent = `Оригинальная запись — строка #${row.source_row_number}`;
        document.getElementById('row-raw-body').innerHTML = formatRawDataTable(row.raw_data);
        const jsonPre = document.getElementById('row-raw-json-pre');
        if (jsonPre) jsonPre.textContent = JSON.stringify(row.raw_data || {}, null, 2);
        const jsonCollapse = document.getElementById('row-raw-json');
        if (jsonCollapse?.classList.contains('show')) {
            bootstrap.Collapse.getOrCreateInstance(jsonCollapse).hide();
        }
        rowRawModal?.show();
    }

    function formatLlmMeta(data) {
        const parts = [];
        parts.push(`Статус: ${LLM_STATUS_LABELS[data.llm_status] || data.llm_status || '—'}`);
        if (data.llm_model) parts.push(`Модель: ${escapeHtml(data.llm_model)}`);
        if (data.llm_provider) parts.push(`Provider: ${escapeHtml(data.llm_provider)}`);
        if (data.llm_confidence != null) parts.push(`Уверенность: ${(data.llm_confidence * 100).toFixed(0)}%`);
        if (data.llm_duration_ms != null) parts.push(`Время: ${data.llm_duration_ms} ms`);
        if (data.llm_token_usage?.total) parts.push(`Токены: ${data.llm_token_usage.total}`);
        if (data.llm_prompt_version) parts.push(`Prompt v${escapeHtml(data.llm_prompt_version)}`);
        return parts.join(' · ');
    }

    function buildLlmAlerts(data) {
        const alerts = [];
        if (data.llm_status === 'not_required') {
            const reason = data.deterministic_reason || data.deterministic_category || 'правила';
            alerts.push(`<div class="alert alert-info py-2 small mb-2">ИИ не использовалась: ${escapeHtml(reason)}</div>`);
        } else if (data.llm_status === 'pending' || data.llm_status === 'processing') {
            alerts.push('<div class="alert alert-warning py-2 small mb-2">ИИ ещё не обработала эту строку.</div>');
        } else if (data.llm_status === 'failed' || data.llm_status === 'invalid') {
            const err = (data.llm_validation_errors || []).join('; ') || data.llm_error_type || 'ошибка';
            alerts.push(`<div class="alert alert-danger py-2 small mb-2">${escapeHtml(err)}</div>`);
        }
        if (data.llm_input_truncated) {
            alerts.push('<div class="alert alert-warning py-2 small mb-2">Входной текст был обрезан перед отправкой в ИИ.</div>');
        }
        if (data.input_hash_matches === false) {
            alerts.push('<div class="alert alert-warning py-2 small mb-2">Восстановленный запрос может отличаться от отправленного (данные строки изменились после вызова ИИ).</div>');
        }
        return alerts.join('');
    }

    function formatLlmResponse(data) {
        if (data.llm_result && Object.keys(data.llm_result).length) {
            return JSON.stringify(data.llm_result, null, 2);
        }
        if (data.llm_status === 'not_required') {
            return '— (ИИ не вызывалась)';
        }
        if (data.llm_status === 'pending' || data.llm_status === 'processing') {
            return '— (ожидает обработки)';
        }
        const err = (data.llm_validation_errors || []).join('\n');
        return err || '— (нет ответа)';
    }

    async function openRowLlmViewer(importId, rowId) {
        const row = importRowsCache.find((r) => r.id === rowId);
        if (!row) return;
        document.getElementById('row-llm-title').textContent = `ИИ — строка #${row.source_row_number}`;
        const loading = document.getElementById('row-llm-loading');
        const content = document.getElementById('row-llm-content');
        loading?.classList.remove('d-none');
        content?.classList.add('d-none');
        rowLlmModal?.show();
        try {
            const data = await fetchJson(`/api/call-results/imports/${importId}/rows/${rowId}/llm`);
            document.getElementById('row-llm-alerts').innerHTML = buildLlmAlerts(data);
            document.getElementById('row-llm-meta').textContent = formatLlmMeta(data);
            document.getElementById('row-llm-system').textContent = data.system_prompt || '';
            document.getElementById('row-llm-user').textContent = data.user_message || JSON.stringify(data.user_payload || {}, null, 2);
            document.getElementById('row-llm-response').textContent = formatLlmResponse(data);
        } catch (e) {
            document.getElementById('row-llm-alerts').innerHTML =
                `<div class="alert alert-danger py-2 small mb-2">${escapeHtml(e.message)}</div>`;
            document.getElementById('row-llm-meta').textContent = '';
            document.getElementById('row-llm-system').textContent = '';
            document.getElementById('row-llm-user').textContent = '';
            document.getElementById('row-llm-response').textContent = '';
        } finally {
            loading?.classList.add('d-none');
            content?.classList.remove('d-none');
        }
    }

    function openRowEditor(rowId) {
        const row = importRowsCache.find((r) => r.id === rowId);
        if (!row) return;
        document.getElementById('row-editor-id').value = rowId;
        document.getElementById('row-editor-title').textContent = `Строка #${row.source_row_number}`;
        document.getElementById('row-editor-phone').value = row.raw_phone || '';
        document.getElementById('row-editor-summary').value = (row.extracted_data?.summary) || '';
        document.getElementById('row-editor-contact-name').value = (row.extracted_data?.contact_name) || '';
        document.getElementById('row-editor-full-phone').value = (row.extracted_data?.full_phone) || '';
        document.getElementById('row-editor-email').value = (row.extracted_data?.email) || '';
        document.getElementById('row-editor-extension').value = (row.extracted_data?.phone_extension) || '';
        const cb = document.getElementById('row-editor-callback');
        if (cb) cb.value = row.callback_at ? row.callback_at.slice(0, 16) : '';
        const signals = row.business_signals || {};
        document.querySelectorAll('.row-signal').forEach((el) => {
            el.checked = !!signals[el.dataset.signal];
        });
        const dealSel = document.getElementById('row-editor-deal');
        if (dealSel) {
            const opts = (row.candidate_matches || []).map((c) =>
                `<option value="${c.local_id}" ${row.matched_deal_local_id === c.local_id ? 'selected' : ''}>${escapeHtml(c.title)} (${c.deal_id})</option>`
            ).join('');
            dealSel.innerHTML = `<option value="">— не менять —</option>${opts}`;
        }
        rowEditorModal?.show();
    }

    async function saveRowEditor(importId) {
        const rowId = parseInt(document.getElementById('row-editor-id').value, 10);
        const body = {
            summary: document.getElementById('row-editor-summary').value || null,
            contact_name: document.getElementById('row-editor-contact-name').value || null,
            full_phone: document.getElementById('row-editor-full-phone').value || null,
            email: document.getElementById('row-editor-email').value || null,
            phone_extension: document.getElementById('row-editor-extension').value || null,
        };
        const cbVal = document.getElementById('row-editor-callback')?.value;
        if (cbVal) body.callback_at = cbVal;
        const dealLocal = document.getElementById('row-editor-deal')?.value;
        if (dealLocal) body.matched_deal_local_id = parseInt(dealLocal, 10);
        document.querySelectorAll('.row-signal').forEach((el) => {
            body[el.dataset.signal] = el.checked;
        });
        const alertEl = document.getElementById('import-alert');
        try {
            const resp = await fetch(`/api/call-results/imports/${importId}/rows/${rowId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Ошибка сохранения');
            rowEditorModal?.hide();
            showAlert(alertEl, 'Строка обновлена, операции пересобраны', 'success');
            loadImport(importId, true);
        } catch (e) {
            showAlert(alertEl, e.message, 'danger');
        }
    }

    async function renderQueues(importId) {
        const block = document.getElementById('queue-tabs-block');
        if (!block) return;
        try {
            const retry = await fetchJson(`/api/call-results/retry-queue?import_id=${importId}`);
            const search = await fetchJson(`/api/call-results/contact-search?import_id=${importId}`);
            if (!retry.length && !search.length) { block.classList.add('d-none'); return; }
            block.classList.remove('d-none');
            document.getElementById('retry-queue-content').innerHTML = retry.length
                ? `<table class="table table-sm"><thead><tr><th>ID</th><th>Телефон</th><th>Причина</th><th>Статус</th><th>Callback</th></tr></thead>
                <tbody>${retry.map((e) => `<tr><td>${e.id}</td><td>${escapeHtml(e.phone || '')}</td><td>${e.reason}</td><td>${e.status}</td><td>${escapeHtml(e.callback_at || '')}</td></tr>`).join('')}</tbody></table>
                <a class="btn btn-sm btn-outline-primary" href="/api/call-results/retry-queue/export.csv?import_id=${importId}">CSV</a>`
                : '<p class="text-muted mb-0">Нет записей</p>';
            document.getElementById('contact-search-content').innerHTML = search.length
                ? `<table class="table table-sm"><thead><tr><th>ID</th><th>Телефон</th><th>Статус</th><th>Summary</th></tr></thead>
                <tbody>${search.map((e) => `<tr><td>${e.id}</td><td>${escapeHtml(e.source_phone || '')}</td><td>${e.status}${e.status === 'contact_search_required' ? ' <span class="badge bg-warning text-dark">TODO: автопоиск</span>' : ''}</td><td>${escapeHtml(e.summary || '')}</td></tr>`).join('')}</tbody></table>`
                : '<p class="text-muted mb-0">Нет записей</p>';
        } catch (e) { block.classList.add('d-none'); }
    }

    function renderHangupWithoutAnswers(hangupRows, importId) {
        const block = document.getElementById('hangup-without-answers-block');
        if (!block) return;
        if (!hangupRows.length) { block.classList.add('d-none'); return; }
        block.classList.remove('d-none');
        block.innerHTML = `<div class="card mb-4 border-danger"><div class="card-header">Бросили трубку без ответов <span class="badge bg-danger">${hangupRows.length}</span></div>
        <div class="table-responsive"><table class="table table-sm mb-0"><thead><tr>
        <th>Строка</th><th>Телефон</th><th>Сделка</th><th>Итог</th><th>Статус</th><th></th></tr></thead>
        <tbody>${hangupRows.map((r) => `<tr>
            <td>${r.source_row_number}</td>
            <td>${escapeHtml(r.phone || '')}</td>
            <td>${escapeHtml(r.deal_title || r.deal_id || '—')}</td>
            <td>${escapeHtml(r.primary_outcome || 'hangup')}</td>
            <td>${escapeHtml(r.execution_status || '—')}</td>
            <td><button type="button" class="btn btn-sm btn-outline-primary btn-view-raw" data-row-id="${r.id}">Оригинал</button></td>
        </tr>`).join('')}</tbody></table></div>
        <div class="card-footer small text-muted">Сигналы: «Бросили трубку» + «Перезвон на другой номер». После Execute — очереди «Поиск контакта» и «Повторные звонки».</div></div>`;
        block.querySelectorAll('.btn-view-raw').forEach((btn) => {
            btn.addEventListener('click', () => openRowRawViewer(parseInt(btn.dataset.rowId, 10)));
        });
    }

    function renderTomoruRows(rows) {
        const block = document.getElementById('tomoru-events-block');
        if (!block) return;
        const tomoruRows = (rows || []).filter((r) => r.scenario_events && r.scenario_events.length);
        if (!tomoruRows.length) { block.classList.add('d-none'); return; }
        block.classList.remove('d-none');
        block.innerHTML = tomoruRows.map((r) => {
            const table = (r.scenario_events || []).map((ev) =>
                `<tr><td>${escapeHtml(ev.field || '')}</td><td>${escapeHtml(ev.match || '')}</td><td>${escapeHtml(ev.transcription || '')}</td></tr>`
            ).join('');
            const rawId = `raw-${r.id}`;
            return `<div class="card mb-3"><div class="card-header">Строка ${r.source_row_number} — Данные разговора Tomoru</div>
            <div class="card-body"><table class="table table-sm"><thead><tr><th>Этап</th><th>Match</th><th>Транскрипция</th></tr></thead><tbody>${table}</tbody></table>
            <button class="btn btn-link btn-sm" data-bs-toggle="collapse" data-bs-target="#${rawId}">Показать исходные данные</button>
            <div class="collapse" id="${rawId}"><pre class="small">${escapeHtml(JSON.stringify(r.raw_data, null, 2))}</pre></div>
            </div></div>`;
        }).join('');
    }

    function renderAttemptHistory(history) {
        const block = document.getElementById('attempt-history-block');
        if (!block) return;
        if (!history || !history.length) { block.classList.add('d-none'); return; }
        block.classList.remove('d-none');
        block.innerHTML = `<div class="card mb-4"><div class="card-header">История попыток (повторные телефоны)</div><div class="card-body">
        ${history.map((h) => `<div class="mb-3"><strong>${escapeHtml(h.normalized_phone)}</strong> — итог: ${escapeHtml(h.latest_outcome || '—')}
        <ul class="small mb-0">${(h.attempts || []).map((a) =>
            `<li>#${a.source_row_number}: ${escapeHtml(a.call_result_display || '')} @ ${escapeHtml(a.called_at || '')}</li>`
        ).join('')}</ul></div>`).join('')}
        </div></div>`;
    }

    window.initCallResultsUploadPage = initCallResultsUploadPage;
    window.initCallResultImportPage = initCallResultImportPage;
})();
