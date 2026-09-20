# Office privé — contrat 1

Le protocole PCM/control reste 1 (header, gain, cadence et profil inchangés).
Les nouvelles capacités `private_state_v1` et `playback_envelope_v1` sont annoncées
dans hello et négociées dans welcome.capabilities. Aucun moteur dans les UI.

## État et commandes

`product_state` transporte un snapshot JSON `schema_version: 1`, `event_id`
(croissant par démarrage serveur), `server_epoch` (UUID de démarrage),
`device_id`, `session_id`, `turn_id`, `stream_id`, `generation`,
`connection` (DISCONNECTED/CONNECTING/CONNECTED/DEGRADED),
`requested_mode`, `confirmed_mode` (OFF/ACTIVE/PASSIVE/COMMAND),
`activity` (IDLE/LISTENING/TRANSCRIBING/GENERATING/PLAYING/ERROR),
`physical: {microphone: bool|null, playing: bool|null, playback_frames: int|null}`,
`error: code|null`, `capabilities`. Inconnu = null. La capture physique vient
d'Android, jamais de `up_stream`. Une session neuve reprend le dernier mode
choisi pour cet appareil (Conversation, Commandes, écoute contextuelle ou micro coupé).
Les flux audio de l'ancienne connexion restent invalidés.

Commandes existantes `set_mode {mode}`, `clear_context {}`, `clear_memory {}`
et `interrupt {}` acceptent `command_id` dans payload (UUID/identifiant <=64 chars).
`clear_memory` exige une confirmation dashboard distincte ; il oublie le résumé
réinjecté après redémarrage, sans supprimer les conversations consultables.
`command_ack {command_id, status: applied|rejected, error: code|null}` termine
la demande sous 5 secondes, sinon UI affiche délai dépassé et coupe localement.
Un accusé confirme la transition serveur ; les faits physiques restent séparés.
OFF ferme AudioRecord localement avant tout envoi. Clear/interrupt laissent OFF
et mémorisent cet arrêt. ACTIVE/PASSIVE/COMMAND restent actifs jusqu'à OFF explicite
ou une erreur de chemin réseau ; une coupure de socket reprend le mode mémorisé
à la reconnexion. L'inactivité ou l'absence de ping ne coupent plus la capture.
Il n'existe aucun quota de tours par activation ; les fenêtres de capture
restent bornées et sont renouvelées sans recréer les moteurs.
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
`POST /api/command` => `{device_id,action: set_mode|interrupt|clear_context|
clear_memory,mode?,command_id,confirm?}` ; même gateway.command que l'APK.
`clear_memory` et `clear_context` exigent `confirm: true`.
Action administrateur `preview_voice` uniquement : phrase TTS locale fixe,
mode OFF et capture fermée ; aucun appel cloud. Aucun token Echo ne peut l'appeler.
Action admin `passive_smoke` confirmée : active PASSIVE après le clic humain,
borne à une question adressée avec le budget diagnostic historique restant,
sans choisir ni consommer le budget nominal. Aucune capture avant le clic.
`GET /api/context?device_id=...` => `{entries:[{text,source,age_s,expires_in_s}],
limits:{seconds,chars,utterances},archive_enabled,next_turn_max_chars:3500}`.
`GET /api/settings` / `POST /api/settings` => préférences non secrètes et budget.
`POST /api/tv` (propriétaire, CSRF, loopback) =>
`{action: enable|disable|defaults|pair|revoke|playlist_create|playlist_add|playlist_remove|playlist_delete|playlist_play, confirm?, device_id?, video?, film?, name?, playlist_id?, track_id?, url?}`.
Aucun jeton TV/Echo. `pair` et `revoke` exigent `confirm: true`. Le document
d'appairage n'est renvoyé qu'à ce POST, jamais dans `GET /api/state`.
Playlists SmartTube numérotées, URLs YouTube persistées dans le registre TV.
L'enchaînement utilise l'état de lecture frais (identité, position, durée
observée ou métadonnée yt-dlp), jamais un minuteur. « suivante » oral saute.

## TV v1 (contrat commun 17 septembre 2026)

Listener TLS LAN distinct (`[tv]` bind/port/cert/key), même processus Office,
jamais le dashboard. Routes appareil : `POST /tv/v1/pair`, `POST /tv/v1/session`,
`GET /tv/v1/commands?connection_id=`, `POST /tv/v1/events`. Auth Bearer uniquement,
jamais dans l'URL. `device_id` UUID (le testhost `tv-test` n'est pas compatible).
File bornée, reconnexion sans replay, `completed` jamais déduit d'un intent.
Le dispatcher TV ne s'exécute qu'en mode Echo Commandes (`COMMAND`) : Jarvis y
est optionnel, JSON texte validé, jamais `tool_calls`, jamais d'historique ni de
phrase TTS (son OK ou erreur). Conversation (`ACTIVE`) reste du chat. SQLite
schéma 2 inchangé : registre TV dans `preferences`.
Avant extracteur IA, une décision locale déterministe
(`MATCH` / `REJECT` / `AMBIGUOUS` / `NO_MATCH`) borne la grammaire : un refus
ou une ambiguïté n'appelle pas `extract()`, ne prépare pas la TV et n'émet
aucune commande. `NO_MATCH` n'autorise l'extraction que pour une demande média
plausible déjà filtrée. Formulations, limites et corpus :
`docs/TV_INTEGRATION_HANDOFF.md`, `tests/tv_command_corpus.py`. Ce n'est pas
une homologation vocale.
Statut : candidat logiciel, non déployé, non homologué matériel.

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

SQLite Office privée hors release : devices, sessions, turns,
conversation_memory, events, preferences, passive_archives, schema_migrations ;
secrets et PCM exclus. Schéma 2. Historique adressé explicite, désactivable,
rétention initiale 30 jours, date d'activation visible. Mémoire persistante
explicite, liée à l'historique, sans backfill : un résumé borné plus les derniers
tours confirmés, rollups nominaux préemptibles, rétention jusqu'à « Effacer la
mémoire ». Archives passives désactivées initialement, RAM effective
1800s/20000 caractères/100 énoncés. Consultation ne réinjecte aucune archive.
File d'écritures bornée hors audio, erreurs visibles, transactions courtes.
Journal DELETE initialement (WAL seulement après audit correctifs SQLite).
Sauvegarde API SQLite, restore isolé et foreign_key_check/integrity_check.

L'API expose le compteur diagnostic existant 9/10 séparément du budget nominal.
Budget nominal sans plafond choisi = activation ACTIVE/PASSIVE/COMMAND refusée avec
`NOMINAL_BUDGET_REQUIRED` (sauf smoke diagnostic explicitement déclenché).
Période/plafond/consommation persistés, aucun appel réseau sur GET/heartbeat.

Intégrateur : gateway/live/audio/voice, dépendances, releases, sécurité, matériel.
Lot données/API : nouveaux storage.py/dashboard.py + tests dédiés, aucun autre
fichier partagé sans coordination. Lot web : static/dashboard/* et ses tests.
Lot Android : worktree Android privé uniquement ; aucun ADB ni processus Mac.
# Apparence synchronisée — extension optionnelle

`preferences.orb_style` vaut `auto` (défaut), `working`, `searching`, `solving`,
`listening`, `connecting`, `weaving`, `composing`, `breathing` ou `shaping`.
Le dashboard utilise le POST `/api/settings` existant, avec contrôle propriétaire
et CSRF. Ce choix global d’apparence est commun au dashboard et aux terminaux.
`product_state.orb_style` transporte le dernier choix persisté ; les anciens
clients peuvent ignorer ce champ. Aucun changement de schéma SQLite ou PCM.

Sur le canal Echo déjà authentifié, `set_orb_style` reçoit exactement
`{command_id, orb_style}`. `orb_style_ack` renvoie `{command_id, status, error}`,
puis `product_state` confirme le choix. Le client ne propose la modification
que si le serveur annonce un style reconnu. Pas de retry automatique ; un délai
de cinq secondes donne une erreur d’apparence sans modifier le propriétaire
audio, le mode, le tour, le contexte ou le budget. Le Mac reste autoritaire
après reconnexion, et le dernier choix confirmé est mis en cache sur l’Echo.

Automatique associe connexion à Connecting, repos à Breathing, micro physique
ouvert à Listening, transcription à Searching, génération à Solving, lecture
physique à Composing. Un style choisi reste animé selon l’activité. Seul l’Echo
dispose du niveau de capture et de l’enveloppe au playback head : il module la
taille avec ces mesures, jamais avec des syllabes synthétiques. Le dashboard
ne représente que l’activité. Les animations respectent le mouvement réduit
et s’arrêtent hors visibilité. Aucun moteur, flux audio ou appel LLM créé par
le rendu ou par le choix d’apparence.
