import {OfficeAPI, acceptsSnapshot, errorLabel, typedValue} from './api.js';
import {mountOrbs} from './orbs.js';

const api = new OfficeAPI();
const orbs = mountOrbs(style => api.post('/api/settings', {orb_style:style}), fail);
const $ = (id) => document.getElementById(id);
const labels = {
  OFF: 'Micro coupé', ACTIVE: 'Conversation', PASSIVE: 'Écoute contextuelle',
  DISCONNECTED: 'Déconnecté', CONNECTING: 'Connexion', CONNECTED: 'Connecté', DEGRADED: 'Dégradé',
  IDLE: 'Repos', LISTENING: 'Écoute', TRANSCRIBING: 'Transcription', GENERATING: 'Génération', PLAYING: 'Lecture', ERROR: 'Erreur',
  complete: 'Complet', partial: 'Partiel', interrupted: 'Interrompu', error: 'Erreur', active: 'En cours', open: 'Ouverte', closed: 'Terminée', ended: 'Terminée',
};
const state = {
  snapshot: null, device: '', page: 'overview', online: false, settings: null,
  catalog: null, table: '', dataOffset: 0, rows: null, filters: [],
  session: '', sessionsOffset: 0, turnOffset: 0, turns: null,
  pending: null, loadGeneration: 0, rowGeneration: 0, contextGeneration: 0,
  contextLoadedAt: 0, diagnosticsSignature: '',
  detailGeneration: 0, latestGeneration: 0,
};
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function set(id, value) { $(id).textContent = value ?? 'Non mesuré'; }
function label(value) { return labels[value] || (value == null ? 'Non mesuré' : String(value)); }
function date(value) {
  if (value === null || value === undefined || value === '') return 'Non mesuré';
  const parsed = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? 'Date non reconnue' : parsed.toLocaleString('fr-FR', {dateStyle:'medium', timeStyle:'short'});
}
function duration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return 'Non mesuré';
  return seconds >= 60 ? `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s` : `${Math.max(0, Math.round(seconds))} s`;
}
function musicDuration(milliseconds) {
  return typeof milliseconds === 'number' && Number.isFinite(milliseconds) ? duration(milliseconds / 1000) : 'Durée non mesurée';
}
function device() { return state.snapshot?.devices?.find(item => item.device_id === state.device) || null; }
function badge(value) {
  const color = /CONNECTED|ACTIVE|complete|OFF/.test(value || '') && value !== 'DISCONNECTED' ? 'cyan' : /PASSIVE|DEGRADED|partial|interrupted/.test(value || '') ? 'amber' : /ERROR|error/.test(value || '') ? 'red' : '';
  return el('span', label(value), `badge ${color}`);
}
function notify(message, kind = '') {
  set('feedback', message); $('feedback').className = `notice ${kind}`; $('feedback').hidden = false;
}
function fail(error) { notify(errorLabel(error)); }
function empty(container, title, message = '', retry = null) {
  const box = el('div', undefined, 'empty'); box.append(el('h3', title));
  if (message) box.append(el('p', message));
  if (retry) { const button = el('button', 'Réessayer', 'button secondary'); button.onclick = retry; box.append(button); }
  container.replaceChildren(box);
}
function loading(container) { container.replaceChildren(el('p', 'Chargement…', 'loading')); }
function displayError(container, error, retry) { empty(container, 'Chargement impossible', errorLabel(error), retry); }
async function confirmAction(title, message, action = 'Confirmer') {
  const dialog = $('confirm-dialog');
  if (dialog.open) return false;
  set('confirm-title', title); set('confirm-message', message); set('confirm-action', action);
  dialog.returnValue = 'cancel'; dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once:true}));
}
function tableMeta(name = state.table) { return state.catalog?.tables?.find(item => item.name === name); }
function primaryKey(name) {
  const known = {devices:'device_id', sessions:'session_id', turns:'turn_id', conversation_memory:'device_id', events:'event_id', preferences:'key', passive_archives:'id', schema_migrations:'version'};
  return tableMeta(name)?.columns?.find(column => column.primary_key)?.name || known[name];
}
function parseJSON(value) {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}
function structured(value, depth = 0) {
  if (value === null || value === undefined) return el('span', 'NULL', 'null-value');
  if (typeof value !== 'object') return el('pre', typeof value === 'boolean' ? (value ? 'true' : 'false') : String(value));
  if (depth >= 20) return el('pre', JSON.stringify(value, null, 2));
  const box = el('div', undefined, 'json-tree');
  for (const [key, child] of Object.entries(value)) {
    const detail = el('details');
    const kind = child === null ? 'NULL' : Array.isArray(child) ? `tableau · ${child.length}` : typeof child === 'object' ? `objet · ${Object.keys(child).length}` : String(child).slice(0,80);
    detail.append(el('summary', `${key} : ${kind}`), structured(child, depth + 1)); box.append(detail);
  }
  if (!Object.keys(value).length) box.append(el('pre', Array.isArray(value) ? '[]' : '{}'));
  return box;
}
function renderFields(row) {
  const list = el('dl');
  for (const [name, raw] of Object.entries(row)) {
    const field = el('div', undefined, 'detail-field'); const content = el('dd');
    const value = /_json$/.test(name) ? parseJSON(raw) : raw;
    if (/_at$/.test(name) && value != null) { content.append(el('span', date(value)), el('small', ` · ${value}`)); }
    else content.append(structured(value));
    const relation = {device_id:'devices', session_id:'sessions', turn_id:'turns'}[name];
    if (relation && raw != null) {
      const link = el('a', `Ouvrir ${relation} →`); link.href = '#data';
      link.onclick = async event => { event.preventDefault(); await showRow(relation, raw); };
      content.append(link);
    }
    field.append(el('dt', name), content); list.append(field);
  }
  return list;
}
function setConnected(online) {
  state.online = online;
  $('connection-dot').className = `status-dot ${online ? 'online' : 'error'}`;
  set('connection-label', online ? 'Service connecté' : 'Reconnexion…');
  $('connection-notice').hidden = online;
  if (!online) set('connection-notice', 'Connexion au service perdue. Les derniers états affichés peuvent être périmés. Pour une coupure immédiate, utilisez « Couper » sur l’Echo.');
  renderControls();
}
function nominalBudget() { return state.snapshot?.budget?.nominal || state.settings?.budget || null; }
function diagnosticBudget() {
  const budget = state.snapshot?.budget?.diagnostic;
  return budget ? `Campagne diagnostic conservée : ${budget.used ?? 'Non mesuré'} / ${budget.limit ?? 'Non mesuré'} appels` : 'Campagne diagnostic : non mesuré';
}
function renderBudget() {
  const budget = nominalBudget(); const box = $('budget-summary'); box.replaceChildren();
  if (budget?.limit == null) {
    box.append(el('strong', 'À définir'), el('span', 'Choisissez votre plafond avant d’activer le terminal.'));
  } else {
    box.append(el('strong', `${budget.used ?? 'Non mesuré'} / ${budget.limit}`), el('span', `requêtes · ${budget.period === 'day' ? 'jour' : 'mois'} en cours`));
    if (typeof budget.used === 'number') {
      const meter = el('div', undefined, 'budget-meter'); const progress = el('progress');
      progress.max = budget.limit; progress.value = budget.used; progress.setAttribute('aria-label', 'Utilisation du plafond nominal'); meter.append(progress); box.append(meter);
    }
  }
  set('diagnostic-budget', diagnosticBudget());
}
function renderControls() {
  const current = device(); const available = Boolean(current && state.online);
  orbs.update(state.snapshot,current,state.online);
  $('quick-off').disabled = !current;
  for (const button of document.querySelectorAll('[data-mode]')) {
    button.disabled = !available || Boolean(state.pending);
    button.setAttribute('aria-pressed', String(current?.confirmed_mode === button.dataset.mode));
  }
  for (const id of ['interrupt','clear-context-overview','clear-context']) $(id).disabled = !available || Boolean(state.pending);
  $('preview-voice').disabled = !available || Boolean(state.pending) || current?.connection !== 'CONNECTED' || current?.confirmed_mode !== 'OFF' || current?.physical?.microphone !== false || current?.physical?.playing !== false;
  const diagnostic=state.snapshot?.budget?.diagnostic;
  $('passive-smoke').disabled=$('preview-voice').disabled || typeof diagnostic?.used!=='number' || typeof diagnostic?.limit!=='number' || diagnostic.used>=diagnostic.limit;
  if (state.pending) set('command-state', 'Changement en cours… En attente de l’accusé du terminal.');
  else if (!current) set('command-state', 'Choisissez un terminal pour accéder aux commandes.');
  else if (current.requested_mode !== current.confirmed_mode) set('command-state', `Demandé : ${label(current.requested_mode)}. Confirmé : ${label(current.confirmed_mode)}.`);
  else set('command-state', `Mode confirmé : ${label(current.confirmed_mode)}. Micro physique : ${current.physical?.microphone === true ? 'ouvert' : current.physical?.microphone === false ? 'fermé' : 'non mesuré'}.`);
}
function renderState(snapshot) {
  if (!acceptsSnapshot(state.snapshot, snapshot)) return;
  const oldTurn = device();
  const oldTurnState = `${oldTurn?.turn_id}/${oldTurn?.activity}`;
  state.snapshot = snapshot;
  const devices = Array.isArray(snapshot.devices) ? snapshot.devices : [];
  if (!devices.some(item => item.device_id === state.device)) state.device = devices[0]?.device_id || '';
  const select = $('device-select'); const identities = devices.map(item => item.device_id).join('\n');
  if (select.dataset.identities !== identities) {
    select.dataset.identities = identities; select.replaceChildren();
    if (!devices.length) select.append(new Option('Aucun terminal connecté', ''));
    for (const item of devices) select.append(new Option(item.name || item.device_id, item.device_id));
  }
  select.value = state.device;
  const current = device();
  set('state-connection', label(current?.connection)); set('state-mode', label(current?.confirmed_mode));
  set('state-requested', `Demandé : ${label(current?.requested_mode)}`); set('state-activity', label(current?.activity));
  set('state-device', current?.device_id || 'Aucun terminal'); set('state-turn', current?.turn_id ? `Tour ${current.turn_id}` : 'Aucun tour actif');
  const physical = current?.physical;
  set('state-microphone', physical?.microphone === true ? 'Ouvert' : physical?.microphone === false ? 'Fermé' : 'Non mesuré');
  $('state-microphone').closest('.card').classList.toggle('micro-open', physical?.microphone === true);
  set('state-playback', `Lecture : ${physical?.playing === true ? 'en cours' : physical?.playing === false ? 'arrêtée' : 'non mesurée'}${physical?.playback_frames != null ? ` · ${physical.playback_frames} frames` : ''}`);
  const connection = badge(current?.connection); connection.id = 'overview-connection'; $('overview-connection').replaceWith(connection);
  renderBudget(); renderControls();
  const alerts = $('alerts'); alerts.replaceChildren();
  for (const alert of Array.isArray(snapshot.alerts) ? snapshot.alerts : []) {
    const code = typeof alert === 'string' ? alert : alert.code;
    if (typeof code === 'string') alerts.append(el('div', `${code} · ${errorLabel({code})}`, 'notice amber'));
  }
  if (current?.error) alerts.append(el('div', `${current.error} · ${errorLabel({code:current.error}, 'Vérifiez le terminal avant toute activation.')}`, 'notice amber'));
  const memory = snapshot.memory || {};
  const memoryState = memory.enabled === true
    ? (memory.summary_present ? 'Résumé disponible après redémarrage.' : 'Mémoire activée, en attente de tours confirmés.')
    : 'Mémoire persistante désactivée.';
  $('memory-overview').textContent = memory.last_error ? errorLabel({code: memory.last_error}, memoryState) : memoryState;
  if (state.online && state.page === 'overview' && oldTurnState !== `${current?.turn_id}/${current?.activity}`) loadLatest();
  if (state.page === 'settings') renderServiceState();
  renderTv(); renderPlaylists();
}
async function refreshState() { const snapshot = await api.get('/api/state'); renderState(snapshot); setConnected(true); return snapshot; }
async function sendCommand(action, mode) {
  if (!state.device || (state.pending && mode !== 'OFF')) return;
  if (mode && mode !== 'OFF' && nominalBudget()?.limit == null) {
    location.hash = '#settings'; notify('Choisissez votre plafond d’usage nominal avant d’activer le terminal.', 'amber'); return;
  }
  const command = {device_id: state.device, action, command_id: crypto.randomUUID(), ...(mode ? {mode} : {}), ...(['clear_context','clear_memory','preview_voice','passive_smoke'].includes(action) ? {confirm:true} : {})};
  state.pending = command.command_id; renderControls();
  try {
    const result = await api.post('/api/command', command, {timeout:5500});
    if (result.status === 'rejected' || result.error) throw {code:result.error || 'INVALID_REQUEST'};
    await refreshState();
    if (state.pending === command.command_id) notify(result.status === 'applied' ? 'Commande confirmée. L’état physique est affiché séparément.' : 'Commande transmise. Vérifiez le mode confirmé et le micro physique.', 'success');
    if (action === 'clear_context' && state.page === 'context') await loadContext();
    return result.status === 'applied';
  } catch (error) { if (state.pending === command.command_id) fail(error); }
  finally { if (state.pending === command.command_id) state.pending = null; renderControls(); }
}
async function clearContext() {
  if (await confirmAction('Effacer le contexte actif ?', 'Les énoncés actuellement retenus en RAM seront effacés et le terminal reviendra sur Micro coupé. Les conversations enregistrées et les archives passives sont conservées.', 'Effacer le contexte')) await sendCommand('clear_context');
}
async function clearMemory() {
  if (!state.device) { notify('Choisissez un terminal avant d’effacer la mémoire.'); return; }
  if (await confirmAction('Effacer la mémoire persistante ?', 'Le résumé réinjecté après redémarrage sera oublié. Les conversations enregistrées restent consultables et ne seront pas relues automatiquement.', 'Effacer la mémoire')) await sendCommand('clear_memory');
}
async function loadSettings() {
  const settings = await api.get('/api/settings'); state.settings = settings;
  return settings;
}
async function loadLatest() {
  const generation = ++state.latestGeneration;
  try {
    const data = await api.get('/api/data/rows', {table:'turns', limit:1, offset:0, sort:'created_at', direction:'desc'});
    if (generation !== state.latestGeneration) return;
    const box = $('latest-turn'); box.className = '';
    if (data.rows?.length) box.replaceChildren(turnCard(data.rows[0], false));
    else empty(box, 'Aucun échange enregistré', 'L’historique commence après activation de la conservation dans Réglages.');
  } catch (error) { if (generation === state.latestGeneration) displayError($('latest-turn'), error, loadLatest); }
}
function turnCard(turn, framed = true) {
  const card = el('article', undefined, framed ? 'card' : ''); const heading = el('div', undefined, 'turn-heading');
  const timestamp = el('time', date(turn.created_at)); if (typeof turn.created_at === 'string') timestamp.dateTime = turn.created_at;
  heading.append(timestamp, badge(turn.status)); card.append(heading);
  card.append(el('span', 'VOUS', 'turn-label'), el('p', turn.user_text ?? 'Texte non disponible', 'turn-text'));
  card.append(el('span', 'JARVIS · PRÉFIXE LIVRÉ CONFIRMÉ', 'turn-label assistant'));
  card.append(el('p', turn.delivered_text || 'Aucun texte livré confirmé.', 'turn-text'));
  const actions = el('div', undefined, 'turn-actions'); const more = el('button', 'Détail et mesures →', 'button secondary');
  more.onclick = () => showTurn(turn);
  actions.append(el('small', `Session ${turn.session_id ?? 'Non mesuré'}`), more); card.append(actions);
  return card;
}
function showTurn(turn) {
  state.detailGeneration++;
  const box = $('detail-content'); box.replaceChildren(); set('detail-title', 'Détail de l’échange');
  box.append(el('div', 'Le texte généré peut dépasser le préfixe livré. La livraison confirmée ne constitue pas une preuve d’écoute acoustique.', 'notice neutral'));
  const metrics = parseJSON(turn.metrics_json) || {};
  const list = el('div', undefined, 'metric-list');
  const fields = [['STT', 'stt_ms'], ['Première sortie utile', 'first_useful_output_ms'], ['File audio', 'audio_queue_ms'], ['Aller-retour réseau (RTT)', 'rtt_ms'], ['Latence acoustique', 'acoustic_latency_ms']];
  for (const [name, key] of fields) {
    const item = el('div', undefined, 'metric-item'); item.append(el('span', name), el('strong', typeof metrics[key] === 'number' ? `${metrics[key]} ms` : 'Non mesuré')); list.append(item);
  }
  box.append(list, renderFields(turn)); if (!$('detail-dialog').open) $('detail-dialog').showModal();
}
async function loadSessions(append = false) {
  try {
    const result = await api.get('/api/data/rows', {table:'sessions', limit:20, offset:state.sessionsOffset, sort:'started_at', direction:'desc'});
    const box = $('sessions-list'); if (!append) box.replaceChildren();
    if (!result.rows?.length && !append) empty(box, 'Aucune session');
    for (const session of result.rows || []) {
      const button = el('button', undefined, 'session-item'); button.append(el('span', date(session.started_at)), el('small', `${session.device_id} · ${label(session.status)}`));
      button.setAttribute('aria-pressed', String(state.session === session.session_id));
      button.onclick = () => { state.session = session.session_id; state.turnOffset = 0; for (const item of box.querySelectorAll('button')) item.setAttribute('aria-pressed','false'); button.setAttribute('aria-pressed','true'); loadTurns(); };
      box.append(button);
    }
    $('more-sessions').hidden = state.sessionsOffset + (result.rows?.length || 0) >= result.total;
  } catch (error) { displayError($('sessions-list'), error, () => loadSessions()); }
}
async function loadTurns() {
  const generation = ++state.loadGeneration; loading($('turns-list'));
  const filters = [];
  if (state.session) filters.push({column:'session_id', op:'eq', value:state.session});
  if ($('conversation-status').value) filters.push({column:'status', op:'eq', value:$('conversation-status').value});
  try {
    const result = await api.get('/api/data/rows', {table:'turns', limit:20, offset:state.turnOffset, sort:'created_at', direction:'desc', q:$('conversation-query').value.trim(), filters});
    if (generation !== state.loadGeneration) return;
    state.turns = result; const box = $('turns-list'); box.replaceChildren();
    if (!result.rows?.length) empty(box, 'Aucun échange dans ce périmètre', 'Essayez une autre recherche ou activez la conservation pour les prochains échanges.');
    for (const turn of result.rows || []) box.append(turnCard(turn));
    set('conversation-scope', `${state.session ? `Session ${state.session}` : 'Toutes les sessions'} · ${result.total ?? 'Non mesuré'} échanges${result.truncated ? ' · résultat partiel' : ''}`);
    set('turns-page', `${result.rows?.length ? state.turnOffset + 1 : 0}–${state.turnOffset + (result.rows?.length || 0)} / ${result.total ?? '?'}`);
    $('turns-prev').disabled = state.turnOffset === 0; $('turns-next').disabled = state.turnOffset + (result.rows?.length || 0) >= result.total;
  } catch (error) { if (generation === state.loadGeneration) displayError($('turns-list'), error, loadTurns); }
}
async function loadConversations() {
  const outcomes = await Promise.allSettled([loadSettings(), loadSessions(), loadTurns()]);
  if (outcomes[0].status === 'fulfilled') {
    const settings = state.settings;
    set('history-notice', `${settings.history_enabled ? 'Conservation activée' : 'Conservation des nouveaux échanges désactivée'} · Rétention : ${settings.retention_days} jours. ${settings.history_started_at ? `Historique commencé le ${date(settings.history_started_at)}.` : 'Aucun échange ancien n’est reconstruit.'}`);
  } else set('history-notice', 'État de conservation indisponible. Les échanges ci-dessous proviennent uniquement du stockage local.');
}
async function loadContext(quiet = false) {
  const generation = ++state.contextGeneration;
  state.contextLoadedAt = Date.now();
  if (!state.device) {
    empty($('context-entries'), 'Aucun terminal sélectionné', 'Le contexte apparaît ici pendant sa durée de vie.');
    for (const id of ['context-limits','context-archive','context-bound']) set(id, 'Non mesuré');
    return;
  }
  if (!quiet) loading($('context-entries'));
  try {
    const result = await api.get('/api/context', {device_id:state.device});
    if (generation !== state.contextGeneration) return;
    const limits = result.limits || {};
    set('context-limits', `${duration(limits.seconds)} / ${limits.chars ?? '?'} caractères / ${limits.utterances ?? '?'} énoncés`);
    set('context-archive', result.archive_enabled === true ? 'Activé explicitement' : result.archive_enabled === false ? 'Désactivé · RAM uniquement' : 'Non mesuré');
    set('context-bound', result.next_turn_max_chars == null ? 'Non mesuré' : `${result.next_turn_max_chars} caractères maximum`);
    const box = $('context-entries'); box.replaceChildren();
    if (!result.entries?.length) empty(box, 'Le contexte est vide', 'Aucun énoncé n’est actuellement retenu pour ce terminal.');
    for (const entry of result.entries || []) {
      const item = el('article', undefined, 'context-entry'); const meta = el('div', undefined, 'context-meta');
      meta.append(el('span', `Source : ${entry.source ?? 'Non mesuré'}`), el('span', `Âge : ${duration(entry.age_s)}`), el('span', `Expire dans ${duration(entry.expires_in_s)}`));
      item.append(el('p', entry.text), meta); box.append(item);
    }
  } catch (error) { if (generation === state.contextGeneration) displayError($('context-entries'), error, loadContext); }
}
async function loadCatalog() {
  state.catalog = await api.get('/api/data/catalog');
  const catalog = $('catalog-tables'); catalog.replaceChildren();
  for (const table of state.catalog.tables || []) {
    const button = el('button', table.name, 'catalog-item'); button.append(el('span', table.count ?? '?'));
    button.setAttribute('aria-pressed', String(table.name === state.table));
    button.onclick = () => selectTable(table.name); catalog.append(button);
  }
  if (!state.catalog.tables?.length) empty(catalog, 'Aucune table accessible');
  const restricted = $('restricted-items'); restricted.replaceChildren();
  for (const item of state.catalog.restricted || []) restricted.append(el('p', typeof item === 'string' ? item : `${item.name ?? 'Système'} : ${item.reason ?? 'Contenu restreint'}`));
  if (!restricted.childNodes.length) restricted.append(el('p', 'Les clés, secrets et données des autres applications sont exclus des tables métier.'));
  const reports = $('reports-catalog'); reports.replaceChildren();
  for (const report of state.catalog.reports || []) { const item = el('div', undefined, 'report-item'); item.append(renderFields(typeof report === 'object' ? report : {name:report})); reports.append(item); }
  if (!reports.childNodes.length) empty(reports, 'Aucun rapport dans le catalogue autorisé');
  if (!tableMeta()) state.table = state.catalog.tables?.[0]?.name || '';
  if (state.table) selectTable(state.table);
}
function selectTable(name) {
  if (!tableMeta(name)) return;
  state.table = name; state.dataOffset = 0; state.filters = []; $('data-filters').replaceChildren(); $('data-query').value = '';
  $('data-sort').replaceChildren();
  const table = tableMeta();
  for (const column of table.columns) $('data-sort').append(new Option(column.name, column.name));
  $('data-sort').value = primaryKey(name) || table.columns[0]?.name || '';
  set('table-title', name); set('table-count', `${table.count ?? 'Non mesuré'} lignes · comptage du ${date(table.counted_at)}`);
  for (const button of $('catalog-tables').children) button.setAttribute('aria-pressed', String(button.firstChild?.textContent === name));
  const schema = $('schema-content'); schema.replaceChildren();
  for (const column of table.columns) { const item = el('div', column.name, 'schema-column'); item.append(el('small', `${column.type} · ${column.nullable ? 'NULL autorisé' : 'non NULL'}${column.primary_key ? ' · clé primaire' : ''}`)); schema.append(item); }
  $('add-filter').disabled = false; loadRows();
}
function addFilter() {
  if (state.filters.length >= 8 || !tableMeta()) return;
  const index = state.filters.length; const row = el('div', undefined, 'filter-row');
  const column = el('select'); for (const item of tableMeta().columns) column.append(new Option(item.name, item.name));
  const op = el('select');
  for (const [value,text] of [['eq','Est égal à'],['ne','Est différent de'],['lt','Inférieur à'],['le','Inférieur ou égal'],['gt','Supérieur à'],['ge','Supérieur ou égal'],['contains','Contient'],['isnull','Est NULL']]) op.append(new Option(text,value));
  const value = el('input'); value.maxLength = 500;
  for (const [title,control] of [['Colonne',column],['Condition',op],['Valeur',value]]) { const labelNode = el('label'); labelNode.append(el('span',title),control); row.append(labelNode); }
  const updateType = () => { const type = tableMeta().columns.find(item => item.name === column.value)?.type; value.type = /INT|REAL|FLOAT|DOUBLE|NUMERIC/i.test(type || '') ? 'number' : 'text'; value.step = 'any'; value.disabled = op.value === 'isnull'; };
  column.onchange = updateType; op.onchange = updateType; updateType();
  const remove = el('button','✕','button secondary icon-button'); remove.type='button'; remove.setAttribute('aria-label',`Supprimer le filtre ${index+1}`);
  const filter = {row,column,op,value}; remove.onclick = () => { row.remove(); state.filters = state.filters.filter(item => item !== filter); $('add-filter').disabled=false; };
  row.append(remove); $('data-filters').append(row); state.filters.push(filter); $('add-filter').disabled = state.filters.length >= 8;
}
function dataScope() {
  return {table:state.table, limit:Number($('data-limit').value), offset:state.dataOffset, sort:$('data-sort').value, direction:$('data-direction').value, q:$('data-query').value.trim(), filters:state.filters.map(item => ({column:item.column.value,op:item.op.value,value:typedValue(tableMeta().columns.find(column => column.name === item.column.value)?.type || '',item.value.value,item.op.value)}))};
}
async function loadRows() {
  if (!state.table) return;
  let scope; try { scope = dataScope(); } catch { notify('Un filtre numérique nécessite une valeur valide.'); return; }
  const generation = ++state.rowGeneration; loading($('table-result')); $('export-data').disabled=true;
  try {
    const result = await api.get('/api/data/rows', scope); if (generation !== state.rowGeneration) return;
    state.rows = result; state.rows.scope = scope;
    const box = $('table-result'); box.replaceChildren();
    if (!result.rows?.length) empty(box, 'Aucune ligne dans ce périmètre');
    else {
      const table = el('table'); const head = el('thead'); const headings = el('tr');
      const columns = (result.columns || tableMeta().columns).map(item => typeof item === 'string' ? item : item.name);
      for (const name of [...columns,'Détail']) { const th = el('th',name); th.scope='col'; headings.append(th); } head.append(headings); table.append(head);
      const body = el('tbody');
      for (const row of result.rows) {
        const tr = el('tr');
        for (const name of columns) {
          const td = el('td'); const value = row[name];
          td.append(value == null ? el('span','NULL','null-value') : el('span',typeof value === 'object' ? JSON.stringify(value) : String(value),'cell-text')); tr.append(td);
        }
        const cell = el('td'); const open = el('button','Ouvrir →','button secondary'); open.onclick=()=>showRow(result.table,row[primaryKey(result.table)]); cell.append(open); tr.append(cell); body.append(tr);
      }
      table.append(body); box.append(table);
    }
    set('data-scope', `${result.total ?? 'Non mesuré'} lignes correspondantes · ${scope.filters.length} filtre(s)${scope.q ? ` · recherche « ${scope.q} »` : ''}${result.truncated ? ' · résultat partiel ou limite atteinte' : ''}. Textes abrégés dans le tableau ; détail complet via Ouvrir.`);
    set('data-page', `${result.rows?.length ? state.dataOffset+1 : 0}–${state.dataOffset+(result.rows?.length || 0)} / ${result.total ?? '?'}`);
    $('data-prev').disabled = state.dataOffset===0; $('data-next').disabled = state.dataOffset+(result.rows?.length || 0)>=result.total;
    $('export-data').disabled = !result.rows?.length;
  } catch(error) { if(generation===state.rowGeneration) displayError($('table-result'),error,loadRows); }
}
async function showRow(table, id) {
  if (id === null || id === undefined) { notify('Identifiant de ligne indisponible.'); return; }
  set('detail-title', `${table} · détail de la ligne`); loading($('detail-content'));
  const generation=++state.detailGeneration;
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
  try { const result=await api.get('/api/data/row',{table,id}); if(generation===state.detailGeneration)$('detail-content').replaceChildren(renderFields(result.row)); }
  catch(error) { if(generation===state.detailGeneration)displayError($('detail-content'),error,()=>showRow(table,id)); }
}
async function download(path,body,filename) {
  const blob=await api.post(path,body,{download:true,timeout:15000});
  const url=URL.createObjectURL(blob); const link=el('a'); link.href=url; link.download=filename; document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function exportData() {
  if (!state.rows?.scope) return;
  state.detailGeneration++;
  const scope=state.rows.scope;
  const box=$('detail-content'); box.replaceChildren(); set('detail-title','Exporter le périmètre affiché');
  box.append(el('p',`Table ${scope.table} · départ ligne ${scope.offset+1} · ${scope.limit} lignes maximum · ${scope.filters.length} filtre(s) · tri ${scope.sort} ${scope.direction}${scope.q ? ` · recherche « ${scope.q} »` : ''}.`));
  for(const filter of scope.filters)box.append(el('p',`${filter.column} · ${filter.op} · ${filter.op==='isnull' ? 'NULL' : filter.value}`,'small'));
  box.append(el('p','Export réservé au propriétaire, expurgé côté serveur. Le CSV neutralise les formules de tableur. Aucune autre table ne sera exportée.','helper'));
  const actions=el('div',undefined,'actions');
  for(const format of ['csv','json']) { const button=el('button',`Confirmer l’export ${format.toUpperCase()}`,'button secondary'); button.onclick=async()=>{ button.disabled=true; try{await download('/api/data/export',{...scope,format,confirm:true},`office-${scope.table}.${format}`); $('detail-dialog').close(); notify('Export du périmètre téléchargé.','success');}catch(error){fail(error);}finally{button.disabled=false;} }; actions.append(button); }
  box.append(actions); if(!$('detail-dialog').open)$('detail-dialog').showModal();
}
function renderTv() {
  const tv = state.snapshot?.tv || {};
  const configured = tv.configured === true;
  const enabled = tv.enabled === true;
  const pin = typeof tv.cert_sha256 === 'string' ? tv.cert_sha256.slice(-8) : '';
  const status = !configured
    ? 'Listener TV non configuré. Ajoutez une section [tv] au TOML privé, hors Git.'
    : `${enabled ? 'TV active' : 'TV désactivée'} · port ${tv.port ?? 'non mesuré'}${pin ? ` · empreinte …${pin}` : ''}.`;
  set('tv-overview-status', status);
  set('tv-settings-status', `${status}${tv.pairing_pending ? ' Un document d’appairage est encore valable.' : ''}`);
  $('tv-enabled-badge').textContent = !configured ? 'ABSENT' : enabled ? 'ACTIVÉE' : 'DÉSACTIVÉE';
  $('tv-enable').disabled = !configured || enabled || Boolean(state.pending);
  $('tv-disable').disabled = !configured || !enabled || Boolean(state.pending);
  $('tv-pair').disabled = !configured || !enabled || Boolean(state.pending);
  $('tv-save-defaults').disabled = !configured || Boolean(state.pending);
  $('tv-video-app').value = tv.defaults?.video === 'avt' ? 'avt' : 'smarttube';
  $('tv-film-app').value = tv.defaults?.film === 'smarttube' ? 'smarttube' : 'avt';
  const devices = Array.isArray(tv.devices) ? tv.devices : [];
  for (const box of [$('tv-overview-devices'), $('tv-devices')]) {
    box.replaceChildren();
    if (!devices.length) {
      empty(box, configured ? 'Aucun appareil TV appairé.' : 'Fonction TV absente.');
      continue;
    }
    for (const item of devices) {
      const row = el('div', undefined, 'device-row');
      const name = el('div', item.device_id || 'appareil');
      const result = item.last_result;
      name.append(el('small', `${label(item.connection)} · file ${item.queue_depth ?? 0}${result?.status ? ` · dernier ${result.status}` : ''}`));
      const apps = item.capabilities?.apps || {};
      row.append(
        name,
        badge(item.connection),
        el('span', `SmartTube ${apps.smarttube?.installed ? 'installé' : 'absent'} · AVT ${apps.avt?.installed ? 'installé' : 'absent'}`),
      );
      if (box.id === 'tv-devices') {
        const revoke = el('button', 'Révoquer…', 'button danger-soft');
        revoke.type = 'button';
        revoke.onclick = () => revokeTv(item.device_id);
        row.append(revoke);
      }
      box.append(row);
    }
  }
}
function renderPlaylists() {
  const box = $('playlists-list');
  if (!box) return;
  const tv = state.snapshot?.tv || {};
  const playlists = Array.isArray(tv.playlists) ? tv.playlists : [];
  box.replaceChildren();
  if (!playlists.length) {
    empty(box, 'Aucune playlist', 'Créez une playlist puis ajoutez des URLs SmartTube ou YouTube.');
    return;
  }
  const active = tv.playlist_state;
  playlists.forEach((playlist, index) => {
    const tracks = Array.isArray(playlist.tracks) ? playlist.tracks : [];
    const card = el('article', undefined, 'card playlist-card');
    const heading = el('div', undefined, 'card-heading');
    const title = el('div'); title.append(el('h2', `Playlist ${index + 1} · ${playlist.name}`), el('p', `${tracks.length} morceau${tracks.length > 1 ? 'x' : ''}`, 'helper'));
    const actions = el('div', '');
    const play = el('button', active?.playlist_id === playlist.id && active?.status === 'playing' ? 'En cours' : 'Lancer', 'button secondary');
    play.type = 'button'; play.disabled = active?.playlist_id === playlist.id && active?.status === 'playing';
    play.onclick = () => sendTv({action:'playlist_play', playlist_id:playlist.id}, `Playlist ${index + 1} envoyée à SmartTube.`);
    const remove = el('button', 'Supprimer', 'button danger-soft'); remove.type = 'button';
    remove.onclick = async () => { if (await confirmAction(`Supprimer la playlist ${index + 1} ?`, 'Tous ses morceaux seront retirés de la bibliothèque locale.', 'Supprimer')) await sendTv({action:'playlist_delete', playlist_id:playlist.id, confirm:true}, 'Playlist supprimée.'); };
    actions.append(play, remove); heading.append(title, actions); card.append(heading);
    const form = el('form', undefined, 'playlist-add');
    const field = el('label'); field.append(el('span', 'URL SmartTube / YouTube'));
    const input = el('input'); input.type = 'url'; input.required = true; input.maxLength = 2048; input.placeholder = 'https://www.youtube.com/watch?v=…'; field.append(input);
    const add = el('button', 'Ajouter', 'button'); add.type = 'submit'; form.append(field, add);
    form.onsubmit = async event => { event.preventDefault(); add.disabled = true; try { const result = await sendTv({action:'playlist_add', playlist_id:playlist.id, url:input.value}, 'Morceau ajouté avec ses métadonnées SmartTube.'); if (result) input.value = ''; } finally { add.disabled = false; } };
    card.append(form);
    const list = el('ol', undefined, 'playlist-tracks');
    if (!tracks.length) list.append(el('li', 'Aucun morceau.'));
    tracks.forEach((track, trackIndex) => {
      const row = el('li', undefined, 'playlist-track');
      const info = el('div'); info.append(el('strong', `${trackIndex + 1}. ${track.title || track.content?.id || 'Sans titre'}`), el('small', musicDuration(track.duration_ms)));
      const del = el('button', 'Retirer', 'button ghost'); del.type = 'button'; del.onclick = () => sendTv({action:'playlist_remove', playlist_id:playlist.id, track_id:track.id}, 'Morceau retiré.');
      row.append(info, del); list.append(row);
    });
    card.append(list); box.append(card);
  });
}
async function sendTv(body, success) {
  try {
    const result = await api.post('/api/tv', body, {timeout:body.action === 'playlist_add' ? 22000 : 8500});
    if (result.status === 'rejected' || result.error) throw {code: result.error || 'INVALID_REQUEST'};
    await refreshState();
    if (success) notify(success, 'success');
    return result;
  } catch (error) { fail(error); return null; }
}
async function pairTv() {
  if (!await confirmAction('Appairer une télévision ?', 'Un document d’appairage à usage unique sera téléchargé. Importez-le uniquement sur l’appareil TV prévu. Le code n’est pas affiché dans cette page.', 'Télécharger le document')) return;
  const result = await sendTv({action:'pair', confirm:true});
  if (!result?.document) return;
  const blob = new Blob([JSON.stringify(result.document)], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const link = el('a'); link.href = url; link.download = 'jarvis-tv-pairing.json';
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notify('Document d’appairage téléchargé. Il expire rapidement.', 'success');
}
async function revokeTv(deviceId) {
  if (!deviceId || !await confirmAction('Révoquer cet appareil TV ?', 'Le jeton actuel sera invalidé. L’appareil devra être réappairé. Aucune commande en file ne sera rejouée.', 'Révoquer')) return;
  await sendTv({action:'revoke', device_id:deviceId, confirm:true}, 'Appareil TV révoqué.');
}
function renderServiceState() {
  const box=$('settings-devices'); box.replaceChildren();
  for(const item of state.snapshot?.devices || []) { const row=el('div',undefined,'device-row'); const name=el('div',item.name || item.device_id); name.append(el('small',item.device_id)); row.append(name,badge(item.connection),el('span',`${label(item.confirmed_mode)} · micro ${item.physical?.microphone === true ? 'ouvert' : item.physical?.microphone === false ? 'fermé' : 'non mesuré'}`)); box.append(row); }
  if(!box.childNodes.length) empty(box,'Aucun terminal connecté');
  const diagnostics={versions:state.snapshot?.versions ?? null,storage:state.snapshot?.storage ?? null,budget_diagnostic:state.snapshot?.budget?.diagnostic ?? null,server_epoch:state.snapshot?.server_epoch ?? null};
  const signature=JSON.stringify(diagnostics);
  if(signature!==state.diagnosticsSignature){state.diagnosticsSignature=signature;$('settings-diagnostics').replaceChildren(renderFields(diagnostics));}
}
async function showSettings() {
  try {
    const settings=await loadSettings(); $('settings-form').inert=false;
    $('budget-limit').value=settings.budget?.limit ?? ''; $('budget-period').value=settings.budget?.period || 'month';
    set('budget-choice',settings.budget?.limit == null ? 'CHOIX REQUIS' : 'PLAFOND DÉFINI');
    $('history-enabled').checked=settings.history_enabled === true; $('retention-days').value=settings.retention_days;
    $('memory-enabled').checked=settings.memory_enabled === true;
    $('archive-passive').checked=settings.archive_passive === true; $('passive-retention-days').value=settings.passive_retention_days;
    $('backup-retention-days').value=settings.backup_retention_days;
    set('settings-budget-used',`Consommation : ${settings.budget?.used ?? 'Non mesuré'} requêtes · période commencée le ${date(settings.budget?.period_started_at)}. Plafond vide : activation bloquée.`);
    set('settings-diagnostic-budget',diagnosticBudget());
    set('history-started',settings.history_started_at ? `Historique commencé le ${date(settings.history_started_at)}. Aucun échange antérieur reconstruit.` : 'L’historique commencera à l’activation de cette option.');
    set('memory-started',settings.memory_started_at ? `Mémoire commencée le ${date(settings.memory_started_at)}. Aucun échange antérieur n’est résumé.` : 'La mémoire commencera à l’activation de cette option.');
    const memory=state.snapshot?.memory || {};
    const pending=typeof memory.pending_turns === 'number' ? `${memory.pending_turns} tour(s) en attente de résumé` : 'aucun lot en attente';
    set('memory-status', memory.last_error ? errorLabel({code:memory.last_error}) : `État : ${memory.state || 'disabled'} · ${pending}.`);
    $('clear-memory').disabled = !device() || Boolean(state.pending);
    renderServiceState();
    renderTv();
  } catch(error) { $('settings-form').inert=true; fail(error); }
}
async function saveSettings(event) {
  event.preventDefault(); const button=event.submitter;
  if(!state.settings)return;
  if($('archive-passive').checked && !state.settings.archive_passive && !await confirmAction('Archiver le contexte passif ?', `Les prochains énoncés contextuels seront conservés localement pendant ${$('passive-retention-days').value} jour(s), en plus de la mémoire RAM. Aucun audio brut ne sera conservé.`,'Activer l’archivage')) return;
  if($('memory-enabled').checked && !state.settings.memory_enabled && !await confirmAction('Activer la mémoire persistante ?', 'Les tours confirmés seront résumés en arrière-plan. Chaque résumé consomme le budget nominal. Rien n’est reconstruit avant cette activation. Le résumé survit aux redémarrages jusqu’à un effacement explicite.','Activer la mémoire')) return;
  const body={history_enabled:$('history-enabled').checked || $('memory-enabled').checked,memory_enabled:$('memory-enabled').checked,retention_days:Number($('retention-days').value),archive_passive:$('archive-passive').checked,passive_retention_days:Number($('passive-retention-days').value),backup_retention_days:Number($('backup-retention-days').value),budget:{limit:$('budget-limit').value === '' ? null : Number($('budget-limit').value),period:$('budget-period').value,exhaustion:'block'}};
  button.disabled=true;
  try { await api.post('/api/settings',body); await Promise.all([showSettings(),refreshState()]); notify('Réglages enregistrés. Aucun mode audio n’a été activé.','success'); }
  catch(error){fail(error);}finally{button.disabled=false;}
}
async function purge(scope) {
  const conversations=scope==='conversations';
  if(!await confirmAction(conversations ? 'Purger les conversations enregistrées ?' : 'Supprimer les archives passives ?',conversations ? 'Tous les échanges et leurs relations enregistrées seront supprimés. Le contexte actif et les archives passives sont distincts. Les copies déjà exportées ne sont pas effacées.' : 'Toutes les archives passives seront supprimées. Le contexte actuellement en RAM et les conversations enregistrées restent distincts. Les copies déjà exportées ne sont pas effacées.','Supprimer définitivement')) return;
  try { await api.post('/api/data/purge',{scope,confirm:true}); state.loadGeneration++; state.rowGeneration++; state.latestGeneration++; state.detailGeneration++; state.rows=null; $('detail-dialog').close(); for(const id of ['detail-content','turns-list','latest-turn','table-result'])$(id).replaceChildren(); notify('Suppression confirmée par le stockage.','success'); await refreshState(); }
  catch(error){fail(error);}
}
async function navigate() {
  const previousPage=state.page;
  const page=location.hash.slice(1).split('?')[0]; state.page=['overview','playlists','conversations','context','data','settings'].includes(page) ? page : 'overview';
  for(const section of document.querySelectorAll('.page')) section.hidden=section.id!==`page-${state.page}`;
  for(const link of document.querySelectorAll('[data-page]')) { if(link.dataset.page===state.page)link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); }
  document.title=`${$('title-'+state.page).textContent} · Jarvis Office`;
  if(previousPage!==state.page){window.scrollTo({top:0,behavior:'instant'});const title=$('title-'+state.page);title.tabIndex=-1;title.focus({preventScroll:true});}
  if(!state.online)return;
  try {
    if(state.page==='overview')await loadLatest();
    else if(state.page==='playlists')renderPlaylists();
    else if(state.page==='conversations')await loadConversations();
    else if(state.page==='context')await loadContext();
    else if(state.page==='data')await loadCatalog();
    else await showSettings();
  }catch(error){fail(error);}
}
async function poll() {
  let delay=1500;
  try {
    if(!state.online) { await api.bootstrap(); await refreshState(); await navigate(); }
    else {
      const result=await api.get('/api/events',{after:state.snapshot?.event_id ?? 0,epoch:state.snapshot?.server_epoch});
      if(result.snapshot)renderState(result.snapshot);
      else if(result.reset || result.server_epoch!==state.snapshot?.server_epoch || result.event_id>state.snapshot?.event_id)await refreshState();
      setConnected(true);
      if(state.page==='context' && Date.now()-state.contextLoadedAt>5000)await loadContext(true);
    }
  } catch { setConnected(false); delay=5000; }
  setTimeout(poll,document.hidden ? Math.max(delay,5000) : delay);
}

$('device-select').onchange=()=>{state.device=$('device-select').value; if(state.snapshot)renderState(state.snapshot); if(state.page==='context')loadContext();};
for(const button of document.querySelectorAll('[data-mode]'))button.onclick=()=>sendCommand('set_mode',button.dataset.mode);
$('quick-off').onclick=()=>sendCommand('set_mode','OFF'); $('interrupt').onclick=()=>sendCommand('interrupt');
$('clear-context-overview').onclick=clearContext; $('clear-context').onclick=clearContext;
$('clear-memory').onclick=clearMemory;
$('context-refresh').onclick=loadContext;
$('conversation-search').onsubmit=event=>{event.preventDefault();state.turnOffset=0;loadTurns();};
$('all-sessions').onclick=()=>{state.session='';state.turnOffset=0;for(const item of $('sessions-list').querySelectorAll('button'))item.setAttribute('aria-pressed','false');loadTurns();};
$('sessions-refresh').onclick=()=>{state.sessionsOffset=0;loadSessions();};
$('more-sessions').onclick=()=>{state.sessionsOffset+=20;loadSessions(true);};
$('turns-prev').onclick=()=>{state.turnOffset=Math.max(0,state.turnOffset-20);loadTurns();};
$('turns-next').onclick=()=>{state.turnOffset+=20;loadTurns();};
$('catalog-refresh').onclick=()=>loadCatalog().catch(fail);
$('add-filter').onclick=addFilter;
$('data-query-form').onsubmit=event=>{event.preventDefault();state.dataOffset=0;loadRows();};
$('reset-filters').onclick=()=>selectTable(state.table);
$('data-prev').onclick=()=>{state.dataOffset=Math.max(0,state.dataOffset-Number($('data-limit').value));loadRows();};
$('data-next').onclick=()=>{state.dataOffset+=Number($('data-limit').value);loadRows();};
$('export-data').onclick=exportData;
$('settings-refresh').onclick=showSettings; $('settings-form').onsubmit=saveSettings;
 $('playlist-create-form').onsubmit=async event=>{event.preventDefault();const input=$('playlist-name');const name=input.value.trim();if(!name)return;const result=await sendTv({action:'playlist_create',name},'Playlist créée.');if(result)input.value='';};
$('tv-enable').onclick=()=>sendTv({action:'enable'}, 'Fonction TV activée.');
$('tv-disable').onclick=()=>sendTv({action:'disable'}, 'Fonction TV désactivée. Les sessions ouvertes sont coupées.');
$('tv-save-defaults').onclick=()=>sendTv({action:'defaults', video:$('tv-video-app').value, film:$('tv-film-app').value}, 'Applications TV par défaut enregistrées.');
$('tv-pair').onclick=pairTv;
$('purge-conversations').onclick=()=>purge('conversations'); $('purge-passive').onclick=()=>purge('passive_archives');
$('download-backup').onclick=async()=>{if(await confirmAction('Télécharger une sauvegarde privée ?','Le fichier contiendra la base Office autorisée avec ses conversations et archives. Conservez cette copie dans un emplacement privé. Une purge future ne supprimera pas cette copie.','Télécharger')){try{await download('/api/data/backup',{confirm:true},'office-backup.sqlite3');notify('Sauvegarde cohérente téléchargée.','success');}catch(error){fail(error);}}};
$('close-detail').onclick=()=>$('detail-dialog').close();
$('preview-voice').onclick=async()=>{if(await confirmAction('Tester la voix locale ?', 'Une phrase connue sera prononcée sur le terminal sélectionné. Le microphone reste fermé ; aucun appel cloud ne sera effectué.', 'Lire la phrase'))await sendCommand('preview_voice');};
$('passive-smoke').onclick=async()=>{if(await confirmAction('Démarrer le smoke contextuel ?', 'Préparez une phrase non adressée, puis une question adressée à Jarvis qui utilise ce contexte. La capture démarre uniquement après ce clic confirmé. Un appel diagnostic au maximum peut être consommé ; le plafond nominal reste inchangé. « Couper le micro » reste disponible à tout instant.', 'Je suis prêt · démarrer')){if(await sendCommand('passive_smoke'))notify('Smoke demandé. Attendez l’indication physique « micro ouvert » sur le terminal, dites la phrase non adressée, puis posez votre question à Jarvis. Consultez Contexte, effacez-le, puis revenez sur Micro coupé.','amber');}};
window.addEventListener('hashchange',navigate);
navigate(); poll();
