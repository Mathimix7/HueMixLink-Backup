async function api(path, opts = {}) {
    const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
}

function showAlert(msg, ok = true) {
    if (ok === 'warning') {
        showToast('Warning', msg, 'warning');
    } else if (ok) {
        showToast('Success', msg, 'success');
    } else {
        showToast('Error', msg, 'error');
    }
}

function fmtBytes(n) {
    if (n === null || n === undefined) return '—';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(1)} ${u[i]}`;
}

function timeAgo(iso) {
    if (!iso) return '—';
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return 'Just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    const d = Math.floor(s / 86400);
    return d === 1 ? 'Yesterday' : `${d} days ago`;
}

function fmtTime(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
    catch { return iso; }
}

const DOTS = {
    gray: 'inline-block h-2.5 w-2.5 rounded-full bg-gray-300 mr-2',
    green: 'inline-block h-2.5 w-2.5 rounded-full bg-green-500 mr-2',
    red: 'inline-block h-2.5 w-2.5 rounded-full bg-red-500 mr-2',
    blue: 'inline-block h-2.5 w-2.5 rounded-full bg-blue-500 mr-2 animate-pulse',
    amber: 'inline-block h-2.5 w-2.5 rounded-full bg-amber-400 mr-2',
};

function setDot(id, color) {
    document.getElementById(id).className = DOTS[color] || DOTS.gray;
}

function setText(id, text, title) {
    const el = document.getElementById(id);
    el.textContent = text;
    el.title = title || '';
}

let formInit = false;

function syncForm(cfg) {
    // Fill the form once on load and after save — status refreshes never
    // touch it, so unsaved input is never clobbered.
    if (formInit) return;
    document.getElementById('cfg-nas-path').value = cfg.nas_path || '';
    document.getElementById('cfg-username').value = cfg.username || '';
    document.getElementById('cfg-time').value = cfg.backup_time || '02:00';
    document.getElementById('cfg-retention').value = cfg.retention_count ?? 30;
    formInit = true;
}

async function refreshAll() {
    await loadStatus();
}

async function loadStatus() {
    const s = await api('/plugins/nas_backup/status');
    const cfg = s.config || {};
    syncForm(cfg);
    document.getElementById('setup-banner').classList.toggle('hidden', !!s.configured);

    const last = s.last_run;
    const failed = last && last.status !== 'success';

    if (s.running) {
        setDot('dot-state', 'blue'); setText('st-state', 'Backing up…'); setText('st-state-detail', 'Copying to NAS');
    } else if (!s.configured) {
        setDot('dot-state', 'gray'); setText('st-state', 'Not set up'); setText('st-state-detail', 'Enter a destination below');
    } else if (s.nas_error || failed) {
        setDot('dot-state', 'red'); setText('st-state', 'Needs attention'); setText('st-state-detail', s.nas_error || (last && last.error) || 'Last backup failed');
    } else if ((s.nas_count || 0) > 0) {
        const n = s.nas_count;
        setDot('dot-state', 'green'); setText('st-state', `Healthy - ${n} backup${n === 1 ? '' : 's'}`); setText('st-state-detail', '');
    } else {
        setDot('dot-state', 'green'); setText('st-state', 'Healthy'); setText('st-state-detail', 'No backups yet');
    }

    if (!last) {
        setDot('dot-last', 'gray'); setText('st-last', 'Never'); setText('st-last-detail', 'No backups yet');
    } else if (last.status === 'success') {
        setDot('dot-last', 'green'); setText('st-last', timeAgo(last.finished_at || last.started_at));
        setText('st-last-detail', '');
    } else {
        setDot('dot-last', 'red'); setText('st-last', 'Failed');
        setText('st-last-detail', last.error || 'Unknown error');
    }

    if (!s.configured || !s.next_run) {
        setDot('dot-next', 'gray'); setText('st-next', '—'); setText('st-next-detail', !s.configured ? 'Set a destination first' : 'Daily, after cooldown');
    } else {
        setDot('dot-next', 'green'); setText('st-next', fmtTime(s.next_run)); setText('st-next-detail', '');
    }
    return s;
}

let armedRestore = null;

async function openRestoreModal() {
    document.getElementById('restore-modal').classList.remove('hidden');
    await loadRestoreList();
}

function closeRestoreModal() {
    document.getElementById('restore-modal').classList.add('hidden');
    armedRestore = null;
}

async function loadRestoreList() {
    const list = document.getElementById('restore-list');
    list.innerHTML = `<p class="px-1 py-4 text-sm text-gray-400">Loading…</p>`;
    try {
        const data = await api('/plugins/nas_backup/api/nas-list');
        const rows = data.backups || [];
        if (!rows.length) {
            list.innerHTML = `<p class="px-1 py-4 text-sm text-gray-400">${data.configured === false ? 'Set a NAS destination first.' : 'No backups on the NAS yet.'}</p>`;
            return;
        }
        list.innerHTML = rows.map(b => `
            <div class="flex items-center justify-between rounded-xl border border-gray-200 px-4 py-3">
                <div class="min-w-0">
                    <p class="text-sm font-medium text-gray-900">${fmtTime(b.mtime * 1000)}</p>
                    <p class="text-xs text-gray-500">${fmtBytes(b.size)}</p>
                </div>
                <button onclick="restoreBackup('${b.name}', this)" class="ml-4 shrink-0 px-3 py-1 bg-amber-600 text-white rounded hover:bg-amber-700">Restore</button>
            </div>`).join('');
    } catch (e) {
        list.innerHTML = `<p class="px-1 py-4 text-sm text-red-600">${e.message}</p>`;
    }
}

async function restoreBackup(name, btn) {
    // First click arms, second executes. Arming lasts a minute and resets
    // in place; closing the modal disarms.
    if (armedRestore !== name) {
        armedRestore = name;
        document.querySelectorAll('#restore-list button').forEach(b => {
            b.textContent = 'Restore';
            b.className = 'ml-4 shrink-0 px-3 py-1 bg-amber-600 text-white rounded hover:bg-amber-700';
            b.disabled = false;
        });
        btn.textContent = 'Confirm?';
        btn.className = 'ml-4 shrink-0 px-3 py-1 bg-red-600 text-white rounded hover:bg-red-700';
        setTimeout(() => {
            if (armedRestore === name) {
                armedRestore = null;
                if (!document.getElementById('restore-modal').classList.contains('hidden')) {
                    btn.textContent = 'Restore';
                    btn.className = 'ml-4 shrink-0 px-3 py-1 bg-amber-600 text-white rounded hover:bg-amber-700';
                }
            }
        }, 60000);
        return;
    }
    armedRestore = null;
    btn.disabled = true;
    try {
        const data = await api('/plugins/nas_backup/api/restore', { method: 'POST', body: JSON.stringify({ name }) });
        showToast('Success', data.message, 'success');
        closeRestoreModal();
        await refreshAll();
    } catch (e) {
        showToast('Error', e.message, 'error');
        await loadRestoreList();
    }
}

async function saveConfig() {
    const payload = {
        nas_path: document.getElementById('cfg-nas-path').value.trim(),
        username: document.getElementById('cfg-username').value.trim(),
        backup_time: document.getElementById('cfg-time').value,
        retention_count: Number(document.getElementById('cfg-retention').value),
    };
    const pw = document.getElementById('cfg-password').value;
    if (pw) payload.password = pw;
    try {
        const data = await api('/plugins/nas_backup/api/config', { method: 'POST', body: JSON.stringify(payload) });
        document.getElementById('cfg-password').value = '';
        showAlert(data.warning || 'Saved.', data.warning ? 'warning' : true);
        formInit = false; // re-sync form with what was actually saved
        await refreshAll();
    } catch (e) { showAlert(e.message, false); }
}

async function testConnection() {
    try {
        const data = await api('/plugins/nas_backup/api/test', { method: 'POST' });
        showAlert(data.message, true);
        await refreshAll();
    } catch (e) { showAlert(e.message, false); }
}

async function clearPassword() {
    try {
        await api('/plugins/nas_backup/api/config', { method: 'POST', body: JSON.stringify({ password: '__CLEAR__' }) });
        showAlert('Saved password cleared.', true);
    } catch (e) { showAlert(e.message, false); }
}

async function runBackupNow() {
    const btn = document.getElementById('backup-now-btn');
    btn.disabled = true;
    try {
        const data = await api('/plugins/nas_backup/api/backup-now', { method: 'POST' });
        showToast('Success', data.message, 'success');
        await waitForBackupDone();
    } catch (e) { showToast('Error', e.message, 'error'); }
    finally { btn.disabled = false; }
}

async function waitForBackupDone() {
    // Follow a started backup to completion, then refresh once. The NAS is
    // otherwise only touched on load, test, save, and restore actions.
    await new Promise(r => setTimeout(r, 3000));
    const deadline = Date.now() + 10 * 60 * 1000;
    for (;;) {
        let s;
        try {
            s = await loadStatus();
        } catch {
            return;
        }
        if (!s || !s.running) break;
        if (Date.now() > deadline) break;
        await new Promise(r => setTimeout(r, 4000));
    }
    try { await loadStatus(); } catch { /* keep last rendered state */ }
}

document.addEventListener('DOMContentLoaded', refreshAll);
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeRestoreModal();
});
