import assert from 'node:assert/strict';
import test from 'node:test';
import {ORBS, orbState} from '../src/jarvis_office/static/dashboard/orbs.js';
import {MODE_FRAMES, resolvePreset, STATE_TO_MODE} from '../src/jarvis_office/static/dashboard/thinking-orbs.js';

test('catalog matches the nine original presets and reacts to physical state', () => {
  assert.deepEqual(Object.keys(ORBS).slice(1), Object.keys(STATE_TO_MODE));
  const device={connection:'CONNECTED', physical:{microphone:false,playing:false}};
  assert.equal(orbState('auto',device),'breathing');
  assert.equal(orbState('auto',{...device,physical:{microphone:true}}),'listening');
  assert.equal(orbState('auto',{...device,activity:'GENERATING'}),'solving');
  assert.equal(orbState('auto',{...device,activity:'TRANSCRIBING'}),'searching');
  assert.equal(orbState('auto',{...device,physical:{playing:true}}),'composing');
  assert.equal(orbState('auto',null),'connecting');
  assert.equal(orbState('invalid',device),'breathing');
  assert.equal(orbState('weaving',device),'weaving');
  for(const style of Object.keys(STATE_TO_MODE)) {
    const preset=resolvePreset(style,64);
    const frame=MODE_FRAMES[preset.mode](300,1.5*preset.speed,preset.opts);
    assert.ok(frame.dots.length>0 && frame.dots.length<=600);
    assert.ok(frame.lines.length<=900);
    assert.ok(frame.dots.every(dot=>Number.isFinite(dot.x) && Number.isFinite(dot.y) && dot.r>0));
  }
});
