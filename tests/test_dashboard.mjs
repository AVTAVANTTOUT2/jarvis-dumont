import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../src/jarvis_office/static/dashboard/', import.meta.url));
const source = readFileSync(root + 'api.js', 'utf8');
const {acceptsSnapshot, typedValue, queryString, errorLabel, OfficeAPI} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('snapshot cursor rejects stale events and accepts a new server epoch', () => {
  const current = {server_epoch:'one', event_id:12};
  assert.equal(acceptsSnapshot(current, {server_epoch:'one',event_id:11}), false);
  assert.equal(acceptsSnapshot(current, {server_epoch:'one',event_id:12}), true);
  assert.equal(acceptsSnapshot(current, {server_epoch:'two',event_id:0}), true);
  assert.equal(acceptsSnapshot(current, {server_epoch:'two'}), false);
  assert.equal(acceptsSnapshot(null, null), false);
});

test('typed filters preserve text, zero and null operations', () => {
  assert.equal(typedValue('INTEGER', '0', 'eq'), 0);
  assert.equal(typedValue('REAL', '1.25', 'le'), 1.25);
  assert.throws(() => typedValue('INTEGER', '', 'eq'));
  assert.throws(() => typedValue('REAL', 'Infinity', 'eq'));
  assert.equal(typedValue('TEXT', "x' OR 1=1", 'eq'), "x' OR 1=1");
  assert.equal(typedValue('TEXT', '', 'isnull'), true);
  const query = new URLSearchParams(queryString({offset:0,q:'écho & bureau',filters:[{column:'status',op:'eq',value:'partial'}]}));
  assert.equal(query.get('offset'),'0');
  assert.equal(query.get('q'),'écho & bureau');
  assert.deepEqual(JSON.parse(query.get('filters')),[{column:'status',op:'eq',value:'partial'}]);
});

test('API sends CSRF only on mutation and never stores credentials', async () => {
  const requests = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    requests.push({url,options});
    return new Response(JSON.stringify(url === '/api/bootstrap' ? {csrf_token:'test-only'} : {status:'applied'}),{status:200,headers:{'Content-Type':'application/json'}});
  };
  try {
    const api = new OfficeAPI();
    await api.bootstrap();
    await api.get('/api/state');
    await api.post('/api/command',{action:'set_mode',mode:'OFF'});
    assert.equal(requests[1].options.method,'GET');
    assert.equal(requests[1].options.headers['X-CSRF-Token'],undefined);
    assert.equal(requests[2].options.headers['X-CSRF-Token'],'test-only');
    assert.equal(requests[2].options.redirect,'error');
    assert.equal(requests[2].options.credentials,'same-origin');
    assert.equal(requests.some(item => item.url.includes('test-only')),false);
  } finally { globalThis.fetch = originalFetch; }
});

test('API and error renderer hide raw response details', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({error:'private speech or credentials'}),{status:500});
  try {
    await assert.rejects(new OfficeAPI().get('/api/state'), error => error.code === 'INVALID_REQUEST');
    assert.equal(errorLabel({code:'untrusted text'}).includes('untrusted'),false);
  } finally { globalThis.fetch = originalFetch; }
});

test('known qualification limitations do not imply a failed command', () => {
  assert.equal(errorLabel({code:'KNOWN_STT_LIMITATIONS'}), 'Des erreurs de transcription connues subsistent.');
  assert.equal(errorLabel({code:'KNOWN_PLAYBACK_LIMITATIONS'}), 'Des irrégularités de lecture connues subsistent.');
  assert.equal(errorLabel({code:'MEMORY_REQUIRES_HISTORY'}), 'Activez d’abord la conservation des conversations.');
  assert.equal(errorLabel({code:'MEMORY_ROLLUP_FAILED'}), 'Le résumé n’a pas pu être mis à jour. Le contexte déjà chargé reste utilisé.');
});

test('DeepSeek timeouts are located on the Mac HTTP client, without judging the Echo link', () => {
  for (const code of ['connect_timeout','first_content_timeout','idle_timeout','total_timeout','transport_timeout']) {
    assert.match(errorLabel({code}), /^Délai du client HTTP DeepSeek du Mac/);
    assert.doesNotMatch(errorLabel({code}), /Echo/);
  }
  assert.match(errorLabel({code:'transport_timeout'}), /lecture, écriture ou pool/);
  assert.equal(errorLabel({code:'other_error'}, 'Repli explicite.'), 'Repli explicite.');
});

test('every statically referenced control exists and no browser capture or HTML injection is used', () => {
  const html = readFileSync(root+'index.html','utf8');
  const js = readFileSync(root+'dashboard.js','utf8');
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]));
  for (const match of js.matchAll(/\$\('([^']+)'\)/g)) assert.ok(ids.has(match[1]),`missing control ${match[1]}`);
  assert.equal(ids.size,[...html.matchAll(/\bid="([^"]+)"/g)].length,'duplicate HTML ID');
  assert.doesNotMatch(js+source,/innerHTML|outerHTML|insertAdjacentHTML|getUserMedia|localStorage|sessionStorage/);
  assert.equal([...html.matchAll(/class="page"/g)].length,6);
});
