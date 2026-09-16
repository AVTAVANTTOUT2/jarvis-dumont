# Métadonnées de diagnostic privées

Instrumentation locale issue de `6e82247656c55ebf2b08920f3564a61748408f4a`.
Elle n'est pas déployée et ne résout aucune RCA historique. Aucun nouveau point
d'API, stockage, schéma SQLite, réglage ou mécanisme d'armement n'est introduit.
Les champs sont additifs et ne décident ni du routage, ni des délais, ni du mode.

## Commandes

`echo/live.py:product_command` et `command_ack` enrichissent les événements
`command` existants, dans `events.details_json`. Un résumé est proposé au
stockage après chaque envoi d'accusé, réussi ou interrompu, y compris un doublon.
Il rapproche les frontières suivantes sans journaliser chaque étape séparément :

| Champs | Source et sens |
| --- | --- |
| `surface` | `dashboard`, `echo`, `internal`, fixé par l'appel serveur. Le JSON client ne choisit pas ce champ. Cela n'identifie pas une personne. |
| `command_id`, `action`, `mode` | Identifiant protocolaire validé, action et mode de la requête (`INVALID` si le mode est rejeté). |
| `status`, `error`, `deduplicated` | Décision/erreur codifiée et réutilisation éventuelle de l'accusé existant. Un doublon ne réapplique rien. |
| `requested_mode`, `confirmed_mode` | État demandé de la session et mode serveur observés à l'accusé. Un ancien accusé réémis peut donc accompagner un mode actuel différent. |
| `server_epoch`, `echo_connection_session`, `conversation_session`, `conversation_session_after` | Identités distinctes de l'instance, de la connexion Echo et de la conversation, avant/après la commande. Aucune identité de session HTTP ni secret. |
| `generation_before`, `generation`, `context_generation_before`, `context_generation` | Générations de commande et d'effacement de contexte observées aux deux frontières. |
| `turn_id` | Tour déjà possédé par cette connexion à l'entrée, sinon `null`. Aucun tour créé pour tracer une commande. |
| `clock`, `received_ns`, `processing_ns`, `decision_ns`, `ack_finished_ns` | `server_monotonic_ns`, réception fournie par la route, entrée après verrou de commande, décision et fin d'envoi. |
| `ack_send_status` | `SENT` : le transport a retourné normalement sous le verrou de `Session.send`. `SKIPPED_INVALIDATED` : session invalidée pendant l'attente du verrou, transport non appelé. `FAILED` : exception ou annulation propagée. Aucun de ces états ne prouve la réception Android, l'audition ou la fermeture physique du micro. |

L'arrêt interne de fin d'armement utilise aussi `command`, avec `surface=internal`,
`command_id=null`, `related_command_id` et le tour existant ou `null`. Il expose
`received_ns`, `decision_ns`, la génération, les sessions, le mode constaté et
`ack_send_status=NOT_ATTEMPTED` : ce chemin n'envoie pas de `command_ack`.
Ce champ remplace le booléen local non déployé `ack_send_completed`.
Les rejets antérieurs à `product_command` (authentification, identifiant invalide,
appareil absent, certaines actions spécifiques du dashboard) ne sont pas ajoutés
à ce journal. Une interruption avant l'envoi de l'accusé peut laisser ce résumé
absent. Les autres chemins internes de transport ne deviennent pas des commandes
fictives. L'absence d'un événement ne permet pas d'attribuer ou d'exclure une action.

## Payload réellement préparé

`PassiveContextBuffer.select` conserve strictement la sélection de `recent` :
expiration, suffixe récent, ordre, arrêt au premier segment trop grand et limites
inchangés. Chaque nouvelle entrée RAM reçoit un UUID aléatoire, sans empreinte du
texte. Les archives ne sont jamais consultées pour construire le contexte.

Dès la création du tour, `Turn.metrics.request_context` conserve la sélection
disponible après filtrage, indépendamment de la préparation HTTP :

- `turn_id`, `server_epoch`, `echo_connection_session`, `conversation_session`,
  `generation`, `context_generation`, `archive_generation` si disponibles ;
- `available_entries`, `available_chars` après l'éviction normale ;
- `selected_entries`, `selected_chars`, `selected_payload_chars`,
  `selected_entry_ids`, `entry_ids_truncated`, `selection_reason` ;
- `payload_constructed`, `payload_includes_passive_context`,
  `incorporated_entries`, `incorporated_chars`, `incorporated_payload_chars`,
  `incorporation_reason`, `message_count`, `payload_chars`.

Les caractères disponibles/sélectionnés/incorporés comptent les textes retenus,
sans préfixe ni séparateurs. Les compteurs `*_payload_chars` incluent le préfixe
et les sauts de ligne réellement insérés. `payload_chars` compte les contenus de
tous les messages, pas les octets JSON. L'incorporation est constatée dans la liste
de messages passée au constructeur HTTP, après les règles existantes d'historique.
Une intégration sans trace de sélection conserve des nombres d'entrées/caractères
`null` plutôt que de les inventer ; les caractères du payload restent mesurables.

Motifs de sélection : `ALL_SELECTED`, `EMPTY`, `EXPIRED`, `CHAR_LIMIT`,
`UTTERANCE_LIMIT`, `LIMIT_DISABLED`, `INACTIVE_OR_PLAYING`, ou `UNOBSERVED`.
`EXPIRED` signifie que cette sélection a observé l'éviction ; `EMPTY` ne prouve pas
l'absence de contexte antérieur. Incorporation : `NOT_PREPARED` initialement,
puis `INCLUDED` ou `EMPTY_CONTEXT` seulement après le retour du constructeur HTTP.
Un échec de construction, y compris d'encodage, ou une annulation avant la première
exécution de `_read` conserve `payload_constructed=false`, les comptes incorporés
à zéro et les identités/comptes de sélection disponibles. `message_count` et
`payload_chars` restent absents ; la sélection ne prouve aucune incorporation.
Le motif d'échec du tour reste dans `metrics.reason`, sans message brut d'exception.

Une requête préparée peut être refusée par la réservation de budget. Le début
réseau est distinct (`timeline.network_started_s` et `requests=1`). Il ne prouve
pas à lui seul que le fournisseur a reçu la requête. Aucun corps HTTP, texte,
extrait, hash de phrase, cookie ou en-tête n'est exporté par cette instrumentation.

## Capture et segmentation

`RemoteEchoAudio.capture_diagnostics` est un anneau de 20 résumés, recopié dans le
rapport privé existant par `LiveGateway.report`. L'export copie seulement chaque
résumé et son petit dictionnaire `worker` : un réarmement ultérieur enrichit
l'anneau sans modifier l'instantané déjà retourné. Cela n'implique aucune ancienne
modification rétroactive d'un fichier sérialisé. Un résumé correspond à un appel
`listen`, pas à une phrase humaine ni à une garantie de segment complet.

| Groupe | Champs exacts et origine |
| --- | --- |
| Corrélation | `server_epoch`, `turn_id`, `echo_connection_session`, `conversation_session`, `generation`, `context_generation`, `archive_generation`, `stream_id`, `audio_worker_pid` (sinon `null`). |
| Contrôleur | `clock=controller_perf_counter_absolute_seconds`, `listen_requested_s`, `ingress_opened_s`, `last_ingress_opened_s`, `ingress_closed_s`, `transcribing_event_s`, `result_received_s`, `listen_finished_s`, `context_delivery_s`, `rearmed_s`, `input_closed_interval_s`. Certains champs restent absents ou `null` si la frontière n'a pas été observée. |
| Réception serveur | `frame_clock=server_monotonic_ns`, `first_frame_received_ns`, `last_frame_received_ns`, `received_frames`, `received_samples`, `received_audio_s`. Échantillons par canal, durée calculée avec le format du paquet. |
| Destination des trames | `forwarded_frames`, `forwarded_samples` après écriture complète dans le pipe existant ; `ignored_frames`, `ignored_while_closed_frames`, `ingress_failed_frames`. Transmettre au pipe n'est pas consommer dans le worker. |
| Worker | `worker_clock=audio_worker_perf_counter`, `worker_opened_s` reçu dans l'événement `listening` ; `worker` contient seulement `capture_opened`, `first_block_consumed`, `last_block_consumed`, `input_closed`, `vad_finalized`, `input_samples`, `input_blocks`, `input_audio_s`, `normalized_samples`, `utterance_samples`, `callback_dropped`, `reconnects`, `stt_started`, `stt_finished`, `speech_start_estimate`, `speech_end_estimate`, `vad_delay_s`, `pre_roll_s`, `terminal_silence_ms`, `normalized_rate`, `vad_frame_samples`, `rms_capture`, `input_clock`, quand disponibles. |
| Résultat | `segmentation_reason=terminal_silence|max_duration|UNKNOWN`, `status=REQUESTED|LISTENING|RESULT|SILENCE|CANCELLED|FAILED`, `accepted`, `produced_chars`, `context_chars`, `context_entry_id`, `context_outcome`. |
| Complétude | `worker_listen_events`, `counter_saturated`, `diagnostic_incomplete=true` si une mesure worker est rejetée, et compteurs de perte du rapport décrits ci-dessous. |

`REQUESTED` constate l'initialisation de la demande ; `LISTENING` requiert un
événement worker corrélé. Un timeout ou une annulation pendant `audio_start`
finalise le résumé en `FAILED` ou `CANCELLED`, avec `listen_finished_s`, sans
appeler `listen` dans le worker. Sans ouverture observée, les heures d'ouverture
et `ingress_closed_s` restent `null` ; aucune fermeture physique n'est inventée.

`opened_at` et les mesures de `timing` passent par le même filtre avant rétention,
callback et remise du résultat à VoiceLoop :
types JSON `int`/`float` exacts, bornes `[0, 2**63[` vérifiées sans conversion
flottante. Booléens, chaînes, objets, nombres non finis et hors intervalle sont
omis, sans copie de leur représentation. Une mesure invalide retire aussi une
ancienne valeur du même champ ; les autres mesures restent disponibles. Un
`opened_at` invalide est retiré du callback transmis à VoiceLoop ; un `timing`
invalide devient un dictionnaire vide sans bloquer l'événement métier `transcribing`.
La liste autorisée conserve les mesures nominales produites par `AudioEngine.listen` ;
`first_block_consumed`, `speech_start_estimate` et `speech_end_estimate` peuvent
conserver `null`, qui signifie indisponible. `input_clock` accepte seulement
`server_receive_estimate`, `ADC_mapped_to_perf_counter_estimate` et `UNAVAILABLE` ;
`segmentation_reason` accepte seulement les deux motifs du tableau. Les champs
inconnus sont omis et rendent le diagnostic incomplet. Le résultat brut n'est pas
remis à VoiceLoop : son `timing` est remplacé par ce dictionnaire filtré et ses
horodatages de premier niveau sont également filtrés. Un horodatage invalide
présent à ce niveau devient `null` ; aucune valeur brute ne gagne les métriques ou
le rapport par cette voie. Sans début STT disponible, `stt_wait_s` n'est pas calculé.

`context_outcome` vaut `APPENDED`, `ADDRESSED`, `INACTIVE_OR_PLAYING` ou
`MODE_OR_GENERATION_EXCLUDED`. `context_chars` compte le texte livré au point de
décision, avant la normalisation/rétention du buffer ; ce n'est pas le nombre de
caractères finalement retenus. `produced_chars` compte la sortie STT sans la copier.

Les premières/dernières consommations sont horodatées dans le worker lors du
retrait de sa file. Le timestamp transporté dans le pipe provient du contrôleur :
il n'est pas renommé en timestamp du worker. Les durées de fermeture/réarmement
utilisent seulement le contrôleur et ne relient que des générations/sessions
identiques. Elles incluent les traitements intermédiaires, pas seulement le STT.
Les durées STT utilisent seulement le worker. Aucune soustraction avec une horloge
Android n'est introduite. `ingress_closed_s` mesure le retrait d'entrée côté
serveur ; aucune fermeture physique du microphone n'en est déduite.

Les trames rejetées avant `RemoteEchoAudio.feed` et celles d'un ancien stream après
le remplacement du résumé ne sont pas comptées dans ce résumé. Une reconnexion
interne peut ouvrir plusieurs fois le même `listen` : `worker_listen_events`
l'indique, les comptes serveur couvrent cet appel et les métriques worker portent
sur la dernière tentative observable. Un échec STT peut laisser ses timestamps
indisponibles ; la frontière `transcribing` et son motif connu restent conservés.
Ces compteurs ne permettent pas de reconstituer les mots effectivement prononcés.

## Lectures HTTP/SSE

`Turn.metrics.timeline.clock=controller_perf_counter_seconds_since_turn_started`.
Champs : `request_prepared_s`, `network_started_s`, `headers_received_s`,
`first_read_started_s`, `last_read_started_s`, `read_operations`,
`first_read_received_s`, `last_read_received_s`, `read_chunks`, `read_bytes`,
`stop_s`, `response_closed_s`, `finalized_s`, `read_pending_at_stop`.

Les lectures correspondent à `aiter_raw`/`anext` HTTPX observés par le contrôleur,
pas aux paquets TCP ni aux octets arrivés dans le noyau. Un réveil du timer de
segmentation conserve sa lecture en attente et n'incrémente pas `read_operations`.
`stop_s` précède le drainage éventuel. `response_closed_s` n'est renseigné qu'après
le retour réussi de `response.aclose()` ; il reste `null` sans réponse HTTP ou si
sa fermeture échoue ou est annulée. Ce n'est pas une preuve de fermeture du socket physique.
`finalized_s` remplace le champ local `closed_s` et constate la fin du traitement
du tour, y compris après annulation pendant la fermeture de la réponse. Une seconde
demande d'annulation ne réinterrompt pas le drainage déjà engagé ; l'appelant reste
lié à la tâche jusqu'à sa jonction, même si lui-même est annulé. L'annulation de
l'appelant est propagée après la jonction et la libération du tour actif. Aucun
nouvel appel HTTP ni retry n'est introduit. Lors d'une annulation avant `_read`,
`stop_s=finalized_s`, aucune lecture
n'est en attente et aucune requête n'est construite ni démarrée. Les métriques #25
`wire.{chunks,bytes,events,comments}`, `first_content_s`, `generated_chars`,
`http_status`, `phase`, `timeouts`, `transport_exception` restent inchangées.
Le parseur peut traiter une terminaison synthétique à EOF : ses comptes sont
distincts de ceux des lectures. Des événements parsés ne sont pas tous du contenu
accepté. HTTP 200 et keep-alive ne révèlent pas la cause interne de l'attente du
fournisseur ; une réponse rapide ne reproduit pas le défaut réseau historique.

## Bornes, stockage et perte

- Commandes : au plus un événement par passage valide à l'accusé, doublons inclus,
  plus un résumé lors d'une fin d'armement interne. Cache d'accusés inchangé (64).
  Identifiants protocole 1–64 caractères ASCII validés ; sessions/époques/entrées
  générées par UUID (32 hex), identifiant de stream sur 63 bits. Aucune dérivation
  de secret. La surface ne constitue pas une identité humaine.
- Événements : borne existante de 4096 caractères JSON, file existante de 128 par
  défaut, rétention existante de 10 000 événements au plus et durée configurée.
  L'enqueue diagnostique ne bloque pas et laisse au moins la moitié de la file aux
  écritures ordinaires. La classification diagnostique reste attachée à l'opération
  jusqu'à l'exécution de la file, sans changer ordre ni capacité. Indisponibilité,
  dépassement, saturation ou échec d'exécution de cette opération incrémentent
  `OfficeStore.status().diagnostic_dropped`, aussi exposé dans le rapport privé
  comme `storage_diagnostic_dropped`, sans déclencher l'erreur bloquante du produit.
  Les vrais échecs d'écritures métier conservent leur politique d'erreur globale.
- Capture : 20 résumés, sans texte ni tableaux de trames ; export plafonné à 4096
  caractères JSON ASCII par résumé. Un dépassement conserve seulement l'époque,
  le tour et `diagnostic_truncated=true` ; `capture_diagnostics_truncated` compte
  ces résumés dans l'export courant. Champs/énumérations fixes, UUID bornés, compteurs
  saturés à `2**63-1`, nombres worker filtrés finis dans `[0, 2**63[`. L'anneau ajoute
  un nombre borné de champs et de résumés. La borne de 20 × 4096 caractères concerne
  les représentations exportées testées, pas l'allocation des objets Python ni la
  taille du fichier final indenté. Aucune sérialisation ni nouvelle E/S sur chaque
  trame ; le rapport suit sa cadence existante.
- `capture_diagnostics_overwritten` compte les résumés remplacés,
  `capture_diagnostics_stale_events` les événements/résultats non corrélables.
  Ces compteurs de vie d'instance saturent à `2**63-1` (alors lire « au moins »).
  `counter_saturated` signale un compteur de capture tronqué. Aucune file audio
  n'est agrandie pour conserver des diagnostics.
- Requêtes : nombres bornés sur 63 bits, liste d'au plus 20 UUID sélectionnés,
  `entry_ids_truncated` explicite si davantage sont sélectionnés, sans changer
  le payload. Les métriques de tour restent dans l'anneau existant de 20 tours.
  Si les nouveaux diagnostics font dépasser la borne SQLite existante de 16 384
  caractères de métriques, ils sont retirés avant les anciennes métriques ;
  `diagnostic_dropped` et, si la place le permet, `diagnostic_truncated` le signalent.
  Un dépassement déjà causé par les anciennes métriques garde son erreur existante.
- Historique et archivage conservent leurs préférences. Les UUID d'entrées sont
  éphémères, les anciens segments archivés ne reçoivent pas rétroactivement d'ID.
  Les métadonnées ne contiennent aucun contenu humain ; les données existantes
  des fonctions Contexte/Historique restent régies par leurs règles antérieures.

## Validation hors ligne

Les tests des chemins réels enrichis se trouvent dans `test_private_live.py`
(surfaces, falsification, doublon, rejet, échec d'accusé, fin interne, OFF/ACTIVE,
payload, capture/fermeture/réarmement, génération, anneau, nombres worker invalides,
échec d'`audio_start`, invalidation sous verrou, panne d'écriture diagnostique,
instantané), `test_chat.py`
(payload intercepté vide/réduit/expiré/effacé, IDs bornés, préparation sans envoi,
échec d'encodage et annulation avant préparation, commentaire puis contenu et
commentaire seul), `test_echo_live.py`
(pipe, segmentation et frontières du worker avec VAD/STT simulés) et
`test_storage.py` (saturation/troncature et maintien de l'erreur des écritures métier).

Les tests RCA préexistants sont conservés et enrichis. Les fixtures sont
synthétiques ; aucune parole du propriétaire n'est présente. Les tests ne
qualifient pas le rappel humain, l'audition Echo, le STT complet, la fiabilité audio
prolongée ou la latence acoustique. La revue du diff précède toute décision séparée
de mise en service et tout nouvel essai humain autorisé.
