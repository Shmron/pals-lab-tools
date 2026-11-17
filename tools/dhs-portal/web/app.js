const API = '/api/dhs';
let token = localStorage.getItem('dhs_token') || '';
let userName = '';
let countriesAllowed = [];
let chatHistory = [];   // [{role, content}] — plain text turns only; tool calls stay server-side per request
let coverageMap = null;

// ── small helpers ────────────────────────────────────────────────────────────

function $(id) { return document.getElementById(id); }

function authHeaders(extra) {
  return Object.assign({'Content-Type': 'application/json', 'X-Session-Token': token}, extra || {});
}

function showMsg(el, text, cls) {
  el.innerHTML = text ? `<div class="msg ${cls}">${text}</div>` : '';
}

// ── auth screen wiring ──────────────────────────────────────────────────────

document.querySelectorAll('#auth-tabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#auth-tabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    $('login-form').style.display = tab.dataset.tab === 'login' ? 'block' : 'none';
    $('signup-form').style.display = tab.dataset.tab === 'signup' ? 'block' : 'none';
    $('forgot-form').style.display = 'none';
    showMsg($('auth-msg'), '', '');
    if (tab.dataset.tab === 'signup') loadSignupCountries();
  });
});

$('forgot-link').addEventListener('click', () => {
  $('login-form').style.display = 'none';
  $('signup-form').style.display = 'none';
  $('forgot-form').style.display = 'block';
});
$('back-to-login').addEventListener('click', () => {
  $('forgot-form').style.display = 'none';
  $('login-form').style.display = 'block';
});

async function loadSignupCountries() {
  const box = $('su-countries');
  try {
    const res = await fetch(`${API}/countries`);
    const data = await res.json();
    if (!data.countries || !data.countries.length) {
      box.innerHTML = '<em style="color:var(--muted);font-size:12px">No surveys available yet.</em>';
      return;
    }
    box.innerHTML = data.countries.map(c =>
      `<label><input type="checkbox" value="${c.country_code}"> ${c.country_name} (${c.surveys.length} survey${c.surveys.length===1?'':'s'})</label>`
    ).join('');
  } catch (e) {
    box.innerHTML = '<em style="color:var(--danger);font-size:12px">Could not load country list.</em>';
  }
}

$('su-select-all').addEventListener('click', () => {
  $('su-countries').querySelectorAll('input[type=checkbox]').forEach(i => i.checked = true);
});
$('su-select-none').addEventListener('click', () => {
  $('su-countries').querySelectorAll('input[type=checkbox]').forEach(i => i.checked = false);
});

$('signup-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const countries = Array.from($('su-countries').querySelectorAll('input:checked')).map(i => i.value);
  const body = {
    name: $('su-name').value.trim(),
    email: $('su-email').value.trim(),
    password: $('su-password').value,
    institution: $('su-institution').value.trim(),
    purpose: $('su-purpose').value.trim(),
    countries_requested: countries,
  };
  const res = await fetch(`${API}/signup`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  showMsg($('auth-msg'), data.message || data.error, data.ok ? 'ok' : 'error');
  if (data.ok) e.target.reset();
});

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {email: $('login-email').value.trim(), password: $('login-password').value};
  const res = await fetch(`${API}/login`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!data.ok) { showMsg($('auth-msg'), data.error, 'error'); return; }
  token = data.token;
  localStorage.setItem('dhs_token', token);
  enterApp(data.name, data.countries_allowed);
});

$('forgot-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {email: $('forgot-email').value.trim()};
  const res = await fetch(`${API}/forgot-password`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  showMsg($('auth-msg'), data.message, 'ok');
});

$('reset-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const params = new URLSearchParams(location.search);
  const body = {token: params.get('reset_token'), password: $('reset-password').value};
  const res = await fetch(`${API}/reset-password`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  showMsg($('auth-msg'), data.message || data.error, data.ok ? 'ok' : 'error');
  if (data.ok) { $('reset-form').style.display = 'none'; $('login-form').style.display = 'block'; }
});

$('logout-link').addEventListener('click', async () => {
  await fetch(`${API}/logout`, {method: 'POST', headers: authHeaders()}).catch(() => {});
  localStorage.removeItem('dhs_token');
  location.reload();
});

// ── app entry ─────────────────────────────────────────────────────────────

function enterApp(name, countries) {
  userName = name;
  countriesAllowed = countries || [];
  $('side-name').textContent = userName;
  $('side-countries').textContent = countriesAllowed.length ? countriesAllowed.join(', ') : 'none yet — contact an admin';
  $('auth-wrap').style.display = 'none';
  $('app').classList.add('active');
  addBubble('assistant', `Hi ${userName.split(' ')[0]} — ask me anything about the DHS data you're approved for, or try one of the suggestions on the left.`);
}

async function init() {
  const params = new URLSearchParams(location.search);
  if (params.get('reset_token')) {
    document.querySelectorAll('#login-form, #signup-form, #forgot-form').forEach(f => f.style.display = 'none');
    $('reset-form').style.display = 'block';
    return;
  }
  if (!token) return;
  try {
    const res = await fetch(`${API}/validate-session`, {headers: authHeaders()});
    const data = await res.json();
    if (data.ok) enterApp(data.name, data.countries_allowed);
    else localStorage.removeItem('dhs_token');
  } catch (e) { /* stay on auth screen */ }
}
init();

// ── chat ──────────────────────────────────────────────────────────────────

function addBubble(role, text) {
  const el = document.createElement('div');
  el.className = `bubble ${role}`;
  if (role === 'assistant' && window.marked && window.DOMPurify) {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
  } else {
    el.textContent = text;
  }
  $('chat-scroll').appendChild(el);
  $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
  return el;
}

function addStatusBubble(text) {
  const el = document.createElement('div');
  el.className = 'bubble status';
  el.textContent = text;
  $('chat-scroll').appendChild(el);
  $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
  return el;
}

document.querySelectorAll('.prompt-chip').forEach(btn => {
  btn.addEventListener('click', () => {
    $('chat-input').value = btn.dataset.prompt;
    $('chat-form').dispatchEvent(new Event('submit'));
  });
});

$('chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('chat-input').value.trim();
  if (!text) return;
  $('chat-input').value = '';
  $('chat-send').disabled = true;

  addBubble('user', text);
  chatHistory.push({role: 'user', content: text});

  const statusEl = addStatusBubble('Thinking…');
  let assistantEl = null;

  try {
    const res = await fetch(`${API}/chat`, {
      method: 'POST', headers: authHeaders(), body: JSON.stringify({messages: chatHistory}),
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({error: 'Request failed'}));
      statusEl.remove();
      addBubble('assistant', `⚠️ ${err.error || 'Something went wrong.'}`);
      $('chat-send').disabled = false;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data:')) continue;
        let evt;
        try { evt = JSON.parse(line.slice(5).trim()); } catch { continue; }

        if (evt.type === 'status') {
          statusEl.textContent = evt.text;
        } else if (evt.type === 'text') {
          statusEl.remove();
          assistantEl = addBubble('assistant', evt.text);
          chatHistory.push({role: 'assistant', content: evt.text});
        } else if (evt.type === 'chart') {
          renderChartInto(assistantEl || addBubble('assistant', ''), evt.spec);
        } else if (evt.type === 'error') {
          statusEl.remove();
          addBubble('assistant', `⚠️ ${evt.text}`);
        } else if (evt.type === 'done') {
          // stream complete
        }
      }
    }
  } catch (e) {
    statusEl.remove();
    addBubble('assistant', '⚠️ Connection error — please try again.');
  } finally {
    $('chat-send').disabled = false;
  }
});

// ── inline chart / map rendering ────────────────────────────────────────────

let chartCounter = 0;

function renderChartInto(bubbleEl, spec) {
  if (spec.chart_type === 'map') {
    const id = `map-${chartCounter++}`;
    const wrap = document.createElement('div');
    wrap.className = 'map-wrap';
    wrap.id = id;
    bubbleEl.appendChild(wrap);
    $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
    setTimeout(() => {
      const map = L.map(id);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors', maxZoom: 18,
      }).addTo(map);
      const pts = (spec.map_data || []).filter(p => p.lat != null && p.lon != null);
      if (!pts.length) { map.setView([0, 20], 3); return; }
      const bounds = [];
      pts.forEach(p => {
        const marker = L.circleMarker([p.lat, p.lon], {
          radius: 5, color: '#2f9e8f', fillColor: '#2f9e8f', fillOpacity: .7, weight: 1,
        }).addTo(map);
        marker.bindPopup(`<b>${p.label || ''}</b>${p.value != null ? `<br>${spec.color_label || 'value'}: ${p.value}` : ''}`);
        bounds.push([p.lat, p.lon]);
      });
      map.fitBounds(bounds, {padding: [20, 20]});
    }, 30);
    return;
  }

  const id = `chart-${chartCounter++}`;
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.innerHTML = `<canvas id="${id}"></canvas>`;
  bubbleEl.appendChild(wrap);
  $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
  setTimeout(() => {
    const ctx = document.getElementById(id).getContext('2d');
    new Chart(ctx, {
      type: spec.chart_type,
      data: {labels: spec.labels || [], datasets: (spec.datasets || []).map(d => ({label: d.label, data: d.data}))},
      options: {
        responsive: true,
        plugins: {title: {display: !!spec.title, text: spec.title || ''}},
        scales: (spec.chart_type === 'pie' || spec.chart_type === 'doughnut') ? {} : {
          x: {title: {display: !!spec.x_label, text: spec.x_label || ''}},
          y: {title: {display: !!spec.y_label, text: spec.y_label || ''}},
        },
      },
    });
  }, 30);
}

// ── coverage modal ───────────────────────────────────────────────────────────

$('open-coverage').addEventListener('click', async () => {
  $('coverage-modal').classList.add('active');
  const res = await fetch(`${API}/coverage`, {headers: authHeaders()});
  const data = await res.json();
  renderCoverageMap(data.coverage || []);
});
$('close-coverage').addEventListener('click', () => $('coverage-modal').classList.remove('active'));
$('coverage-modal').addEventListener('click', (e) => { if (e.target.id === 'coverage-modal') $('coverage-modal').classList.remove('active'); });

function renderCoverageMap(rows) {
  if (coverageMap) { coverageMap.remove(); coverageMap = null; }
  coverageMap = L.map('coverage-map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors', maxZoom: 18,
  }).addTo(coverageMap);

  const bounds = [];
  rows.forEach(r => {
    if (r.centroid_lat == null || r.centroid_lon == null) return;
    const radius = Math.min(28, 6 + Math.sqrt(r.n_clusters));
    const marker = L.circleMarker([r.centroid_lat, r.centroid_lon], {
      radius, color: '#2f9e8f', fillColor: '#6fc9bc', fillOpacity: .55, weight: 1.5,
    }).addTo(coverageMap);
    marker.bindPopup(
      `<b>${r.country_name}</b> — ${r.survey_code} (${r.dhsyear || '?'})<br>${r.n_clusters} clusters with coordinates`
    );
    bounds.push([r.centroid_lat, r.centroid_lon]);
  });
  if (bounds.length) coverageMap.fitBounds(bounds, {padding: [30, 30]});
  else coverageMap.setView([0, 20], 3);

  $('coverage-list').innerHTML = rows.length ? (
    '<table class="result-table"><thead><tr><th>Country</th><th>Survey</th><th>Year</th><th>Clusters w/ coordinates</th></tr></thead><tbody>' +
    rows.map(r => `<tr><td>${r.country_name}</td><td>${r.survey_code}</td><td>${r.dhsyear || '—'}</td><td>${r.n_clusters}</td></tr>`).join('') +
    '</tbody></table>'
  ) : '<em>No coverage data for your approved countries yet.</em>';
}
