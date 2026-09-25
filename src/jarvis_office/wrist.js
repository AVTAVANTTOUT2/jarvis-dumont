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
  talking: {label: 'Liaison…', tone: 'hot', orb: 'listening'},
  transcribing: {label: 'Décodage…', tone: 'live', orb: 'searching'},
  responding: {label: 'Jarvis réfléchit…', tone: 'live', orb: 'solving'},
  speaking: {label: 'Jarvis répond…', tone: 'live', orb: 'composing'},
  stopping: {label: 'Arrêt…', tone: 'idle', orb: 'breathing'},
  error: {label: 'Erreur', tone: 'fault', orb: 'breathing'},
});

export const CREW = Object.freeze(['aymen', 'evann', 'alexandre', 'elias', 'faiz']);
export const CREW_LABEL = Object.freeze({
  aymen: 'Aymen',
  evann: 'Evann',
  alexandre: 'Alexandre',
  elias: 'Elias',
  faiz: 'Faiz',
});

const MICROPHONE = {open: 'Ouvert', closed: 'Fermé', closure_unverified: 'Non vérifié'};

export function crewOthers(me) {
  return CREW.filter((id) => id !== me);
}

export function seatTaken(seats, id, me) {
  return Boolean(id && seats?.[id]?.online && id !== me);
}

export function inCall(snapshot, id) {
  const call = snapshot?.crew?.call;
  const who = id ?? snapshot?.crew?.me;
  return Boolean(call && who && (who === call.a || who === call.b));
}

export function phaseOf(snapshot, linked) {
  if (linked === false) return 'offline';
  if (inCall(snapshot, snapshot?.crew?.me)) return 'talking';
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

export const SUIT_KINDS = Object.freeze(['hr', 'o2', 'kpa', 'th']);
export const SUIT = Object.freeze({
  hr: {digits: 0, unit: 'bpm', lo: 64, hi: 98},
  o2: {digits: 1, unit: '%', lo: 20.1, hi: 21.2},
  kpa: {digits: 1, unit: 'kPa', lo: 100.3, hi: 102.5},
  th: {digits: 1, unit: '°C', lo: 36.1, hi: 37.6},
});
const SPARK_W = 120;
const SPARK_H = 36;
const SPARK_DEPTH = 40;

export function suitLoad(phase, vu) {
  if (phase === 'listening') return Math.min(1, 0.28 + Math.max(0, Number(vu) || 0) * 0.72);
  if (phase === 'speaking' || phase === 'responding') return 0.42;
  if (phase === 'transcribing') return 0.22;
  if (phase === 'offline' || phase === 'error') return 0.75;
  return 0.08;
}

export function suitSample(kind, t, load = 0) {
  const time = Number(t);
  if (!Number.isFinite(time)) throw new RangeError(`suitSample time must be finite, got ${t}`);
  const stress = Math.min(1, Math.max(0, Number(load) || 0));
  switch (kind) {
    case 'hr':
      return 72 + 10 * Math.sin(time * 1.35) + 3.5 * Math.sin(time * 0.28) + 14 * stress;
    case 'o2':
      return 20.72 + 0.45 * Math.sin(time * 0.41) + 0.12 * Math.sin(time * 1.1) - 0.16 * stress;
    case 'kpa':
      return 101.32 + 0.85 * Math.sin(time * 0.24) + 0.28 * Math.sin(time * 0.88);
    case 'th':
      return 36.78 + 0.42 * Math.sin(time * 0.19) + 0.12 * Math.sin(time * 0.7) + 0.32 * stress;
    default:
      throw new RangeError(`unknown suit metric: ${kind}`);
  }
}

export function sparkPoints(values, width = SPARK_W, height = SPARK_H, pad = 1.5, range) {
  const n = values.length;
  if (!n) return '';
  let min = values[0], max = values[0];
  if (range && Number.isFinite(range.lo) && Number.isFinite(range.hi) && range.hi > range.lo) {
    min = range.lo;
    max = range.hi;
  } else {
    for (let i = 1; i < n; i++) {
      const v = values[i];
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const last = n - 1;
  const span = max - min || 1;
  const low = pad;
  const high = height - pad;
  const flat = max === min;
  return values
    .map((value, i) => {
      const x = pad + (last ? (i / last) * innerW : innerW / 2);
      const y = flat
        ? height / 2
        : Math.min(high, Math.max(low, high - ((value - min) / span) * innerH));
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');
}

export function sparkFill(values, width = SPARK_W, height = SPARK_H, pad = 1.5, range) {
  const line = sparkPoints(values, width, height, pad, range);
  if (!line) return '';
  const lastX = line.slice(line.lastIndexOf(' ') + 1).split(',')[0];
  const base = (height - pad).toFixed(2);
  return `${pad.toFixed(2)},${base} ${line} ${lastX},${base}`;
}

export const PHONE_RATE = 16000;
export const PHONE_FRAME = 320;

export function downsample(input, fromRate, toRate) {
  const source = Number(fromRate), target = Number(toRate);
  if (!input.length || !(source > 0) || !(target > 0)) return new Float32Array(0);
  if (source === target) return Float32Array.from(input);
  const ratio = source / target;
  const n = Math.floor(input.length / ratio);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = i * ratio;
    const i0 = Math.floor(x);
    const frac = x - i0;
    const a = input[i0] || 0;
    const b = input[i0 + 1] ?? a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

export function pcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++) {
    const clipped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, clipped < 0 ? Math.round(clipped * 0x8000) : Math.round(clipped * 0x7fff), true);
  }
  return bytes;
}

export function createUplink() {
  let hold = new Float32Array(0);
  return {
    push(samples, fromRate) {
      const mono = downsample(samples, fromRate, PHONE_RATE);
      const joined = new Float32Array(hold.length + mono.length);
      joined.set(hold);
      joined.set(mono, hold.length);
      const frames = [];
      let offset = 0;
      while (offset + PHONE_FRAME <= joined.length) {
        frames.push(pcm16(joined.subarray(offset, offset + PHONE_FRAME)));
        offset += PHONE_FRAME;
      }
      hold = joined.subarray(offset);
      return frames;
    },
    reset() {
      hold = new Float32Array(0);
    },
  };
}

export function decodePcm(b64) {
  const binary = typeof atob === 'function' ? atob(b64) : Buffer.from(b64, 'base64').toString('binary');
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
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
  const traces = Object.fromEntries(SUIT_KINDS.map((kind) => [kind, []]));
  let snapshot = null, linked = null, linkedAt = 0, failures = 0, rtt = NaN;
  let settled = false, chosen = false;
  let phase = 'boot', armed = false, fault = '';
  let clock = 0.6, last = 0, raf = 0, sparkAt = 0, labelAt = 0;
  let vu = 0, vuTarget = 0;
  let orbState = PHASES.boot.orb, previousOrb = null, changedAt = 0;
  let visorSize = 0, dpr = 1;
  let pendingLabel = null, faultTimer = 0;
  const put = (id, value, tone) => {
    const el = $(id);
    if (!el) return;
    if (el.textContent !== value) el.textContent = value;
    if (tone !== undefined && el.dataset.tone !== tone) el.dataset.tone = tone;
  };

  // 64 px libraries.dev preset, slightly denser and chunkier for the ~280 px visor.
  const preset = (state, hero) => {
    const key = `${state}-${hero ? 'hero' : 20}`;
    if (!presets.has(key)) {
      const base = engine.resolvePreset(state, hero ? 64 : 20);
      presets.set(key, hero ? {...base, opts: refine(base.opts, 1.6, 1.35)} : base);
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

  // ---- visor: thinking-orbs hero, no watch bezel ------------------------------
  const visor = $('orb-canvas');
  const vctx = visor.getContext('2d');

  function paintVisor(now) {
    if (!visorSize) return;
    const size = visorSize;
    vctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    vctx.clearRect(0, 0, size, size);
    const tone = PHASES[phase].tone;
    const pulse = 1 + (phase === 'listening' || phase === 'talking' ? 0.05 * vu : 0);
    const mix = previousOrb === null ? 1 : easeOut(Math.min(1, (now - changedAt) / 420));
    const dim = tone === 'fault' || phase === 'stopping' ? 0.5 : 1;
    if (mix < 1 && previousOrb !== null) {
      paintOrb(vctx, previousOrb, clock, size, (1 - mix) * dim, pulse * (1 + 0.06 * mix));
    }
    paintOrb(vctx, orbState, clock, size, mix * dim, pulse * (0.94 + 0.06 * mix));
    if (mix >= 1) previousOrb = null;
  }

  function paintSuit(labels) {
    for (const kind of SUIT_KINDS) {
      const buf = traces[kind];
      const spec = SUIT[kind];
      const line = $(`${kind}-spark`);
      const fill = $(`${kind}-fill`);
      if (line) line.setAttribute('points', sparkPoints(buf, SPARK_W, SPARK_H, 1.5, spec));
      if (fill) fill.setAttribute('points', sparkFill(buf, SPARK_W, SPARK_H, 1.5, spec));
      if (labels && buf.length) put(`${kind}-val`, buf[buf.length - 1].toFixed(spec.digits));
    }
  }

  function ingestSuit(labels) {
    const load = suitLoad(phase, vu);
    for (const kind of SUIT_KINDS) {
      const buf = traces[kind];
      buf.push(suitSample(kind, clock, load));
      if (buf.length > SPARK_DEPTH) buf.shift();
    }
    paintSuit(labels);
  }

  function seedSuit() {
    for (const kind of SUIT_KINDS) traces[kind].length = 0;
    const load = suitLoad(phase, vu);
    for (let i = 0; i < SPARK_DEPTH; i++) {
      for (const kind of SUIT_KINDS) traces[kind].push(suitSample(kind, i * 0.08, load));
    }
    paintSuit(true);
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
      if (now - sparkAt >= 80) {
        const labels = now - labelAt >= 200;
        sparkAt = now;
        if (labels) labelAt = now;
        ingestSuit(labels);
      }
    }
    schedule();
  }

  function schedule() {
    if (reduced.matches) {
      clock = 0.6;
      vu = vuTarget;
      previousOrb = null;
      paintVisor(0);
      if (!traces.hr.length) seedSuit();
      else paintSuit(true);
      return;
    }
    if (doc.hidden) {
      last = 0;
      paintVisor(win.performance.now());
      if (!traces.hr.length) seedSuit();
      else paintSuit(true);
      return;
    }
    if (!raf) raf = win.requestAnimationFrame(frame);
  }

  function resize() {
    dpr = Math.min(2, win.devicePixelRatio || 1);
    const box = orb.getBoundingClientRect();
    visorSize = Math.max(0, Math.round(Math.min(box.width, box.height)));
    visor.width = visor.height = Math.round(visorSize * dpr);
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
    if (reduced.matches || doc.hidden || typeof pillText.animate !== 'function') {
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

  // ---- telemetry --------------------------------------------------------------
  function tick() {
    const now = new Date();
    const time = clockOf(now);
    put('clock-hm', time.hm);
    put('clock-s', time.s);
    $('clock').dateTime = now.toISOString();
    put('gmt', time.gmt);
    put('met', linkedAt ? missionTime(Date.now() - linkedAt) : 'T+--:--:--');
    if (reduced.matches) schedule();
  }

  function render() {
    phase = phaseOf(snapshot, linked);
    if (phase === 'talking' && snapshot.crew.call.id === blockedCall) phase = 'paused';
    const otherPhone = snapshot?.phone_audio?.owned === false &&
      (snapshot?.armed || LIVE.has(snapshot?.state) || snapshot?.state === 'transcribing');
    if (phase !== 'talking' && (otherPhone || voiceStopped)) phase = 'paused';
    const {label, tone, orb: next} = PHASES[phase];
    if (next !== orbState) {
      previousOrb = orbState;
      orbState = next;
      changedAt = win.performance.now();
    }
    armed = snapshot?.armed === true && linked !== false && !otherPhone && !voiceStopped;
    hud.dataset.phase = phase;
    hud.dataset.tone = tone;
    setStatus(label, tone);
    const callLive = Boolean(snapshot?.crew?.call);
    const action = phase === 'talking' ? 'Liaison' : armed ? 'Pause' : 'Écouter';
    put('orb-label', action);
    if (orb.getAttribute('aria-label') !== action) orb.setAttribute('aria-label', action);
    orb.disabled = callLive || !!otherPhone;
    const hang = $('cancel').querySelector('span');
    if (hang) hang.textContent = phase === 'talking' ? 'Raccrocher' : 'Couper';
    vuTarget = linked && phase === 'listening' ? loudness(snapshot.level) : 0;
    put('notice', linked === false ? 'Reconnexion automatique en cours.' : otherPhone ? 'Jarvis est utilisé sur un autre appareil.' : '');
    $('sig-bars').dataset.level = String(linked ? signalBars(rtt) : 0);
    const sig = linked === false ? 'Coupée' : Number.isFinite(rtt) ? `${Math.round(rtt)} ms` : '—';
    put('sig', sig, linked === false ? 'fault' : '');
    const mic = snapshot?.microphone;
    const micTone = mic === 'open' || (phase === 'talking' && phoneStream) ? 'hot' : mic === 'closure_unverified' ? 'fault' : '';
    put(
      'mic',
      linked === false ? '—' : phase === 'talking' && phoneStream ? MICROPHONE.open : MICROPHONE[mic] || '—',
      micTone,
    );
    const session = typeof snapshot?.session === 'string' ? snapshot.session.slice(0, 4) : '';
    put('session', linked && session ? `#${session.toUpperCase()}` : '—');
    put('devices', 'Téléphone');
    $('link-dot').dataset.tone = linked === true ? 'hot' : linked === false ? 'fault' : '';
    const problem = linked === false ? 'Liaison locale coupée' : fault ||
      (snapshot?.crew?.error === 'audio_stalled' ? 'Liaison audio interrompue. Relancez l’appel.' : '') ||
      snapshot?.error || '';
    $('fault').hidden = !problem;
    put('fault-code', problem);
    renderCrew();
    syncPhone();
    wantLock(armed || phase === 'talking');
    if (!doc.hidden) pumpOut();
    schedule();
  }

  function renderCrew() {
    const crew = snapshot?.crew;
    const me = typeof crew?.me === 'string' ? crew.me : '';
    const seats = crew?.seats || {};
    const call = crew?.call;
    put('crew-tag', me && CREW_LABEL[me] ? `EVA · ${CREW_LABEL[me]}` : 'EVA · Dumont');
    const gate = $('gate');
    const locked = !me && linked !== false;
    const opened = locked && gate.hidden;
    gate.hidden = !locked;
    for (const el of hud.querySelectorAll(':scope > :not(#gate)')) el.inert = locked;
    if (opened) gate.querySelector('[data-claim]:not(:disabled)')?.focus();
    const problem = linked === false ? 'Liaison locale coupée' : fault || snapshot?.error || '';
    $('gate-fault').hidden = !locked || !problem;
    put('gate-fault-code', problem);
    for (const btn of doc.querySelectorAll('[data-claim]')) {
      const id = btn.getAttribute('data-claim');
      const taken = seatTaken(seats, id, me);
      btn.disabled = taken;
      const note = btn.querySelector('small');
      if (note) note.hidden = !taken;
    }
    const others = me ? crewOthers(me) : [];
    const cards = doc.querySelectorAll('[data-crew-card]');
    cards.forEach((card, index) => {
      const id = others[index];
      const name = card.querySelector('strong');
      const state = card.querySelector('small');
      if (!id) {
        card.hidden = true;
        card.dataset.id = '';
        return;
      }
      const online = seats[id]?.online === true;
      const live = Boolean(call && (call.a === id || call.b === id));
      if (name) name.textContent = CREW_LABEL[id];
      if (state) state.textContent = live ? 'En liaison' : online ? 'En ligne' : 'Hors ligne';
      card.hidden = false;
      card.dataset.id = id;
      card.dataset.tone = live ? 'hot' : online ? 'live' : '';
      card.disabled = !online || Boolean(call);
      const label = CREW_LABEL[id];
      card.setAttribute(
        'aria-label',
        live ? `${label}, en liaison` : online ? `Appeler ${label}` : `${label}, hors ligne`,
      );
    });
  }

  // ---- link -------------------------------------------------------------------
  async function api(path, data = {}, options = {}) {
    const response = await win.fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      body: JSON.stringify(data),
      ...options,
      headers: {...HEADERS, 'X-Jarvis-Client': phoneClient, ...options.headers},
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
      if (!settled && !chosen && next.crew) {
        settled = true;
        if (next.crew.me) api('/crew/release', {}).catch(() => {});
      }
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

  // ---- phone I/O: capture and playback stay on the device, never the Mac -----
  const AudioCtx = win.AudioContext || win.webkitAudioContext;
  const phoneClient = (win.crypto || globalThis.crypto).randomUUID();
  const uplink = createUplink();
  let phoneCtx = null, phoneStream = null, phoneNode = null, phoneMute = null;
  let phoneArming = null, speakerCursor = 0, speakerBytes = 0, speakerRate = 0;
  const speakerSources = [];
  let pumping = false, capturing = false, activeCall = '', blockedCall = '';
  let audioEpoch = 0, audioRoute = '', captureRoute = '';
  let talkPending = [], talkRequest = null, talkPull = null;
  const TALK_QUEUE_FRAMES = 10; // 200 ms at 16 kHz, independent of Jarvis STT.
  const TALK_TIMEOUT_MS = 500;
  let voicePending = [], voiceRequest = null, voiceCapture = '', voiceSequence = 0;
  let blockedCapture = '', voiceStopped = false;

  function resetVoiceTransport() {
    voicePending = [];
    voiceRequest?.abort();
    voiceRequest = null;
    voiceSequence = 0;
  }

  function stopVoice(message = '') {
    voiceStopped = true;
    capturing = false;
    audioEpoch += 1;
    resetVoiceTransport();
    uplink.reset();
    cutSpeaker();
    if (message) fault = message;
  }

  function failVoice(message) {
    if (voiceStopped) return;
    stopVoice(message);
    api('/control', {action: 'pause'}).catch(() => {});
    render();
  }

  async function flushVoice() {
    if (voiceRequest || !voiceCapture || !voicePending.length || voiceStopped) return;
    const capture = voiceCapture;
    const controller = new AbortController();
    voiceRequest = controller;
    const frames = voicePending;
    voicePending = [];
    const body = new Uint8Array(frames.length * PHONE_FRAME * 2);
    frames.forEach((frame, i) => body.set(frame, i * PHONE_FRAME * 2));
    const sequence = voiceSequence;
    voiceSequence += frames.length;
    const timeout = win.setTimeout(() => {
      if (voiceRequest === controller) failVoice('Micro interrompu : liaison trop lente. Relancez l’écoute.');
    }, 500);
    try {
      const response = await win.fetch('/uplink', {
        method: 'POST', credentials: 'same-origin', body, signal: controller.signal,
        headers: {
          'Content-Type': 'application/octet-stream', 'X-Jarvis-Local': '1',
          'X-Jarvis-Client': phoneClient, 'X-Jarvis-Capture': capture,
          'X-Jarvis-Sequence': String(sequence),
        },
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        if (voiceRequest !== controller) return;
        // The VAD closes capture before the next snapshot reaches this page.
        // Frames already on the wire then expire normally; do not cancel STT.
        if (response.status === 409 && detail.error === 'stale_phone_capture') {
          blockedCapture = capture;
          resetVoiceTransport();
          capturing = false;
          uplink.reset();
          return;
        }
        throw new Error('phone_uplink_failed');
      }
    } catch {
      if (voiceRequest === controller) failVoice('Micro interrompu : transmission perdue. Relancez l’écoute.');
    } finally {
      win.clearTimeout(timeout);
      if (voiceRequest === controller) {
        voiceRequest = null;
        flushVoice();
      }
    }
  }

  function queueVoice(frames) {
    if (voicePending.length + frames.length > 10) {
      failVoice('Micro interrompu : liaison trop lente. Relancez l’écoute.');
      return;
    }
    voicePending.push(...frames);
    flushVoice();
  }

  function resetTalkTransport() {
    talkPending = [];
    talkRequest?.abort();
    talkPull?.abort();
    talkRequest = talkPull = null;
  }

  function syncPhone() {
    const id = phase === 'talking' && !doc.hidden ? snapshot?.crew?.call?.id || '' : '';
    // Voice PCM can arrive before its next snapshot. A new turn alone must
    // not stop audio already dequeued for that turn.
    const owned = snapshot?.phone_audio?.owned === true && !voiceStopped;
    const route = id ? `talk:${id}` : `voice:${snapshot?.session}:${owned}`;
    if (route !== audioRoute) {
      audioRoute = route;
      audioEpoch += 1;
      resetTalkTransport();
      resetVoiceTransport();
      cutSpeaker();
    }
    activeCall = id;
    if (id && (!phoneStream || phoneCtx?.state !== 'running')) {
      endTalk('Audio interrompu. Touchez l’écran puis relancez l’appel.');
      return;
    }
    const available = owned && phase === 'listening' ? snapshot?.phone_audio?.capture || '' : '';
    const nextCapture = available && available !== blockedCapture ? available : '';
    if (nextCapture !== voiceCapture) {
      voiceCapture = nextCapture;
      resetVoiceTransport();
    }
    capturing = !doc.hidden && !!phoneStream && phoneCtx?.state === 'running' &&
      (!!voiceCapture || !!activeCall);
    const capture = capturing ? (id ? route : voiceCapture) : '';
    if (capture !== captureRoute) {
      captureRoute = capture;
      uplink.reset();
    }
  }

  function endTalk(message = '') {
    const id = activeCall;
    if (!id) return;
    blockedCall = id;
    activeCall = '';
    capturing = false;
    audioEpoch += 1;
    resetTalkTransport();
    uplink.reset();
    cutSpeaker();
    fault = message;
    api('/talk/hangup', {}, {headers: {'X-Jarvis-Call': id}}).catch(() => {});
    render();
  }

  async function flushTalk() {
    if (talkRequest || !activeCall || !talkPending.length) return;
    const id = activeCall;
    const controller = new AbortController();
    talkRequest = controller;
    const frames = talkPending;
    talkPending = [];
    const body = new Uint8Array(frames.length * PHONE_FRAME * 2);
    frames.forEach((frame, i) => body.set(frame, i * PHONE_FRAME * 2));
    const timeout = win.setTimeout(() => {
      controller.abort();
      if (talkRequest === controller) endTalk('Liaison audio trop lente. Relancez l’appel.');
    }, TALK_TIMEOUT_MS);
    try {
      const response = await win.fetch('/talk/uplink', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream', 'X-Jarvis-Local': '1', 'X-Jarvis-Call': id,
        },
        credentials: 'same-origin', body, signal: controller.signal,
      });
      if (!response.ok) throw new Error('talk_uplink_failed');
    } catch {
      if (talkRequest === controller) endTalk('Liaison audio interrompue. Relancez l’appel.');
    } finally {
      win.clearTimeout(timeout);
      if (talkRequest === controller) {
        talkRequest = null;
        flushTalk();
      }
    }
  }

  function queueTalk(frames) {
    if (talkPending.length + frames.length > TALK_QUEUE_FRAMES) {
      endTalk('Liaison audio trop lente. Relancez l’appel.');
      return;
    }
    talkPending.push(...frames);
    flushTalk();
  }

  async function unlockAudio() {
    if (!AudioCtx) throw new Error('web_audio_unavailable');
    if (!phoneCtx) {
      phoneCtx = new AudioCtx({latencyHint: 'interactive'});
      phoneCtx.onstatechange = () => {
        if (phoneCtx.state !== 'running') {
          endTalk('Audio interrompu. Touchez l’écran puis relancez l’appel.');
          if (snapshot?.phone_audio?.owned) failVoice('Audio interrompu. Relancez l’écoute.');
          cutSpeaker();
        }
        render();
      };
    }
    if (phoneCtx.state !== 'running') await phoneCtx.resume();
    return phoneCtx;
  }

  async function armPhone() {
    const ctx = await unlockAudio();
    if (phoneStream) return;
    if (!nav.mediaDevices?.getUserMedia) throw new Error('phone_mic_unavailable');
    if (phoneArming) return phoneArming;
    phoneArming = openPhone(ctx);
    try {
      await phoneArming;
    } finally {
      phoneArming = null;
    }
  }

  async function openPhone(ctx) {
    const stream = await nav.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
      video: false,
    });
    phoneStream = stream;
    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(1024, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.onaudioprocess = (event) => {
      const path = phase === 'talking' ? '/talk/uplink' : phase === 'listening' ? '/uplink' : '';
      if (!capturing || !path) return;
      const input = event.inputBuffer.getChannelData(0);
      const frames = uplink.push(input, ctx.sampleRate);
      if (activeCall) {
        queueTalk(frames);
        return;
      }
      queueVoice(frames);
    };
    source.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
    phoneNode = node;
    phoneMute = mute;
    for (const track of stream.getTracks()) {
      track.addEventListener?.('ended', () => {
        endTalk('Micro interrompu. Touchez l’écran puis relancez l’appel.');
        if (snapshot?.phone_audio?.owned) failVoice('Micro interrompu. Relancez l’écoute.');
        stopPhone();
        render();
      });
    }
    render();
  }

  function stopPhone() {
    capturing = false;
    resetVoiceTransport();
    uplink.reset();
    if (phoneNode) {
      phoneNode.onaudioprocess = null;
      phoneNode.disconnect();
      phoneNode = null;
    }
    if (phoneMute) {
      phoneMute.disconnect();
      phoneMute = null;
    }
    if (phoneStream) {
      for (const track of phoneStream.getTracks()) track.stop();
      phoneStream = null;
    }
  }

  function floatFromI16(bytes) {
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    const out = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) out[i] = samples[i] / (samples[i] < 0 ? 0x8000 : 0x7fff);
    return out;
  }

  function cutSpeaker() {
    for (const source of speakerSources) {
      try {
        source.stop();
      } catch {
        // The buffer already reached its scheduled end.
      }
    }
    speakerSources.length = 0;
    speakerBytes = 0;
    speakerCursor = phoneCtx ? phoneCtx.currentTime : 0;
  }

  function speak(bytes, rate, talk = false) {
    if (!bytes.length || !rate) return;
    // Playback uses the same context unlocked when the user selects a seat.
    // Creating/resuming a second context here can hang forever on mobile.
    if (phoneCtx?.state !== 'running') throw new Error('phone_audio_suspended');
    if (talk && Math.max(0, speakerCursor - phoneCtx.currentTime) + bytes.length / (rate * 2) > 0.5) {
      throw new Error('talk_playback_delayed');
    }
    const floats = downsample(floatFromI16(bytes), rate, phoneCtx.sampleRate);
    if (!floats.length) return;
    const buffer = phoneCtx.createBuffer(1, floats.length, phoneCtx.sampleRate);
    buffer.getChannelData(0).set(floats);
    const source = phoneCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(phoneCtx.destination);
    source.onended = () => {
      const index = speakerSources.indexOf(source);
      if (index >= 0) speakerSources.splice(index, 1);
    };
    speakerSources.push(source);
    // Never start before audio already queued on this context.
    const start = Math.max(phoneCtx.currentTime + 0.015, speakerCursor);
    source.start(start);
    speakerCursor = start + buffer.duration;
    speakerBytes += bytes.length;
    speakerRate = rate;
  }

  async function pumpOut() {
    if (pumping || phoneCtx?.state !== 'running') return;
    pumping = true;
    try {
      while (pumping && !doc.hidden && phoneCtx?.state === 'running') {
        if (inCall(snapshot) && !activeCall) break;
        const id = activeCall;
        const talk = !!id;
        if (!talk && (snapshot?.phone_audio?.owned !== true || voiceStopped)) break;
        const epoch = audioEpoch;
        const controller = new AbortController();
        let timeout = 0;
        if (talk) {
          talkPull = controller;
          timeout = win.setTimeout(() => {
            controller.abort();
            if (talkPull === controller) endTalk('Liaison audio trop lente. Relancez l’appel.');
          }, TALK_TIMEOUT_MS);
        }
        let chunk = {pcm: '', rate: 0, done: true};
        try {
          chunk = await api(talk ? '/talk/pcm' : '/pcm', {}, {
            headers: talk ? {'X-Jarvis-Call': id} : {}, signal: controller.signal,
          });
          if (epoch !== audioEpoch || doc.hidden) continue;
          if (talk && chunk.call !== id) throw new Error('stale_call');
          if (chunk.pcm) speak(decodePcm(chunk.pcm), Number(chunk.rate) || speakerRate, talk);
        } catch {
          if (epoch !== audioEpoch) continue;
          if (talk) {
            endTalk('Liaison audio interrompue. Relancez l’appel.');
            break;
          }
          await new Promise((resolve) => win.setTimeout(resolve, 400));
          continue;
        } finally {
          win.clearTimeout(timeout);
          if (talkPull === controller) talkPull = null;
        }
        if (!talk && chunk.done && speakerBytes) {
          await api('/heard', {bytes: speakerBytes, done: true}).catch(() => {});
          speakerBytes = 0;
        }
        await new Promise((resolve) => win.setTimeout(resolve, talk ? 20 : chunk.pcm ? 20 : 80));
      }
    } finally {
      pumping = false;
    }
  }

  // ---- controls ---------------------------------------------------------------
  orb.addEventListener('click', async () => {
    if (phase === 'talking' || snapshot?.crew?.call) return;
    buzz(8);
    lockLandscape();
    if (!armed) {
      try {
        await armPhone();
        capturing = true;
      } catch {
        fault = 'Micro du téléphone refusé';
        render();
        return;
      }
    }
    const action = armed ? 'pause' : 'resume';
    if (action === 'pause') stopVoice();
    if (await send('/control', {action})) {
      if (action === 'resume') voiceStopped = false;
    }
  });
  $('cancel').addEventListener('click', () => {
    buzz(8);
    if (phase === 'talking') endTalk();
    else {
      stopVoice();
      send('/control', {action: 'cancel'});
    }
  });
  $('deck').addEventListener('click', async (event) => {
    const card = event.target.closest('[data-crew-card]');
    if (!card || card.disabled || !card.dataset.id) return;
    buzz(8);
    try {
      await armPhone();
    } catch {
      fault = 'Micro du téléphone refusé';
      render();
      return;
    }
    send('/talk/call', {peer: card.dataset.id});
  });
  $('gate').addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-claim]');
    if (!btn || btn.disabled) return;
    const id = btn.getAttribute('data-claim');
    if (!CREW.includes(id)) return;
    chosen = true;
    buzz(8);
    lockLandscape();
    try {
      await armPhone();
    } catch {
      chosen = false;
      fault = 'Micro du téléphone refusé';
      render();
      return;
    }
    if (!(await send('/crew/claim', {id}))) chosen = false;
  });

  const clear = $('clear');
  let holdTimer = 0, confirmTimer = 0;
  const commitClear = () => {
    buzz([12, 40, 12]);
    stopVoice();
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
        voiceStopped = false;
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

  const lockLandscape = () => {
    const api = win.screen?.orientation;
    if (typeof api?.lock !== 'function') return;
    const type = String(api.type || '');
    if (type.startsWith('landscape')) return;
    api.lock('landscape').catch(() => {});
  };

  doc.addEventListener('visibilitychange', () => {
    last = 0;
    if (doc.hidden) {
      endTalk('Appel interrompu pendant que l’application était masquée.');
      if (snapshot?.phone_audio?.owned && !voiceStopped) failVoice('Écoute interrompue pendant que l’application était masquée.');
      capturing = false;
      uplink.reset();
      cutSpeaker();
      audioEpoch += 1;
    }
    if (!doc.hidden) {
      lockLandscape();
      syncLock();
      pumpOut();
    }
    render();
  });
  reduced.addEventListener?.('change', schedule);
  win.addEventListener('resize', resize);
  win.screen?.orientation?.addEventListener?.('change', resize);
  doc.addEventListener(
    'pointerdown',
    () => {
      lockLandscape();
      if (snapshot?.crew?.me) armPhone().catch(() => {});
    },
    {passive: true},
  );
  new win.ResizeObserver(resize).observe(orb);
  lockLandscape();
  seedSuit();
  resize();
  tick();
  win.setInterval(tick, 1000);
  render();
  api('/bootstrap')
    .catch(() => {})
    .then(poll);
  if ('serviceWorker' in nav) nav.serviceWorker.register('/sw.js').catch(() => {});
}
