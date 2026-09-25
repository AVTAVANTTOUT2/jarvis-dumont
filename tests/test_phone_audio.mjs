import assert from 'node:assert/strict';
import test from 'node:test';
import {browser} from './phone_browser.mjs';

async function listening(page, capture = 'a'.repeat(32), owned = true) {
  page.state.state = 'listening';
  page.state.armed = true;
  page.state.microphone = 'open';
  page.state.phone_audio = {owned, capture, playback: ''};
  const before = page.requests.filter((r) => r.path === '/snapshot').length;
  await page.until(() => page.requests.filter((r) => r.path === '/snapshot').length > before);
}

const uploads = (page) => page.requests.filter((r) => r.path === '/uplink');

test('a second device stays silent even when the shared voice loop is listening', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page, '', false);
  const pulls = page.requests.filter((r) => r.path === '/pcm').length;
  page.capture();
  await page.flush();
  assert.equal(uploads(page).length, 0);
  assert.equal(page.requests.filter((r) => r.path === '/pcm').length, pulls);
});

test('Jarvis receives one ordered upload at a time, tagged with its acquisition', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page);
  page.capture(0.25);
  page.capture(0.5);
  page.capture(-0.5);
  await page.flush();
  assert.equal(uploads(page).length, 1);
  const first = uploads(page)[0];
  assert.equal(first.options.headers['X-Jarvis-Capture'], 'a'.repeat(32));
  assert.equal(first.options.headers['X-Jarvis-Sequence'], '0');
  assert.equal(first.options.headers['X-Jarvis-Client'], '11111111-1111-1111-1111-111111111111');
  first.reply();
  await page.flush();
  const second = uploads(page)[1];
  assert.equal(second.options.headers['X-Jarvis-Sequence'], '1');
  const samples = uploads(page).flatMap((r) => [...new Int16Array(r.options.body.buffer)]);
  assert.deepEqual(samples.filter((v, i) => i === 0 || v !== samples[i - 1]), [8192, 16384, -16384]);
});

test('new capture discards old queued PCM and restarts sequence at zero', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page);
  page.capture(0.25);
  page.capture(0.5);
  await page.flush();
  const first = uploads(page)[0];
  await listening(page, 'b'.repeat(32));
  assert.equal(first.aborted, true);
  page.capture(-0.5);
  await page.flush();
  const second = uploads(page).at(-1);
  assert.equal(second.options.headers['X-Jarvis-Capture'], 'b'.repeat(32));
  assert.equal(second.options.headers['X-Jarvis-Sequence'], '0');
  assert.ok([...new Int16Array(second.options.body.buffer)].every((v) => v === -16384));
});

test('network congestion visibly pauses Jarvis instead of accumulating old speech', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page);
  for (let i = 0; i < 25; i++) page.capture();
  await page.flush();
  assert.equal(uploads(page).length, 1);
  assert.equal(page.requests.filter((r) => r.path === '/control' && JSON.parse(r.options.body).action === 'pause').length, 1);
  assert.equal(page.element('fault').hidden, false);
});

test('normal VAD closure discards late frames without cancelling transcription', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page);
  page.capture();
  page.capture();
  uploads(page)[0].reply({error: 'stale_phone_capture'}, 409);
  await page.flush();
  page.capture();
  assert.equal(uploads(page).length, 1);
  assert.equal(page.requests.filter((r) => r.path === '/control').length, 0);
  await listening(page, 'b'.repeat(32));
  page.capture();
  await page.flush();
  assert.equal(uploads(page).length, 2);
  assert.equal(uploads(page)[1].options.headers['X-Jarvis-Capture'], 'b'.repeat(32));
});

test('an interrupted microphone invalidates the take even if the context resumes', async () => {
  const page = browser();
  await page.chooseProfile();
  await listening(page);
  page.capture();
  uploads(page)[0].reply();
  await page.flush();
  const context = page.contexts[0];
  context.state = 'interrupted';
  context.onstatechange();
  context.state = 'running';
  context.onstatechange();
  page.capture();
  await page.flush();
  assert.equal(uploads(page).length, 1, 'speech after interruption cannot be spliced into the old take');
  assert.equal(page.requests.filter((r) => r.path === '/control' && JSON.parse(r.options.body).action === 'pause').length, 1);
  assert.equal(page.element('fault').hidden, false);
});
