import assert from 'node:assert/strict';
import test from 'node:test';
import {browser, CALL} from './phone_browser.mjs';

function remotePcm() {
  const bytes = Buffer.alloc(640);
  for (let offset = 0; offset < bytes.length; offset += 2) bytes.writeInt16LE(16384, offset);
  return {pcm: bytes.toString('base64'), rate: 16000, done: false, call: CALL.id};
}

function sentSamples(body) {
  const view = body instanceof ArrayBuffer
    ? new DataView(body)
    : new DataView(body.buffer, body.byteOffset, body.byteLength);
  return Array.from({length: view.byteLength / 2}, (_, index) => view.getInt16(index * 2, true));
}

test('choosing a profile unlocks playback before the first incoming call PCM', async () => {
  const page = browser();
  await page.chooseProfile();
  assert.ok(page.contexts.length > 0, 'a context is unlocked by the profile gesture');
  assert.ok(page.contexts.every((context) => context.createdDuringGesture && context.state === 'running'));
  const beforeAudio = page.contexts.length;
  const incoming = await page.receiveCall();
  incoming.reply(remotePcm());
  await page.flush();
  assert.equal(page.audible().length, 1, 'the first remote PCM plays without another gesture');
  assert.equal(page.contexts.length, beforeAudio, 'receiving PCM must reuse already unlocked audio');
  assert.equal(page.audible()[0].context.state, 'running');
  assert.equal(incoming.options.headers['X-Jarvis-Call'], CALL.id);
});

test('slow intercom uploads keep one request in flight instead of sending every frame', async () => {
  const page = browser();
  await page.chooseProfile();
  await page.receiveCall();
  page.capture(0.25);
  await page.flush();
  assert.equal(page.uploads().length, 1, 'the first complete frame starts an upload');
  for (const value of [0.5, -0.5, -0.25]) page.capture(value);
  await page.flush();
  assert.equal(page.uploads().length, 1, 'blocked network must not fan out audio requests');
  const first = page.uploads()[0];
  assert.equal(first.options.headers['X-Jarvis-Call'], CALL.id);
  assert.ok(first.options.body.byteLength >= 640);
  assert.equal(first.options.body.byteLength % 640, 0, 'uploads contain whole 20 ms PCM frames');
  assert.deepEqual(sentSamples(first.options.body), Array(320).fill(8192));
  first.reply();
  await page.flush();
  assert.equal(page.uploads().length, 2, 'buffered speech advances after the preceding upload completes');
  // The transport may batch queued frames or send them individually.
  for (let i = 0; i < 3; i++) {
    assert.equal(page.uploads().filter((request) => !request.resolved).length, 1);
    const current = page.uploads().at(-1);
    assert.equal(current.options.headers['X-Jarvis-Call'], CALL.id);
    assert.equal(current.options.body.byteLength % 640, 0);
    if (page.uploads().reduce((bytes, request) => bytes + request.options.body.byteLength, 0) === 2560) break;
    current.reply();
    await page.flush();
  }
  const sent = page.uploads().flatMap(({options}) => sentSamples(options.body));
  assert.equal(sent.length, 1280, 'four complete 20 ms frames are sent');
  assert.deepEqual(
    sent.filter((value, index) => index === 0 || value !== sent[index - 1]),
    [8192, 16384, -16384, -8192],
    'buffered speech keeps its original amplitude and order',
  );
});

test('prolonged uplink congestion ends the call visibly without accumulating requests', async () => {
  const page = browser();
  await page.chooseProfile();
  await page.receiveCall();
  for (let i = 0; i < 25; i++) page.capture();
  await page.flush();
  assert.equal(page.uploads().length, 1, 'congestion must not fan out pending audio uploads');
  assert.equal(page.requests.filter((request) => request.path === '/talk/hangup').length, 1);
  assert.equal(page.element('fault').hidden, false, 'the interruption is visible');
  assert.ok(page.element('fault-code').textContent.length > 0);
  const first = page.uploads()[0];
  first.reply();
  await page.flush();
  assert.equal(page.uploads().length, 1, 'no old queued speech is sent after congestion hangup');
});

test('a PCM response arriving after local hangup cannot restart playback', async () => {
  // An already permissive browser isolates call invalidation from autoplay.
  const page = browser({autoplayAllowed: true});
  await page.chooseProfile();
  const incoming = await page.receiveCall();
  await page.hangup();
  assert.ok(page.requests.some((request) => request.path === '/talk/hangup'));
  incoming.reply(remotePcm());
  await page.flush();
  assert.equal(page.audible().length, 0, 'late PCM from the hung-up call must be discarded');
});

test('hangup discards PCM whose response has arrived but playback has not started', async () => {
  const page = browser({autoplayAllowed: true});
  await page.chooseProfile();
  const incoming = await page.receiveCall();
  // The fetch promise is already fulfilled: aborting alone cannot retract it.
  // Raccrocher must invalidate its pending async continuation before playback.
  incoming.reply(remotePcm());
  await page.hangup();
  await page.flush();
  assert.equal(page.audible().length, 0, 'an already-resolved response cannot outlive its call');
});

test('voice PCM received before its turn snapshot keeps playing when that snapshot arrives', async () => {
  const page = browser();
  await page.chooseProfile();
  page.setSnapshot({phone_audio: {owned: true, capture: '', playback: 'reply-1'}});
  await page.until(() => page.requests.some((r) => r.path === '/pcm'));
  const bytes = Buffer.alloc(32000);
  for (let offset = 0; offset < bytes.length; offset += 2) bytes.writeInt16LE(16384, offset);
  page.queueVoicePcm({pcm: bytes.toString('base64'), rate: 16000, done: false});
  await page.until(() => page.audible().length === 1);
  const {source} = page.audible()[0];
  assert.equal(source.buffer.duration, 1, 'the received answer lasts longer than one snapshot poll');
  assert.equal(source.stopped, false);
  page.setSnapshot({turn: 't1', state: 'speaking'});
  await page.until(() => page.element('hud').dataset.phase === 'speaking');
  assert.equal(source.stopped, false, 'the new turn snapshot must preserve PCM already removed from the server');
  assert.equal(page.audible().length, 1, 'the answer continues without being replayed');
});

test('an intercom PCM request that remains silent for 500 ms ends the call visibly', async () => {
  const page = browser();
  await page.chooseProfile();
  const incoming = await page.receiveCall();
  await page.advance(499);
  assert.equal(page.requests.filter((request) => request.path === '/talk/hangup').length, 0);
  await page.advance(1);
  assert.equal(incoming.aborted, true, 'the stalled audio request is cancelled at its deadline');
  const hangups = page.requests.filter((request) => request.path === '/talk/hangup');
  assert.equal(hangups.length, 1);
  assert.equal(hangups[0].options.headers['X-Jarvis-Call'], CALL.id);
  assert.equal(page.element('fault').hidden, false);
  assert.ok(page.element('fault-code').textContent.length > 0);
  incoming.reply(remotePcm());
  await page.flush();
  assert.equal(page.audible().length, 0, 'a response past the deadline cannot restart audio');
});

test('an incoming call with an already interrupted audio context fails visibly', async () => {
  const page = browser();
  await page.chooseProfile();
  const context = page.contexts[0];
  context.state = 'interrupted';
  context.onstatechange?.();
  await page.flush();
  const snapshots = page.requests.filter((request) => request.path === '/snapshot').length;
  page.setSnapshot({crew: {me: 'elias', seats: {}, call: {...CALL}}});
  await page.until(() => page.requests.filter((request) => request.path === '/snapshot').length > snapshots);
  assert.equal(page.element('fault').hidden, false, 'an unusable incoming audio path must be reported');
  assert.ok(page.element('fault-code').textContent.length > 0);
  assert.equal(page.requests.filter((request) => request.path === '/talk/hangup').length, 1);
  page.capture();
  assert.equal(page.uploads().length, 0, 'an interrupted context cannot advertise working capture');
  assert.equal(page.audible().length, 0);
});
