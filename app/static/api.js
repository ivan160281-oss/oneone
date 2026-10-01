// Small shared helpers for the ALTGEO pages.
async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  if (r.status === 401 && !url.startsWith('/api/auth/')) { location.href = '/'; throw new Error('Нужно войти'); }
  const data = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = 'Проверьте заполнение полей';
    throw new Error(msg || ('Ошибка ' + r.status));
  }
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(t) {
  return t ? new Date(t * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—';
}

function fmtDuration(s) {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}

function ago(t) {
  if (!t) return 'нет данных';
  const s = Date.now() / 1000 - t;
  if (s < 90) return 'только что';
  if (s < 3600) return Math.round(s / 60) + ' мин назад';
  if (s < 86400) return Math.round(s / 3600) + ' ч назад';
  return fmtTime(t);
}

async function logout() {
  try { await api('POST', '/api/auth/logout'); } finally { location.href = '/'; }
}

function makeMap(el) {
  const map = L.map(el).setView([55.75, 37.62], 10);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap',
  }).addTo(map);
  return map;
}

function setupTabs() {
  const buttons = document.querySelectorAll('nav.tabs button');
  const show = (name) => {
    buttons.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('section[data-tab]').forEach(s => { s.hidden = s.dataset.tab !== name; });
    document.dispatchEvent(new CustomEvent('tab', { detail: name }));
  };
  buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
  show(buttons[0].dataset.tab);
}
