/* Phone export history — click phone to see which exports included it */

const EXPORT_MODE_LABELS = {
    region_lpr: 'Tomoru / ЛПР',
    intelligent_export: 'Intelligent Export',
    region: 'Регион',
    stage: 'Стадия',
    category_full: 'Полная / воронка',
};

let phoneExportHistoryModal = null;
let phoneExportHistoryInitialized = false;

function exportModeLabel(mode) {
    return EXPORT_MODE_LABELS[mode] || mode || '—';
}

function formatExportDate(value) {
    if (!value) return '—';
    try {
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return '—';
        return d.toLocaleString('ru-RU', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        });
    } catch {
        return '—';
    }
}

function renderPhoneLink(phone) {
    const text = phone === null || phone === undefined || phone === '' ? '' : String(phone).trim();
    if (!text) return '—';
    return (
        `<button type="button" class="btn btn-link btn-sm p-0 align-baseline phone-export-link js-phone-export-history"`
        + ` data-phone="${escapeHtml(text)}">${escapeHtml(text)}</button>`
    );
}

function renderPhoneExportHistoryBody(data) {
    const items = data.items || [];
    if (!items.length) {
        return (
            '<div class="text-muted">'
            + '<p class="mb-2">Телефон не участвовал в зарегистрированных выгрузках.</p>'
            + '<p class="small mb-0">История доступна только для Tomoru / ЛПР и Intelligent Export. '
            + 'Выгрузки по региону, стадии и полной воронке телефоны в реестр не записывают.</p>'
            + '</div>'
        );
    }

    const rows = items.map((item) => {
        const mode = item.export_mode || item.job_mode;
        const params = item.parameters || {};
        const paramBits = [];
        if (params.region_name) {
            paramBits.push(escapeHtml(params.region_name));
        } else if (params.region_id != null) {
            paramBits.push(`регион ${escapeHtml(params.region_id)}`);
        }
        if (params.category_id != null) {
            paramBits.push(`воронка ${escapeHtml(params.category_id)}`);
        }
        const paramText = paramBits.length ? `<div class="small text-muted">${paramBits.join(' · ')}</div>` : '';

        return (
            '<tr>'
            + `<td><a href="/exports/${item.export_job_id}">#${item.export_job_id}</a>${paramText}</td>`
            + `<td>${escapeHtml(exportModeLabel(mode))}</td>`
            + `<td><span class="badge status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>`
            + `<td>${escapeHtml(formatExportDate(item.created_at))}</td>`
            + `<td>${item.deal_id ? escapeHtml(item.deal_id) : '—'}</td>`
            + `<td>${item.contact_id ? escapeHtml(item.contact_id) : '—'}</td>`
            + '</tr>'
        );
    }).join('');

    return (
        '<p class="small text-muted mb-3">'
        + 'Показаны выгрузки Tomoru / ЛПР и Intelligent Export, в которых этот номер был записан в реестр.'
        + '</p>'
        + '<div class="table-responsive">'
        + '<table class="table table-sm table-bordered mb-0">'
        + '<thead><tr>'
        + '<th>Выгрузка</th><th>Тип</th><th>Статус</th><th>Дата</th><th>Сделка</th><th>Контакт</th>'
        + '</tr></thead>'
        + `<tbody>${rows}</tbody>`
        + '</table>'
        + '</div>'
    );
}

async function openPhoneExportHistory(phone) {
    const text = phone === null || phone === undefined ? '' : String(phone).trim();
    if (!text) return;

    const modalEl = document.getElementById('phone-export-history-modal');
    const titleEl = document.getElementById('phone-export-history-title');
    const bodyEl = document.getElementById('phone-export-history-body');
    if (!modalEl || !titleEl || !bodyEl) return;

    if (!phoneExportHistoryModal) {
        phoneExportHistoryModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    }

    titleEl.textContent = `Выгрузки с телефоном ${text}`;
    bodyEl.innerHTML = '<div class="text-muted small">Загрузка…</div>';
    phoneExportHistoryModal.show();

    try {
        const data = await fetchJson(`/api/phones/export-history?phone=${encodeURIComponent(text)}`);
        bodyEl.innerHTML = renderPhoneExportHistoryBody(data);
    } catch (err) {
        bodyEl.innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(err.message)}</div>`;
    }
}

function initPhoneExportHistory() {
    if (phoneExportHistoryInitialized) return;
    phoneExportHistoryInitialized = true;

    document.addEventListener('click', (event) => {
        const btn = event.target.closest('.js-phone-export-history');
        if (!btn) return;
        event.preventDefault();
        event.stopPropagation();
        openPhoneExportHistory(btn.dataset.phone || btn.textContent);
    });
}
