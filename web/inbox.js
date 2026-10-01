(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  try { const t = localStorage.getItem('shell.theme'); if (t && t !== 'auto') document.documentElement.dataset.theme = t; } catch { /* private mode */ }

  const params = new URLSearchParams(location.search);
  const sid = params.get('sid');
  const api = (path, opts = {}) => fetch(path, {
    ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  }).then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.json(); });

  const app = $('[data-app]');
  const list = $('[data-list]');
  const pane = $('[data-pane]');
  let inbox = null;
  let selected = null; // email id, 'new', or null
  let query = '';
  let saveTimer = null;

  // ── formatting ──
  const when = (at) => {
    const d = new Date(at * 1000);
    return d.toDateString() === new Date().toDateString()
      ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  };
  const matches = (e) => !query || `${e.from_name} ${e.from_email} ${e.subject} ${e.body}`.toLowerCase().includes(query);

  // ── list ──
  function renderList() {
    const emails = inbox.emails.filter(matches);
    if (!emails.length) {
      list.innerHTML = `<div class="ib-empty">${inbox.emails.length ? 'No emails match.' : 'No emails yet.<br>Write one with <b>New email</b>.'}</div>`;
      return;
    }
    list.innerHTML = emails.map((e) => `
      <button class="ib-item${e.unread ? ' unread' : ''}${e.id === selected ? ' active' : ''}" data-id="${e.id}">
        <span class="ib-dot"></span>
        <span class="ib-from">${esc(e.from_name || e.from_email || '(no sender)')}</span>
        <span class="ib-time">${e.starred ? '<span class="ib-star">★</span>' : ''}${when(e.at)}</span>
        <span class="ib-subj">${esc(e.subject || '(no subject)')}</span>
        <span class="ib-snip">${esc(e.body.replace(/\s+/g, ' ').slice(0, 140))}</span>
      </button>`).join('');
  }

  // ── pane ──
  function field(name, label, value, type = 'input') {
    return `<label class="ib-field"><span>${label}</span>${type === 'textarea'
      ? `<textarea name="${name}">${esc(value)}</textarea>`
      : `<input name="${name}" value="${esc(value)}" autocomplete="off">`}</label>`;
  }

  function renderPane() {
    if (selected === null) {
      pane.innerHTML = `<div class="ib-placeholder"><div><svg><use href="#i-mail"/></svg>
        Pick an email to read or edit it, or write a new one.</div></div>`;
      return;
    }
    const isNew = selected === 'new';
    const e = isNew ? { from_name: '', from_email: '', subject: '', body: '', unread: true, starred: false }
      : inbox.emails.find((x) => x.id === selected);
    if (!e) { selected = null; return renderPane(); }
    pane.innerHTML = `
      <div class="ib-pane-head">
        <button class="btn ghost ib-back" data-back><svg><use href="#i-back"/></svg>Inbox</button>
        <h2>${isNew ? 'New email' : 'Edit email'}</h2>
        <span class="ib-saved" data-saved></span>
      </div>
      <form class="ib-form" data-form>
        <div class="ib-row">${field('from_name', 'From (name)', e.from_name)}${field('from_email', 'From (email)', e.from_email)}</div>
        ${field('subject', 'Subject', e.subject)}
        ${field('body', 'Message', e.body, 'textarea')}
        <div class="ib-toggles">
          ${isNew ? '' : `<label><input type="checkbox" name="unread" ${e.unread ? 'checked' : ''}> Unread</label>`}
          <label><input type="checkbox" name="starred" ${e.starred ? 'checked' : ''}> Starred</label>
          ${isNew ? '' : `<span class="ib-hint">Received ${esc(new Date(e.at * 1000).toLocaleString())}</span>`}
        </div>
        <div class="ib-form-actions">${isNew
          ? '<button class="btn primary" type="button" data-add><svg><use href="#i-plus"/></svg>Add</button>'
          : '<button class="btn danger" type="button" data-delete><svg><use href="#i-trash"/></svg>Delete</button>'}
        </div>
      </form>`;
    if (isNew) $('[name="from_name"]', pane).focus();
  }

  function render() {
    $('[data-account]').innerHTML = `<b>${esc(inbox.account)}</b>`;
    renderList();
    renderPane();
    app.classList.toggle('reading', selected !== null);
  }

  const formData = () => {
    const f = $('[data-form]', pane);
    return {
      from_name: f.from_name.value.trim(), from_email: f.from_email.value.trim(),
      subject: f.subject.value.trim(), body: f.body.value,
      unread: f.unread ? f.unread.checked : true, starred: f.starred.checked,
    };
  };

  function flashSaved(text) {
    const el = $('[data-saved]', pane);
    if (el) el.textContent = text;
  }

  async function save() {
    if (typeof selected !== 'number') return;
    const fields = formData();
    flashSaved('Saving…');
    const updated = await api(`/api/mail/emails/${selected}`, { method: 'PATCH', body: JSON.stringify({ sid, email: fields }) });
    Object.assign(inbox.emails.find((x) => x.id === selected), updated);
    renderList();
    flashSaved('Saved');
  }

  async function add() {
    const fields = formData();
    if (!fields.subject && !fields.body) return flashSaved('Add a subject or message first');
    const email = await api('/api/mail/emails', { method: 'POST', body: JSON.stringify({ sid, email: fields }) });
    inbox.emails.unshift(email);
    selected = email.id;
    render();
    flashSaved('Added');
  }

  // ── events ──
  list.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-id]');
    if (!b) return;
    selected = Number(b.dataset.id);
    const email = inbox.emails.find((x) => x.id === selected);
    if (email && email.unread) { // opening it reads it, like a mail app
      email.unread = false;
      api(`/api/mail/emails/${email.id}`, { method: 'PATCH', body: JSON.stringify({ sid, email: { unread: false } }) });
    }
    render();
  });

  pane.addEventListener('input', (e) => {
    if (typeof selected !== 'number') return;
    clearTimeout(saveTimer);
    flashSaved('Editing…');
    saveTimer = setTimeout(save, e.target.type === 'checkbox' ? 0 : 500);
  });

  pane.addEventListener('click', async (e) => {
    if (e.target.closest('[data-back]')) { selected = null; return render(); }
    if (e.target.closest('[data-add]')) return add();
    if (e.target.closest('[data-delete]')) {
      await api(`/api/mail/emails/${selected}?sid=${encodeURIComponent(sid)}`, { method: 'DELETE' });
      inbox.emails = inbox.emails.filter((x) => x.id !== selected);
      selected = null;
      render();
    }
  });

  $('[data-compose]').addEventListener('click', () => { selected = 'new'; render(); });
  $('[data-search]').addEventListener('input', (e) => { query = e.target.value.trim().toLowerCase(); renderList(); });

  $('[data-reset]').addEventListener('click', async () => {
    if (!confirm('Put the inbox back to how it started? Your edits will be lost.')) return;
    inbox = await api('/api/mail/reset', { method: 'POST', body: JSON.stringify({ sid }) });
    selected = null;
    render();
  });

  // ── boot ──
  async function boot() {
    try {
      inbox = await api(`/api/mail/inbox?sid=${encodeURIComponent(sid || '')}`);
    } catch {
      document.body.innerHTML = '<main class="consent"><div class="c-card c-done"><h1>Link expired</h1><p>Open the Connect Gmail card from your chat again.</p></div></main>';
      return;
    }
    if (inbox.status !== 'connected') return location.replace(`/connect.html?sid=${encodeURIComponent(sid)}`);
    if (params.get('welcome')) {
      const banner = $('[data-banner]');
      banner.hidden = false;
      banner.innerHTML = `✓ Connected. ${esc(inbox.agent_name)} can see this inbox now, and any change you make here. <button aria-label="Dismiss">✕</button>`;
      banner.querySelector('button').onclick = () => { banner.hidden = true; };
      history.replaceState(null, '', `/inbox.html?sid=${encodeURIComponent(sid)}`);
    }
    render();
  }
  boot();
})();
