const COLORS = ['#d6322b', '#1f6feb', '#1f9d55', '#c26a00', '#8e44ad', '#0097a7', '#ad1457'];
let devices = [];
let liveMap, liveLayer, liveTimer, liveFitted = false;
let repMap, repLayer;

function colorFor(id) { return COLORS[id % COLORS.length]; }

async function loadMe() {
  const me = await api('GET', '/api/me');
  document.getElementById('who').textContent = me.name || me.login;
}

// ---------------- Online ----------------
async function refreshLive() {
  const data = await api('GET', '/api/my/live');
  const list = document.getElementById('live-list');
  document.getElementById('live-empty').hidden = data.length > 0;
  list.innerHTML = data.map(d => `
    <li data-id="${d.id}"><span class="dot ${d.online ? 'on' : ''}"></span><b>${esc(d.name)}</b>
    <div class="muted" style="font-size:13px">${d.online ? 'в сети, ' : ''}${esc(ago(d.last_seen))}</div></li>`).join('');

  liveLayer.clearLayers();
  const bounds = [];
  const byId = {};
  data.forEach(d => {
    if (!d.track.length) return;
    const latlngs = d.track.map(p => [p.lat, p.lon]);
    L.polyline(latlngs, { color: colorFor(d.id), weight: 4, opacity: .85 }).addTo(liveLayer);
    const last = latlngs[latlngs.length - 1];
    const m = L.circleMarker(last, { radius: 8, color: '#fff', weight: 2, fillColor: colorFor(d.id), fillOpacity: 1 })
      .bindTooltip(`${esc(d.name)}<br>${esc(fmtTime(d.track[d.track.length - 1].t))}`).addTo(liveLayer);
    byId[d.id] = m;
    bounds.push(...latlngs);
  });
  if (bounds.length && !liveFitted) { liveMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 16 }); liveFitted = true; }
  list.querySelectorAll('li').forEach(li => li.addEventListener('click', () => {
    const m = byId[li.dataset.id];
    if (m) { liveMap.setView(m.getLatLng(), Math.max(liveMap.getZoom(), 15)); m.openTooltip(); }
  }));
}

function startLive() {
  if (!liveMap) {
    liveMap = makeMap('live-map');
    liveLayer = L.layerGroup().addTo(liveMap);
  }
  setTimeout(() => liveMap.invalidateSize(), 0);
  refreshLive().catch(console.error);
  clearInterval(liveTimer);
  liveTimer = setInterval(() => refreshLive().catch(console.error), 20000);
}

// ---------------- Reports ----------------
function toLocalInput(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function setRange(kind) {
  const now = new Date();
  const start = new Date(now); start.setHours(0, 0, 0, 0);
  let from = start, to = now;
  if (kind === 'yesterday') { from = new Date(start); from.setDate(from.getDate() - 1); to = start; }
  if (kind === 'week') { from = new Date(start); from.setDate(from.getDate() - 6); }
  document.getElementById('rep-from').value = toLocalInput(from);
  document.getElementById('rep-to').value = toLocalInput(to);
}

async function buildReport() {
  const err = document.getElementById('rep-err');
  err.textContent = '';
  const id = document.getElementById('rep-device').value;
  if (!id) { err.textContent = 'Сначала добавьте устройство'; return; }
  const from = Math.floor(new Date(document.getElementById('rep-from').value).getTime() / 1000);
  const to = Math.floor(new Date(document.getElementById('rep-to').value).getTime() / 1000);
  if (!from || !to) { err.textContent = 'Укажите период'; return; }
  try {
    const r = await api('GET', `/api/my/devices/${id}/track?t_from=${from}&t_to=${to}`);
    const s = r.summary;
    document.getElementById('rep-result').hidden = false;
    document.getElementById('rep-stats').innerHTML = s.points ? `
      <div class="stat"><span class="muted">Пройдено</span><b>${s.distance_km} км</b></div>
      <div class="stat"><span class="muted">В движении</span><b>${fmtDuration(s.moving_s)}</b></div>
      <div class="stat"><span class="muted">Макс. скорость</span><b>${s.max_speed_kmh} км/ч</b></div>
      <div class="stat"><span class="muted">Стоянок</span><b>${s.stops.length}</b></div>
      <div class="stat"><span class="muted">Начало</span><b style="font-size:15px">${fmtTime(s.start)}</b></div>
      <div class="stat"><span class="muted">Конец</span><b style="font-size:15px">${fmtTime(s.end)}</b></div>`
      : '<p class="muted">За этот период данных нет.</p>';
    document.getElementById('rep-gpx').href = `/api/my/devices/${id}/track.gpx?t_from=${from}&t_to=${to}`;
    document.getElementById('rep-gpx').hidden = !s.points;

    repLayer.clearLayers();
    const latlngs = r.track.map(p => [p.lat, p.lon]);
    if (latlngs.length) {
      L.polyline(latlngs, { color: '#d6322b', weight: 4 }).addTo(repLayer);
      L.circleMarker(latlngs[0], { radius: 7, color: '#fff', weight: 2, fillColor: '#1f9d55', fillOpacity: 1 }).bindTooltip('Старт').addTo(repLayer);
      L.circleMarker(latlngs[latlngs.length - 1], { radius: 7, color: '#fff', weight: 2, fillColor: '#d6322b', fillOpacity: 1 }).bindTooltip('Финиш').addTo(repLayer);
      s.stops.forEach(st => L.circleMarker([st.lat, st.lon], { radius: 6, color: '#c26a00', fillOpacity: .8 })
        .bindTooltip(`Стоянка ${fmtDuration(st.duration_s)}<br>${fmtTime(st.from)}`).addTo(repLayer));
      repMap.fitBounds(latlngs, { padding: [30, 30], maxZoom: 16 });
    }
    document.getElementById('rep-stops-panel').hidden = !s.stops.length;
    document.getElementById('rep-stops').innerHTML = s.stops.map(st =>
      `<tr><td>${fmtTime(st.from)}</td><td>${fmtTime(st.to)}</td><td>${fmtDuration(st.duration_s)}</td></tr>`).join('');
  } catch (ex) {
    err.textContent = ex.message;
  }
}

function startReports() {
  if (!repMap) {
    repMap = makeMap('rep-map');
    repLayer = L.layerGroup().addTo(repMap);
  }
  setTimeout(() => repMap.invalidateSize(), 0);
  const sel = document.getElementById('rep-device');
  const prev = sel.value;
  sel.innerHTML = devices.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
  if (prev) sel.value = prev;
}

// ---------------- Devices ----------------
async function loadDevices() {
  devices = await api('GET', '/api/my/devices');
  const kindName = { imei: 'GSM-трекер', sd: 'Трекер с SD-картой' };
  document.getElementById('dev-rows').innerHTML = devices.length ? devices.map(d => `
    <tr data-id="${d.id}">
      <td><b>${esc(d.name)}</b></td>
      <td>${kindName[d.kind]}${d.imei ? `<div class="muted" style="font-size:13px">IMEI ${esc(d.imei)}</div>` : ''}</td>
      <td>${esc(ago(d.last_seen))}</td>
      <td class="actions">
        <button class="ghost" data-act="rename">Переименовать</button>
        ${d.kind === 'sd' ? '<button class="ghost" data-act="token">Новый пароль синхронизации</button>' : ''}
        <button class="ghost" data-act="delete">Удалить</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="4" class="muted">Устройств пока нет.</td></tr>';
}

function showSyncSecret(password) {
  document.getElementById('add-secret').hidden = false;
  document.getElementById('add-secret-text').textContent =
    `server_url=${location.origin}\nsync_password=${password}`;
  document.getElementById('add-secret').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

document.getElementById('dev-rows').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const id = btn.closest('tr').dataset.id;
  const d = devices.find(x => String(x.id) === id);
  try {
    if (btn.dataset.act === 'rename') {
      const name = prompt('Новое название', d.name);
      if (!name) return;
      await api('PATCH', `/api/my/devices/${id}`, { name });
    } else if (btn.dataset.act === 'delete') {
      if (!confirm(`Удалить «${d.name}»? Трек этого устройства перестанет быть виден в кабинете.`)) return;
      await api('DELETE', `/api/my/devices/${id}`);
    } else if (btn.dataset.act === 'token') {
      if (!confirm('Старый пароль синхронизации перестанет работать. Продолжить?')) return;
      const r = await api('POST', `/api/my/devices/${id}/sync-password`);
      showSyncSecret(r.sync_password);
    }
    await loadDevices();
  } catch (ex) {
    alert(ex.message);
  }
});

function syncAddForm() {
  const sd = document.getElementById('add-kind').value === 'sd';
  document.getElementById('add-imei-wrap').hidden = sd;
  document.getElementById('add-hint').textContent = sd
    ? 'После добавления появится пароль синхронизации: его нужно вписать в sync_config.txt на SD-карте.'
    : 'IMEI видно в настройках Bluetooth любого телефона рядом с трекером: устройство называется ALTGEO_IMEI_ и 15 цифр.';
}
document.getElementById('add-kind').addEventListener('change', syncAddForm);

document.getElementById('add-go').addEventListener('click', async () => {
  const err = document.getElementById('add-err');
  err.textContent = '';
  document.getElementById('add-secret').hidden = true;
  const body = {
    name: document.getElementById('add-name').value.trim(),
    kind: document.getElementById('add-kind').value,
    imei: document.getElementById('add-imei').value.trim(),
  };
  if (!body.name) { err.textContent = 'Укажите название'; return; }
  try {
    const r = await api('POST', '/api/my/devices', body);
    document.getElementById('add-name').value = '';
    document.getElementById('add-imei').value = '';
    if (r.sync_password) showSyncSecret(r.sync_password);
    await loadDevices();
    liveFitted = false;
  } catch (ex) {
    err.textContent = ex.message;
  }
});

// ---------------- Profile ----------------
document.getElementById('pw-go').addEventListener('click', async () => {
  const err = document.getElementById('pw-err');
  err.className = 'err'; err.textContent = '';
  try {
    await api('POST', '/api/me/password', {
      old_password: document.getElementById('pw-old').value,
      new_password: document.getElementById('pw-new').value,
    });
    err.className = 'ok'; err.textContent = 'Пароль изменён';
    document.getElementById('pw-old').value = document.getElementById('pw-new').value = '';
  } catch (ex) {
    err.textContent = ex.message;
  }
});

// ---------------- Init ----------------
document.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => { setRange(b.dataset.range); buildReport(); }));
document.getElementById('rep-go').addEventListener('click', buildReport);
document.addEventListener('tab', (e) => {
  if (e.detail === 'live') startLive(); else clearInterval(liveTimer);
  if (e.detail === 'reports') startReports();
});

(async () => {
  setRange('today');
  syncAddForm();
  await Promise.all([loadMe(), loadDevices()]);
  setupTabs();
})();
