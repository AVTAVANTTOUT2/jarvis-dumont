# État du projet

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
