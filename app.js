(() => {
  'use strict';

  // ── helpers ──────────────────────────────────────────────────
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pick = (a) => a[Math.floor(Math.random() * a.length)];
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const fmtDur = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const icon = (id, cls = '') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const UNKNOWN = '+1 (415) 555-0132';
  const PERSON = icon('i-person');
  const JUMBO = /^(?:\p{Extended_Pictographic}|\p{Emoji_Modifier}|‍|️|\s){1,12}$/u;

  const OTHERS = [
    { name: 'Maya Patel', prev: 'lol ok see you at 7 then', time: 'Yesterday', unread: true },
    { name: 'Dad', prev: 'Call me when you land', time: 'Yesterday' },
    { name: 'Climbing Crew', prev: 'Jordan: who has the extra harness?', time: 'Tuesday' },
    { name: 'Sam Rivera', prev: 'Sent the deck — lmk what you think', time: 'Monday' },
    { name: 'Alex Kim', prev: '🙌', time: '9/24/26' },
  ];

  // ── state ────────────────────────────────────────────────────
  const state = {
    agentName: null,
    messages: [],
    step: 'intro',
    call: 'idle', // idle | ringing | active | minimized
    callStart: 0,
    muted: false,
    sessionStart: new Date(),
  };
  let seq = 0;
  const fresh = new Set();
  let popAvatar = false;

  const mk = (m) => ({ id: ++seq, at: new Date(), from: 'agent', kind: 'text', ...m });
  function push(m) {
    const msg = mk(m);
    state.messages.push(msg);
    fresh.add(msg.id);
    render();
    return msg;
  }

  // Serialise everything the agent does so replies never interleave.
  let chain = Promise.resolve();
  const queue = (fn) => (chain = chain.then(fn).catch((e) => console.error(e)));

  async function agentSay(text, delay) {
    // Agent "reads" everything the user sent before it starts typing.
    const now = new Date();
    state.messages.forEach((m) => { if (m.from === 'user' && !m.readAt) m.readAt = now; });
    const typing = push({ kind: 'typing' });
    await sleep(delay ?? Math.min(2100, 520 + text.length * 19));
    state.messages = state.messages.filter((m) => m.id !== typing.id);
    push({ text });
  }

  // ── rendering ────────────────────────────────────────────────
  const isBubble = (m) => m && m.kind !== 'event';

  function bubbleHTML(m) {
    switch (m.kind) {
      case 'typing':
        return '<div class="bubble typing"><i></i><i></i><i></i></div>';
      case 'card':
        return `<a class="bubble card" href="#" data-action="card">
          <div class="card-art">${icon('i-mail')}</div>
          <div class="card-meta"><div class="card-title">${esc(m.title)}</div><div class="card-sub">${esc(m.sub)}</div></div>
        </a>`;
      case 'call': {
        const label = { ended: 'Audio Call', declined: 'Declined', missed: 'Missed Call' }[m.status];
        const sub = m.status === 'ended' ? fmtDur(m.dur) : m.status === 'missed' ? 'Tap to call back' : 'Audio Call';
        return `<div class="bubble callrec ${m.status}" data-action="call-out">
          <span class="cr-icon">${icon('i-phone', m.status === 'ended' ? '' : 'rot')}</span>
          <span class="cr-text"><b>${label}</b><small>${sub}</small></span>
        </div>`;
      }
      default: {
        const jumbo = JUMBO.test(m.text) && /\p{Extended_Pictographic}/u.test(m.text);
        return `<div class="bubble${jumbo ? ' jumbo' : ''}">${esc(m.text)}</div>`;
      }
    }
  }

  function threadHTML() {
    const all = state.messages;
    const msgs = [...all.filter((m) => m.kind !== 'typing'), ...all.filter((m) => m.kind === 'typing')];
    const lastReal = [...msgs].reverse().find((m) => m.kind !== 'typing' && m.kind !== 'event');
    let html = `<div class="stamp">iMessage<br><b>Today</b> ${fmtTime(state.sessionStart)}</div>`;

    msgs.forEach((m, i) => {
      if (m.kind === 'event') {
        html += `<div class="event${fresh.has(m.id) ? ' enter' : ''}">${m.html}</div>`;
        return;
      }
      const prev = msgs[i - 1];
      const next = msgs[i + 1];
      const first = !(isBubble(prev) && prev.from === m.from);
      const last = !(isBubble(next) && next.from === m.from);
      const cls = ['msg', m.from === 'user' ? 'out' : 'in', first && 'first', last && 'last',
        m.kind === 'typing' && 'typing', fresh.has(m.id) && 'enter'].filter(Boolean).join(' ');
      html += `<div class="${cls}">${bubbleHTML(m)}</div>`;
      if (m === lastReal && m.from === 'user') {
        html += `<div class="receipt">${m.readAt ? `<b>Read</b> ${fmtTime(m.readAt)}` : 'Delivered'}</div>`;
      }
    });
    return html;
  }

  function avatarInner(name) {
    return name ? esc([...name][0].toUpperCase()) : PERSON;
  }

  function renderContact() {
    const name = state.agentName;
    $$('[data-contact-name]').forEach((el) => { el.textContent = name || UNKNOWN; });
    $$('[data-contact-avatar]').forEach((el) => {
      el.classList.toggle('agent', !!name);
      el.innerHTML = avatarInner(name);
      if (popAvatar) { el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop'); }
    });
    popAvatar = false;
  }

  function previewOf(m) {
    if (!m) return '';
    if (m.kind === 'card') return `🔗 ${m.title}`;
    if (m.kind === 'call') return { ended: 'Audio Call', declined: 'Declined call', missed: 'Missed Call' }[m.status];
    return m.text;
  }

  function convoHTML({ name, prev, time, unread, agent, active }) {
    return `<button class="convo${active ? ' active' : ''}">
      ${unread ? '<span class="unread"></span>' : ''}
      <span class="avatar md${agent && state.agentName ? ' agent' : ''}">${agent ? avatarInner(state.agentName) : esc(initials(name))}</span>
      <span class="convo-body">
        <span class="convo-top"><span class="convo-name">${esc(name)}</span><span class="convo-time">${esc(time)}</span></span>
        <span class="convo-prev">${esc(prev)}</span>
      </span>
    </button>`;
  }
  const initials = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();

  function renderSidebar() {
    const last = [...state.messages].reverse().find((m) => m.kind !== 'typing' && m.kind !== 'event');
    const agentRow = {
      name: state.agentName || UNKNOWN,
      prev: previewOf(last),
      time: last ? fmtTime(last.at) : '',
      agent: true,
      active: true,
    };
    $$('[data-convos]').forEach((el) => { el.innerHTML = [agentRow, ...OTHERS].map(convoHTML).join(''); });
  }

  function render() {
    const html = threadHTML();
    const smooth = fresh.size > 0;
    for (const t of $$('[data-thread]')) {
      t.innerHTML = html;
      requestAnimationFrame(() => t.scrollTo({ top: t.scrollHeight, behavior: smooth ? 'smooth' : 'auto' }));
    }
    fresh.clear();
    renderContact();
    renderSidebar();
  }

  // ── scripted conversation (placeholder brain) ────────────────
  const GREETING = /^(hi+|hey+|hello+|yo+|sup|hiya|howdy|heya|hey there)[\s!.]*$/i;
  const NO = /\b(no+|nah|nope|not now|later|don'?t|rather not|text|busy|can'?t)\b/i;

  function extractName(raw) {
    let s = raw.trim()
      .replace(/^(?:hmm+|um+|uh+|ok(?:ay)?|well|so|oh)[,.!\s]+/i, '')
      .replace(/^(?:i(?:'ll| will)?\s+call\s+you|call\s+you|your\s+name(?:\s+is|'s)?|you(?:'re|\s+are)|let'?s\s+(?:go\s+with|do)|how\s+about|maybe|go\s+with|i\s+(?:like|choose|pick))\s+/i, '')
      .replace(/["“”]+/g, '')
      .replace(/[.!?,]+$/g, '')
      .trim();
    if (!s) return null;
    let name = s.split(/\s+/).slice(0, 3).join(' ');
    name = name[0].toUpperCase() + name.slice(1);
    return name.slice(0, 24).trim() || null;
  }

  function setAgentName(name) {
    state.agentName = name;
    popAvatar = true;
    push({ kind: 'event', html: `Contact saved as <b>${esc(name)}</b>` });
  }

  async function intro() {
    await sleep(800);
    await agentSay("hey! 👋 you just set me up — I'm your new assistant.");
    await agentSay("first things first: I don't have a name yet. what do you want to call me?");
    state.step = 'name';
  }

  async function respond(text) {
    await sleep(380);
    if (state.step === 'name') {
      if (GREETING.test(text)) return agentSay('hi hi 😄 so — what do you want to call me?');
      if (/\?\s*$/.test(text)) return agentSay("just so you've got something to call me that isn't a phone number. anything works — even something silly.");
      const name = extractName(text);
      if (!name) return agentSay('give me something to go by — anything works.');
      setAgentName(name);
      await agentSay(`${name}. I like it.`);
      await agentSay("typing's a slow way to get to know each other — mind if I give you a quick call?");
      state.step = 'call-offer';
      return;
    }
    if (state.step === 'call-offer') {
      state.step = 'free';
      if (NO.test(text)) {
        await agentSay('totally fine, we can do it right here.');
        return agentSay('so what should I call you?');
      }
      await agentSay('calling you now 📞', 700);
      await sleep(1100);
      return startRinging();
    }
    return agentSay(pick([
      'got it — noting that down ✍️',
      'mm, tell me more?',
      'love that. what else?',
      "makes sense. (the real brain plugs in here later 🧠)",
    ]));
  }

  function onUserSend(text) {
    push({ from: 'user', text });
    queue(() => respond(text));
  }

  // ── call ─────────────────────────────────────────────────────
  let ringTimer = null;
  let tickTimer = null;
  let captionTimers = [];

  function setCall(s) {
    state.call = s;
    $$('[data-ios-call]').forEach((el) => {
      el.dataset.state = s;
      if (s === 'ringing' || s === 'active') el.dataset.view = s;
    });
    $$('[data-mac-call]').forEach((el) => {
      const m = s === 'minimized' ? 'active' : s;
      el.dataset.state = m;
      if (m === 'ringing' || m === 'active') el.dataset.view = m;
    });
    const live = s === 'active' || s === 'minimized';
    $$('[data-call-chip]').forEach((el) => { el.hidden = !live; });
    $$('[data-call-banner]').forEach((el) => { el.hidden = s !== 'minimized'; });
    const phone = $('.phone');
    phone.classList.toggle('call-open', s === 'ringing' || s === 'active');
    phone.classList.toggle('call-live', s === 'minimized');

    clearInterval(tickTimer);
    if (live) tickTimer = setInterval(updateTimer, 500);
    updateTimer();
  }

  function updateTimer() {
    const live = state.call === 'active' || state.call === 'minimized';
    const t = live ? fmtDur(Date.now() - state.callStart) : '';
    $$('[data-call-timer]').forEach((el) => { el.textContent = t; });
    $$('[data-call-sub]').forEach((el) => { el.textContent = state.call === 'ringing' ? el.dataset.ringing : t; });
  }

  function setCaption(text) {
    $$('[data-caption]').forEach((el) => {
      el.textContent = text;
      el.classList.remove('in'); void el.offsetWidth; el.classList.add('in');
    });
  }

  function startRinging() {
    if (state.call !== 'idle') return;
    setCaption('');
    setCall('ringing');
    clearTimeout(ringTimer);
    ringTimer = setTimeout(missed, 22000);
  }

  function connect() {
    clearTimeout(ringTimer);
    state.callStart = Date.now();
    state.muted = false;
    $$('[data-action="mute"]').forEach((b) => b.classList.remove('on'));
    setCall('active');
    const n = state.agentName || 'your assistant';
    const lines = [`hey — it's ${n}! 👋`, 'so, what should I call you?', "…and what's the one thing you'd love off your plate this week?"];
    captionTimers = lines.map((l, i) => setTimeout(() => setCaption(l), 900 + i * 3200));
  }

  function hangup(status) {
    clearTimeout(ringTimer);
    captionTimers.forEach(clearTimeout);
    const dur = Date.now() - state.callStart;
    const wasLive = state.call === 'active' || state.call === 'minimized';
    setCall('idle');
    if (status === 'ended' && wasLive) {
      push({ kind: 'call', status: 'ended', dur });
      queue(async () => { await sleep(900); await agentSay('good chatting! picking things back up here 👇'); });
    }
  }

  function decline(withText) {
    if (state.call !== 'ringing') return;
    hangup('declined');
    push({ kind: 'call', status: 'declined' });
    queue(async () => {
      await sleep(withText ? 400 : 1100);
      await agentSay('no worries — we can keep going here.');
      await agentSay('what should I call you?');
    });
  }

  function missed() {
    if (state.call !== 'ringing') return;
    hangup('missed');
    push({ kind: 'call', status: 'missed' });
    queue(async () => { await sleep(700); await agentSay('tried you — no stress, texting works too.'); });
  }

  // ── dev controls ─────────────────────────────────────────────
  function setLayout(v) {
    document.body.dataset.layout = v;
    store.set('shell.layout', v);
    markSeg('layout', v);
  }
  function setTheme(v) {
    if (v === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = v;
    store.set('shell.theme', v);
    markSeg('theme', v);
  }
  function markSeg(name, v) {
    $$(`[data-seg="${name}"] button`).forEach((b) => b.classList.toggle('on', b.dataset.value === v));
  }
  let devTyping = null;

  // ── events ───────────────────────────────────────────────────
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const a = el.dataset.action;
    if (a === 'card') e.preventDefault();

    switch (a) {
      case 'accept': return connect();
      case 'decline': return decline(false);
      case 'decline-text': return decline(true);
      case 'end': return hangup('ended');
      case 'minimize': return setCall('minimized');
      case 'restore': return state.call === 'minimized' && setCall('active');
      case 'call-out': if (state.call === 'idle') connect(); return;
      case 'mute':
        state.muted = !state.muted;
        $$('[data-action="mute"]').forEach((b) => b.classList.toggle('on', state.muted));
        return;
      case 'card':
        el.animate([{ transform: 'scale(.97)' }, { transform: 'none' }], { duration: 220, easing: 'ease-out' });
        return;

      case 'dev-toggle': {
        const dev = $('[data-dev]');
        dev.classList.toggle('open');
        store.set('shell.dev', dev.classList.contains('open') ? '1' : '0');
        return;
      }
      case 'dev-msg':
        return queue(() => agentSay(pick([
          'quick q — what does a normal tuesday look like for you?',
          "I'm pretty good at inbox triage, scheduling, and chasing people down. what sounds useful?",
          'no rush, I\'m here whenever 🙂',
        ])));
      case 'dev-typing':
        if (devTyping) { state.messages = state.messages.filter((m) => m.id !== devTyping); devTyping = null; render(); }
        else devTyping = push({ kind: 'typing' }).id;
        return;
      case 'dev-card':
        return queue(async () => {
          await agentSay('tap this and I can actually show you instead of describing it');
          push({ kind: 'card', title: 'Connect Gmail', sub: 'Sign in with Google · read-only' });
        });
      case 'dev-ring': return startRinging();
      case 'dev-reset': return location.reload();
      case 'layout': return setLayout(el.dataset.value);
      case 'theme': return setTheme(el.dataset.value);
    }
  });

  $$('[data-composer]').forEach((form) => {
    const input = $('[data-input]', form);
    const field = $('.field', form);
    input.addEventListener('input', () => field.classList.toggle('has-text', input.value.trim().length > 0));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      field.classList.remove('has-text');
      onUserSend(text);
    });
  });

  // Keep the iOS thread padded exactly under its translucent bars.
  const phone = $('.phone');
  const top = $('.ios-top');
  const bottom = $('.ios-bottom');
  const ro = new ResizeObserver(() => {
    phone.style.setProperty('--top-h', `${top.offsetHeight}px`);
    phone.style.setProperty('--bot-h', `${bottom.offsetHeight}px`);
  });
  ro.observe(top);
  ro.observe(bottom);

  // Status bar clock.
  const clock = () => {
    const d = new Date();
    $$('[data-clock]').forEach((el) => { el.textContent = `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, '0')}`; });
  };
  clock();
  setInterval(clock, 15000);

  // ── boot ─────────────────────────────────────────────────────
  setLayout(store.get('shell.layout') || 'auto');
  setTheme(store.get('shell.theme') || 'auto');
  const devPref = store.get('shell.dev');
  if (devPref === '1') $('[data-dev]').classList.add('open');

  render();
  queue(intro);
})();
