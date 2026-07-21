(function () {
    const alertEl = document.getElementById('users-alert');
    const tbody = document.getElementById('users-table-body');

    function showAlert(message, type) {
        alertEl.textContent = message;
        alertEl.className = `alert alert-${type}`;
        alertEl.classList.remove('d-none');
    }

    function hideAlert() {
        alertEl.classList.add('d-none');
    }

    async function api(path, options) {
        const res = await fetch(path, {
            headers: {'Content-Type': 'application/json'},
            ...options,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = data.detail;
            const message = detail?.message || (typeof detail === 'string' ? detail : 'Ошибка запроса');
            throw new Error(message);
        }
        return data;
    }

    function parseBitrixId(raw) {
        const trimmed = String(raw || '').trim();
        if (!trimmed) return null;
        const value = parseInt(trimmed, 10);
        return Number.isFinite(value) && value > 0 ? value : null;
    }

    function roleOptions(selectedRole) {
        const labels = {viewer: 'Наблюдатель', analyst: 'Аналитик', admin: 'Администратор'};
        return Object.entries(labels).map(([role, label]) =>
            '<option value="' + role + '"' + (role === selectedRole ? ' selected' : '') + '>' + label + '</option>'
        ).join('');
    }

    function renderUsers(users) {
        if (!users.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-muted p-3">Пользователей пока нет</td></tr>';
            return;
        }
        tbody.innerHTML = users.map((user) => `
            <tr data-user-id="${user.id}">
                <td>${escapeHtml(user.email)}</td>
                <td>${escapeHtml(user.display_name || user.email)}</td>
                <td>${user.crm_user_external_id != null ? escapeHtml(user.crm_user_external_id) : '—'}</td>
                <td><select class="form-select form-select-sm user-role">${roleOptions(user.role || 'viewer')}</select></td>
                <td>${user.is_active ? '<span class="badge text-bg-success">Активен</span>' : '<span class="badge text-bg-secondary">Заблокирован</span>'}</td>
                <td class="text-end">
                    <button type="button" class="btn btn-sm btn-outline-secondary btn-reset-password">Сбросить пароль</button>
                    ${user.is_active
                        ? '<button type="button" class="btn btn-sm btn-outline-danger btn-deactivate ms-1">Заблокировать</button>'
                        : '<button type="button" class="btn btn-sm btn-outline-success btn-activate ms-1">Разблокировать</button>'}
                </td>
            </tr>
        `).join('');
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    async function loadUsers() {
        const data = await api('/api/app-users');
        renderUsers(data.users || []);
    }

    document.getElementById('create-user-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        hideAlert();
        try {
            const bitrixId = parseBitrixId(document.getElementById('new-bitrix-id').value);
            await api('/api/app-users', {
                method: 'POST',
                body: JSON.stringify({
                    email: document.getElementById('new-email').value,
                    display_name: document.getElementById('new-display-name').value,
                    password: document.getElementById('new-password').value,
                    role: document.getElementById('new-role').value,
                    crm_user_external_id: bitrixId,
                }),
            });
            e.target.reset();
            showAlert('Пользователь создан', 'success');
            await loadUsers();
        } catch (err) {
            showAlert(err.message, 'danger');
        }
    });

    tbody.addEventListener('change', async (e) => {
        if (!e.target.classList.contains('user-role')) return;
        const row = e.target.closest('tr[data-user-id]');
        if (!row) return;
        hideAlert();
        try {
            await api('/api/app-users/' + row.dataset.userId, {
                method: 'PATCH',
                body: JSON.stringify({role: e.target.value}),
            });
            showAlert('Роль обновлена', 'success');
        } catch (err) {
            showAlert(err.message, 'danger');
            await loadUsers();
        }
    });

    tbody.addEventListener('click', async (e) => {
        const row = e.target.closest('tr[data-user-id]');
        if (!row) return;
        const userId = row.dataset.userId;
        hideAlert();
        try {
            if (e.target.classList.contains('btn-reset-password')) {
                const password = window.prompt('Новый пароль (минимум 6 символов):');
                if (!password) return;
                await api(`/api/app-users/${userId}`, {
                    method: 'PATCH',
                    body: JSON.stringify({password}),
                });
                showAlert('Пароль обновлён', 'success');
            } else if (e.target.classList.contains('btn-deactivate')) {
                if (!window.confirm('Заблокировать пользователя?')) return;
                await api(`/api/app-users/${userId}`, {
                    method: 'PATCH',
                    body: JSON.stringify({is_active: false}),
                });
                showAlert('Пользователь заблокирован', 'success');
                await loadUsers();
            } else if (e.target.classList.contains('btn-activate')) {
                await api(`/api/app-users/${userId}`, {
                    method: 'PATCH',
                    body: JSON.stringify({is_active: true}),
                });
                showAlert('Пользователь разблокирован', 'success');
                await loadUsers();
            }
        } catch (err) {
            showAlert(err.message, 'danger');
        }
    });

    loadUsers().catch((err) => showAlert(err.message, 'danger'));
})();
