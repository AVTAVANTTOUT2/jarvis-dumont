// Real Chromium + real LocalUI/WebPhoneAudio, fake microphone and no STT/LLM.
// No engines, provider requests, private configuration or hardware capture.
// PLAYWRIGHT_MODULE may point to an existing Playwright installation.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('..', import.meta.url));
const fixture = String.raw`
import asyncio
from jarvis_office.local_ui import LocalUI
from jarvis_office.web_audio import WebPhoneAudio
from types import SimpleNamespace
import uuid

class Voice:
    error = None
    state = "paused"
    armed = False
    frames = 0
    def __init__(self):
        worker = SimpleNamespace(event=lambda event: None, feed_remote=self.feed)
        self.audio = WebPhoneAudio(worker)
    def feed(self, token, pcm, received):
        assert token == self.audio.token
        assert len(pcm) == 640
        self.frames += 1
    def snapshot(self):
        return {"session": "test", "turn": "test", "state": self.state, "armed": self.armed,
                "microphone": "open" if self.armed else "closed", "level": 0, "error": None,
                "metrics": {"frames": self.frames}}
    async def control(self, action):
        self.armed = action == "resume"
        self.state = "listening" if self.armed else "paused"
        self.audio.token = ""
        if self.armed:
            self.audio._on_event({"event":"listening", "data":{"device":{"ingress_token":uuid.uuid4().hex}}})

async def main():
    ui = LocalUI(Voice(), 0)
    await ui.start()
    port = ui.server.sockets[0].getsockname()[1]
    ui.host = f"127.0.0.1:{port}"
    ui.origin = f"http://{ui.host}"
    ui.by_host = {ui.host: ui.origin}
    print(port, flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await ui.close()

asyncio.run(main())
`;
const server = spawn(process.env.PYTHON || `${root}/.venv/bin/python`, ['-c', fixture], {
  cwd: root, stdio: ['ignore', 'pipe', 'pipe'],
});
let browser;
try {
  const port = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('fixture_start_timeout')), 5000);
    server.once('error', reject);
    server.once('exit', () => reject(new Error('fixture_start_failed')));
    server.stdout.once('data', (data) => {
      clearTimeout(timeout);
      const value = Number(String(data).trim());
      if (!Number.isInteger(value) || value < 1) reject(new Error('fixture_port_invalid'));
      else resolve(value);
    });
  });
  browser = await chromium.launch({headless: true, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    '--autoplay-policy=document-user-activation-required', '--mute-audio',
  ]});
  const errors = [];
  const pages = [];
  for (const [index, viewport] of [{width: 844, height: 390}, {width: 390, height: 844}].entries()) {
    const context = await browser.newContext({
      viewport, permissions: ['microphone'], serviceWorkers: 'block',
    });
    // Asymmetric network delays expose overlapping requests and growing queues
    // that a zero-latency localhost run would miss.
    await context.route('**/talk/uplink', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, index ? 100 : 60));
      await route.continue().catch(() => {}); // Hangup may abort an in-flight upload.
    });
    await context.addInitScript(() => {
      window.audioChecks = {starts: 0, uploads: 0, captures: [], maxPending: 0, pending: 0, failures: 0};
      const original = AudioContext.prototype.createBufferSource;
      AudioContext.prototype.createBufferSource = function(...args) {
        const source = original.apply(this, args);
        const start = source.start.bind(source);
        source.start = (...when) => { window.audioChecks.starts += 1; return start(...when); };
        return source;
      };
      const fetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const upload = args[0] === '/uplink';
        if (upload) {
          window.audioChecks.uploads += 1;
          window.audioChecks.captures.push(args[1].headers['X-Jarvis-Capture']);
          window.audioChecks.pending += 1;
          window.audioChecks.maxPending = Math.max(window.audioChecks.maxPending, window.audioChecks.pending);
        }
        try {
          const response = await fetch(...args);
          if (upload && !response.ok) window.audioChecks.failures += 1;
          return response;
        } finally {
          if (upload) window.audioChecks.pending -= 1;
        }
      };
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.name));
    await page.goto(`http://127.0.0.1:${port}/`);
    pages.push(page);
  }
  const [first, second] = pages;
  await first.getByRole('button', {name: 'Elias', exact: true}).click();
  await second.getByRole('button', {name: 'Aymen', exact: true}).click();
  await first.getByRole('button', {name: 'Écouter', exact: true}).click();
  await first.waitForFunction(() => window.audioChecks.uploads >= 20);
  await second.waitForFunction(() => document.getElementById('notice').textContent.includes('autre appareil'));
  const firstStats = await first.evaluate(() => window.audioChecks);
  assert.equal(firstStats.maxPending, 1);
  assert.equal(firstStats.failures, 0);
  assert.equal(await second.evaluate(() => window.audioChecks.uploads), 0, 'other microphone stays silent');
  await first.getByRole('button', {name: 'Pause', exact: true}).click();
  await second.waitForFunction(() => !document.getElementById('orb').disabled);
  await second.getByRole('button', {name: 'Écouter', exact: true}).click();
  await second.waitForFunction(() => window.audioChecks.uploads >= 20);
  const secondStats = await second.evaluate(() => window.audioChecks);
  assert.equal(secondStats.maxPending, 1);
  assert.equal(secondStats.failures, 0);
  assert.notEqual(firstStats.captures[0], secondStats.captures[0], 'new owner has a new capture');
  const stopped = await first.evaluate(() => window.audioChecks.uploads);
  await second.waitForFunction((before) => window.audioChecks.uploads > before + 10, secondStats.uploads);
  assert.equal(await first.evaluate(() => window.audioChecks.uploads), stopped);
  await second.getByRole('button', {name: 'Pause', exact: true}).click();
  assert.deepEqual(errors, [], 'no JavaScript exception on either device');
  console.log('PASS: two isolated browsers, one microphone owner, ordered real HTTP uploads, pause and ownership transfer; portrait + landscape.');
  console.log('Human voice, STT, LLM, hardware playback and mobile Safari: NOT RUN.');
} finally {
  await browser?.close();
  server.kill('SIGTERM');
}
