# Office privé — contrat 1

Le protocole PCM/control reste 1 (header, gain, cadence et profil inchangés).
Les nouvelles capacités `private_state_v1` et `playback_envelope_v1` sont annoncées
dans hello et négociées dans welcome.capabilities. Aucun moteur dans les UI.

## État et commandes

`product_state` transporte un snapshot JSON `schema_version: 1`, `event_id`
(croissant par démarrage serveur), `server_epoch` (UUID de démarrage),
`device_id`, `session_id`, `turn_id`, `stream_id`, `generation`,
`connection` (DISCONNECTED/CONNECTING/CONNECTED/DEGRADED),
`requested_mode`, `confirmed_mode` (OFF/ACTIVE/PASSIVE),
`activity` (IDLE/LISTENING/TRANSCRIBING/GENERATING/PLAYING/ERROR),
`physical: {microphone: bool|null, playing: bool|null, playback_frames: int|null}`,
`error: code|null`, `capabilities`. Inconnu = null. La capture physique vient
d'Android, jamais de `up_stream`. Session neuve/reconnexion = OFF, flux invalidés.

Commandes existantes `set_mode {mode}`, `clear_context {}` et nouvelle
`interrupt {}` acceptent `command_id` dans payload (UUID/identifiant <=64 chars).
`command_ack {command_id, status: applied|rejected, error: code|null}` termine
la demande sous 5 secondes, sinon UI affiche délai dépassé et coupe localement.
Un accusé confirme la transition serveur ; les faits physiques restent séparés.
OFF ferme AudioRecord localement avant tout envoi. Clear/interrupt laissent OFF.
`client_state {microphone, playing, playback_frames, stream_id}` rapporte les
faits physiques au contrôle, hors callbacks audio. Ancien stream rejeté/ignoré.

## Bouche

`playback_envelope {stream_id, turn_id, offset_frames, step_frames, values}` :
valeurs d'énergie normalisées 0..1 calculées sur le PCM downlink existant Mac,
par pas de 960 frames (20 ms à 48 kHz). Au plus 25 valeurs/message, file Android
bornée à 250 valeurs. Offset absolu depuis création du stream. Interpolation
sur la tête de lecture locale ; données absentes/périmées => bouche fermée.
Jamais attendre une enveloppe pour écrire/lire le PCM. Drain/abort/disconnect,
reset de tête ou changement de stream invalident les enveloppes.

## Dashboard/API loopback

HTTP Python éprouvé dans le même processus que LiveGateway. Loopback uniquement,
session cookie HttpOnly SameSite=Strict, contrôle Host/Origin/Fetch Metadata,
CSRF sur toutes mutations, CSP ; tokens Echo refusés pour les API admin.
`GET /api/state` => `{schema_version, server_epoch, event_id, devices: [snapshot],
budget, storage, versions, alerts}`.
`GET /api/events?after=N&epoch=UUID` => `{server_epoch,event_id,reset,events,snapshot}`,
ring <=256, snapshot si curseur perdu ; consultation sans activation audio.
`POST /api/command` => `{device_id,action: set_mode|interrupt|clear_context,
mode?,command_id}` ; même gateway.command que l'APK.
`GET /api/context?device_id=...` => `{entries:[{text,source,age_s,expires_in_s}],
limits:{seconds,chars,utterances},archive_enabled,next_turn_max_chars:3500}`.
`GET /api/settings` / `POST /api/settings` => préférences non secrètes et budget.

`GET /api/data/catalog` => `{tables:[{name,columns:[{name,type,nullable}],
count,counted_at}],restricted, reports}`.
`GET /api/data/rows?table=...&limit=50&offset=0&sort=...&direction=asc&q=...&filters=JSON`
=> `{table,columns,rows,total,limit,offset,truncated}`. Filtres tableau
`[{column,op:eq|ne|lt|le|gt|ge|contains|isnull,value}]`, identifiants allowlist,
valeurs SQL paramétrées ; <=100 lignes/page, <=8 filtres, délais bornés.
`GET /api/data/row?table=...&id=...` => `{table,row}`.
`POST /api/data/export` avec le même périmètre + format csv|json, confirmation
explicite, <=1000 lignes, CSV anti-formules. `POST /api/data/purge` pour archives
ou conversations séparément, confirmation et génération anti-résultat tardif.
`POST /api/data/backup` => téléchargement SQLite cohérent confirmé/authentifié.
Les vues Conversations réutilisent sessions/tours ; aucune démo ni SQL libre.

## Stockage et intégration

SQLite Office privée hors release : devices, sessions, turns, events,
preferences, passive_archives, schema_migrations ; secrets et PCM exclus.
Historique adressé explicite, désactivable, rétention initiale 30 jours, date
d'activation visible. Archives passives désactivées initialement, RAM effective
1800s/20000 caractères/100 énoncés. Consultation ne réinjecte aucune archive.
File d'écritures bornée hors audio, erreurs visibles, transactions courtes.
Journal DELETE initialement (WAL seulement après audit correctifs SQLite).
Sauvegarde API SQLite, restore isolé et foreign_key_check/integrity_check.

L'API expose le compteur diagnostic existant 9/10 séparément du budget nominal.
Budget nominal sans plafond choisi = activation ACTIVE/PASSIVE refusée avec
`NOMINAL_BUDGET_REQUIRED` (sauf smoke diagnostic explicitement déclenché).
Période/plafond/consommation persistés, aucun appel réseau sur GET/heartbeat.

Intégrateur : gateway/live/audio/voice, dépendances, releases, sécurité, matériel.
Lot données/API : nouveaux storage.py/dashboard.py + tests dédiés, aucun autre
fichier partagé sans coordination. Lot web : static/dashboard/* et ses tests.
Lot Android : worktree Android privé uniquement ; aucun ADB ni processus Mac.
