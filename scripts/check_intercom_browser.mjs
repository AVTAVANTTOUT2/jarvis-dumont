// Real Chromium + real LocalUI/TalkBridge, fake microphone and muted output.
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

class Voice:
    audio = None
    error = None
    state = "paused"
    def snapshot(self):
        return {"session": "test", "turn": "test", "state": "paused", "armed": False,
                "microphone": "closed", "level": 0, "error": None}
    async def control(self, action):
        pass

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
      window.audioChecks = {starts: 0, maxPending: 0, pending: 0, failures: 0};
      const original = AudioContext.prototype.createBufferSource;
      AudioContext.prototype.createBufferSource = function(...args) {
        const source = original.apply(this, args);
        const start = source.start.bind(source);
        source.start = (...when) => { window.audioChecks.starts += 1; return start(...when); };
        return source;
      };
      const fetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const upload = args[0] === '/talk/uplink';
        if (upload) {
          window.audioChecks.pending += 1;
          window.audioChecks.maxPending = Math.max(window.audioChecks.maxPending, window.audioChecks.pending);
        }
        try {
          const response = await fetch(...args);
          if (String(args[0]).startsWith('/talk/') && !response.ok) window.audioChecks.failures += 1;
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
  await first.getByRole('button', {name: 'Appeler Aymen', exact: true}).click();
  await Promise.all(pages.map((page) => page.waitForFunction(() =>
    document.getElementById('hud').dataset.phase === 'talking' && window.audioChecks.starts >= 50,
  )));
  for (const page of pages) {
    const check = await page.evaluate(() => window.audioChecks);
    assert.equal(check.maxPending, 1, 'one microphone upload in flight');
    assert.equal(check.failures, 0, 'no failed call request during duplex audio');
    assert.equal(await page.locator('#fault').isVisible(), false);
  }
  await first.getByRole('button', {name: 'Raccrocher', exact: true}).click();
  await Promise.all(pages.map((page) => page.waitForFunction(() =>
    document.getElementById('hud').dataset.phase === 'paused',
  )));
  const before = await Promise.all(pages.map((page) => page.evaluate(() => window.audioChecks.starts)));
  await second.getByRole('button', {name: 'Appeler Elias', exact: true}).click();
  await Promise.all(pages.map((page, i) => page.waitForFunction((starts) =>
    document.getElementById('hud').dataset.phase === 'talking' && window.audioChecks.starts >= starts + 20,
    before[i],
  )));
  await second.getByRole('button', {name: 'Raccrocher', exact: true}).click();
  assert.deepEqual(errors, [], 'no JavaScript exception on either device');
  console.log('PASS: duplex synthetic audio with 60/100 ms uplink delay, ordered uploads, hangup and reverse call; portrait + landscape.');
  console.log('Hardware microphone, speaker and mobile Safari: NOT RUN.');
} finally {
  await browser?.close();
  server.kill('SIGTERM');
}
