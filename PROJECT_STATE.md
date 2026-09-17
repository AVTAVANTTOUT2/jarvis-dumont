# État du projet

## Orbes interactives — développement, 16 septembre 2026

- Neuf Thinking Orbs `0.3.1` de Libraries.dev, moteur MIT local sans React,
  et choix automatique selon l’activité. Galerie dans le dashboard ; aperçu
  et choix natifs dans l’APK Kotlin, dans le worktree Echo dédié aux orbes.
- Préférence `orb_style` validée et persistée dans SQLite, partagée avec
  `product_state`. Commande Echo `set_orb_style` distincte des commandes audio ;
  une erreur ne coupe ni le micro ni la réponse. Aucun changement de schéma.
- Validation logicielle et aperçu web sur stockage temporaire uniquement.
  257 tests Python, 8 JavaScript et 20 JVM Android réussis ; Ruff, Mypy,
  lint Android debug/release, wheel et APK debug/release construits.
  Cette version n’est pas activée en production ni installée sur l’Echo ;
  validation visuelle et performance matérielle restent à effectuer.

## Travail local — lot Jarvis TV v1, 17 septembre 2026

- Worktree `feature/tv-v1-office` : serveur `/tv/v1`, file bornée, dispatcher
  vocal après adresse Jarvis, UI dashboard loopback. Schéma SQLite 2 inchangé
  (`preferences.tv_registry`). La release privée active est construite et activée
  depuis un commit ; aucun runtime de production ne démarre depuis le checkout.
- Candidat intégré dans la release privée active via le pointeur `current`. Le service AVT
  Binder et le récepteur TV existent dans leurs lots.
  Testhost corrigé avec UUID. Qualification Philips et livraison privée terminées ;
  aucun résultat matériel n'est déduit des tests unitaires.

### Intégration privée et corrections matérielles

- Release TV `0.3.0-1bcd877e8f29` construite depuis le commit `1bcd877e8f290a3fac46474f17f2f837e8167b01`
  et activée atomiquement. Mode Echo OFF et micro fermé vérifiés ; branche privée poussée.
- Le pool DeepSeek est borné à deux connexions afin qu'un tour vocal garde une
  connexion pendant l'annulation d'un rollup mémoire ; le `PoolTimeout` du tour
  Echo du 17 septembre est corrigé.
- Philips Android 14 : APK récepteur et AVT debug installés côte à côte avec
  l'AVT habituel, conservé. Binder réel : profil confirmé et trois résultats
  de catalogue ; appairage TLS réel par le formulaire du récepteur réussi.
- File Office corrigée : une commande de lecture après une consultation ne
  provoque plus de KeyError ; une commande déjà rejetée ne quitte plus la file.
  Huit commandes en attente maximum, historique RAM borné à 128 résultats,
  expiration et résultat tardif sans réactivation d'une commande abandonnée.
- Validation du correctif : 278 tests Python, tests dashboard, Ruff, format et
  Mypy réussis. L'endurance TV réelle de 30 minutes est passée sans écart.
  Ces vérifications ne constituent pas une confirmation humaine d'image/son.
- Correctif de confirmation vocale activé : profil de recherche transmis à AVT ;
  ancienne lecture, autre profil/épisode, chargement et pause ne confirment
  jamais le nouveau lancement. Échecs reproduits puis 275 tests Python,
  dashboard, Ruff/format et Mypy réussis.
- Philips : Binder réel et transports AVT/SmartTube validés, y compris via HTTPS
  sans ADB. Redémarrage TV avec retour automatique du récepteur sans lecture.
  Corrections TV : observations indépendantes du long-poll, conversion d'horloge,
  timeout client supérieur à l'attente serveur. Aucun micro armé pour ces essais.
- SmartTube : `yt-dlp` local résout les titres sans télécharger de média, valide
  les identifiants YouTube et choisit le premier résultat. Le test réel
  « Petunia de Werenoi » a ouvert SmartTube sur la Philips et affiché le clip.
- Playlists SmartTube (worktree `jarvis-office-tv-final`, non activées dans la
  release courante) : page dashboard, persistance locale, « playlist numéro N »
  et « suivante ». L'auto-avancement lie l'identité observée (SmartTube ne
  renvoie pas `playback_id` dans `play_content`) et la position réelle, durée
  yt-dlp si `duration_ms` manque. Preuve unitaire seulement, pas un enchaînement
  matériel Philips.
- Compréhension : le parseur local accepte les formulations polies et le prompt
  d'extraction demande une étape vérifiable au lieu d'un refus global ; six
  extractions DeepSeek réelles ont renvoyé `smarttube/search` sans action TV.

## Mise en service — Prompt vocal, 16 septembre 2026

- Release `0.3.0-72b08a88cbc6` vérifiée puis activée atomiquement ; la
  précédente `0.3.0-bae9d90c432f` reste disponible. Le service privé a
  redémarré prêt et en pause, micro fermé, sans écoute ni erreur runtime.
- Le prompt système le présente comme assistant vocal Echo ; les étiquettes
  mémoire/contexte ne sont plus « donnée non fiable ». Le résumé persisté a
  été effacé (révision 4, texte vide) sans supprimer les tours archivés.
  Aucune requête DeepSeek n'a été déclenchée pour cette bascule.

## Mise en service — Mode Echo persistant, 16 septembre 2026

- Release `0.3.0-bae9d90c432f` vérifiée puis activée atomiquement ; la release
  précédente `0.3.0-90e679016c44` reste disponible. Le service privé a
  redémarré prêt et en pause, micro fermé, sans écoute ni erreur runtime.
- L'inactivité et l'absence de ping ne coupent plus la capture. Le dernier
  mode choisi (Conversation, écoute contextuelle ou micro coupé) est mémorisé
  par appareil et repris à la reconnexion. OFF explicite, clear/interrupt et
  perte du chemin réseau restent des arrêts. Aucun bump de schéma SQLite.
- 254 tests réussis. Aucune requête DeepSeek ni capture matérielle déclenchée
  par ces contrôles.

## Mise en service — DeepSeek V4.1 Flash, 16 septembre 2026

- Release `0.3.0-90e679016c44` vérifiée puis activée atomiquement ; la release
  précédente `0.3.0-8b72c208029f` reste disponible. Le service privé a
  redémarré prêt et en pause, avec le microphone fermé, aucune écoute et
  aucune erreur runtime.
- Les TOML privés demandent désormais `deepseek-flash` (DeepSeek-V4.1-Flash),
  non réfléchissant. L'ancien identifiant `deepseek-v4-flash` n'est plus
  envoyé. Aucune requête DeepSeek n'a été déclenchée pour cette bascule.
- Le manifeste conserve `sqlite_schema_max: 2` ; la mémoire persistante et
  le schéma SQLite restent ceux de la mise en service précédente.

## Mise en service — Mémoire persistante, 16 septembre 2026

- Release `0.3.0-8b72c208029f` vérifiée puis activée atomiquement ; la release
  précédente `0.3.0-de24890214de` reste disponible. SQLite a migré en schéma 2
  après sauvegarde isolée. Le service privé a redémarré prêt et en pause, avec
  le microphone fermé, aucune écoute et aucune erreur runtime.
- La mémoire persistante et l'historique adressé sont activés depuis le
  dashboard loopback. Aucun résumé antérieur n'a été reconstruit. Le résumé
  n'est chargé qu'à une prochaine activation Conversation/Écoute contextuelle.
- Le manifeste de cette candidate porte `sqlite_schema_max: 2`. Un retour vers
  une ancienne release au schéma 1 reste refusé tant que la base active est en v2.

## Mise en service — Conversation continue et phrases longues, 16 septembre 2026

- Les modes Echo ACTIVE/PASSIVE ne s'arrêtent plus après le compteur technique
  de tours : chaque fenêtre de capture reste bornée et se renouvelle tant que
  le mode est autorisé. OFF, déconnexion et erreur restent des arrêts fermes ;
  le smoke diagnostic conserve sa limite explicite d'un tour.
- Le délai de fin de parole par défaut tolère désormais une pause naturelle
  d'une seconde au milieu d'une phrase et finalise après environ 1,5 seconde
  de silence. La durée maximale d'un énoncé et le profil audio restent inchangés.
- Régressions rouge/vert ajoutées. Suite complète : 235 tests réussis ; Ruff,
  format sur 76 fichiers, mypy strict sur 35 modules et build wheel/sdist réussis.
  Aucune capture matérielle ni requête DeepSeek n'a été déclenchée par ces contrôles.
- Release `0.3.0-de24890214de` vérifiée puis activée atomiquement ; la release
  précédente reste disponible. Le service privé a redémarré prêt et en pause,
  avec le microphone fermé, aucun tour ni transcription sur le nouveau processus
  et aucune erreur runtime.

## Travail local — Conversation sans préfixe Jarvis, 16 septembre 2026

- Sur `conversation-all-speech` : en mode Conversation (ACTIVE), chaque
  transcription acceptée est une demande DeepSeek/TTS. PASSIVE, OFF, CLI
  locale et le plafond de tours par armement restent inchangés.
- Aucune release, activation, capture matérielle ou tentative cloud
  supplémentaire n'est effectuée par ce travail source.

## État courant — candidate intégrée en service, 15 septembre 2026

- Release active : `0.3.0-83cf03dfa735`, source revue
  `83cf03dfa7351999e962f710388e73775068d159`. Construction hors réseau depuis
  le commit, wheel non editable, trois environnements définitifs et actifs vérifiés.
  SHA-256 wheel : `9a6b065dec800581de8e7412167f37a4de38a94ce8d5a98981b68b1a45fc5163`.
- Contre-revue finale indépendante : `READY_FOR_CANDIDATE_PREPARATION`,
  B1–B6/N1 et supervision confirmés ; 156 tests distincts réussis, aucun échec,
  erreur, skip ou déclenchement de garde. Un test nécessitant de tuer un enfant
  réel est exclu avant constitution de cette suite. Ruff/format (76 fichiers),
  mypy (35 modules) et les 7 tests Node passent ; ces périmètres ne s'additionnent
  pas en une qualification matérielle.
- Sauvegarde privée et restauration SQLite isolée validées avant bascule atomique.
  Ancienne release #25 vérifiée et conservée pour rollback, non exécuté.
  Le job privé, précédemment arrêté, a été démarré depuis la candidate à 16:45 UTC.
- Vérification du runtime à 16:47 UTC : 20/20 contrôles de santé réussis,
  identité installée et époque serveur stables, moteurs prêts, état paused,
  Echo CONNECTED/OFF. Télémétrie Android fraîche : microphone false et playing false.
  Dashboard HTTP 200, fermeture de la session de contrôle HTTP 204.
- Aucune nouvelle capture, transcription, réponse TTS ou tentative cloud.
  Warmup local attesté indirectement par la readiness ; sa branche produit ne
  livre pas le PCM au lecteur. Les files internes et l'écoute acoustique ne sont
  pas directement mesurées. Aucun changement APK, configuration, plafond ou appairage.
- Diagnostic 10/10 inchangé par empreinte. Ligne nominale SQLite inchangée
  (7/500 pour le 14 septembre) ; dashboard 0/500 pour le 15 septembre par calcul
  journalier existant. Les cinq archives présentes sont conservées à l'identique ;
  seule la date de dernière présence de l'appareil change dans les données SQLite.
- PR #24 et #25 restent ouvertes ; aucune publication ni fusion GitHub.
  Le présent bilan documentaire est postérieur au commit de la release.
- Restent ouverts : premier contenu DeepSeek et réponse réellement entendue,
  fidélité STT, rappel PASSIVE, anti-réinjection, fiabilité prolongée et latence
  acoustique. La cause historique de l'arrêt du superviseur n'est pas établie.

## Historique — intégration locale avant activation, 15 septembre 2026

- Base privée #25 : `6e82247656c55ebf2b08920f3564a61748408f4a`.
  Intégration sur `codex/private-diagnostics-integration`, séparée des worktrees
  d'origine. Aucune nouvelle release activée, capture ou tentative cloud.
- La première contre-revue a confirmé B2, B3, B4, B5 et N1, mais reproduit deux
  défauts : une mesure worker rejetée encore exportée via VoiceLoop (B1), et une
  finalisation manquante après annulation pendant la fermeture HTTP (B6).
  Les corrections filtrent désormais résultats et callbacks worker avant
  VoiceLoop, conservent les mesures nominales admises et finalisent les tours
  avec jonction protégée pendant la fermeture annulée. Contrôle de l'implémenteur :
  103 tests distincts réussis, dont 5 reproductions indépendantes réutilisées,
  sans déclenchement de garde ; Ruff/format réussis. Revue finale indépendante
  encore requise ; deux tests de pipes sont confiés à son exécuteur dédié.
- Isolation des journaux de tests et validation des caractères de contrôle du
  périphérique d'entrée reprises depuis les correctifs Claude existants, avec
  adaptation aux deux tests `run_command` réellement présents sur #25.
  Revue indépendante : 7 tests distincts réussis, Ruff/format et diff check
  réussis, aucun accès production ou réseau. Ce résultat couvre ces adaptations.
- Supervision : journal expurgé des phases et sorties, 64 événements et deux
  fichiers de 64 Kio maximum, avec conservation du journal précédent lors de
  la reprise. Les trois tentatives, délais, signaux et contrat OFF restent
  inchangés. Le compteur de tentative après arrêt entre deux essais a été
  corrigé après reproduction. 11 tests distincts, Ruff/format, typage et diff
  check réussis ; revue finale de l'intégration encore requise.
- Les 7 tests Node du dashboard passent. Les compteurs ci-dessus décrivent
  des périmètres distincts ou recouvrants ; ils ne sont pas additionnés en un
  total de qualification. Aucune preuve simulée ne vaut validation matérielle.
- Le dernier audit opérationnel du 15 septembre a trouvé le job privé chargé
  mais arrêté (confirmé à 16:14 UTC), avec la release sélectionnée
  `0.3.0-6e82247656c5`. L'ancien état
  STARTING ne prouve pas une instance vivante ; la cause de sortie reste inconnue.
  Les observations de service actif ci-dessous sont historiques.
- Rappel PASSIVE, fidélité STT réelle, réponse Echo entendue, anti-réinjection,
  fiabilité audio prolongée et latence acoustique restent non qualifiés.

## Historique local — premier bilan des correctifs, 15 septembre 2026

- Travail local dans le worktree isolé issu de `6e82247656c55ebf2b08920f3564a61748408f4a` ;
  aucune release construite, activation, modification Android, configuration ou budget.
  Les trois tests RCA préexistants non commités sont conservés et enrichis.
- Instrumentation additive des commandes, du contexte réellement incorporé aux
  messages HTTP, des frontières capture/STT/réarmement et des lectures HTTP/SSE.
  Schéma, bornes, sources et limites : [DIAGNOSTIC_METADATA.md](DIAGNOSTIC_METADATA.md).
  Aucun texte, hash de phrase, secret ou PCM ajouté aux nouvelles métadonnées.
- Correctifs locaux B1–B6 après revue indépendante : filtre numérique worker avant
  rétention et callback, pertes SQLite diagnostiques isolées, finalisation des
  échecs d'audio_start, résultat d'envoi observé sous verrou, preuve de
  non-préparation et finalisation des annulations précoces de tour. N1 traité :
  les résumés exportés sont des instantanés ; l'anneau garde son enrichissement.
  Neuf nouvelles méthodes de régression, défauts reproduits avant correction ;
  les échecs d'écriture métier conservent leur erreur globale.
- Vérifications hors ligne : 130 tests distincts réussis, Ruff/format et mypy
  (35 modules) réussis ; réseau externe et accès aux fichiers de production
  interdits avant les imports, huit modules directement vérifiés dans ce worktree.
  Les contrôles de santé sont des tests loopback isolés. Trois tests VoiceTests
  de démarrage/sous-processus exclus ; aucun test matériel ou moteur réel.
  Ce contrôle de l'implémenteur prépare une relecture ciblée, sans constituer une
  nouvelle revue indépendante ni une autorisation de déploiement.
- Production observée en lecture seule le 14 septembre à 22:15 UTC : toujours
  `0.3.0-6e82247656c5`, instance identique, service prêt, Echo connecté OFF,
  flux arrêtés, contexte RAM vide, aucune configuration d'enregistrement active
  dans Android ; diagnostic 10/10 et nominal 7/500 sur la journée UTC du 14.
  Historique désactivé, archivage passif activé : préférences conservées.
- Cette instrumentation n'est pas en service. Les causes des échecs humains
  restent ouvertes : attente du premier contenu, origine des changements de mode,
  coupure signalée et inclusion réelle du contexte lors de l'ancien essai.
  Aucun nouveau test humain ni appel d'inférence n'a été effectué.
- Les sections suivantes conservent leur chronologie historique ; elles ne
  remplacent pas ce constat courant. Le PROJECT_STATE du worktree de déploiement,
  déjà modifié avant cette mission, n'a pas été écrasé.

## Délai du flux DeepSeek — candidate du 14 septembre 2026

- Incident du smoke PASSIVE humain sur la release active 0.3.0-70d83c3eed73 :
  HTTP 200 `text/event-stream`, zéro texte généré, `transport_timeout` après
  10,36 s mesurées depuis la création du tour. Délais effectifs vérifiés dans
  `config/private-voice.toml` : connexion 10 s, premier contenu 20 s, inactivité
  10 s, total 60 s ; `deepseek.py`/`config.py` installés identiques à `70d83c3`.
- Défaut client démontré : le client HTTPX imposait un délai de lecture socket
  égal à `idle_timeout` (10 s). Un flux muet après les en-têtes expirait donc à
  10 s + connexion, avant l'échéance applicative de premier contenu (20 s).
  Reproduit sur origine HTTP loopback réelle, sans MockTransport, avec les
  rapports de production réduits (0,6/1,2/0,6/3,6 s) : silence 0,9 s puis
  réponse valide donnait `transport_timeout` ; après correctif, réponse livrée.
- Correctif minimal : délai de lecture HTTPX = max(premier contenu, inactivité).
  La boucle `Turn._read` reste seule juge des échéances premier contenu,
  inactivité et totale, toujours antérieures ; connexion, écriture, pool,
  annulation et absence de retry inchangés. Six scénarios loopback dans
  `tests/test_chat.py` (silence puis réponse, commentaires keep-alive muets,
  inactivité après contenu, aucun contenu, annulation, réutilisation après
  échec). Métriques ajoutées : phase, délais effectifs, compteurs réseau/SSE
  bornés et nom de classe de l'exception HTTPX, jamais son message. Dashboard :
  libellés neutres localisant chaque délai sur le client HTTP DeepSeek du Mac,
  sans préjuger de l'état de la liaison Echo–Mac.
- Non établi : pourquoi aucun texte utile n'a été reçu pendant environ 10 s
  après les en-têtes. Les compteurs réseau historiques (blocs, octets,
  commentaires SSE) n'existaient pas dans cette release, la trace de connexion
  et l'exception d'origine n'ont pas été conservées : aucun « zéro octet » n'est
  affirmé, et `transport_timeout` regroupe plusieurs exceptions HTTPX.
  Aucun nouvel appel cloud, budget diagnostic 10/10 et budget nominal inchangés.
  Candidate à construire depuis ce commit sans activation de `current` ;
  PR empilée sur #24, aucune fusion automatique.

## Cohérence de santé privée — candidate du 11 septembre 2026

- La CLI et le dashboard installés interrogent le même gateway 0.3.0, identifié
  par son verrou, son PID/démarrage et les sockets qu'il possède. Le défaut
  `instance_session_mismatch` compare la session conversationnelle initiale du
  verrou à une session renouvelée sans redémarrage serveur.
- Correction ciblée : publier et vérifier le `server_epoch` déjà présent dans
  le protocole privé. Les contrôles de propriétaire/PID restent obligatoires ;
  ancienne métadonnée et autre instance restent refusées. Les tests couvrent
  redémarrage, reconnexion Echo et changement de conversation.
- Les vérifications ont aussi reproduit la saturation des 16 sessions HTTP :
  l'ancienne CLI en créait une par appel sans la libérer. La candidate termine
  uniquement sa propre session avec authentification/CSRF ; vingt vérifications
  successives préservent la session du navigateur et la capacité disponible.
- Release active, APK, WSS, appairage, profils audio et budgets inchangés.
  Cette correction est candidate, sans activation de `current` ni fusion
  automatique. Le défaut de la CLI installée demeure jusqu'à activation
  autorisée ; ce n'est pas un PASS hérité des tests de la candidate.
- Les tests PASSIVE utilisent les doubles existants, sans capture ni cloud :
  contexte RAM non adressé, données distinctes du système, suppression du
  self-echo, refus des générations périmées et effacement. Le test humain reste
  soumis à l'action explicite du propriétaire via le smoke déjà disponible.
  Preuves courantes et budgets : bilan privé `private-v1/health-review.json`.

## Mise en service privée — 10 septembre 2026

- `APPLICATION_DEPLOYED`, `APK_INSTALLED`, `DASHBOARD_AVAILABLE`,
  `DATA_BROWSER_READY`, `TRANSPORT_SECURED` : release Mac non editable 0.3.0,
  APK signée 0.3.0-private (code 11), protocole 1 et schéma SQLite 1.
  Dashboard propriétaire : `http://127.0.0.1:8768/`, loopback uniquement.
  Gateway WSS, CA privée limitée à l'application, SAN vérifié et autorisation
  par appareil. Echo connecté OFF avec capture fermée après appairage.
- Sources produit intégrées par PR serveur #22 et APK #5. Le correctif
  d'annulation #11 est porté avec conservation des contrats Echo ; les branches
  de recherche et les deux corrections retirées ECHO-02D ne sont pas activées.
  Les identités exactes des wheels et du couple installé sont dans le manifeste
  de release et le bilan privé `private-v1/deployment.json`.
- SQLite locale indépendante des releases, écritures bornées hors audio,
  sauvegarde cohérente et restauration isolée vérifiées. Conservation des
  conversations explicitement activable (30 jours proposés), initialement
  désactivée ; contexte passif RAM consultable, archive passive désactivée.
- Deux lectures TTS locales sur le vrai Echo, animations normales puis réduites,
  se terminent avec zéro underrun rapporté. Captures des deux pages et vidéo
  privée du visage en lecture disponibles ; aucune capture micro ni requête
  DeepSeek pour ces vérifications. Ce smoke n'est pas une homologation audio.
- `PASSIVE_FUNCTIONAL` reste à confirmer par le smoke humain déclenché dans
  l'interface. Le compteur diagnostic est conservé à 9/10 ; le plafond nominal
  distinct reste à choisir par le propriétaire avant son activation.
- `KNOWN_STT_LIMITATIONS`, `KNOWN_PLAYBACK_LIMITATIONS` restent ouverts.
  Les résultats anciens ci-dessous sont historiques et inchangés.
  Exploitation et rollback : [PRIVATE_OPERATIONS.md](PRIVATE_OPERATIONS.md).

## Echo ECHO-02D — remédiation ciblée bloquée

- Baseline physique reproduite : une starvation sur 40 lectures. Prefill de
  100 ms réellement écrit ; allocation native et réserve disponible distinctes.
- Deux corrections bornées testées puis retirées : placement de réserve et
  verrou Wi-Fi pendant lecture. La fiabilité complète n'est pas démontrée.
- Livrable limité aux diagnostics/tests ; paramètres audio et sender nominal
  conservés. Budget réel 9/10, PASSIVE et STT/TTS non exécutés dans cette phase.
- `ECHO_02D_BLOCKED`, `OWNER_DEVICE_ARCHITECTURE_DECISION`.
  Détails : `src/jarvis_office/echo/ECHO-02D.md`. PR empilées, aucune fusion.

## Echo ECHO-02C — chantier parallèle

- Deux tours réels Echo → VoiceLoop existant → Echo ont réussi sans redémarrage ;
  son et timbre confirmés par le propriétaire. APK native 0.2.0-dev.3 installée.
- Streaming unique par réponse, y compris deux segments TTS ; correction serveur
  du rattrapage en rafale et crédits PCM bornés, profil Android Ethernet inchangé.
- `REMOTE_PIPELINE_FUNCTIONAL=PASS`, `REMOTE_STT_QUALIFIED=NO`.
  Le contexte passif complet sur micro réel et les budgets de latence restent
  non qualifiés. Transport `DEV_INSECURE_LAN`, `NOT_SECURE_RELEASE`.
- Détails, mesures et limites dans `src/jarvis_office/echo/ECHO-02C.md`.
  PR Echo empilées seulement, aucune fusion ni modification du service principal.

## Phase 06 — en cours

- Base `d95e0a8` (merge #6, même arbre que `dfbda8b`), branche `codex/06-release`.
  Main/03 ne contiennent pas la clôture 05C ; PR prévue vers `codex/05-voice-loop`.
- RCA des 5 tours : médiane logicielle 4,361 s, min 4,085, max/p95 exploratoire 4,420.
  Contributions moyennes par tour : STT 51,1 %, VAD 12,8 %, DeepSeek 17,9 %,
  segmentation 4,5 %, TTS 8,9 %, sortie 1,1 % ; reliquat files/dispatch 3,7 %.
  LATENCY_TARGET_NOT_MET. Aucun seuil/actif/paramètre vocal modifié sur intuition.
- Nouveau banc technique : 5 fixtures synthétiques/silence/bruit × 3 × 2 candidats,
  aucun appel réseau. Small plus rapide mais WER 8–9 %, turbo 0–3,74 % ; erreurs
  critiques de holdout présentes. Turbo CPU float32/beam 1 conservé provisoirement.
  Corpus humain : manifeste seul, zéro prise disponible ; benchmark humain 20 phrases
  et qualification formelle restent BLOCKED_USER, NO_ACCEPTABLE_STT conservé.
- Durcissement : verrou noyau unique, état de santé distinct, logs metadata-only bornés,
  trace connexion HTTP sans contenu, budget 06 distinct 20 (historique 05 = 6 conservé).
  Release/service en préparation et non encore validés. Aucun armement automatique.
- Campagne 100 replays/30 minutes en cours : VAD/STT et Qwen réels, LLM/sortie simulés,
  micro fermé. Ce n'est ni une session micro ni 100 appels cloud.

## Preuve matérielle conservée — phase 05C

**PHASE_05_MAC_AUDIO_VALIDATED** — cinq tours réels sur le Mac, écoute confirmée humainement.
**STT_QUALIFICATION_PENDING**. TV : **DEFERRED — future phase**, pas un blocage.

- Phase 05B : micro déjà sélectionné conservé ; haut-parleurs locaux Mac sélectionnés
  explicitement dans le TOML privé, sans modifier les préférences système.
  Listes passives `input-list --json` / `output-list --json`, formats interrogés seulement.
- Sortie isolée réelle : Qwen3/profil Office, français ICL, 24 kHz mono PCM16 vers
  sounddevice/SoXR 48 kHz stéréo float32 ; 1,76 s audio, zéro sous-alimentation,
  stream et worker fermés. Premier PCM 0,376 s, premier callback 0,427 s depuis la
  requête TTS ; **pas une latence depuis la parole**. Mesures historiques de phase 05B.
- Annulation réelle de sortie sur PCM Qwen connu en RAM : abort/close, file purgée,
  aucun échantillon livré tardivement, puis lecture complète suivante réussie.
  Annulation avant TTS et invalidation du tour vérifiées avec doubles, pas avec micro.
- Phase 05C : contrôle CoreAudio répété, autorisation accordée et aucun accès micro
  actif à cet instant. Test Qwen3 → sortie locale rejoué : 1,52 s, zéro sous-alimentation,
  stream et worker fermés. L'utilisateur confirme **AUDIO_MAC=oui, VOIX_JARVIS=oui,
  ARTEFACT=non** pour ce replay ; cela ne résout pas les droits/provenances.
- Micro réel 05C : après deux prises incomplètes (adresse seule puis silence), opérateur
  prêt et troisième prise exploitable : adresse et contenu principal reconnus. Aucun LLM
  ni WAV ; mono 48 kHz effectifs, normalisation 16 kHz, Silero puis STT CPU float32.
  Zéro perte, RMS 0,0167 (maximum RMS observé 0,0554), pré-roll 0,320 s,
  silence terminal 0,512 s sur trames ; fin parole estimée → VAD 0,566 s,
  attente STT 0,133 s, inférence 2,158 s. Validation technique ponctuelle, pas homologation.
  Une première fenêtre de `run` était restée sans tour reconnu malgré une parole signalée ;
  cause non établie, aucun correctif ni changement de seuil/modèle pour forcer le résultat.
- Clôture 05C : armement local par l'utilisateur, **5 tours PASS dans une même session**,
  dont deux consécutifs sans redémarrage. Micro réel → Silero/STT → Flash streaming →
  Qwen3/profil Office → haut-parleurs locaux ; réponses entendues et fonctionnement
  confirmé par l'utilisateur. Aucun texte des cinq échanges naturels conservé/reconstitué.
  Zéro perte capture, sous-alimentation sortie, erreur, reprise TTS ou retry signalé.
  Toutes les réponses sont confirmées intégralement selon l'échéance DAC estimée ;
  l'écoute humaine est une preuve distincte. Aucun auto-écho signalé ; les débuts de parole
  des tours acceptés suivent la fin de lecture précédente. Réarmement éligible après
  0,351 s environ ; réouvertures automatiques observées après 0,783–0,812 s.
  Les intervalles avec reprise manuelle ne sont pas une latence de réarmement automatique.
  Deux réponses commencent le TTS avant la fin DeepSeek (avance 0,066/0,103 s).
  Fin parole estimée → première écriture pilote, tours 1–5 : **4,361 / 4,085 / 4,420 /
  4,283 / 4,405 s**. Objectifs non atteints sur ce petit N, aucun p50/p95 solide revendiqué.
  STT 2,165–2,223 s ; PCM livré → pilote 0,047–0,048 s. PCM produit MLX relatif à sa
  synthèse, pas un timestamp acoustique ; tableau complet dans le rapport JSON privé.
  Budget : **5 nouvelles requêtes, 6/20 cumulées**. Aucun essai réseau supplémentaire.
  Capture, contrôleur, workers et port local fermés après la confirmation humaine.
- Corrections ciblées : métriques premier PCM converti, format réel du stream, RMS/pertes,
  pré-roll et réarmement observé conservé dans le rapport. UI en pause n'annonce plus
  une écoute armée ; sélection configurée distincte de la vérification matérielle.
  HTTP local réellement exercé en pause : Pause/Annuler/Effacer/Arrêter, session invalidée,
  arrêt code 0, micro fermé, aucune requête API. Aucun nouveau moteur ou dépendance.

- Code : `run` en pause, contrôleur HTTPX/UI, worker STT/Silero/capture/sortie et worker
  Qwen3 séparé. Versions/locks audio conservés ; aucun poids ou paquet ajouté.
  Signature du code vérifiée dans les workers ; aucun runtime V1.
- Un tour actif, adresse Jarvis uniquement au début, capture fermée avant STT/réponse.
  SSE, synthèse et lecture découplés, texte 2048 caractères, PCM deux secondes au format
  réel, crédits de capacité et délais bornés. Pause/Annuler restent en pause ; Effacer
  invalide session/historique. Arrêt des seuls enfants Office avant fermeture du contrôle.
- UI HTML originale sur 127.0.0.1:8768 : Host/Origin/Fetch Metadata, cookie HttpOnly,
  aucun effet sur GET, aucun texte LLM injecté en HTML. Aucun frontend lourd/service installé.
- Confirmation vocale : préfixe de segments intégralement terminé selon échéance DAC
  estimée, jamais d'inférence de mots depuis un pourcentage de PCM. Horloges rapprochées
  explicitement ou métrique indisponible ; PCM produit, livré et remis au pilote distincts.
- Tests simulés du checkpoint 05B : **90 tests réussis**, dont dix tours successifs, erreurs/reprise,
  annulation/file pleine, enfant bloqué tué/récolté, trames, reset et protections UI.
  Rejoués en 05C : **90/90** sous interdiction réseau macOS ; Ruff/format et mypy passent.
  Clôture 05C : build wheel/sdist et installation propre à nouveau réussis hors réseau,
  dépendances installées avec hashes, 90 tests depuis le wheel et import sans moteur.
  Suite exécutée sous interdiction réseau macOS dans le checkout et depuis un wheel
  neuf installé avec hashes, entièrement hors ligne cette fois, chemin avec espaces.
  Imports inertes sans extras ; 28 entrées wheel dont control.html, aucun actif privé.
  Ruff/format, mypy 21 fichiers, trois locks et build wheel/sdist réussis.
- Moteurs réels : STT CPU float32, warmup puis quatre fixtures Office (silence/bruit
  synthétique/deux phrases). Silence/bruit non transcrits ; 0 erreur sur 37 mots
  synthétiques, N insuffisant pour homologation. Inférences parole 2,328 s et 5,173 s.
  Qwen3/profil Office : deux segments, 8,96 s PCM, RTF 0,573/0,565, aucune lecture.
- API réelle : **6 tentatives sur 20**, dont cinq tours matériels 05C ; HTTP 200,
  deepseek-v4-flash sans réflexion, streaming, 256 tokens maximum, aucune cascade.
  Test supplémentaire Qwen réel/LLM simulé lent : PCM à 0,524 s, texte terminé à 3,106 s ;
  progression avant fin prouvée sans requête API supplémentaire.
- Serveur local réellement exercé en pause : moteurs prêts, micro fermé ; accès tiers,
  absence de cookie et GET de contrôle refusés ; reconnexion sans appel/capture.
  Arrêt code 0. Connexion réseau explicitement refusée par macOS dans les deux runtimes.
- Conversation micro → sortie locale Mac : **PASS sur les cinq essais 05C**, pas une
  qualification générale. Commande disponible : `run --arm --seconds 30 --turns 2`.
  Démarrage normal `run` en pause ; arrêt par Arrêter/Ctrl+C. Aucun enregistrement conservé.
- Qualifications conservées : turbo provisoire, **NO_ACCEPTABLE_STT** au banc strict
  antérieur ; 50 prises/transcriptions humaines manquantes. Timbre/écoute du replay
  confirmés humainement, jalon E2E Mac validé mais droits vocaux toujours ouverts ; inconnues
  binaires/provenance conservées dans les notices.
  Lectures V1 historiquement refusées non contournées ; segmenteur autonome réutilisé.
- V1 : Git et quatre lanceurs ciblés sans différence avant/après (2 dirty/20 non suivis
  préexistants, lanceurs non chargés). Contrôle limité, pas d'audit global ou affirmation
  d'immuabilité exhaustive ; aucun service/modèle/réglage V1 modifié par Office.
- Git : `codex/05-voice-loop` depuis `3c2e333`, qui contient `7481ee4`.
  Phase 05B sur `codex/05b-mac-audio-validation` depuis `7117d39` ; exécution 05C `68371a8`
  (sources inchangées depuis `3f660bd`),
  [PR #6](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/6) vers `codex/05-voice-loop`.
  Clôture 05C : documentation/preuves seulement, aucune correction source ni fusion.
  PR #2/#3/#4 déjà fusionnées dans leurs bases respectives ; main ne contient pas la
  phase 04. PR de cette phase vers `codex/03-stt`, sans réécrire/fusionner les précédentes.
  Checkpoint code `f4afda1` poussé ; [PR #5](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/5)
  ouverte vers la base contenant la phase 04, deux contrôles CI réussis. Aucune fusion.
  Suppression utilisateur de
  `.env.example` préservée hors commit ; `.env` et clé privée non suivis.
- Rapport structuré privé et inventaire unique actualisés, mémoires Serena durables
  mises à jour. Budget partagé sous config privée, sans reset/retry automatique.
  Aucun réglage Codex global, LaunchAgent, release, phase 06 ou déploiement Echo Show.
