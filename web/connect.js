(() => {
  'use strict';
  // Tapping "Connect Gmail" just connects: a brief beat, then the inbox.
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const sid = new URLSearchParams(location.search).get('sid');
  const inboxUrl = (welcome) => `/inbox.html?sid=${encodeURIComponent(sid)}${welcome ? '&welcome=1' : ''}`;

  function fail() {
    $('[data-card]').innerHTML = '<h1>Link expired</h1><p class="c-account">Open the Connect Gmail card from your chat again.</p>';
  }

  async function run() {
    if (!sid) return fail();
    const res = await fetch(`/api/mail/inbox?sid=${encodeURIComponent(sid)}`);
    if (!res.ok) return fail();
    const inbox = await res.json();
    if (inbox.status === 'connected') return location.replace(inboxUrl(false));
    $('[data-account]').innerHTML = `as <b>${esc(inbox.account)}</b>`;
    const [connected] = await Promise.all([
      fetch('/api/mail/connect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sid }),
      }),
      new Promise((r) => setTimeout(r, 900)), // long enough to read, short enough not to wait on
    ]);
    if (!connected.ok) return fail();
    location.replace(inboxUrl(true));
  }

  run();
})();
