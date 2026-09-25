import assert from 'node:assert/strict';
import test from 'node:test';
import {MODE_FRAMES, resolvePreset} from '../src/jarvis_office/static/dashboard/thinking-orbs.js';
import {
  CREW,
  PHASES,
  clockOf,
  createTranscript,
  crewOthers,
  inCall,
  seatTaken,
  loudness,
  missionTime,
  phaseOf,
  refine,
  signalBars,
  sparkFill,
  sparkPoints,
  suitLoad,
  suitSample,
  createUplink,
  decodePcm,
  downsample,
  pcm16,
  PHONE_FRAME,
  track,
} from '../src/jarvis_office/wrist.js';

const snap = (fields) => ({
  session: 's1',
  turn: 't0',
  state: 'paused',
  transcription: '',
  response: '',
  ...fields,
});

function replay(snapshots) {
  let memo = createTranscript();
  const ops = [];
  for (const snapshot of snapshots) {
    const result = track(memo, snapshot);
    memo = result.memo;
    ops.push(...result.ops);
  }
  return {memo, ops};
}

test('every voice state has a shipped orb and a tone; the link overrides it', () => {
  for (const [phase, spec] of Object.entries(PHASES)) {
    assert.ok(resolvePreset(spec.orb, 64).mode, phase);
    assert.ok(['idle', 'live', 'hot', 'fault'].includes(spec.tone), phase);
  }
  for (const state of ['starting', 'paused', 'listening', 'transcribing', 'responding', 'speaking', 'stopping', 'error']) {
    assert.equal(phaseOf({state}, true), state);
  }
  assert.equal(phaseOf(null, null), 'boot');
  assert.equal(phaseOf({state: 'mystery'}, true), 'boot');
  assert.equal(phaseOf({state: 'listening'}, false), 'offline');
  assert.equal(PHASES.listening.tone, 'hot');
  assert.equal(PHASES.talking.tone, 'hot');
  assert.equal(PHASES.offline.tone, 'fault');
});

test('talking wins over paused when this seat is in the call', () => {
  const call = {a: 'elias', b: 'aymen'};
  assert.equal(
    phaseOf({state: 'paused', crew: {me: 'elias', call, seats: {}}}, true),
    'talking',
  );
  assert.equal(
    phaseOf({state: 'paused', crew: {me: 'faiz', call, seats: {}}}, true),
    'paused',
  );
  assert.deepEqual(crewOthers('elias'), ['aymen', 'evann', 'alexandre', 'faiz']);
  assert.equal(CREW.length, 5);
  assert.equal(inCall({crew: {me: 'elias', call}}, 'elias'), true);
  assert.equal(inCall({crew: {me: 'elias', call: null}}, 'elias'), false);
});

test('a seat is taken only when someone else is online', () => {
  const seats = {elias: {online: true}, aymen: {online: false}, evann: {online: true}};
  assert.equal(seatTaken(seats, 'elias', ''), true);
  assert.equal(seatTaken(seats, 'elias', 'elias'), false);
  assert.equal(seatTaken(seats, 'aymen', ''), false);
  assert.equal(seatTaken(seats, 'faiz', ''), false);
  assert.equal(seatTaken(seats, '', ''), false);
  assert.equal(seatTaken(seats, 'evann', 'elias'), true);
});

test('a streamed answer grows one exchange in place instead of stacking copies', () => {
  const question = 'Jarvis, quelle heure est-il ?';
  const {ops, memo} = replay([
    snap({state: 'listening', turn: 't1'}),
    snap({state: 'transcribing', turn: 't1'}),
    snap({state: 'responding', turn: 't1', transcription: question}),
    snap({state: 'responding', turn: 't1', transcription: question, response: 'Il est'}),
    snap({state: 'speaking', turn: 't1', transcription: question, response: 'Il est midi.'}),
    snap({state: 'listening', turn: 't2', transcription: question, response: 'Il est midi.'}),
    snap({state: 'transcribing', turn: 't2', transcription: question, response: 'Il est midi.'}),
  ]);
  assert.deepEqual(ops.filter((op) => op.op === 'open'), [{op: 'open', id: 1, user: question}]);
  assert.deepEqual(ops.filter((op) => op.op === 'bot').map((op) => op.text), ['Il est', 'Il est midi.']);
  assert.deepEqual(ops.filter((op) => op.op === 'live').map((op) => op.live), [true, false]);
  assert.equal(memo.exchange.bot, 'Il est midi.');
});

test('a repeated sentence on a new turn is a new exchange; Effacer resets the log', () => {
  const question = 'Jarvis, encore ?';
  const {ops} = replay([
    snap({state: 'responding', turn: 't1', transcription: question}),
    snap({state: 'listening', turn: 't2', transcription: question, response: 'Oui.'}),
    snap({state: 'responding', turn: 't3', transcription: question}),
    snap({session: 's2', turn: 't3'}),
  ]);
  assert.deepEqual(
    ops.map((op) => op.op),
    ['open', 'live', 'bot', 'live', 'open', 'live', 'reset'],
  );
  assert.deepEqual(
    ops.filter((op) => op.op === 'open').map((op) => op.id),
    [1, 2],
  );
});

test('history survives a reload and a turn missed between polls still shows', () => {
  const {ops} = replay([
    snap({transcription: 'A', response: 'a'}),
    snap({transcription: 'A', response: 'a'}),
    snap({transcription: 'B', response: 'b'}),
  ]);
  assert.deepEqual(
    ops.map((op) => `${op.op}:${op.user ?? op.text}`),
    ['open:A', 'bot:a', 'open:B', 'bot:b'],
  );
  assert.deepEqual(replay([snap({}), snap({state: 'listening'})]).ops, []);
});

test('mic RMS reads on a dBFS scale, clocks and link bars are formatted', () => {
  assert.equal(loudness(0), 0);
  assert.equal(loudness(-0.2), 0);
  assert.equal(loudness('noise'), 0);
  assert.equal(loudness(1e-4), 0);
  assert.equal(loudness(1), 1);
  assert.ok(Math.abs(loudness(10 ** (-30 / 20)) - 0.52) < 1e-9);
  assert.equal(missionTime(0), 'T+00:00:00');
  assert.equal(missionTime(-5000), 'T+00:00:00');
  assert.equal(missionTime(3723999), 'T+01:02:03');
  assert.equal(missionTime(100 * 3600 * 1000), 'T+100:00:00');
  const date = new Date(Date.UTC(2026, 8, 22, 9, 5, 7));
  const time = clockOf(date);
  assert.equal(time.gmt, '09:05');
  assert.equal(time.s, '07');
  assert.match(time.hm, /^\d\d:\d\d$/);
  assert.deepEqual([NaN, 12, 120, 900].map(signalBars), [0, 3, 2, 1]);
});

test('phone uplink packs exact 20 ms 16 kHz frames after downsample', () => {
  const uplink = createUplink();
  const fortyEight = new Float32Array(48000 * 0.02);
  fortyEight[0] = 0.5;
  const frames = uplink.push(fortyEight, 48000);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].length, PHONE_FRAME * 2);
  const twice = downsample(new Float32Array([0, 1, 0, 1]), 2, 1);
  assert.equal(twice.length, 2);
  assert.ok(Math.abs(twice[0]) < 1e-9);
  const raw = pcm16(new Float32Array([0, 1, -1]));
  const encoded = Buffer.from(raw).toString('base64');
  assert.deepEqual(Array.from(decodePcm(encoded)), Array.from(raw));
});

test('suit telemetry is a deterministic RP waveform and sparkline stays in the viewBox', () => {
  assert.equal(suitSample('hr', 0, 0), 72);
  assert.ok(suitSample('hr', 0, 1) > suitSample('hr', 0, 0));
  assert.ok(suitSample('o2', 0, 1) < suitSample('o2', 0, 0));
  assert.equal(suitLoad('paused', 0), 0.08);
  assert.equal(suitLoad('listening', 1), 1);
  assert.equal(sparkPoints([]), '');
  assert.equal(sparkPoints([4], 100, 20, 0), '50.00,10.00');
  assert.equal(sparkPoints([1, 3], 100, 20, 0), '0.00,20.00 100.00,0.00');
  assert.equal(sparkPoints([0, 50], 100, 20, 0, {lo: 0, hi: 100}), '0.00,20.00 100.00,10.00');
  assert.equal(sparkFill([1, 3], 100, 20, 0), '0.00,20.00 0.00,20.00 100.00,0.00 100.00,20.00');
  for (const point of sparkPoints([1, 2, 1.4, 2.8], 120, 36).split(' ')) {
    const [x, y] = point.split(',').map(Number);
    assert.ok(x >= 0 && x <= 120 && y >= 0 && y <= 36);
  }
});

test('the hero orb doubles the preset density without touching the cached preset', () => {
  const base = resolvePreset('listening', 64);
  const before = structuredClone(base.opts);
  const fine = refine(base.opts, 2, 0.8);
  assert.deepEqual(base.opts, before);
  assert.equal(fine.rings, Math.round(base.opts.rings * Math.SQRT2));
  assert.equal(fine.lonDensity, Math.round(base.opts.lonDensity * Math.SQRT2));
  assert.ok(Math.abs(fine.rBase - base.opts.rBase * 0.8) < 1e-12);
  assert.equal(refine({nodeN: 40, ghostN: 0}, 2, 1).nodeN, 80);
  assert.equal(refine({nodeN: 40, ghostN: 0}, 2, 1).ghostN, 0);
  for (const orb of new Set(Object.values(PHASES).map((spec) => spec.orb))) {
    const preset = resolvePreset(orb, 64);
    const frame = MODE_FRAMES[preset.mode](180, 1.5 * preset.speed, refine(preset.opts, 2, 0.8));
    assert.ok(frame.dots.length > 0 && frame.dots.length <= 1600, orb);
    assert.ok(frame.lines.length <= 900, orb);
    assert.ok(frame.dots.every((dot) => Number.isFinite(dot.x) && Number.isFinite(dot.y) && dot.r > 0), orb);
  }
});
