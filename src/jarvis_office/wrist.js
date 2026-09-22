// Landscape wrist HUD for the loopback conversation page (control.html).
// Pure helpers are exported for `node --test`; mount() wires the DOM. The orb
// engine (static/dashboard/thinking-orbs.js, MIT, Jakub Antalik) is injected
// so this module has no imports and loads unchanged under Node.

const two = (value) => String(value).padStart(2, '0');
const LIVE = new Set(['responding', 'speaking']);

export const PHASES = Object.freeze({
  boot: {label: 'Liaison…', tone: 'live', orb: 'connecting'},
  offline: {label: 'Liaison coupée', tone: 'fault', orb: 'connecting'},
  starting: {label: 'Initialisation…', tone: 'live', orb: 'connecting'},
  paused: {label: 'En veille', tone: 'idle', orb: 'breathing'},
  listening: {label: 'À l’écoute…', tone: 'hot', orb: 'listening'},
  transcribing: {label: 'Transcription…', tone: 'live', orb: 'searching'},
  responding: {label: 'Jarvis réfléchit…', tone: 'live', orb: 'solving'},
  speaking: {label: 'Jarvis répond…', tone: 'live', orb: 'composing'},
  stopping: {label: 'Arrêt…', tone: 'idle', orb: 'breathing'},
  error: {label: 'Erreur', tone: 'fault', orb: 'breathing'},
});

const MICROPHONE = {open: 'Ouvert', closed: 'Fermé', closure_unverified: 'Non vérifié'};

export function phaseOf(snapshot, linked) {
  if (linked === false) return 'offline';
  if (!snapshot || !Object.hasOwn(PHASES, snapshot.state)) return 'boot';
  return snapshot.state;
}

// The worker reports block RMS; speech sits around -30 dBFS, so a linear
// percentage would barely move. Map -56..-6 dBFS onto 0..1 instead.
export function loudness(rms) {
  const value = Number(rms);
  if (!(value > 0)) return 0;
  return Math.min(1, Math.max(0, (20 * Math.log10(value) + 56) / 50));
}

export function clockOf(date) {
  return {
    hm: `${two(date.getHours())}:${two(date.getMinutes())}`,
    s: two(date.getSeconds()),
    gmt: `${two(date.getUTCHours())}:${two(date.getUTCMinutes())}`,
  };
}

export function missionTime(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60;
  return `T+${two(hours)}:${two(minutes)}:${two(total % 60)}`;
}

export function signalBars(rtt) {
  if (!Number.isFinite(rtt)) return 0;
  return rtt < 80 ? 3 : rtt < 250 ? 2 : 1;
}

export function createTranscript() {
  return {session: null, exchange: null, seq: 0};
}

// The snapshot only carries the latest exchange and streams the answer into
// `response`. Rebuild a log from it: a new turn seen responding (or a changed
// transcription between polls) opens an exchange, later polls update it in
// place, and a new session (Effacer) resets everything.
export function track(memo, snapshot) {
  const ops = [];
  let {session, exchange, seq} = memo;
  const user = typeof snapshot.transcription === 'string' ? snapshot.transcription : '';
  const bot = typeof snapshot.response === 'string' ? snapshot.response : '';
  const live = LIVE.has(snapshot.state);
  const current = snapshot.session ?? null;
  if (current !== session) {
    if (session !== null) ops.push({op: 'reset'});
    session = current;
    exchange = null;
  }
  const fresh = live
    ? exchange === null || exchange.turn !== snapshot.turn
    : exchange === null
      ? Boolean(user || bot)
      : Boolean(user) && user !== exchange.user;
  if (fresh) {
    seq += 1;
    exchange = {id: seq, turn: live ? snapshot.turn : null, user, bot: '', live: false};
    ops.push({op: 'open', id: seq, user});
  }
  if (exchange !== null) {
    if (bot !== exchange.bot) {
      exchange = {...exchange, bot};
      ops.push({op: 'bot', id: exchange.id, text: bot});
    }
    if (live !== exchange.live) {
      exchange = {...exchange, live};
      ops.push({op: 'live', id: exchange.id, live});
    }
  }
  return {memo: {session, exchange, seq}, ops};
}

// The library's `dots` / `dotSize` props, ported because the vendored engine
// build does not export its scalers: paired counts scale by the square root so
// a mode keeps its balance, single counts linearly, every radius by the factor.
const PAIRED = [['latRings', 'lonDensity'], ['rings', 'lonDensity'], ['lanes', 'segs']];
const COUNTS = ['orbitN', 'ghostN', 'nodeN', 'strandN', 'signals'];
const RADII = ['rBase', 'rDepth', 'rActive', 'rDot', 'ghostR', 'partR', 'partRDepth', 'nodeR', 'nodeRDepth'];

export function refine(opts, dots, dotSize) {
  const out = {...opts}, used = new Set(), root = Math.sqrt(dots);
  for (const [a, b] of PAIRED) {
    if (out[a] == null || out[b] == null || used.has(a) || used.has(b)) continue;
    out[a] = Math.max(2, Math.round(out[a] * root));
    out[b] = Math.max(2, Math.round(out[b] * root));
    used.add(a).add(b);
  }
  for (const key of COUNTS) {
    if (out[key] && !used.has(key)) out[key] = Math.max(1, Math.round(out[key] * dots));
  }
  if (out.iconD != null) out.iconD = Math.max(0.02, out.iconD * dots);
  for (const key of RADII) if (out[key] != null) out[key] *= dotSize;
  out.rSizeMul = (out.rSizeMul ?? 1) * dotSize;
  return out;
}

const HOLD_MS = 1000;
const HEADERS = {'Content-Type': 'application/json', 'X-Jarvis-Local': '1'};
const ACCENT = '85,207,255';
const DANGER = '255,122,112';
const easeOut = (x) => 1 - (1 - x) ** 3;

export function mount(doc, engine) {
  const win = doc.defaultView;
  const nav = win.navigator;
  const $ = (id) => doc.getElementById(id);
  const hud = $('hud');
  const orb = $('orb');
  const pill = $('status');
  const pillText = $('status-text');
  const reduced = win.matchMedia('(prefers-reduced-motion: reduce)');
  const presets = new Map();
  const chips = new Set();
  const rows = new Map();
  let memo = createTranscript();
  let snapshot = null, linked = null, linkedAt = 0, failures = 0, rtt = NaN;
  let phase = 'boot', armed = false, fault = '';
  let clock = 0, last = 0, raf = 0, second = -1;
  let vu = 0, vuTarget = 0;
  let orbState = PHASES.boot.orb, previousOrb = null, changedAt = 0;
  let visorSize = 0, dpr = 1;
  let pendingLabel = null, faultTimer = 0;
  const put = (id, value, tone) => {
    const el = $(id);
    if (el.textContent !== value) el.textContent = value;
    if (tone !== undefined && el.dataset.tone !== tone) el.dataset.tone = tone;
  };

  // The hero orb is ~3x the 64 px preset: twice the dots, 0.8x their radius.
  const preset = (state, hero) => {
    const key = `${state}-${hero ? 'hero' : 20}`;
    if (!presets.has(key)) {
      const base = engine.resolvePreset(state, hero ? 64 : 20);
      presets.set(key, hero ? {...base, opts: refine(base.opts, 2, 0.8)} : base);
    }
    return presets.get(key);
  };

  function paintOrb(ctx, state, t, size, alpha = 1, scale = 1) {
    const {mode, speed, opts} = preset(state, size >= 40);
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(size / 2, size / 2);
    ctx.scale(scale, scale);
    ctx.translate(-size / 2, -size / 2);
    engine.paintFrame(ctx, engine.MODE_FRAMES[mode](size, t * speed, opts), true);
    ctx.restore();
  }

  // ---- visor: bezel ticks (mic VU + seconds) around the orb ------------------
  const visor = $('orb-canvas');
  const vctx = visor.getContext('2d');

  function paintVisor(now) {
    if (!visorSize) return;
    const size = visorSize, c = size / 2;
    vctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    vctx.clearRect(0, 0, size, size);
    const tone = PHASES[phase].tone;
    const lit = Math.round(vu * 60);
    const outer = c - 2;
    vctx.lineCap = 'round';
    for (let i = 0; i < 60; i++) {
      const angle = -Math.PI / 2 + (i * Math.PI) / 30;
      const major = i % 5 === 0;
      let length = major ? Math.max(7, size * 0.04) : Math.max(4, size * 0.022);
      const cos = Math.cos(angle), sin = Math.sin(angle);
      let color = `rgba(255,255,255,${major ? 0.3 : 0.13})`;
      if (tone === 'fault' && major) color = `rgba(${DANGER},0.7)`;
      else if (i < lit) {
        color = `rgba(${ACCENT},${0.5 + 0.5 * ((i + 1) / 60)})`;
        length += size * 0.014;
      } else if (i === second) color = 'rgba(255,255,255,0.85)';
      else if (tone === 'hot') color = `rgba(${ACCENT},${major ? 0.34 : 0.12})`;
      vctx.strokeStyle = color;
      vctx.lineWidth = major ? 1.6 : 1.1;
      vctx.beginPath();
      vctx.moveTo(c + cos * (outer - length), c + sin * (outer - length));
      vctx.lineTo(c + cos * outer, c + sin * outer);
      vctx.stroke();
    }
    const inner = size * 0.72, offset = (size - inner) / 2;
    const pulse = 1 + (phase === 'listening' ? 0.05 * vu : 0);
    const mix = previousOrb === null ? 1 : easeOut(Math.min(1, (now - changedAt) / 420));
    const dim = tone === 'fault' || phase === 'stopping' ? 0.5 : 1;
    vctx.save();
    vctx.translate(offset, offset);
    if (mix < 1 && previousOrb !== null) {
      paintOrb(vctx, previousOrb, clock, inner, (1 - mix) * dim, pulse * (1 + 0.06 * mix));
    }
    paintOrb(vctx, orbState, clock, inner, mix * dim, pulse * (0.94 + 0.06 * mix));
    vctx.restore();
    if (mix >= 1) previousOrb = null;
  }

  function paintChips() {
    for (const chip of chips) {
      if (!chip.canvas.isConnected) {
        chips.delete(chip);
        continue;
      }
      if (chip.canvas.closest('[hidden]')) continue;
      const ctx = chip.canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, 20, 20);
      paintOrb(ctx, chip.state, clock, 20);
    }
  }

  function chip(state, label) {
    const root = doc.createElement('span');
    root.className = 'chip';
    const canvas = doc.createElement('canvas');
    canvas.width = canvas.height = Math.round(20 * dpr);
    canvas.setAttribute('aria-hidden', 'true');
    const text = doc.createElement('span');
    text.className = 'shimmer';
    text.textContent = label;
    text.dataset.text = label;
    root.append(canvas, text);
    chips.add({canvas, state});
    return root;
  }

  function frame(now) {
    raf = 0;
    if (doc.hidden) {
      last = 0;
      return;
    }
    const busy = phase !== 'paused' || vu > 0.01 || previousOrb !== null;
    if (!last || now - last >= (busy ? 32 : 48)) {
      const dt = last ? Math.min(now - last, 250) / 1000 : 0;
      last = now;
      clock += dt * (phase === 'paused' ? 0.6 : 1);
      const blend = 1 - Math.exp(-dt / (vuTarget > vu ? 0.06 : 0.3));
      vu += (vuTarget - vu) * blend;
      if (vu < 0.004) vu = 0;
      paintVisor(now);
      paintChips();
    }
    schedule();
  }

  function schedule() {
    if (reduced.matches) {
      clock = 0.6;
      vu = vuTarget;
      previousOrb = null;
      paintVisor(0);
      paintChips();
      return;
    }
    if (!raf && !doc.hidden) raf = win.requestAnimationFrame(frame);
  }

  function resize() {
    dpr = Math.min(2, win.devicePixelRatio || 1);
    const box = orb.getBoundingClientRect();
    visorSize = Math.max(0, Math.round(Math.min(box.width, box.height)));
    visor.width = visor.height = Math.round(visorSize * dpr);
    for (const item of chips) item.canvas.width = item.canvas.height = Math.round(20 * dpr);
    drawStars();
    schedule();
  }

  // ---- starfield: painted once per resize, never animated --------------------
  function drawStars() {
    const canvas = $('stars');
    const w = win.innerWidth, h = win.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    let seed = 0x5eed;
    const rand = () => (seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296;
    const count = Math.round((w * h) / 3200);
    for (let i = 0; i < count; i++) {
      const x = rand() * w, y = rand() * h, m = rand(), tint = rand();
      const a = 0.1 + rand() * (m > 0.9 ? 0.65 : 0.3);
      const rgb = tint > 0.82 ? '190,215,255' : tint < 0.08 ? '255,226,196' : '255,255,255';
      ctx.fillStyle = `rgba(${rgb},${a.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(x, y, m > 0.97 ? 1.15 : m > 0.8 ? 0.8 : 0.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // ---- status pill: blur crossfade between labels ----------------------------
  function setStatus(label, tone) {
    pill.dataset.tone = tone;
    if (pendingLabel !== null) {
      pendingLabel = label;
      return;
    }
    if (label === pillText.textContent) return;
    const swap = (value) => {
      pillText.textContent = value;
      pillText.dataset.text = value;
    };
    if (reduced.matches || typeof pillText.animate !== 'function') {
      swap(label);
      return;
    }
    pendingLabel = label;
    const out = pillText.animate(
      [
        {opacity: 1, transform: 'none', filter: 'blur(0)'},
        {opacity: 0, transform: 'translateY(-4px)', filter: 'blur(2px)'},
      ],
      {duration: 120, easing: 'cubic-bezier(.22,1,.36,1)'},
    );
    out.onfinish = out.oncancel = () => {
      const value = pendingLabel;
      pendingLabel = null;
      swap(value);
      pillText.animate(
        [
          {opacity: 0, transform: 'translateY(4px)', filter: 'blur(2px)'},
          {opacity: 1, transform: 'none', filter: 'blur(0)'},
        ],
        {duration: 200, easing: 'cubic-bezier(.22,1,.36,1)'},
      );
    };
  }

  // ---- comms log --------------------------------------------------------------
  const log = $('log');
  const empty = $('empty');
  const pending = $('pending');
  empty.prepend(chip('breathing', 'Canal ouvert'));
  pending.append(chip('searching', 'Transcription…'));

  function message(kind, who) {
    const root = doc.createElement('div');
    root.className = `msg ${kind}`;
    const label = doc.createElement('span');
    label.className = 'who';
    label.textContent = who;
    const text = doc.createElement('p');
    root.append(label, text);
    return {root, text};
  }

  function settle(row) {
    row.thinking?.remove();
    row.thinking = null;
  }

  function applyOps(ops) {
    const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    for (const op of ops) {
      if (op.op === 'reset') {
        for (const row of rows.values()) row.root.remove();
        rows.clear();
      } else if (op.op === 'open') {
        const root = doc.createElement('article');
        root.className = 'exchange';
        const {hm} = clockOf(new Date());
        const user = message('user', `Vous · ${hm}`);
        user.text.textContent = op.user;
        const bot = message('bot', 'Jarvis');
        const thinking = chip('solving', 'Jarvis réfléchit…');
        thinking.classList.add('thinking');
        bot.root.append(thinking);
        if (op.user) root.append(user.root);
        root.append(bot.root);
        log.insertBefore(root, pending);
        rows.set(op.id, {root, bot, thinking});
        while (rows.size > 40) {
          const [oldest, row] = rows.entries().next().value;
          row.root.remove();
          rows.delete(oldest);
        }
      } else if (op.op === 'bot') {
        const row = rows.get(op.id);
        if (row) {
          row.bot.text.textContent = op.text;
          if (op.text !== '') settle(row);
        }
      } else if (op.op === 'live') {
        const row = rows.get(op.id);
        if (row) {
          row.bot.root.classList.toggle('live', op.live);
          row.bot.root.setAttribute('aria-busy', String(op.live));
          if (!op.live) settle(row);
        }
      }
    }
    empty.hidden = rows.size > 0;
    const count = rows.size;
    put('count', count ? `${count} échange${count > 1 ? 's' : ''}` : 'Canal local');
    if (stick || ops.some((op) => op.op === 'open')) log.scrollTop = log.scrollHeight;
  }

  // ---- telemetry --------------------------------------------------------------
  function tick() {
    const now = new Date();
    const time = clockOf(now);
    put('clock-hm', time.hm);
    put('clock-s', time.s);
    $('clock').dateTime = now.toISOString();
    put('gmt', time.gmt);
    put('met', linkedAt ? missionTime(Date.now() - linkedAt) : 'T+--:--:--');
    second = now.getSeconds();
    if (reduced.matches) schedule();
  }

  function render() {
    phase = phaseOf(snapshot, linked);
    const {label, tone, orb: next} = PHASES[phase];
    if (next !== orbState) {
      previousOrb = orbState;
      orbState = next;
      changedAt = win.performance.now();
    }
    armed = snapshot?.armed === true && linked !== false;
    hud.dataset.phase = phase;
    hud.dataset.tone = tone;
    setStatus(label, tone);
    put('orb-label', armed ? 'Pause' : 'Écouter');
    vuTarget = linked && phase === 'listening' ? loudness(snapshot.level) : 0;
    put('notice', linked === false ? 'Reconnexion automatique en cours.' : snapshot?.notice || '');
    $('sig-bars').dataset.level = String(linked ? signalBars(rtt) : 0);
    const sig = linked === false ? 'Coupée' : Number.isFinite(rtt) ? `${Math.round(rtt)} ms` : '—';
    put('sig', sig, linked === false ? 'fault' : '');
    const mic = snapshot?.microphone;
    const micTone = mic === 'open' ? 'hot' : mic === 'closure_unverified' ? 'fault' : '';
    put('mic', linked === false ? '—' : MICROPHONE[mic] || '—', micTone);
    const session = typeof snapshot?.session === 'string' ? snapshot.session.slice(0, 4) : '';
    put('session', linked && session ? `#${session.toUpperCase()}` : '—');
    const devices = [snapshot?.selected_input, snapshot?.selected_output].filter(Boolean);
    put('devices', devices.join(' → ') || 'Conversation locale');
    $('link-dot').dataset.tone = linked === true ? 'hot' : linked === false ? 'fault' : '';
    const problem = linked === false ? 'Liaison locale coupée' : fault || snapshot?.error || '';
    $('fault').hidden = !problem;
    put('fault-code', problem);
    const transcribing = phase === 'transcribing';
    if (transcribing === pending.hidden) {
      pending.hidden = !transcribing;
      if (transcribing) log.scrollTop = log.scrollHeight;
    }
    if (snapshot && linked) {
      const result = track(memo, snapshot);
      memo = result.memo;
      if (result.ops.length) applyOps(result.ops);
    }
    wantLock(armed);
    schedule();
  }

  // ---- link -------------------------------------------------------------------
  async function api(path, data = {}) {
    const response = await win.fetch(path, {
      method: 'POST',
      headers: HEADERS,
      credentials: 'same-origin',
      body: JSON.stringify(data),
    });
    if (!response.ok) {
      const error = new Error('link');
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  // Only the request decides the link state; the next poll is always scheduled.
  async function poll() {
    const started = win.performance.now();
    let next = null;
    try {
      next = await api('/snapshot');
    } catch (error) {
      failures += 1;
      // A restarted server issues a new cookie; bootstrap has no side effect.
      if (error.status === 403) await api('/bootstrap').catch(() => {});
      if (failures >= 2) {
        linked = false;
        rtt = NaN;
      }
    }
    win.setTimeout(poll, doc.hidden ? 1500 : 300);
    if (next) {
      const spent = win.performance.now() - started;
      rtt = Number.isFinite(rtt) ? rtt * 0.7 + spent * 0.3 : spent;
      failures = 0;
      linked = true;
      linkedAt ||= Date.now();
      snapshot = next;
    }
    render();
  }

  async function send(path, data) {
    try {
      await api(path, data);
      fault = '';
      return true;
    } catch {
      fault = 'Commande non transmise';
      win.clearTimeout(faultTimer);
      faultTimer = win.setTimeout(() => {
        fault = '';
        render();
      }, 4000);
      render();
      return false;
    }
  }

  const buzz = (ms) => nav.vibrate?.(ms);

  // ---- controls ---------------------------------------------------------------
  orb.addEventListener('click', () => {
    buzz(8);
    send('/control', {action: armed ? 'pause' : 'resume'});
  });
  $('cancel').addEventListener('click', () => {
    buzz(8);
    send('/control', {action: 'cancel'});
  });

  const clear = $('clear');
  let holdTimer = 0, confirmTimer = 0;
  const commitClear = () => {
    buzz([12, 40, 12]);
    send('/control', {action: 'clear'});
  };
  const release = () => {
    if (holdTimer) win.clearTimeout(holdTimer);
    holdTimer = 0;
    clear.classList.remove('holding');
  };
  const confirmMode = (on) => {
    clear.classList.toggle('confirm', on);
    $('clear-label').textContent = on ? 'Confirmer' : 'Effacer';
    if (confirmTimer) win.clearTimeout(confirmTimer);
    confirmTimer = on ? win.setTimeout(() => confirmMode(false), 4000) : 0;
  };
  clear.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || holdTimer) return;
    try {
      clear.setPointerCapture(event.pointerId);
    } catch {
      // Capture only keeps the hold alive when the finger slides; not required.
    }
    clear.classList.add('holding');
    holdTimer = win.setTimeout(() => {
      release();
      commitClear();
    }, HOLD_MS);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    clear.addEventListener(type, release);
  }
  clear.addEventListener('contextmenu', (event) => event.preventDefault());
  // Keyboard and assistive tech clicks (detail 0) confirm in two steps instead.
  clear.addEventListener('click', (event) => {
    if (event.detail !== 0) return;
    if (clear.classList.contains('confirm')) {
      confirmMode(false);
      commitClear();
    } else confirmMode(true);
  });

  const composer = $('composer');
  const text = $('text');
  const write = $('write');
  write.addEventListener('click', () => {
    if (typeof composer.showModal === 'function') composer.showModal();
    else composer.setAttribute('open', '');
    write.setAttribute('aria-expanded', 'true');
    text.focus();
  });
  composer.addEventListener('close', () => write.setAttribute('aria-expanded', 'false'));
  $('composer-close').addEventListener('click', () => composer.close());
  composer.addEventListener('click', (event) => {
    const box = composer.getBoundingClientRect();
    const inside =
      event.clientX >= box.left &&
      event.clientX <= box.right &&
      event.clientY >= box.top &&
      event.clientY <= box.bottom;
    if (event.target === composer && !inside) composer.close();
  });
  const form = $('composer-form');
  let sending = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = text.value.trim();
    if (!value || sending) return;
    sending = true;
    try {
      if (await send('/say', {text: value})) {
        text.value = '';
        composer.close();
      }
    } finally {
      sending = false;
    }
  });
  // Explicit Enter: some keyboards never fire the implicit form submission.
  text.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing || event.shiftKey) return;
    event.preventDefault();
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.querySelector('[type=submit]').click();
  });

  // ---- screen wake lock while listening, battery when the browser exposes it --
  let lock = null, wanted = false, locking = false;
  async function syncLock() {
    if (!nav.wakeLock || locking) return;
    locking = true;
    const target = wanted;
    try {
      if (wanted && !lock && !doc.hidden) {
        const current = await nav.wakeLock.request('screen');
        current.addEventListener('release', () => {
          if (lock === current) lock = null;
          renderLock();
        });
        lock = current;
      } else if (!wanted && lock) {
        const held = lock;
        lock = null;
        await held.release();
      }
    } catch {
      lock = null;
    } finally {
      locking = false;
      renderLock();
    }
    if (wanted !== target) syncLock();
  }
  function renderLock() {
    put('wake', lock ? 'Maintenu' : 'Auto', lock ? 'hot' : '');
  }
  function wantLock(value) {
    if (value === wanted) return;
    wanted = value;
    syncLock();
  }
  if (nav.wakeLock) {
    $('wake-row').hidden = false;
    renderLock();
  }
  nav
    .getBattery?.()
    .then((battery) => {
      const show = () => {
        $('pwr-row').hidden = false;
        const level = `${Math.round(battery.level * 100)}\u00a0%${battery.charging ? ' · secteur' : ''}`;
        put('pwr', level, battery.level <= 0.15 && !battery.charging ? 'fault' : '');
      };
      show();
      battery.addEventListener('levelchange', show);
      battery.addEventListener('chargingchange', show);
    })
    .catch(() => {});

  doc.addEventListener('visibilitychange', () => {
    last = 0;
    if (!doc.hidden) syncLock();
    schedule();
  });
  reduced.addEventListener?.('change', schedule);
  win.addEventListener('resize', resize);
  new win.ResizeObserver(resize).observe(orb);
  resize();
  tick();
  win.setInterval(tick, 1000);
  render();
  api('/bootstrap')
    .catch(() => {})
    .then(poll);
  if ('serviceWorker' in nav) nav.serviceWorker.register('/sw.js').catch(() => {});
}
