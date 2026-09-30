(() => {
  'use strict';

  // ── helpers ──────────────────────────────────────────────────
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const fmtDur = (secs) => { const s = Math.max(0, Math.floor(secs)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const icon = (id, cls = '') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);

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

  // ── state (the server is the source of truth) ────────────────
  const state = {
    sid: store.get('onboarding.sid') || newId(),
    createdAt: Date.now() / 1000,
    agentName: null,
    messages: [],
    typing: false,
    call: { status: 'idle', direction: 'incoming', started_at: null },
    minimized: false,
    muted: false,
    online: false,
  };
  store.set('onboarding.sid', state.sid);

  const fresh = new Set();
  let popAvatar = false;

  // ── rendering ────────────────────────────────────────────────
  const isEvent = (m) => m && m.kind === 'event';
  const isBubble = (m) => m && !isEvent(m);

  function bubbleHTML(m) {
    switch (m.kind) {
      case 'typing':
        return '<div class="bubble typing"><i></i><i></i><i></i></div>';
      case 'card':
        return `<a class="bubble card" href="#" data-action="card">
          <div class="card-art">${icon('i-mail')}</div>
          <div class="card-meta"><div class="card-title">${esc(m.meta.title)}</div><div class="card-sub">${esc(m.meta.sub)}</div></div>
        </a>`;
      case 'call': {
        const st = m.meta.status;
        const label = { ended: 'Audio Call', declined: 'Declined', missed: 'Missed Call' }[st] || 'Call';
        const sub = st === 'ended' ? fmtDur(m.meta.duration || 0) : st === 'missed' ? 'Tap to call back' : 'Audio Call';
        return `<div class="bubble callrec ${esc(st)}" data-action="call-out">
          <span class="cr-icon">${icon('i-phone', st === 'ended' ? '' : 'rot')}</span>
          <span class="cr-text"><b>${label}</b><small>${sub}</small></span>
        </div>`;
      }
      default: {
        const jumbo = JUMBO.test(m.text) && /\p{Extended_Pictographic}/u.test(m.text);
        return `<div class="bubble${jumbo ? ' jumbo' : ''}">${esc(m.text)}</div>`;
      }
    }
  }

  function eventHTML(m) {
    let html = esc(m.text);
    if (m.meta && m.meta.emphasis) html = html.replace(esc(m.meta.emphasis), `<b>${esc(m.meta.emphasis)}</b>`);
    return html;
  }

  function threadHTML() {
    const msgs = [...state.messages];
    if (state.typing) msgs.push({ id: 'typing', sender: 'agent', kind: 'typing' });
    const lastReal = [...state.messages].reverse().find(isBubble);
    let html = `<div class="stamp">iMessage<br><b>Today</b> ${fmtTime(new Date(state.createdAt * 1000))}</div>`;

    msgs.forEach((m, i) => {
      if (isEvent(m)) {
        html += `<div class="event${fresh.has(m.id) ? ' enter' : ''}">${eventHTML(m)}</div>`;
        return;
      }
      const prev = msgs[i - 1];
      const next = msgs[i + 1];
      const first = !(isBubble(prev) && prev.sender === m.sender);
      const last = !(isBubble(next) && next.sender === m.sender);
      const cls = ['msg', m.sender === 'user' ? 'out' : 'in', first && 'first', last && 'last',
        m.kind === 'typing' && 'typing', fresh.has(m.id) && 'enter'].filter(Boolean).join(' ');
      html += `<div class="${cls}">${bubbleHTML(m)}</div>`;
      if (m === lastReal && m.sender === 'user') {
        html += `<div class="receipt">${m.read_at ? `<b>Read</b> ${fmtTime(new Date(m.read_at * 1000))}` : 'Delivered'}</div>`;
      }
    });
    return html;
  }

  const avatarInner = (name) => (name ? esc([...name][0].toUpperCase()) : PERSON);
  const initials = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();

  function renderContact() {
    const name = state.agentName;
    $$('[data-contact-name]').forEach((el) => { el.textContent = name || UNKNOWN; });
    $$('[data-contact-avatar]').forEach((el) => {
      el.classList.toggle('agent', !!name);
      el.innerHTML = avatarInner(name);
      if (popAvatar) { el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop'); }
    });
    popAvatar = false;
    $$('.who-sub').forEach((el) => { el.textContent = state.online ? 'iMessage' : 'Connecting…'; });
  }

  function previewOf(m) {
    if (!m) return '';
    if (m.kind === 'card') return `🔗 ${m.meta.title}`;
    if (m.kind === 'call') return { ended: 'Audio Call', declined: 'Declined call', missed: 'Missed Call' }[m.meta.status] || 'Call';
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

  function renderSidebar() {
    const last = [...state.messages].reverse().find(isBubble);
    const agentRow = {
      name: state.agentName || UNKNOWN,
      prev: previewOf(last),
      time: last ? fmtTime(new Date(last.at * 1000)) : '',
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

  // ── server connection ────────────────────────────────────────
  let ws = null;
  let retry = 0;
  const outbox = [];

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    else outbox.push(obj);
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/ws?sid=${encodeURIComponent(state.sid)}`);
    ws = sock;
    sock.onopen = () => {
      retry = 0;
      state.online = true;
      renderContact();
      while (outbox.length) sock.send(JSON.stringify(outbox.shift()));
    };
    sock.onmessage = (e) => onEvent(JSON.parse(e.data));
    sock.onclose = () => {
      if (ws !== sock) return; // replaced on purpose (restart)
      state.online = false;
      renderContact();
      setTimeout(connect, Math.min(5000, 400 * 2 ** retry++));
    };
  }

  function onEvent(ev) {
    switch (ev.type) {
      case 'snapshot': {
        const s = ev.session;
        state.createdAt = s.created_at;
        state.agentName = s.agent_name;
        state.messages = s.messages;
        state.typing = s.typing;
        applyCall(s.call);
        render();
        return;
      }
      case 'message':
        state.messages.push(ev.message);
        fresh.add(ev.message.id);
        return render();
      case 'typing':
        state.typing = ev.on;
        if (ev.on) fresh.add('typing');
        return render();
      case 'read':
        state.messages.forEach((m) => { if (ev.ids.includes(m.id)) m.read_at = ev.at; });
        return render();
      case 'contact':
        state.agentName = ev.agent_name;
        popAvatar = true;
        return render();
      case 'call':
        return applyCall(ev.call);
      case 'caption':
        return setCaption(ev.text, ev.role);
      case 'speaking':
        $$('[data-ios-call],[data-mac-call]').forEach((el) => el.classList.toggle('speaking', ev.on));
        return;
    }
  }

  // ── call UI ──────────────────────────────────────────────────
  let tickTimer = null;

  function applyCall(call) {
    const prev = state.call.status;
    state.call = call;
    if (call.status === 'idle') {
      state.minimized = false;
      voice.stop();
      $$('[data-ios-call],[data-mac-call]').forEach((el) => el.classList.remove('speaking'));
    }
    if (call.status === 'ringing' && prev !== 'ringing') setCaption('');
    if (call.status === 'ringing' && call.direction === 'incoming') state.minimized = false;
    renderCall();
  }

  function uiState() {
    const s = state.call.status;
    if (s === 'idle') return 'idle';
    if (s === 'ringing') return 'ringing';
    return state.minimized ? 'minimized' : 'active';
  }

  function renderCall() {
    const s = uiState();
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
    $$('[data-action="mute"]').forEach((b) => b.classList.toggle('on', state.muted));

    clearInterval(tickTimer);
    if (live) tickTimer = setInterval(updateTimer, 500);
    updateTimer();
  }

  function updateTimer() {
    const c = state.call;
    let t = '';
    if (c.status === 'active' && c.started_at) t = fmtDur(Date.now() / 1000 - c.started_at);
    else if (c.status === 'connecting') t = 'connecting…';
    $$('[data-call-timer]').forEach((el) => { el.textContent = t; });
    $$('[data-call-sub]').forEach((el) => { el.textContent = c.status === 'ringing' ? el.dataset.ringing : t; });
  }

  function setCaption(text, role = 'agent') {
    $$('[data-caption]').forEach((el) => {
      const changedTurn = el.dataset.role !== role || !text.startsWith(el.textContent.slice(0, 8));
      el.textContent = text;
      el.dataset.role = role;
      if (changedTurn) { el.classList.remove('in'); void el.offsetWidth; el.classList.add('in'); }
    });
  }

  // ── voice (WebRTC to the Pipecat pipeline) ───────────────────
  const voice = {
    pc: null,
    dc: null,
    mic: null,
    ping: null,
    gen: 0,
    audio: $('[data-remote-audio]'),

    async start() {
      const gen = ++this.gen;
      this.audio.play().catch(() => {}); // unlock playback inside the tap gesture
      try {
        this.mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (err) {
        console.warn('microphone unavailable', err);
        if (gen === this.gen) send({ type: 'call_failed', reason: 'mic' });
        return;
      }
      if (gen !== this.gen) return this._release();

      const track = this.mic.getAudioTracks()[0];
      track.enabled = !state.muted;
      const pc = (this.pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] }));
      pc.addTransceiver(track, { direction: 'sendrecv' });
      pc.ontrack = (e) => {
        this.audio.srcObject = e.streams[0] || new MediaStream([e.track]);
        this.audio.play().catch((err) => console.warn('audio playback blocked', err));
      };
      pc.onconnectionstatechange = () => {
        if (gen === this.gen && pc.connectionState === 'failed') send({ type: 'hangup' });
      };
      // Pipecat treats the call as alive while it hears these pings.
      const dc = (this.dc = pc.createDataChannel('pipecat', { ordered: true }));
      dc.onopen = () => {
        this.ping = setInterval(() => { if (dc.readyState === 'open') dc.send(`ping: ${Date.now()}`); }, 1000);
      };

      try {
        await pc.setLocalDescription(await pc.createOffer());
        await iceGathered(pc, 2500);
        if (gen !== this.gen) return;
        const res = await fetch('/api/offer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
            request_data: { session_id: state.sid },
          }),
        });
        if (!res.ok) {
          // 503 = no API key; the server already recorded the failure.
          if (res.status !== 503) throw new Error(`offer rejected: ${res.status}`);
          return;
        }
        const answer = await res.json();
        if (gen !== this.gen) return;
        await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
      } catch (err) {
        console.error('call setup failed', err);
        if (gen === this.gen) send({ type: 'call_failed', reason: 'webrtc' });
      }
    },

    stop() {
      this.gen++;
      this._release();
    },

    _release() {
      clearInterval(this.ping);
      this.ping = null;
      try { this.dc && this.dc.close(); } catch { /* already closed */ }
      try { this.pc && this.pc.close(); } catch { /* already closed */ }
      if (this.mic) this.mic.getTracks().forEach((t) => t.stop());
      this.dc = this.pc = this.mic = null;
      this.audio.srcObject = null;
    },

    setMuted(muted) {
      if (this.mic) this.mic.getAudioTracks().forEach((t) => { t.enabled = !muted; });
    },
  };

  function iceGathered(pc, timeoutMs) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { pc.removeEventListener('icegatheringstatechange', check); resolve(); };
      const check = () => { if (pc.iceGatheringState === 'complete') done(); };
      pc.addEventListener('icegatheringstatechange', check);
      setTimeout(done, timeoutMs);
    });
  }

  function startCall() {
    state.muted = false;
    send({ type: 'call_accept' });
    voice.start();
  }

  // ── shell controls ───────────────────────────────────────────
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
  function restart() {
    voice.stop();
    state.sid = newId();
    store.set('onboarding.sid', state.sid);
    Object.assign(state, { agentName: null, messages: [], typing: false, minimized: false, muted: false });
    applyCall({ status: 'idle', direction: 'incoming', started_at: null });
    render();
    const old = ws;
    ws = null;
    if (old) old.close();
    connect();
  }

  // ── events ───────────────────────────────────────────────────
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const a = el.dataset.action;
    if (a === 'card') e.preventDefault();

    switch (a) {
      case 'accept': return state.call.status === 'ringing' && startCall();
      case 'decline': return send({ type: 'call_decline' });
      case 'decline-text': return send({ type: 'call_decline', text_instead: true });
      case 'end':
        voice.stop();
        return send({ type: 'hangup' });
      case 'minimize':
        state.minimized = true;
        return renderCall();
      case 'restore':
        state.minimized = false;
        return renderCall();
      case 'call-out':
        return state.call.status === 'idle' && startCall();
      case 'mute':
        state.muted = !state.muted;
        voice.setMuted(state.muted);
        return renderCall();
      case 'card':
        el.animate([{ transform: 'scale(.97)' }, { transform: 'none' }], { duration: 220, easing: 'ease-out' });
        return;

      case 'dev-toggle': {
        const dev = $('[data-dev]');
        dev.classList.toggle('open');
        store.set('shell.dev', dev.classList.contains('open') ? '1' : '0');
        return;
      }
      case 'dev-msg': return send({ type: 'dev_nudge' });
      case 'dev-typing':
        state.typing = !state.typing;
        return render();
      case 'dev-card': return send({ type: 'dev_card' });
      case 'dev-ring': return send({ type: 'dev_ring' });
      case 'dev-reset': return restart();
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
      send({ type: 'user_message', text });
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

  const clock = () => {
    const d = new Date();
    $$('[data-clock]').forEach((el) => { el.textContent = `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, '0')}`; });
  };
  clock();
  setInterval(clock, 15000);

  // ── boot ─────────────────────────────────────────────────────
  setLayout(store.get('shell.layout') || 'auto');
  setTheme(store.get('shell.theme') || 'auto');
  if (store.get('shell.dev') === '1') $('[data-dev]').classList.add('open');

  render();
  renderCall();
  connect();
})();
