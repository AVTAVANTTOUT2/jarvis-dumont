import assert from 'node:assert/strict';
import test from 'node:test';
import {MODE_FRAMES, resolvePreset} from '../src/jarvis_office/static/dashboard/thinking-orbs.js';
import {
  PHASES,
  clockOf,
  createTranscript,
  loudness,
  missionTime,
  phaseOf,
  refine,
  signalBars,
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
  assert.equal(PHASES.offline.tone, 'fault');
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
