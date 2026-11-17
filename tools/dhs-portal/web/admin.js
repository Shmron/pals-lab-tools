const API = '/api/dhs';
let adminToken = localStorage.getItem('dhs_admin_token') || '';
let availableCountries = [];

function $(id) { return document.getElementById(id); }
function headers(extra) { return Object.assign({'Content-Type': 'application/json', 'X-Admin-Token': adminToken}, extra || {}); }

$('admin-login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {username: $('admin-username').value.trim(), password: $('admin-password').value};
  const res = await fetch(`${API}/admin/login`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!data.ok) {
    $('auth-msg').innerHTML = `<div class="msg error">${data.error}</div>`;
    return;
  }
  adminToken = data.token;
  localStorage.setItem('dhs_admin_token', adminToken);
  enterAdmin(data.must_change_pw);
});

$('admin-logout').addEventListener('click', async () => {
  await fetch(`${API}/admin/logout`, {method: 'POST', headers: headers()}).catch(() => {});
  localStorage.removeItem('dhs_admin_token');
  location.reload();
});

$('change-pw-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = await fetch(`${API}/admin/change-password`, {
    method: 'POST', headers: headers(), body: JSON.stringify({new_password: $('new-admin-pw').value}),
  });
  const data = await res.json();
  if (data.ok) { $('must-change-panel').style.display = 'none'; }
});

async function enterAdmin(mustChange) {
  $('auth-wrap').style.display = 'none';
  $('admin-app').style.display = 'block';
  if (mustChange) $('must-change-panel').style.display = 'block';
  await loadCountries();
  await Promise.all([loadPending(), loadAllUsers(), loadInvites()]);
}

async function loadCountries() {
  const res = await fetch(`${API}/countries`);
  const data = await res.json();
  availableCountries = data.countries || [];
  const html = availableCountries.map(c => `<label><input type="checkbox" value="${c.country_code}"> ${c.country_name}</label>`).join('');
  document.querySelectorAll('#inv-countries').forEach(el => el.innerHTML = html || '<em>No surveys loaded yet.</em>');
}

$('inv-select-all').addEventListener('click', () => {
  $('inv-countries').querySelectorAll('input[type=checkbox]').forEach(i => i.checked = true);
});
$('inv-select-none').addEventListener('click', () => {
  $('inv-countries').querySelectorAll('input[type=checkbox]').forEach(i => i.checked = false);
});

function countryCheckboxes(containerId, preselected) {
  const pre = new Set(preselected || []);
  return availableCountries.map(c =>
    `<label><input type="checkbox" data-country="${c.country_code}" ${pre.has(c.country_code) ? 'checked' : ''}> ${c.country_name}</label>`
  ).join('');
}

async function loadPending() {
  const res = await fetch(`${API}/admin/users?status=pending`, {headers: headers()});
  const data = await res.json();
  const list = $('pending-list');
  if (!data.users || !data.users.length) { list.innerHTML = '<em style="color:var(--muted)">No pending requests.</em>'; return; }
  list.innerHTML = data.users.map(u => `
    <div class="row-item" data-id="${u.id}">
      <div class="top">
        <div><b>${u.name}</b> — ${u.email}<div class="meta">${u.institution || ''}${u.purpose ? ' · ' + u.purpose : ''}</div></div>
        <span class="status-pill pending">pending</span>
      </div>
      <div style="margin-top:10px">
        <div class="checkbox-grid approve-countries">${countryCheckboxes()}</div>
        <div class="actions-inline" style="margin-top:8px">
          <button class="btn-sm primary approve-btn">Approve</button>
          <button class="btn-sm danger reject-btn">Reject</button>
        </div>
      </div>
    </div>`).join('');

  list.querySelectorAll('.approve-btn').forEach(btn => btn.addEventListener('click', async (e) => {
    const item = e.target.closest('.row-item');
    const countries = Array.from(item.querySelectorAll('.approve-countries input:checked')).map(i => i.dataset.country);
    await fetch(`${API}/admin/users/${item.dataset.id}/approve`, {method: 'POST', headers: headers(), body: JSON.stringify({countries})});
    loadPending(); loadAllUsers();
  }));
  list.querySelectorAll('.reject-btn').forEach(btn => btn.addEventListener('click', async (e) => {
    const item = e.target.closest('.row-item');
    await fetch(`${API}/admin/users/${item.dataset.id}/reject`, {method: 'POST', headers: headers(), body: JSON.stringify({})});
    loadPending(); loadAllUsers();
  }));
}

async function loadAllUsers() {
  const res = await fetch(`${API}/admin/users`, {headers: headers()});
  const data = await res.json();
  const list = $('all-users-list');
  if (!data.users || !data.users.length) { list.innerHTML = '<em style="color:var(--muted)">No accounts yet.</em>'; return; }
  list.innerHTML = data.users.map(u => `
    <div class="row-item" data-id="${u.id}">
      <div class="top">
        <div><b>${u.name}</b> — ${u.email}<div class="meta">Approved for: ${(u.countries_allowed||[]).join(', ') || '—'}</div></div>
        <span class="status-pill ${u.status}">${u.status}</span>
      </div>
      <div class="actions-inline" style="margin-top:8px">
        ${u.status === 'approved' ? '<button class="btn-sm danger revoke-btn">Revoke</button>' : ''}
        ${(u.status === 'revoked' || u.status === 'rejected') ? '<button class="btn-sm primary reinstate-btn">Reinstate</button>' : ''}
      </div>
    </div>`).join('');

  list.querySelectorAll('.revoke-btn').forEach(btn => btn.addEventListener('click', async (e) => {
    const item = e.target.closest('.row-item');
    await fetch(`${API}/admin/users/${item.dataset.id}/revoke`, {method: 'POST', headers: headers()});
    loadAllUsers();
  }));
  list.querySelectorAll('.reinstate-btn').forEach(btn => btn.addEventListener('click', async (e) => {
    const item = e.target.closest('.row-item');
    await fetch(`${API}/admin/users/${item.dataset.id}/reinstate`, {method: 'POST', headers: headers()});
    loadAllUsers();
  }));
}

async function loadInvites() {
  const res = await fetch(`${API}/admin/invites`, {headers: headers()});
  const data = await res.json();
  const list = $('invite-list');
  const pending = (data.invites || []).filter(i => !i.claimed_at);
  if (!pending.length) { list.innerHTML = '<em style="color:var(--muted)">No pending invites.</em>'; return; }
  list.innerHTML = pending.map(i => `
    <div class="row-item" data-token="${i.token}">
      <div class="top">
        <div><b>${i.name || i.email}</b> — ${i.email}<div class="meta">Approved for: ${(i.countries_allowed||[]).join(', ') || '—'} · expires ${i.expires_at.slice(0,10)}</div></div>
        <button class="btn-sm danger revoke-invite-btn">Revoke</button>
      </div>
    </div>`).join('');
  list.querySelectorAll('.revoke-invite-btn').forEach(btn => btn.addEventListener('click', async (e) => {
    const item = e.target.closest('.row-item');
    await fetch(`${API}/admin/invites/${item.dataset.token}/revoke`, {method: 'POST', headers: headers()});
    loadInvites();
  }));
}

$('invite-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const countries = Array.from($('inv-countries').querySelectorAll('input:checked')).map(i => i.value);
  const body = {email: $('inv-email').value.trim(), name: $('inv-name').value.trim(), countries};
  const res = await fetch(`${API}/admin/invites`, {method: 'POST', headers: headers(), body: JSON.stringify(body)});
  const data = await res.json();
  $('invite-msg').innerHTML = data.ok
    ? `<div class="msg ok">Invite sent. Link: <a href="${data.claim_url}">${data.claim_url}</a></div>`
    : `<div class="msg error">${data.error}</div>`;
  if (data.ok) { e.target.reset(); loadInvites(); }
});

if (adminToken) {
  fetch(`${API}/admin/users?status=pending`, {headers: headers()}).then(res => {
    if (res.ok) enterAdmin(false); else localStorage.removeItem('dhs_admin_token');
  });
}
