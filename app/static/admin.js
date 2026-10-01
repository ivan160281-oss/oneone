let clients = [];
let srcMap, srcLayer;

function showCred(login, password) {
  document.getElementById('cred').hidden = false;
  document.getElementById('cred-text').textContent = `Адрес: ${location.origin}\nЛогин: ${login}\nПароль: ${password}`;
}

async function loadClients() {
  clients = await api('GET', '/api/admin/clients');
  document.getElementById('client-rows').innerHTML = clients.length ? clients.map(c => `
    <tr data-id="${c.id}">
      <td><b>${esc(c.login)}</b></td>
      <td>${esc(c.name)}</td>
      <td>${c.devices.length ? c.devices.map(d => `${esc(d.name)} <span class="muted">(${esc(ago(d.last_seen))})</span>`).join('<br>') : '<span class="muted">нет</span>'}</td>
      <td>${c.is_active ? '<span class="ok">активен</span>' : '<span class="muted">заблокирован</span>'}</td>
      <td class="actions">
        <button class="ghost" data-act="reset">Новый пароль</button>
        <button class="ghost" data-act="toggle">${c.is_active ? 'Заблокировать' : 'Разблокировать'}</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="5" class="muted">Клиентов пока нет.</td></tr>';
}

document.getElementById('client-rows').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const c = clients.find(x => String(x.id) === btn.closest('tr').dataset.id);
  try {
    if (btn.dataset.act === 'reset') {
      if (!confirm(`Выдать «${c.login}» новый пароль? Старый перестанет работать.`)) return;
      const r = await api('POST', `/api/admin/clients/${c.id}/reset-password`);
      showCred(c.login, r.password);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      await api('PATCH', `/api/admin/clients/${c.id}`, { is_active: !c.is_active });
    }
    await loadClients();
  } catch (ex) {
    alert(ex.message);
  }
});

document.getElementById('new-go').addEventListener('click', async () => {
  const err = document.getElementById('new-err');
  err.textContent = '';
  try {
    const r = await api('POST', '/api/admin/clients', {
      login: document.getElementById('new-login').value.trim(),
      name: document.getElementById('new-name').value.trim(),
    });
    showCred(r.login, r.password);
    document.getElementById('new-login').value = document.getElementById('new-name').value = '';
    await loadClients();
  } catch (ex) {
    err.textContent = ex.message;
  }
});

async function loadSources() {
  const rows = await api('GET', '/api/admin/sources');
  document.getElementById('source-rows').innerHTML = rows.length ? rows.map(s => `
    <tr>
      <td><code>${esc(s.source_key)}</code>${s.device_name ? `<div class="muted" style="font-size:13px">${esc(s.device_name)}</div>` : ''}</td>
      <td>${s.owner ? esc(s.owner) : '<span class="muted">нет</span>'}</td>
      <td>${s.points} (${s.gps_points})</td>
      <td>${esc(ago(s.last_seen))}</td>
      <td class="actions"><button class="ghost" data-key="${esc(s.source_key)}" data-last="${s.last_seen}">Трек</button></td>
    </tr>`).join('') : '<tr><td colspan="5" class="muted">Данных пока нет.</td></tr>';
}

document.getElementById('source-rows').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-key]');
  if (!btn) return;
  const to = Number(btn.dataset.last), from = to - 86400;
  const r = await api('GET', `/api/admin/track?source_key=${encodeURIComponent(btn.dataset.key)}&t_from=${from}&t_to=${to}`);
  document.getElementById('src-title').textContent =
    `${btn.dataset.key}: ${r.summary.distance_km} км, ${r.summary.points} точек, ${fmtTime(from)} — ${fmtTime(to)}`;
  srcLayer.clearLayers();
  const latlngs = r.track.map(p => [p.lat, p.lon]);
  if (latlngs.length) {
    L.polyline(latlngs, { color: '#d6322b', weight: 4 }).addTo(srcLayer);
    srcMap.fitBounds(latlngs, { padding: [30, 30], maxZoom: 16 });
  }
});

document.addEventListener('tab', (e) => {
  if (e.detail === 'sources') {
    if (!srcMap) { srcMap = makeMap('src-map'); srcLayer = L.layerGroup().addTo(srcMap); }
    setTimeout(() => srcMap.invalidateSize(), 0);
    loadSources().catch(console.error);
  }
});

(async () => {
  const me = await api('GET', '/api/me');
  document.getElementById('who').textContent = me.login;
  await loadClients();
  setupTabs();
})();
