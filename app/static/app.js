function toggleAll(cb) {
  document.querySelectorAll('input[name=ids]').forEach(x => x.checked = cb.checked);
}
function toggleInc(cb) {
  document.querySelectorAll('input.inc').forEach(x => x.checked = cb.checked);
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---------------------------------------------------------------- 3.9 what's running
// The sidebar (and Activity → Running now) lists every background task with its progress.
// One renderer for both; /api/status is polled every 2 s while something runs, every 8 s
// when idle, and not at all while the tab is hidden.
const Tasks = (function () {
  const fmtEta = s => s < 60 ? 'under a minute left' : s < 3600 ? `about ${Math.round(s / 60)} min left`
    : `about ${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min left`;
  const fmtAgo = s => s < 60 ? 'just now' : `${Math.round(s / 60)} min ago`;
  const num = n => n.toLocaleString();

  function item(t, recent) {
    const tag = t.url ? 'a' : 'div', href = t.url ? ` href="${esc(t.url)}"` : '';
    const state = recent ? t.state : t.state === 'paused' ? 'paused' : 'running';
    const count = recent ? fmtAgo(t.ago) : t.total ? `${num(t.done)} / ${num(t.total)}` : '';
    const mark = recent ? (t.state === 'error' ? '✗ ' : '✓ ') : '';
    const bar = recent ? '' : `<div class="t-bar${t.pct === null ? ' indet' : ''}" role="progressbar" aria-label="${esc(t.label)}"
      ${t.pct === null ? '' : `aria-valuenow="${t.pct}" aria-valuemin="0" aria-valuemax="100"`}><i style="width:${t.pct === null ? 100 : t.pct}%"></i></div>`;
    const detail = recent ? (t.error || (t.total ? `${num(t.total)} done` : 'Finished')) : t.detail;
    const sub = recent ? '' : [t.sub, t.eta ? fmtEta(t.eta) : ''].filter(Boolean).join(' · ');
    return `<${tag} class="task st-${state}"${href}>
      <div class="t-top"><span class="t-name">${mark}${esc(t.label)}</span><span class="t-num">${esc(count)}</span></div>
      ${bar}${detail ? `<div class="t-detail" title="${esc(detail)}">${esc(detail)}</div>` : ''}
      ${sub ? `<div class="t-sub">${esc(sub)}</div>` : ''}</${tag}>`;
  }

  function render(box, snap) {
    if (!box || !snap) return;
    const max = +box.dataset.max || 99;
    const run = snap.running || [], rec = snap.recent || [];
    const shown = run.slice(0, max), recShown = rec.slice(0, Math.max(0, Math.min(2, max - shown.length + 1)));
    let html = shown.map(t => item(t)).join('');
    if (run.length > shown.length) html += `<a class="t-more" href="/activity/now">+${run.length - shown.length} more running</a>`;
    if (!run.length) html += '<div class="t-idle">Nothing running</div>';
    html += (box.dataset.max ? recShown : rec).map(t => item(t, true)).join('');
    box.innerHTML = html;
  }

  function apply(s) {
    const snap = s.tasks;
    render(document.getElementById('tasks'), snap);
    render(document.getElementById('tasks-full'), snap);
    const n = (snap.running || []).length;
    const words = {live: 'Watching', paused: 'Paused', off: 'Not watching'};
    document.querySelectorAll('.mstatus .dot').forEach(d => { d.className = 'dot ' + (n ? 'busy' : (s.watch_state || 'off')); });
    const mt = document.querySelector('.mstatus-text');
    if (mt) mt.textContent = n ? `${n} running` : (words[s.watch_state] || '');
    if (s.watch_state) {
      const wd = document.getElementById('watch-dot'), wt = document.getElementById('watch-text');
      if (wd) wd.className = 'dot ' + s.watch_state;
      if (wt) wt.textContent = {live: 'Watcher live', paused: 'Paused', off: 'Watcher off'}[s.watch_state];
    }
    for (const [id, v] of [['nav-queue', s.queue_count], ['nav-dupes', s.dupe_count]]) {
      const el = document.getElementById(id);
      if (el && v !== undefined) { el.hidden = !v; el.textContent = v < 1000 ? v : '999+'; }
    }
    return n;
  }

  let timer = null;
  function poll() {
    clearTimeout(timer);
    if (document.hidden) return;
    fetch('/api/status', {cache: 'no-store'}).then(r => r.ok ? r.json() : Promise.reject())
      .then(s => { const n = apply(s); timer = setTimeout(poll, n ? 2000 : 8000); })
      .catch(() => { timer = setTimeout(poll, 10000); });
  }

  // first paint from the data the page came with, then keep it current
  const init = document.getElementById('tasks-init');
  if (init) {
    try {
      const snap = JSON.parse(init.textContent);
      render(document.getElementById('tasks'), snap);
      render(document.getElementById('tasks-full'), snap);
      timer = setTimeout(poll, (snap.running || []).length ? 2000 : 8000);
    } catch (e) { poll(); }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // phones: the status chip in the top bar opens the list
  const chip = document.getElementById('mstatus'), card = document.getElementById('statuscard');
  if (chip && card) {
    chip.addEventListener('click', e => {
      e.stopPropagation();
      const open = !card.classList.contains('open');
      card.classList.toggle('open', open);
      chip.setAttribute('aria-expanded', open);
      if (open) poll();
    });
    document.addEventListener('click', e => {
      if (card.classList.contains('open') && !card.contains(e.target)) {
        card.classList.remove('open'); chip.setAttribute('aria-expanded', 'false');
      }
    });
  }
  return {poll};
})();

// album search on the album page (iTunes or JioSaavn)
function itSearch(aid) {
  const q = document.getElementById('itq').value.trim();
  const srcSel = document.getElementById('itsrc');
  const src = srcSel ? srcSel.value : 'itunes';
  const ul = document.getElementById('itres');
  if (!q) return;
  ul.innerHTML = '<li class="muted">Searching…</li>';
  fetch('/api/search?source=' + encodeURIComponent(src) + '&q=' + encodeURIComponent(q))
    .then(r => r.json()).then(res => {
      if (res.error) { ul.innerHTML = `<li class="muted">${esc(res.error)}</li>`; return; }
      if (!res.length) { ul.innerHTML = '<li class="muted">No albums found</li>'; return; }
      ul.innerHTML = res.map(c => `
        <li><div class="noimg" aria-hidden="true">♪${(c.thumb || c.artwork) ? `<img src="${esc(c.thumb || c.artwork)}" alt="" loading="lazy" onerror="this.remove()">` : ''}</div>
          <div><b>${esc(c.album)}</b><br><span class="muted">${esc(c.album_artist)} · ${esc((c.date||'').slice(0,4))} · ${c.track_count || '?'} tr${c.genre ? ' · ' + esc(c.genre) : ''}</span>
            <span class="badge src">${esc(c.source_label || c.source || src)}</span></div>
          <form method="post" action="/album/${aid}/rematch" class="usebtn">
            <input type="hidden" name="collection_id" value="${esc(c.ref)}">
            <button class="btn xs">Use</button></form></li>`).join('');
    }).catch(() => { ul.innerHTML = '<li class="muted">Search failed</li>'; });
}
document.addEventListener('keydown', e => {
  if (e.target && e.target.id === 'itq' && e.key === 'Enter') {
    e.preventDefault();
    document.getElementById('itbtn').click();
  }
});

// folder browser on the scan page
const FOLDER_SVG = '<svg class="icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const UP_SVG = '<svg class="icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
function browse(path) {
  const ul = document.getElementById('browser');
  const crumbs = document.getElementById('crumbs');
  if (!ul) return;
  fetch('/api/browse?path=' + encodeURIComponent(path || '')).then(r => r.json()).then(d => {
    if (d.error) { ul.innerHTML = `<li class="muted">${esc(d.error)}</li>`; return; }
    const input = document.getElementById('scanpath');
    input.value = d.rel === '/' ? '' : d.rel;
    crumbs.innerHTML = `${esc(d.path)} · ${d.dirs.length} folder(s)` + (d.audio ? ` · ${d.audio} audio file(s) here` : '');
    let html = d.parent !== null && d.parent !== undefined
      ? `<li class="up" tabindex="0" data-p="${esc(d.parent === '/' ? '' : d.parent)}">${UP_SVG} Up</li>` : '';
    html += d.dirs.map(n => `<li tabindex="0" data-p="${esc((d.rel === '/' ? '' : d.rel + '/') + n)}">${FOLDER_SVG} ${esc(n)}</li>`).join('');
    ul.innerHTML = html || '<li class="muted">No sub-folders</li>';
    ul.querySelectorAll('li[data-p]').forEach(li => {
      li.onclick = () => browse(li.dataset.p);
      li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); browse(li.dataset.p); } };
    });
  }).catch(() => { ul.innerHTML = '<li class="muted">Could not list folders</li>'; });
}
(function () {
  const btn = document.getElementById('browsebtn'), box = document.getElementById('browsebox');
  if (!btn || !box) return;
  btn.addEventListener('click', () => {
    const open = box.hidden;
    box.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) browse(document.getElementById('scanpath').value.trim());
  });
})();

// Scan page: live progress of the latest scan
(function () {
  const panel = document.getElementById('runpanel');
  if (!panel || !panel.dataset.scan) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const fmt = n => Number(n).toLocaleString('en-US');
  // Reload once when a scan that was running when the page loaded finishes, not on every visit
  // to a page whose last scan is long done (that reloaded it every few seconds, closing Browse).
  let wasRunning = panel.dataset.running === '1';
  const tick = () => fetch('/api/scan', {cache: 'no-store'}).then(r => r.json()).then(d => {
    const s = d.scan;
    if (!s) return;
    if (String(s.id) !== panel.dataset.scan) { location.reload(); return; }
    set('run-state', s.paused && s.running ? 'Paused' : s.running ? 'Running' : 'Finished');
    set('run-count', `[${fmt(s.done)} / ${fmt(s.total)} folders]`);
    const bar = document.getElementById('run-bar');
    if (bar) { bar.setAttribute('aria-valuenow', s.pct); bar.firstElementChild.style.width = s.pct + '%'; }
    set('n-correct', s.correct); set('n-review', s.review); set('n-missed', s.missed);
    const log = document.getElementById('run-log');
    if (log && d.log && d.log.length) {
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.innerHTML = d.log.map(([st, text]) => `<div class="ln"><span class="lv lv-${esc(st)}">${esc(st)}</span> ${esc(text)}</div>`).join('');
      if (stick) log.scrollTop = log.scrollHeight;
    }
    const browsing = document.getElementById('browsebox') && !document.getElementById('browsebox').hidden;
    if (wasRunning && !s.running && !browsing) { setTimeout(() => location.reload(), 1200); return; }
    wasRunning = s.running;
    setTimeout(tick, s.running ? 2000 : 10000);
  }).catch(() => setTimeout(tick, 5000));
  setTimeout(tick, 1500);
})();

// Forms that save as soon as a control changes (Scan page watcher settings)
document.addEventListener('change', e => {
  const el = e.target;
  if (el.hasAttribute && el.hasAttribute('data-autosubmit') && el.form) {
    if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
  }
});

// Settings: insert a field token into the rename pattern at the cursor
document.addEventListener('click', e => {
  const b = e.target.closest('button[data-insert]');
  if (!b) return;
  const inp = document.getElementById(b.dataset.target);
  if (!inp) return;
  const t = b.dataset.insert;
  const s0 = inp.selectionStart ?? inp.value.length, s1 = inp.selectionEnd ?? inp.value.length;
  inp.value = inp.value.slice(0, s0) + t + inp.value.slice(s1);
  inp.focus();
  inp.setSelectionRange(s0 + t.length, s0 + t.length);
  inp.dispatchEvent(new Event('input', {bubbles: true}));
});

// Settings: live preview of a rename pattern with a sample song
(function () {
  const sample = {track: 3, title: 'Munbe Vaa', artist: 'Shreya Ghoshal, Naresh Iyer', album: 'Sillunu Oru Kaadhal',
                  albumartist: 'A.R. Rahman', year: 2006, disc: 1, discnumber: 1, tracknumber: 3, genre: 'Tamil'};
  const render = pat => pat.replace(/\{(\w+)(?::0?(\d+)d)?\}/g, (m, k, w) => {
    if (!(k in sample)) return m;
    let v = String(sample[k]);
    if (w) v = v.padStart(+w, '0');
    return v;
  }).replace(/[\\/:*?"<>|]/g, '_') + '.flac';
  document.querySelectorAll('input[data-preview]').forEach(inp => {
    const out = document.getElementById(inp.dataset.preview);
    if (!out) return;
    const up = () => { out.textContent = inp.value.trim() ? render(inp.value.trim()) : '(original file names kept)'; };
    inp.addEventListener('input', up);
    up();
  });
})();

// "Use" / "Match" buttons: show that something is happening straight away
document.addEventListener('submit', e => {
  const f = e.target;
  if (!f.classList || !f.classList.contains('usebtn')) return;
  const b = f.querySelector('button');
  if (b) { b.disabled = true; b.textContent = 'Matching…'; }
  document.querySelectorAll('.usebtn button').forEach(x => x.disabled = true);
});

// album page: poll while the album is being analyzed/applied, reload when it finishes
(function watchAlbum() {
  const box = document.getElementById('working');
  if (!box) return;
  const aid = box.dataset.aid;
  const label = {queued: 'Waiting to analyze', analyzing: 'Analyzing', applying: 'Writing tags'};
  const tick = () => fetch('/api/album/' + aid).then(r => r.json()).then(s => {
    if (!(s.status in label)) { location.reload(); return; }
    let t = label[s.status] + '…';
    if (s.status === 'queued' && s.ahead) t = `Waiting — ${s.ahead} job(s) ahead…`;
    document.getElementById('working-text').textContent = t;
    setTimeout(tick, 2000);
  }).catch(() => setTimeout(tick, 5000));
  setTimeout(tick, 1500);
})();

// Click-to-sort for short, single-page tables (album tracks, settings). Long lists (queue,
// history, log) sort on the server instead, so sorting covers every page, not just this one.
// A cell's sort value is its data-sort attribute, else the value typed in its input, else its text.
(function clientSort() {
  const value = (td, type) => {
    if (!td) return null;
    let v = td.dataset.sort;
    if (v === undefined) {
      const inp = td.querySelector('input:not([type=checkbox]), select');
      v = inp ? inp.value : td.textContent;
    }
    v = (v || '').trim();
    if (type === 'num') { const n = parseFloat(v); return isNaN(n) ? null : n; }
    return v === '' ? null : v.toLowerCase();
  };
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
  document.querySelectorAll('table.csort').forEach(table => {
    const ths = [...table.tHead.rows[0].cells];
    ths.forEach((th, col) => {
      if (th.classList.contains('nosort')) return;
      th.classList.add('sortable');
      th.tabIndex = 0;
      th.title = 'Sort by ' + th.textContent.trim().toLowerCase();
      const label = document.createElement('span'); label.className = 'arrow'; label.textContent = '↕';
      th.appendChild(label);
      const sort = () => {
        const type = th.dataset.type || 'text';
        const asc = th.getAttribute('aria-sort') !== 'ascending';
        ths.forEach(h => { h.removeAttribute('aria-sort'); h.classList.remove('sorted');
                           const a = h.querySelector('.arrow'); if (a) a.textContent = '↕'; });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        th.classList.add('sorted');
        label.textContent = asc ? '▲' : '▼';
        const body = table.tBodies[0];
        const rows = [...body.rows].map((r, i) => ({r, i, v: value(r.cells[col], type)}));
        rows.sort((a, b) => {
          if (a.v === null && b.v === null) return a.i - b.i;
          if (a.v === null) return 1;            // blanks last either way
          if (b.v === null) return -1;
          const c = type === 'num' ? a.v - b.v : collator.compare(a.v, b.v);
          return (asc ? c : -c) || a.i - b.i;
        });
        rows.forEach(x => body.appendChild(x.r));
      };
      th.addEventListener('click', e => { if (e.target.closest('input')) return; sort(); });
      th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
    });
  });
})();

// ---------------------------------------------------------------- 1.4.0 phone / web-view support

// Tap anywhere on a card to open it (links, checkboxes and inputs keep their own behaviour).
document.addEventListener('click', e => {
  const tr = e.target.closest('tr[data-href]');
  if (!tr || e.target.closest('a, input, button, select, label, textarea')) return;
  if (window.getSelection && String(window.getSelection())) return;   // don't hijack text selection
  location.href = tr.dataset.href;
});

// Bulk-selection bar: appears (on phones) once at least one album is ticked.
function updateSel(form) {
  const all = form.querySelector('input[name=all_matching]');
  const n = all && all.checked ? +form.dataset.total : form.querySelectorAll('input[name=ids]:checked').length;
  form.classList.toggle('has-sel', n > 0);
  form.querySelectorAll('.selcount').forEach(c => { c.textContent = n; });
}
document.addEventListener('change', e => {
  const form = e.target.closest('form.selectable');
  if (form) updateSel(form);
});
const _toggleAll = window.toggleAll;
window.toggleAll = function (cb) {
  _toggleAll(cb);
  document.querySelectorAll('input[onclick^="toggleAll"]').forEach(x => x.checked = cb.checked);
  const form = document.querySelector('form.selectable'); if (form) updateSel(form);
};

// In-page confirm for forms with data-confirm. Native confirm() can be silently blocked in
// app web views such as nzb360's Web Interfaces, which would make these buttons do nothing.
(function () {
  const modal = document.getElementById('confirm');
  if (!modal) return;
  let pending = null;
  const close = () => { modal.hidden = true; pending = null; };
  document.addEventListener('submit', e => {
    const f = e.target;
    const msg = (e.submitter && e.submitter.dataset.confirm) || (f.dataset && f.dataset.confirm);  // button or form
    if (!msg || f.dataset.confirmed) return;
    if (e.submitter && e.submitter.hasAttribute('data-noconfirm')) return;
    e.preventDefault();
    pending = {form: f, submitter: e.submitter};
    document.getElementById('confirm-text').textContent = msg;
    modal.hidden = false;
    document.getElementById('confirm-ok').focus();
  }, true);
  document.getElementById('confirm-ok').addEventListener('click', () => {
    if (!pending) return;
    const {form, submitter} = pending;
    form.dataset.confirmed = '1';
    close();
    if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
    delete form.dataset.confirmed;      // the next button on the same form asks again
  });
  modal.addEventListener('click', e => { if (e.target === modal || e.target.hasAttribute('data-close')) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) close(); });
})();

// Phones hide table headers, so client-sorted tables (album tracks, settings) get a sort picker.
(function () {
  document.querySelectorAll('table.csort.cards').forEach(table => {
    const ths = [...table.tHead.rows[0].cells].filter(th => th.classList.contains('sortable'));
    if (!ths.length) return;
    const wrap = document.createElement('div'); wrap.className = 'listbar';
    const label = document.createElement('label'); label.className = 'msort';
    label.innerHTML = '<span>Sort</span>';
    const sel = document.createElement('select'); sel.setAttribute('aria-label', 'Sort by');
    sel.add(new Option('Original order', ''));
    ths.forEach((th, i) => {
      const name = th.childNodes[0].textContent.trim();
      const num = th.dataset.type === 'num';
      sel.add(new Option(`${name} · ${num ? 'low → high' : 'A → Z'}`, i + ':asc'));
      sel.add(new Option(`${name} · ${num ? 'high → low' : 'Z → A'}`, i + ':desc'));
    });
    sel.addEventListener('change', () => {
      if (!sel.value) { location.reload(); return; }
      const [i, dir] = sel.value.split(':');
      const th = ths[+i];
      // the header's own handler toggles direction; set the state so one click lands on `dir`
      th.setAttribute('aria-sort', dir === 'asc' ? 'descending' : 'ascending');
      th.click();
    });
    label.appendChild(sel); wrap.appendChild(label);
    const host = table.closest('.tablewrap, .card') || table;
    host.parentNode.insertBefore(wrap, host);
  });
})();

// Touch screens can't hover: tapping a badge with details (old → new value, lyrics reason)
// shows them in a small bubble.
(function () {
  let tip = null;
  const hide = () => { if (tip) { tip.remove(); tip = null; } };
  document.addEventListener('click', e => {
    const el = e.target.closest('.tag[title], .small[title], .conf[title]');
    hide();
    if (!el || !matchMedia('(hover: none)').matches) return;
    tip = document.createElement('div'); tip.className = 'tip'; tip.textContent = el.getAttribute('title');
    document.body.appendChild(tip);
    const r = el.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left + scrollX), scrollX + innerWidth - tip.offsetWidth - 8);
    tip.style.left = left + 'px';
    tip.style.top = (r.bottom + scrollY + 6) + 'px';
  });
  addEventListener('scroll', hide, {passive: true});
})();

// Library: batch album-artist change. Enter in the name box must not trigger "Approve",
// and the change is confirmed with the album count first.
(function () {
  const inp = document.getElementById('new_artist'), go = document.getElementById('setaa-go');
  if (!inp || !go) return;
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go.click(); } });
  go.addEventListener('click', () => {
    const form = go.form;
    const all = form.querySelector('input[name=all_matching]');
    const n = all && all.checked ? +form.dataset.total : form.querySelectorAll('input[name=ids]:checked').length;
    delete form.dataset.confirmed;
    const sel = document.getElementById('lpolicy');
    const kind = sel ? sel.options[sel.selectedIndex].text.split(' — ')[0] : 'this kind';
    const who = inp.value.trim() ? ` with Album Artist “${inp.value.trim()}”` : '';
    form.dataset.confirm = n
      ? `Decide ${n} album(s) as ${kind}${who}? It's written to every file of each folder and kept by re-checks; Undo is on each album's page.`
      : '';
  });
  // the other bulk buttons don't ask
  go.form.querySelectorAll('button[name=action]:not(#setaa-go)').forEach(b =>
    b.addEventListener('click', () => { delete go.form.dataset.confirm; }));
})();

// Library: "Update only…" asks before it starts, naming the parts and the album count;
// and only one of the bulk pop-ups is open at a time.
(function () {
  const go = document.getElementById('only-go');
  if (!go) return;
  const form = go.form;
  go.addEventListener('click', e => {
    const picked = [...form.querySelectorAll('input[name=only]:checked')].map(i => i.parentNode.textContent.trim());
    const all = form.querySelector('input[name=all_matching]');
    const n = all && all.checked ? +form.dataset.total : form.querySelectorAll('input[name=ids]:checked').length;
    delete form.dataset.confirm;
    if (!picked.length || !n) {
      e.preventDefault();
      const hint = document.getElementById('only-hint');
      if (hint) { hint.textContent = !n ? 'Select the albums to update first.' : 'Tick what to update.'; hint.hidden = false; }
      return;
    }
    form.dataset.confirm = `Update only ${picked.join(', ')} on ${n} album(s)?`;
  });
  form.querySelectorAll('.bulk details.setaa').forEach(d => d.addEventListener('toggle', () => {
    if (d.open) form.querySelectorAll('.bulk details.setaa[open]').forEach(o => { if (o !== d) o.open = false; });
  }));
})();

// ---------------------------------------------------------------- 2.2 playlists

// Example prompts fill the request box
document.addEventListener('click', e => {
  const b = e.target.closest('button[data-fill]');
  if (!b) return;
  const t = document.getElementById('prompt');
  if (t) { t.value = b.dataset.fill; t.focus(); }
});

// Song picker ("From a song" / "Radio")
(function () {
  const q = document.getElementById('seedq'), res = document.getElementById('seedres');
  if (!q || !res) return;
  let timer = null;
  const pick = li => {
    document.getElementById('seed').value = li.dataset.id;
    document.getElementById('seedsel').innerHTML = li.innerHTML;
    document.getElementById('seedpick').classList.add('has');
    res.innerHTML = ''; q.value = '';
  };
  q.addEventListener('input', () => {
    clearTimeout(timer);
    const v = q.value.trim();
    if (v.length < 2) { res.innerHTML = ''; return; }
    timer = setTimeout(() => fetch('/api/songs/search?q=' + encodeURIComponent(v)).then(r => r.json()).then(list => {
      res.innerHTML = list.length ? list.map(s => `<li role="option" tabindex="0" data-id="${s.id}"><b>${esc(s.title)}</b>
        <span class="muted">${esc(s.artist)} · ${esc(s.film)} ${s.year || ''}</span></li>`).join('')
        : '<li class="muted">No songs found</li>';
      res.querySelectorAll('li[data-id]').forEach(li => {
        li.onclick = () => pick(li);
        li.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); pick(li); } };
      });
    }), 250);
  });
  q.addEventListener('keydown', e => { if (e.key === 'Enter') e.preventDefault(); });
})();

// Live "N songs to choose from" as filters change
(function () {
  const form = document.getElementById('plform'), out = document.getElementById('pool');
  if (!form || !out) return;
  const upd = () => {
    const p = new URLSearchParams(new FormData(form));
    ['prompt', 'title', 'size', 'mode', 'seed'].forEach(k => p.delete(k));
    fetch('/api/playlists/count?' + p).then(r => r.json()).then(d => {
      out.textContent = Number(d.count).toLocaleString('en-US');
      const c = document.getElementById('estcost');
      if (c && !/free/.test(c.textContent)) c.textContent = '≈ $' + d.cost.toFixed(3);
    }).catch(() => {});
  };
  form.addEventListener('change', e => { if (e.target.closest('#filters')) upd(); });
})();

// Wait for a playlist that's being made, then show it
(function () {
  const box = document.getElementById('plworking');
  if (!box) return;
  const log = document.getElementById('plsteps');
  const tick = () => fetch('/api/playlists/' + box.dataset.pid, {cache: 'no-store'}).then(r => r.json()).then(d => {
    if (log && d.progress && d.progress.length) {
      log.innerHTML = d.progress.map(([t, x]) => `<div class="ln wrap"><span class="lv lv-${esc(t)}">${esc(t)}</span> ${esc(x)}</div>`).join('');
    }
    if (d.status !== 'working') setTimeout(() => location.reload(), 400); else setTimeout(tick, 1500);
  }).catch(() => setTimeout(tick, 4000));
  setTimeout(tick, 1500);
})();

// Playlist preview: reorder, untick, save target
(function () {
  const list = document.getElementById('plist');
  if (!list) return;
  const count = () => {
    const n = list.querySelectorAll('input[name=keep]:checked').length;
    const el = document.getElementById('n-kept'); if (el) el.textContent = n;
  };
  // "tags look wrong": send the song's album to review without leaving (keeps unsaved order and ticks)
  list.addEventListener('click', e => {
    const b = e.target.closest('button[data-sendback]');
    if (!b) return;
    e.preventDefault();
    const fd = new FormData(); fd.append('from', 'playlists');
    b.disabled = true;
    fetch(b.getAttribute('formaction'), {method: 'POST', body: fd, headers: {'X-Requested-With': 'fetch'}})
      .then(r => r.json()).then(d => {
        if (!d.ok) throw new Error(d.error || 'failed');
        b.classList.add('sent'); b.title = `Sent “${d.album}” to the review queue`;
        b.setAttribute('aria-label', b.title);
        const t = document.createElement('div'); t.className = 'flash toast'; t.setAttribute('role', 'status');
        t.innerHTML = `Sent <b></b> back to the review queue. <a class="btn xs" href="${d.url}">Open</a>`;
        t.querySelector('b').textContent = `“${d.album}”`;
        document.querySelector('main').prepend(t); setTimeout(() => t.remove(), 8000);
      }).catch(err => { b.disabled = false; alert("Couldn't send it to review: " + err.message); });
  });
  list.addEventListener('click', e => {
    const b = e.target.closest('button[data-move]');
    if (!b) return;
    const li = b.closest('li');
    if (b.dataset.move === '-1' && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
    else if (b.dataset.move === '1' && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
    b.focus();
  });
  list.addEventListener('change', e => {
    const cb = e.target.closest('input[name=keep]');
    if (cb) { cb.closest('li').classList.toggle('off', !cb.checked); count(); }
  });
  // drag to reorder (desktop); the ↑ ↓ buttons do the same on touch screens
  let dragged = null;
  list.addEventListener('dragstart', e => {
    const li = e.target.closest('li'); if (!li) return;
    dragged = li; li.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', ''); } catch (_) { /* old browsers */ }
  });
  list.addEventListener('dragover', e => {
    const li = e.target.closest('li');
    if (!dragged || !li || li === dragged) return;
    e.preventDefault();
    list.querySelectorAll('.over').forEach(x => x.classList.remove('over'));
    li.classList.add('over');
  });
  list.addEventListener('drop', e => {
    const li = e.target.closest('li');
    if (!dragged || !li || li === dragged) return;
    e.preventDefault();
    const after = [...list.children].indexOf(dragged) < [...list.children].indexOf(li);
    list.insertBefore(dragged, after ? li.nextSibling : li);
  });
  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('dragging');
    list.querySelectorAll('.over').forEach(x => x.classList.remove('over'));
    dragged = null;
  });
  const mode = document.getElementById('save_mode'), target = document.getElementById('target');
  if (mode && target) {
    const sync = () => { target.hidden = mode.value === 'new'; target.required = mode.value !== 'new'; };
    mode.addEventListener('change', sync); sync();
  }
})();

// Settings: only show the key field for the chosen AI service
(function () {
  const sel = document.getElementById('in-AI_PROVIDER');
  if (!sel) return;
  // the second service's key shows too; its model row only when there is one
  const fb = document.getElementById('in-AI_FALLBACK');
  const sync = () => {
    const used = [sel.value, fb ? fb.value : 'off'];
    document.querySelectorAll('[data-provider]').forEach(el => { el.hidden = !used.includes(el.dataset.provider); });
    document.querySelectorAll('[data-fallback]').forEach(el => { el.hidden = !fb || fb.value === 'off' || fb.value === sel.value; });
  };
  sel.addEventListener('change', sync);
  if (fb) fb.addEventListener('change', sync);
  sync();
})();

// ---------------------------------------------------------------- 2.4 settings page

// "You have unsaved changes" bar: shown as soon as anything differs from what was loaded.
(function () {
  const form = document.querySelector('form[data-dirty-guard]');
  if (!form) return;
  const bar = document.getElementById('savebar');
  const snap = () => new URLSearchParams(new FormData(form)).toString();
  let base = snap(), saving = false;
  const dirty = () => snap() !== base;
  const update = () => {
    const d = dirty();
    bar.classList.toggle('show', d);
    document.title = (d ? '• ' : '') + document.title.replace(/^• /, '');
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  document.addEventListener('click', e => { if (e.target.closest('button[data-insert]')) setTimeout(update, 0); });
  document.getElementById('discard').addEventListener('click', () => {
    form.reset();
    form.querySelectorAll('input[data-preview]').forEach(i => i.dispatchEvent(new Event('input', {bubbles: true})));
    const sel = document.getElementById('in-AI_PROVIDER'); if (sel) sel.dispatchEvent(new Event('change', {bubbles: true}));
    update();
  });
  form.addEventListener('submit', () => { saving = true; });
  // Ctrl/Cmd + S saves
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      if (dirty()) { saving = true; form.requestSubmit ? form.requestSubmit(document.getElementById('savebtn')) : form.submit(); }
    }
  });
  // leaving with unsaved changes: ask first (in-page dialog for links, browser prompt for closing the tab)
  addEventListener('beforeunload', e => { if (!saving && dirty()) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]');
    if (!a || saving || !dirty() || a.target === '_blank' || a.getAttribute('href').startsWith('#')) return;
    e.preventDefault();
    const modal = document.getElementById('confirm');
    if (!modal) { location.href = a.href; return; }
    document.getElementById('confirm-text').textContent = 'You have unsaved changes on this page. Leave without saving?';
    modal.hidden = false;
    const ok = document.getElementById('confirm-ok');
    const go = () => { cleanup(); saving = true; location.href = a.href; };
    const cancel = ev => { if (ev.target === modal || ev.target.hasAttribute('data-close')) cleanup(); };
    const cleanup = () => { ok.removeEventListener('click', go); modal.removeEventListener('click', cancel); };
    ok.addEventListener('click', go);
    modal.addEventListener('click', cancel);
  }, true);
})();

// Settings search: find any setting by name or description, jump to it on its page
(function () {
  const inp = document.getElementById('setsearch'), out = document.getElementById('ssresults');
  const data = document.getElementById('setindex');
  if (!inp || !out || !data) return;
  let index = [];
  try { index = JSON.parse(data.textContent); } catch (_) { return; }
  const norm = s => (s || '').toLowerCase();
  const render = () => {
    const q = norm(inp.value.trim());
    if (q.length < 2) { out.hidden = true; return; }
    const words = q.split(/\s+/);
    const hits = index.filter(it => words.every(w => norm(it.l + ' ' + it.h + ' ' + it.tl + ' ' + it.k).includes(w))).slice(0, 12);
    out.innerHTML = hits.length
      ? hits.map(it => `<li><a href="/settings?tab=${encodeURIComponent(it.t)}#row-${encodeURIComponent(it.k)}">${esc(it.l)}<span class="tl">${esc(it.tl)}</span></a></li>`).join('')
      : '<li class="none">No setting matches</li>';
    out.hidden = false;
  };
  inp.addEventListener('input', render);
  inp.addEventListener('focus', render);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { const a = out.querySelector('a'); if (a) { e.preventDefault(); a.click(); } }
    if (e.key === 'Escape') { out.hidden = true; inp.blur(); }
    if (e.key === 'ArrowDown') { const a = out.querySelector('a'); if (a) { e.preventDefault(); a.focus(); } }
  });
  document.addEventListener('click', e => { if (!e.target.closest('.ssearch')) out.hidden = true; });
  // "/" focuses the search box, like many apps
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); inp.focus(); }
  });
})();

// Duplicates: follow a running check and reload when it's done
(function () {
  const box = document.getElementById('dupescan');
  if (!box) return;
  const tick = () => fetch('/api/duplicates/status', {cache: 'no-store'}).then(r => r.json()).then(d => {
    if (!d.running) { location.reload(); return; }
    const c = document.getElementById('ds-count');
    if (c && d.total) c.textContent = `[${Number(d.done).toLocaleString('en-US')} / ${Number(d.total).toLocaleString('en-US')} songs]`;
    const bar = document.getElementById('ds-bar');
    if (bar && d.total) { const p = Math.round(d.done / d.total * 100); bar.setAttribute('aria-valuenow', p); bar.firstElementChild.style.width = p + '%'; }
    const h = box.querySelector('h2'); if (h && d.phase) h.textContent = d.phase + '…';
    setTimeout(tick, 2000);
  }).catch(() => setTimeout(tick, 5000));
  setTimeout(tick, 1500);
})();

// Full text on hover for anything cut short with "…" (album, track, folder and file names, everywhere).
// Only shows when the text really is truncated; reuses an element's own title (e.g. a full path) if it has one.
(function () {
  if (!matchMedia('(hover: hover)').matches) return;          // phones: no hover
  const tip = document.createElement('div');
  tip.className = 'fulltip'; tip.setAttribute('role', 'tooltip'); tip.hidden = true;
  document.body.appendChild(tip);
  let cur = null, timer = null;
  const cut = el => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 2;
  const clipped = el => {
    for (let i = 0; el && i < 4 && el !== document.body; i++, el = el.parentElement) {
      const cs = getComputedStyle(el);
      if ((cs.textOverflow === 'ellipsis' || cs.webkitLineClamp !== 'none' && cs.webkitLineClamp) && cut(el)) return el;
    }
    return null;
  };
  const hide = () => {
    clearTimeout(timer); tip.hidden = true;
    if (cur && cur.dataset.tipTitle !== undefined) { cur.title = cur.dataset.tipTitle; delete cur.dataset.tipTitle; }
    cur = null;
  };
  const show = el => {
    const text = (el.title || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) return;
    if (el.title) { el.dataset.tipTitle = el.title; el.title = ''; }   // no double tooltip
    cur = el; tip.textContent = text; tip.hidden = false;
    const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
    let x = Math.min(Math.max(8, r.left), innerWidth - t.width - 8);
    let y = r.bottom + 6;
    if (y + t.height > innerHeight - 8) y = r.top - t.height - 6;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  };
  const enter = e => {
    const el = clipped(e.target);
    if (el === cur) return;
    hide();
    if (el) timer = setTimeout(() => show(el), 250);
  };
  document.addEventListener('mouseover', enter);
  document.addEventListener('focusin', enter);
  document.addEventListener('mouseout', e => { if (cur && !cur.contains(e.relatedTarget)) hide(); else if (!cur) clearTimeout(timer); });
  document.addEventListener('focusout', hide);
  addEventListener('scroll', hide, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
})();

// Shift-click range selection for row checkboxes (Library, album tracks, playlist songs…):
// tick one row, hold Shift and tick another, and every row in between gets the same state.
(function () {
  const rowBoxes = c => [...c.querySelectorAll('td.ck input[type=checkbox], label.ck input[type=checkbox]')]
    .filter(x => !x.disabled && x.offsetParent !== null);
  let last = null, shift = false;
  addEventListener('keydown', e => { if (e.key === 'Shift') shift = true; });
  addEventListener('keyup', e => { if (e.key === 'Shift') shift = false; });
  addEventListener('blur', () => { shift = false; });
  // a shift-click would otherwise also highlight the text between the two rows
  document.addEventListener('mousedown', e => {
    if (e.shiftKey && e.target.closest('td.ck, label.ck')) e.preventDefault();
  });
  document.addEventListener('click', e => {
    const cb = e.target;
    if (!(cb instanceof HTMLInputElement) || cb.type !== 'checkbox') return;
    if (!cb.closest('td.ck, label.ck')) return;
    const box = cb.closest('tbody, ol, ul');
    if (!box) return;
    if ((e.shiftKey || shift) && last && last !== cb && box.contains(last)) {
      const all = rowBoxes(box), a = all.indexOf(last), b = all.indexOf(cb);
      if (a > -1 && b > -1) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        for (let i = lo; i <= hi; i++) {
          if (all[i] !== cb && all[i].checked !== cb.checked) {
            all[i].checked = cb.checked;
            all[i].dispatchEvent(new Event('change', {bubbles: true}));
          }
        }
        const s = getSelection && getSelection(); if (s) s.removeAllRanges();
      }
    }
    last = cb;
  });
})();

// "All" tickbox for a list: <input type=checkbox data-checkall="#listid">
document.addEventListener('change', e => {
  const sel = e.target.dataset && e.target.dataset.checkall;
  if (!sel) return;
  document.querySelectorAll(sel + ' input[type=checkbox]').forEach(x => { x.checked = e.target.checked; });
});

// Folders view: what the ticked folders add up to
(function () {
  const form = document.getElementById('fform');
  if (!form) return;
  const out = document.getElementById('fsel');
  const update = () => {
    let a = 0, s = 0, n = 0;
    form.querySelectorAll('input[name=folders]:checked').forEach(x => { n++; a += +x.dataset.albums || 0; s += +x.dataset.songs || 0; });
    out.textContent = n ? `${n} ticked · ${a.toLocaleString()} album folder${a === 1 ? '' : 's'} · ${s.toLocaleString()} songs` : 'Nothing ticked';
  };
  form.addEventListener('change', update);
  update();
})();

// Home: follow Plex totals and the to-do list without reloading
(function () {
  if (!document.getElementById('pl-dot')) return;
  const fmt = n => (n === null || n === undefined) ? '…' : Number(n).toLocaleString();
  const tick = () => fetch('/api/home', {headers: {'Accept': 'application/json'}}).then(r => r.json()).then(d => {
    for (const k of ['artists', 'albums', 'tracks']) {
      const l = document.getElementById('l-' + k), p = document.getElementById('p-' + k), df = document.getElementById('d-' + k);
      if (l) l.textContent = fmt(d.local[k]);
      if (p && d.plex) {
        p.textContent = fmt(d.plex[k]);
        p.classList.toggle('ok', d.plex[k] === d.local[k]); p.classList.toggle('warn', d.plex[k] !== d.local[k]);
        if (df) { const x = d.plex[k] - d.local[k]; df.textContent = x ? `${x > 0 ? '+' : ''}${x.toLocaleString()} in Plex` : 'in sync'; }
      }
    }
    const live = d.live || {};
    const dot = document.getElementById('pl-dot');
    dot.className = 'dot ' + (live.connected ? 'live' : 'paused');
    document.getElementById('pl-mode').textContent = live.connected ? 'Live link to Plex' : 'Plex: checking every few minutes';
    const sc = live.scanning;
    document.getElementById('pl-scan').textContent = sc ? `· ${sc.title}${sc.progress != null ? ` (${sc.progress}%)` : ''}` : '';
    if (live.last_text) document.getElementById('pl-last').textContent = 'Last change in Plex: ' + live.last_text;
  }).catch(() => {}).finally(() => setTimeout(tick, 5000));
  setTimeout(tick, 5000);
})();

// Review queue: "Approve all ≥ N%" with your own N, and how many albums that is
(function () {
  const form = document.getElementById('approveall');
  if (!form) return;
  const inp = document.getElementById('aa-pct'), thr = document.getElementById('aa-thr');
  const label = document.getElementById('aa-label'), out = document.getElementById('aa-n'), btn = document.getElementById('aa-btn');
  const confs = (form.dataset.confs || '').split(',').filter(Boolean).map(Number);
  const update = () => {
    let p = Math.round(+inp.value || 95);
    p = Math.min(100, Math.max(50, p));
    const n = confs.filter(c => c >= p).length;
    label.textContent = `Approve all ≥ ${p}%`;
    thr.value = (p / 10).toFixed(1);
    out.textContent = `${n} album${n === 1 ? '' : 's'}`;
    btn.disabled = n === 0;
    form.dataset.confirm = `Write tags for the ${n} album${n === 1 ? '' : 's'} in the queue with a match of ${p} % or more?`;
  };
  inp.addEventListener('input', update);
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); update(); } });
  update();
})();
