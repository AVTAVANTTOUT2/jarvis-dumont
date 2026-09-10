# État du projet

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
