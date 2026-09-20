# Chantier session vocale Jarvis — lot 1

Statut : **noyau lot 1.1 corrigé et testé, non raccordé au runtime nominal, non déployé**.

Ce document fige l’audit du checkout et le contrat du lot 1. Il ne remplace
pas `PRIVATE_CONTRACT.md`. Les protections sur secrets, permissions, données
privées, V1 et production restent inchangées. L’évolution du semi-duplex et du
mode `PASSIVE` est une **cible non déployée**.

SHA de départ du lot 1 : `ac51e68a25dc201f0fe963347d56bb01712610ac` (`origin/main`).
Repère superviseur `b939ddc10b7c272151ab8ee74ed4b5e1c729e188` : déjà suivi par
`a4b40c9` (CI indépendante) et `ac51e68` (baseline documentée). Ces commits
sont préservés ; ce lot ne les duplique pas.

SHA de départ du lot 1.1 : `d8c757266360933e992b6b3f05a12365678c2805`.
Le noyau examiné est le blob
`c9dd5842c82599e5407440dd58597d6d26e98d3c` (`conversation_session.py` à
`b115702` et toujours à `d8c7572`). Les commits packaging `5bc0416` et
`d8c7572` restent intacts. Worktree concurrent `lot2-tv-parser` : non
modifié.

Les événements wake/parole des tests du noyau sont **simulés**. Ils prouvent
une politique, pas une reconnaissance acoustique, pas un STT réel, pas une
restitution Echo.

## 1. Comportement actuel vérifié

### 1.1 Qui possède la capture, et quand est-elle interrompue ?

Propriétaire unique : `VoiceLoop` (`src/jarvis_office/voice.py`).
`LiveGateway` (`echo/live.py`) est le seul à l’armer sur Echo.
`RemoteEchoAudio.call("listen")` (`echo/audio.py`) ouvre l’uplink
(`audio_start`) et délègue le worker STT/VAD existant. Pas de second
orchestrateur.

Armement : `VoiceLoop.control("resume")` crée `_listen_loop`,
`deadline = perf_counter + Voice.arm_seconds` (défaut 60 s),
`remaining = Voice.arm_turns` (défaut 3). Sur Echo, `LiveGateway.publish`
renouvelle `resume` tant que le mode persisté reste dans `CAPTURE_MODES`
(`ACTIVE`/`PASSIVE`/`COMMAND`) ; l’inactivité ne force pas `OFF`.

La capture s’arrête aujourd’hui lorsque :

- le mode passe à `OFF` (`LiveGateway.stop_audio` puis `voice.control("pause")`) ;
- `interrupt` / `clear_context` / `clear_memory` (product command, persiste `OFF`) ;
- perte de chemin réseau, drop de session, échec worker ;
- `_listen_loop` voit silence, désarmement, tour invalidé, ou échéance d’armement
  avant renouvellement ;
- **chaque énoncé accepté** : le micro est fermé avant STT/réponse
  (`_listen_loop` pose `microphone = closed`, puis `_respond`, puis
  `acoustic_delay`). Semi-duplex actuel : aucune écoute pendant la restitution.

`RemoteEchoAudio` refuse `listen` si `mode == OFF`. Les trames uplink pendant
la parole de Jarvis sont ignorées (`gateway.py`, `speaking_until` /
`speaking_suppressed_frames`) : ce n’est pas un détecteur d’auto-écoute
identifiant le locuteur, seulement une fenêtre de suppression.

### 1.2 Où l’adresse « Jarvis » est-elle vérifiée ?

Uniquement sur le **texte STT**, regex ancrée en tête :

`voice.addressed` — `^\s*jarvis(?=$|[\s,;:.!?…])`, insensible à la casse,
le reste de la phrase est conservé.

Ce n’est **pas** un détecteur acoustique. Aucun moteur wake word n’est chargé.
Une citation (« Je cite Jarvis. ») ou un dérivé (« Jarvisien ») ne matchent pas
(`tests/test_voice.py`).

Consommateurs : `_listen_loop`, `_respond`, `LiveGateway.transcript_context`,
`PassivePolicy.accept` (`echo/context.py`).

### 1.3 Où le mode autorise-t-il une transcription à devenir une demande ?

| Surface | Condition actuelle |
|---|---|
| Echo `ACTIVE` | Toute utterance acceptée est une demande. `transcript_context` pose `conversation_session` ; `_listen_loop` et `_respond` sautent `addressed`. Preuve : `test_active_unaddressed_speech_is_a_turn`. |
| Echo `PASSIVE`, CLI locale | Il faut `addressed(text)` ; sinon `_respond` retourne. Un « Jarvis » seul pose un état local « Présent… » **sans LLM**. |
| Echo `COMMAND` | Jarvis optionnel. `command_session` ; dispatcher TV ; cue OK/erreur ; pas de TTS conversationnel. Hors périmètre de ce chantier. |
| `OFF` | Pas de `listen` distant. |

### 1.4 Comment le contexte ambiant est-il collecté et sélectionné ?

`PassiveContextBuffer` : RAM, horloge injectable, limites 1800 s / 20 000
caractères / 100 énoncés. Eviction FIFO. `select()` borne 3500 caractères /
20 énoncés pour le prochain tour. Le texte rendu dit explicitement que ces
paroles sont une **information**, pas une consigne.

Collecte actuelle seulement si `mode == PASSIVE` et `context_epoch` courant
(`LiveGateway.transcript_context`). `ACTIVE`/`COMMAND` n’y appendent pas.
`speaking=True` (politique unitaire) ignore l’ajout. Archive SQLite
`passive_archives` existante mais **désactivée** (`archive_passive`).
`clear_context` vide le buffer et incrémente `context_epoch`.

Aucun WAV continu par défaut.

### 1.5 Quel événement représente la fin de restitution, notamment sur Echo ?

Ce n’est **pas** la fin du flux LLM, ni la fin d’un segment TTS, ni l’envoi
du dernier paquet.

Chemin Echo :

1. `RemoteEchoEgress.finish` : fin d’alimentation source.
2. Le client pose `playback_drained` pour le `stream_id` courant
   (`gateway.py` commande `playback_drained`).
3. `progress()["done"]` = `input_ended and playback_drained`.
4. `RemoteEchoEgress.drained` envoie `speaking_finished` et incrémente
   `playback_completed_streams`.
5. `VoiceLoop._respond` enregistre `playback = completed_estimated`.

Limite : `playback_drained` est un ACK logiciel d’AudioTrack drainé, pas une
preuve d’écoute humaine ni une confirmation DAC matérielle. Le noyau du lot 1
prend cet ACK comme **meilleure estimation disponible** du protocole actuel.
Ne pas l’inventer comme confirmation physique.

### 1.6 Sessions, tours, annulations, événements tardifs

Identifiants déjà distincts (ne pas les confondre) :

- `server_epoch` : vie du processus gateway ;
- `Session.id` : connexion Echo ;
- `VoiceLoop.session` : conversation renouvelable (uuid, `clear` la change) ;
- `VoiceLoop.turn` : un tour de réponse ;
- `s.generation` / `context_epoch` / `OfficeStore.generation` : invalidation
  des écritures tardives.

`control("pause"|"cancel")` : `armed = False`, nouveau `turn = ""`, abort
audio, drain du task. `clear` en plus : `chat.reset()`, nouveau
`VoiceLoop.session`, reset audio. `stop` ferme les workers.

Product commands : `command_id` dédupliqué (`command_acks`). PCM uplink d’un
ancien `listen_owner` rejeté (`stale_remote_turn`, génération d’ingress).
Une reconnexion Echo restaure le **mode** persisté après `audio_ready`, pas
une conversation ouverte au sens du nouveau contrat (aujourd’hui `ACTIVE`
recommence à traiter toute parole).

### 1.7 Retirer `PASSIVE` sans casser les clients

Aujourd’hui `PASSIVE` est une valeur de protocole, d’UI et de préférence :

- `echo/protocol.py` : `ECHO_MODES`, `CAPTURE_MODES` ;
- `storage.remember_echo_mode` / `echo_mode` ;
- dashboard `index.html` / `dashboard.js` / `dashboard.py` ;
- contrat `confirmed_mode` ;
- dépôt Android séparé `jarvis-office-echo` (`Mode.valueOf` : un mode inconnu
  plante, déjà vu pour `COMMAND`).

Lot 1 : **aucune suppression d’enum ni de valeur persistée**.

Lot 4 (prévu) : garder `PASSIVE` accepté en lecture ; mapper le nouveau mode
d’écoute unique vers une valeur de compatibilité ; migrer APK puis dashboard ;
seulement ensuite retirer le bouton et, plus tard, la valeur.

## 2. Comportement cible (produit, non déployé)

- Écoute activée = capture + collecte de contexte, **sans** réponse spontanée.
- « Jarvis » ouvre une conversation ; ce n’est pas un bouton de capture.
- La demande qui suit le mot d’activation est conservée.
- Après chaque restitution estimée, fenêtre d’inactivité de **120 s**.
- Parole commencée **avant** l’échéance : le tour peut finir après.
- Parole commencée **à l’échéance ou après** : pas de réouverture sans wake.
- Expiration silencieuse : ferme l’autorisation de converser, ne coupe pas
  l’écoute, ne purge pas le contexte, n’efface pas l’historique, pas de phrase
  de clôture, pas d’appel LLM.
- Plus de choix manuel Conversation vs Écoute contextuelle (cible lot 4).
- `OFF` reste un vrai micro coupé.
- `COMMAND` inchangé.

## 3. Contrat du noyau lot 1

Module : `src/jarvis_office/conversation_session.py`.
Tests : `tests/test_conversation_session.py`.
**Non importé** par `voice.py`, `live.py`, le dashboard, l’APK.

Horloge : `time.monotonic` injectable. Jamais d’horloge civile pour les
échéances. Aucun `Timer`/callback : `expire_due()` / `_sync()` comparent des
deadlines. `pending_timers()` vaut toujours 0.

Constante d’inactivité : `IDLE_SECONDS = 120.0`.
Retard admissible d’entrée : `MAX_EVENT_LAG_SECONDS = 2.0`.

Délais techniques (anti-session infinie, distincts des 120 s) :

| Phase | Constante | Rôle |
|---|---|---|
| Parole VAD | `SPEECH_HOLD_SECONDS = 30` | VAD bloqué |
| STT | `TRANSCRIBE_HOLD_SECONDS = 60` | aligné sur `Voice.stt_timeout` |
| Préparation | `PROCESSING_HOLD_SECONDS = 180` | aligné sur le timeout pipeline de `_respond` |
| Restitution | `PLAYBACK_HOLD_SECONDS = 180` | ACK `playback_drained` absent |

Un faux départ VAD / transcript vide restaure l’échéance **précédente** ; il
n’ajoute pas 120 s. Si cette échéance est déjà dépassée : retour en veille.

« Jarvis » seul : ouvre `TalkPermit.OPEN`, `allow_reply=False`, échéance 120 s,
pas d’appel LLM. Pas de session sans échéance.

Auto-écoute : événement `source="self"` ignoré (pas d’ouverture, pas de
prolongation). Ceci teste la politique. La classification acoustique réelle
est le lot 3.

Redémarrage / `reconnect()` : ne ressuscite pas une conversation ouverte.
L’écoute éventuelle reste un `CaptureState` choisi par l’appelant.

### 3.1 États

Deux axes, volontairement **sans** réutiliser `ACTIVE` (qui désigne aujourd’hui
le mode Echo « toute parole est une demande »).

`CaptureState` : `off` | `listening` | `command`

`TalkPermit` : `idle` (veille) | `open`

`TurnPhase` : `idle` | `speech` | `transcribing` | `processing` | `playing`

Un seul tour de réponse à la fois.

### 3.2 Événements et transitions

| Événement | Garde | Effet |
|---|---|---|
| `set_capture(listening)` | — | Collecte autorisée (`collect_context`). Pas d’ouverture de conversation. |
| `set_capture(command)` | — | Hors conversation. Wake/parole conversationnels ignorés. |
| `off` | — | Capture coupée, permit `idle`, génération++, tour aborti. |
| `wake` simulé, residual non vide | `listening`, `arm_id`/`epoch` courants, `at` valide, pas `self`, pas de tour en cours | Permit `open`, `allow_reply`, residual conservé, phase `processing`, échéance 120 s si nouvelle conversation. |
| `wake` simulé, residual vide | idem | Permit `open`, attente 120 s, pas de LLM. Wake nu redondant ignoré. |
| `speech_start` `user`, `at < idle_deadline` | permit `open`, phase `idle` | Réserve le tour (`speech`). N’ajoute pas 120 s. |
| `speech_start` `at >= idle_deadline` | — | Pas de réouverture. Expire si besoin. |
| `speech_start` en veille / `off` / `self` | — | Refus. Collecte inchangée si `listening`. |
| `begin_transcribe` | ids + génération | Phase `transcribing`. L’échéance 120 s ne court pas. |
| `transcript_ready` texte non vide | ids, phase speech/transcribe | `allow_reply`, phase `processing`. |
| `speech_reject` / transcript vide | ids | Restaure l’échéance précédente ; expire si dépassée. |
| `playback_started` | ids, phase `processing` | L’échéance 120 s ne clôt pas. Pas de `processing_started` : le traitement commence à `wake(residual)` ou `transcript_ready`. |
| `note_llm_token` / `note_tts_segment` | — | Aucun renouvellement. |
| `playback_finished` | ids, phase `playing`, `playback_id` nouveau | `idle_deadline = now + 120`. Un doublon est ignoré. |
| `processing_failed` | ids | Restaure l’échéance précédente ou expire. |
| `cancel` | — | Abort + permit `idle`. **N’arrête pas** la capture. |
| `clear` | — | Comme `cancel`. Le noyau **ne purge pas** le contexte. |
| `reconnect` | — | Permit `idle`. Capture inchangée. |
| `expire_due` à l’échéance, phase `idle` | permit `open` | Expire silencieux : pas d’arrêt capture, pas de purge. |

Frontière d’échéance : l’instant de **début de parole** (`speech_start`,
paramètre `at`) est comparé à `idle_deadline` en `>=`. Égalité = trop tard
sans wake. Le STT peut arriver après. L’heure de réception n’est pas
substituée à `at`.

### 3.3 Lot 1.1 — ordre temporel, origines, effets

Horloge de référence : `time.monotonic` injectable. Domaine : réels finis
`<= now`. Un horodatage NaN / ±inf / non numérique est `invalid_timestamp`.
Un `at > now` est `timestamp_in_future`. Un retard `now - at > 2 s` est
`event_too_late`. Ces rejets ont lieu **avant** toute mutation et sans
appeler `_sync()`.

`at` est le temps de capture/préparation. `now` est le temps de réception.
Un début admissible (`at < idle_deadline`, même armement, retard ≤ 2 s)
réserve le tour même si `now` a déjà dépassé l’échéance d’inactivité.

Finalisation d’inactivité : `expire_due()` ne clôt la veille que lorsque
`now >= idle_deadline + MAX_EVENT_LAG_SECONDS`. Dans la fenêtre de grâce,
`expire_due()` puis `speech_start(at)` (ou l’inverse) donnent le même
résultat : tour réservé. Après finalisation, `_bump_epoch()` ; un
événement rétrodaté ne rouvre pas la session. Off, cancel, clear et
reconnect restent prioritaires sur tout événement antérieur.

Origine : `Acquisition(arm_id, epoch)` est prise à l’armement, pas à la
livraison. `wake` et `speech_start` exigent ces identifiants plus `at`.
Un événement d’un ancien armement/époque est `stale_source`. Un `at`
antérieur à `epoch_started_at` est `timestamp_before_invalidation`.
Déduplication bornée (`deque` 32) sur `(arm_id, epoch, kind, at, residual)`.

Effets d’annulation : `_sync()` renvoie une `SessionDecision` ou `None`.
Timeout technique (`turn_hold_timeout`) : `abort_turn=True` et identifiants
`abandoned_*` du tour/restitution **abandonnés**, figés avant invalidation.
L’effet survit si l’événement déclencheur est ignoré (`note_llm_token`,
événement obsolète). Ce n’est **pas** une preuve d’arrêt matériel : le
futur runtime doit drainer l’ancien travail avant d’en lancer un autre.
Expiration d’inactivité en phase `idle` : `silent_expire=True`,
`abort_turn=False`, pas d’arrêt de capture, pas de purge.

Différence `cancel` vs `off` (cible, distincte du `pause` actuel de
`VoiceLoop` qui ferme déjà la capture) :

- `cancel` : plus de réponse tardive, plus d’autorisation de converser ;
  l’écoute `listening` continue ; il faudra « Jarvis » pour répondre.
- `off` : plus aucune capture, collecte, activation.

`VoiceLoop.pause` aujourd’hui coupe l’écoute. Le lot 3 devra cesser d’assimiler
pause conversationnelle et micro coupé.

## 4. Responsabilités

| Composant | Lot 1 | Plus tard |
|---|---|---|
| `ConversationSession` | Décisions déterministes | Inchangé |
| `VoiceLoop` | Inchangé | Consulter le noyau avant `_respond` |
| `RemoteEchoAudio` | Inchangé | Rester propriétaire unique du micro |
| `PassiveContextBuffer` | Inchangé | Collecter en `listening` même en veille |
| `LiveGateway` / protocole / dashboard / APK | Inchangés | Lot 4 |
| Moteur wake word | Absent | Lot 2, après grille de critères |

## 5. Points d’intégration futurs (ne pas câbler maintenant)

1. `VoiceLoop._listen_loop` après `result["accepted"]` — aujourd’hui
   `addressed` + metadata `conversation_session`.
2. `VoiceLoop._respond` — court-circuit `ACTIVE`.
3. `LiveGateway.transcript_context` — append seulement en `PASSIVE`.
4. `RemoteEchoAudio.call("listen")` — la veille cible ne doit plus fermer
   la capture pour « absence d’adresse ».
5. `RemoteEchoEgress.drained` — `playback_finished(playback_id=stream_id)`.
6. `LiveGateway.publish` renouvellement d’armement — rester distinct de la
   fenêtre 120 s.
7. `product_command` / `ECHO_MODES` / dashboard / APK — lot 4.

Le noyau ne doit jamais devenir un second `_listen_loop`.

## 6. Contexte continu — cible, hors lot 1

Réutiliser `PassiveContextBuffer` (limites déjà là). Distinguer contexte
récent RAM, historique SQLite, PCM brut (aucun enregistrement permanent par
défaut). Collecte visible et désactivable. Saturation/panne signalées.

Lot 1 : aucune migration de base, aucune rétention changée, aucun micro ouvert.

Écoute pendant restitution : lots 3/5. Ne pas « résoudre » en fermant le micro
pendant toute la réponse. Ne pas promettre d’interruption vocale sans
qualification distincte.

## 7. Critères d’un futur moteur wake word (aucun retenu)

Avant tout choix : compatibilité matérielle Mac + Echo visé ; détection de
« Jarvis » en usage francophone réel ; latence ; faux positifs / faux
négatifs ; CPU/RAM locaux ; licences **code et modèles** ; distribution des
actifs Office ; **aucun téléchargement au runtime**. Un événement de test
simulé ne sélectionne pas un moteur.

## 8. Risques et limites

- Regex `addressed` ≠ wake acoustique. Les coller casserait le contrat.
- `playback_drained` = estimation protocole, pas preuve d’écoute.
- VAD ≠ identification du propriétaire.
- Clients `PASSIVE` cassés si on retire l’enum trop tôt.
- Semi-duplex actuel : tant que le micro se ferme pendant `_respond`,
  l’écoute continue n’existe pas, même avec ce noyau.
- `COMMAND` doit rester hors de cette machine.
- CI/fiabilisation récente : ne pas y toucher.

## 9. Lots suivants

| Lot | Contenu | Validation | Retour arrière |
|---|---|---|---|
| 2 | Détecteur acoustique + raccord flux, sans changer STT/TTS nominaux | Banc local hors runtime ; faux positifs mesurés ; licences | Retirer le détecteur, garder le noyau inerte |
| 3 | Voix/contexte, restitution, auto-écoute | Tests déterministes + smoke micro **armé** ; pas d’E2E mocké « vert » | Feature flag off : `VoiceLoop` actuel |
| 4 | Modes, protocole, dashboard, APK | Compat `PASSIVE` lu ; APK avant suppression UI | Restaurer les quatre boutons |
| 5 | Qualification matérielle, endurance, déploiement contrôlé | Preuves humaines + Echo réel | Ne pas activer `current` |

## 10. Retour arrière des lots 1 et 1.1 (non exécuté)

Le runtime ignore encore le module. Revenir en arrière =

1. revert des commits du lot 1.1, puis du lot 1 si besoin ;
2. aucun toucher à `current`, LaunchAgent, SQLite privée, APK, services.

Pas de migration à défaire. Le packaging (`check_wheel.py`,
`test_verify_package.py`, CI) n’appartient pas à ces lots : le conserver.
