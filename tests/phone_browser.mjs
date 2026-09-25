import assert from 'node:assert/strict';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {mount} from '../src/jarvis_office/wrist.js';

export const CALL = {id: 'call-1', a: 'elias', b: 'aymen'};

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

// Only browser boundaries are simulated: mount performs the real capture
// framing, PCM conversion, request scheduling and call-state transitions.
export function browser({autoplayAllowed = false} = {}) {
  let now = 0, serial = 0, gesture = false;
  const timers = new Map(), elements = new Map(), contexts = [], processors = [];
  const requests = [], starts = [], errors = [], voicePcm = [];
  const state = {
    session: 's1', turn: 't0', state: 'paused', armed: false,
    microphone: 'closed', transcription: '', response: '',
    crew: {me: '', seats: {elias: {online: false}, aymen: {online: true}}, call: null},
  };

  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const listeners = new Map(), attributes = new Map(), classes = new Set();
    const el = {
      dataset: {}, hidden: false, disabled: false, textContent: '',
      addEventListener(type, listener) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(listener);
      },
      dispatch(type, event = {}) {
        return Promise.all((listeners.get(type) || []).map((listener) => listener(event)));
      },
      getAttribute: (name) => attributes.get(name) ?? null,
      setAttribute: (name, value) => attributes.set(name, value),
      getBoundingClientRect: () => ({width: 0, height: 0}),
      querySelector: () => null,
      querySelectorAll: () => [],
      focus() {},
      classList: {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      },
      getContext: () => ({setTransform() {}}),
    };
    elements.set(id, el);
    return el;
  }

  function timer(callback, delay, interval = false) {
    const id = ++serial;
    timers.set(id, {callback, at: now + delay, delay, interval});
    return id;
  }

  class AudioContext {
    constructor() {
      this.createdDuringGesture = gesture;
      this.state = gesture || autoplayAllowed ? 'running' : 'suspended';
      this.sampleRate = 48000;
      this.currentTime = 0;
      this.destination = {};
      contexts.push(this);
    }
    resume() {
      if (gesture || autoplayAllowed) this.state = 'running';
      // Browsers leave resume pending when autoplay policy blocks the context.
      return this.state === 'running' ? Promise.resolve() : new Promise(() => {});
    }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createScriptProcessor() {
      const node = {onaudioprocess: null, connect() {}, disconnect() {}};
      processors.push(node);
      return node;
    }
    createGain() { return {gain: {value: 1}, connect() {}, disconnect() {}}; }
    createBuffer(channels, length, rate) {
      const samples = new Float32Array(length);
      return {duration: length / rate, getChannelData: () => samples};
    }
    createBufferSource() {
      const context = this;
      return {
        buffer: null, stopped: false, connect() {}, disconnect() {},
        start(when) { starts.push({source: this, context, when}); },
        stop() { this.stopped = true; },
      };
    }
  }

  const response = (body) => ({ok: true, status: 200, json: async () => body});
  function fetch(path, options = {}) {
    const request = {path, options};
    requests.push(request);
    if (path === '/talk/pcm' || path === '/talk/uplink' || path === '/uplink') {
      const pending = deferred();
      request.reply = (body = {}, status = 200) => {
        request.resolved = true;
        pending.resolve({...response(body), status, ok: status >= 200 && status < 300});
      };
      options.signal?.addEventListener('abort', () => {
        request.aborted = true;
        pending.reject(new DOMException('Aborted', 'AbortError'));
      }, {once: true});
      return pending.promise;
    }
    if (path === '/snapshot') return Promise.resolve(response(structuredClone(state)));
    if (path === '/crew/claim') state.crew.me = JSON.parse(options.body).id;
    if (path === '/talk/hangup') state.crew.call = null;
    if (path === '/pcm') {
      return Promise.resolve(response(voicePcm.shift() || {pcm: '', rate: 16000, done: true}));
    }
    return Promise.resolve(response({}));
  }

  const track = {stopped: false, stop() { this.stopped = true; }};
  const win = {
    AudioContext, AbortController, fetch, crypto: {randomUUID: () => '11111111-1111-1111-1111-111111111111'},
    navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => [track]})}},
    performance: {now: () => now}, innerWidth: 0, innerHeight: 0,
    matchMedia: () => ({matches: true, addEventListener() {}}),
    addEventListener() {},
    setTimeout: (callback, delay) => timer(callback, delay),
    clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, delay) => timer(callback, delay, true),
    clearInterval: (id) => timers.delete(id),
    ResizeObserver: class { observe() {} },
  };
  const doc = {
    defaultView: win, hidden: false, getElementById: element,
    querySelectorAll: () => [], addEventListener() {},
  };
  // Zero-size canvases mean the visual engine is never called in these tests.
  mount(doc, {});

  async function flush() {
    await nextTurn();
    if (errors.length) throw errors.shift();
  }

  const nextTimer = () => [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];

  async function stepTimer() {
    const pending = nextTimer();
    assert.ok(pending, 'a browser timer must make the expected progress');
    const [id, task] = pending;
    timers.delete(id);
    now = task.at;
    if (task.interval) timers.set(id, {...task, at: now + task.delay});
    try { Promise.resolve(task.callback()).catch((error) => errors.push(error)); }
    catch (error) { errors.push(error); }
    await flush();
  }

  async function until(predicate) {
    await flush();
    for (let i = 0; i < 100 && !predicate(); i++) await stepTimer();
    assert.ok(predicate(), 'expected browser event was not reached');
  }

  async function advance(ms) {
    const end = now + ms;
    await flush();
    for (let i = 0; nextTimer()?.[1].at <= end; i++) {
      assert.ok(i < 100, 'browser timers must not spin without advancing time');
      await stepTimer();
    }
    now = end;
    await flush();
  }

  async function chooseProfile() {
    await flush();
    const button = element('claim-elias');
    button.setAttribute('data-claim', 'elias');
    button.closest = () => button;
    gesture = true;
    const clicking = element('gate').dispatch('click', {target: button});
    gesture = false;
    await clicking;
    await flush();
    assert.equal(state.crew.me, 'elias', 'the profile click must reach the server');
  }

  async function receiveCall() {
    state.crew.call = {...CALL};
    await until(() => requests.some((request) => request.path === '/talk/pcm'));
    assert.equal(element('hud').dataset.phase, 'talking');
    return requests.find((request) => request.path === '/talk/pcm');
  }

  function capture(value = 0.25, length = 1024) {
    assert.equal(processors.length, 1, 'one microphone processor is attached');
    processors[0].onaudioprocess?.({
      inputBuffer: {getChannelData: () => new Float32Array(length).fill(value)},
    });
  }

  return {
    contexts, starts, requests, chooseProfile, receiveCall, capture, flush, element, state, doc, until, advance,
    queueVoicePcm: (chunk) => voicePcm.push(chunk),
    setSnapshot: (fields) => Object.assign(state, fields),
    hangup: () => element('cancel').dispatch('click'),
    uploads: () => requests.filter((request) => request.path === '/talk/uplink'),
    audible: () => starts.filter(({source}) => source.buffer?.getChannelData(0).some((value) => value !== 0)),
  };
}
