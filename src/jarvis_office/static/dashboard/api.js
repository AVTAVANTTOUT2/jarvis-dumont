const ERROR_LABELS = {
  KNOWN_STT_LIMITATIONS: 'Des erreurs de transcription connues subsistent.',
  KNOWN_PLAYBACK_LIMITATIONS: 'Des irrégularités de lecture connues subsistent.',
  NOMINAL_BUDGET_REQUIRED: 'Choisissez d’abord un plafond d’usage dans Réglages.',
  NOMINAL_BUDGET_EXHAUSTED: 'Le plafond de cette période est atteint. Les nouveaux appels sont bloqués.',
  BUDGET_EXHAUSTED: 'Le plafond de cette période est atteint.',
  DEVICE_NOT_FOUND: 'Ce terminal n’est plus disponible.',
  DEVICE_DISCONNECTED: 'Le terminal est déconnecté. Vérifiez son état sur l’Echo.',
  TIMEOUT: 'Le délai de réponse est dépassé. L’état physique doit être vérifié sur le terminal.',
  NETWORK: 'Le service Office est inaccessible. Reconnexion en cours.',
  AUTH_REQUIRED: 'La session locale a expiré. Reconnexion en cours.',
  FORBIDDEN: 'Cette opération a été refusée par les protections d’accès.',
  STORAGE_ERROR: 'Le stockage signale une erreur. Consultez les diagnostics.',
  MEMORY_REQUIRES_HISTORY: 'Activez d’abord la conservation des conversations.',
  MEMORY_ROLLUP_FAILED: 'Le résumé n’a pas pu être mis à jour. Le contexte déjà chargé reste utilisé.',
  COMMAND_FAILED: 'La commande n’a pas pu être appliquée. Vérifiez l’état du terminal.',
  TV_NOT_CONFIGURED: 'Le listener TV n’est pas configuré sur ce Mac.',
  TV_DISABLED: 'La fonction TV est désactivée. Activez-la avant d’appairer.',
  CONFIRMATION_REQUIRED: 'Cette action TV exige une confirmation explicite.',
  INVALID_REQUEST: 'La demande est invalide. Vérifiez les champs et leurs limites.',
  INVALID_URL: 'Utilisez une URL HTTPS SmartTube ou YouTube valide.',
  METADATA_UNAVAILABLE: 'Les métadonnées SmartTube ne sont pas disponibles.',
  PLAYLIST_NOT_FOUND: 'Cette playlist n’existe plus.',
  PLAYLIST_EMPTY: 'Cette playlist ne contient aucun morceau.',
  PLAYLIST_LIMIT: 'La playlist a atteint sa limite de morceaux.',
  TRACK_NOT_FOUND: 'Ce morceau n’existe plus dans la playlist.',
  PLAYLIST_NOT_RUNNING: 'Aucune playlist n’est en cours.',
  QUERY_RESULT_TOO_LARGE_REDUCE_PAGE: 'Ce résultat dépasse la limite de 4 Mio. Réduisez la taille de page ou ajoutez un filtre.',
  connect_timeout: 'Délai du client HTTP DeepSeek du Mac : connexion au service non établie à temps.',
  first_content_timeout: 'Délai du client HTTP DeepSeek du Mac : aucun texte utile reçu avant l’échéance de premier contenu.',
  idle_timeout: 'Délai du client HTTP DeepSeek du Mac : plus aucun texte reçu après un début de réponse.',
  total_timeout: 'Délai du client HTTP DeepSeek du Mac : durée totale de réponse dépassée.',
  transport_timeout: 'Délai du client HTTP DeepSeek du Mac pendant l’échange (lecture, écriture ou pool de connexions).',
};

export function errorLabel(error, fallback) {
  return ERROR_LABELS[error?.code] || fallback || 'La demande n’a pas abouti. Actualisez l’état avant de réessayer.';
}

export function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, typeof value === 'object' ? JSON.stringify(value) : String(value));
    }
  }
  return query.toString();
}

export function acceptsSnapshot(current, next) {
  if (!next || !Number.isSafeInteger(next.event_id) || !next.server_epoch) return false;
  return !current || current.server_epoch !== next.server_epoch || next.event_id >= current.event_id;
}

export function typedValue(type, value, operation) {
  if (operation === 'isnull') return true;
  if (/INT|REAL|FLOAT|DOUBLE|NUMERIC/i.test(type)) {
    if (!String(value).trim() || !Number.isFinite(Number(value))) throw new Error('Valeur numérique requise.');
    return Number(value);
  }
  return value;
}

export class OfficeAPI {
  constructor() { this.csrf = null; }
  async bootstrap() {
    const data = await this.request('/api/bootstrap');
    if (typeof data.csrf_token !== 'string') throw {code: 'AUTH_REQUIRED'};
    this.csrf = data.csrf_token;
  }
  async request(path, {body, download = false, timeout = 8000} = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = {'Accept': download ? 'application/octet-stream' : 'application/json'};
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        headers['X-CSRF-Token'] = this.csrf || '';
      }
      const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST', headers,
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        signal: controller.signal, ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      if (!response.ok) {
        let code = response.status === 401 ? 'AUTH_REQUIRED' : response.status === 403 ? 'FORBIDDEN' : 'INVALID_REQUEST';
        try {
          const data = await response.json();
          const candidate = typeof data.error === 'string' ? data.error : data.error?.code;
          if (typeof candidate === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(candidate)) code = candidate;
        } catch { /* Do not display server bodies or transport exceptions. */ }
        throw {code};
      }
      return download ? await response.blob() : await response.json();
    } catch (error) {
      if (error?.code) throw error;
      throw {code: controller.signal.aborted ? 'TIMEOUT' : 'NETWORK'};
    } finally { clearTimeout(timer); }
  }
  get(path, query = {}) { const suffix = queryString(query); return this.request(path + (suffix ? '?' + suffix : '')); }
  post(path, body, options = {}) { return this.request(path, {...options, body}); }
}
