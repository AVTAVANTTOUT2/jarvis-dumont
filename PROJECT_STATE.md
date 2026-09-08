# État du projet

**PHASE_05_MAC_AUDIO_BLOCKED** — phrase micro/STT complète non validée ; zéro tour E2E.
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
- Micro réel 05C : deux prises isolées, sans LLM ni WAV, mono 48 kHz effectifs,
  normalisation 16 kHz, Silero puis STT CPU float32. Première prise : adresse seule,
  contenu principal absent ; 0 perte, RMS 0,0129 (pic observé 0,0540), pré-roll 0,320 s,
  silence terminal 0,512 s sur trames ; fin parole estimée → VAD 0,553 s,
  attente STT 0,128 s, inférence 2,148 s. Deuxième prise : expiration sans parole détectée.
  Pas de troisième capture sans disponibilité explicite de l'opérateur. Workers fermés.
  Aucun nouveau réglage, modèle, correctif source ou appel DeepSeek ; compteur 1/20.
  Prochaine action : opérateur prêt devant le micro pour une phrase complète isolée,
  puis seulement deux tours E2E dans la même session. Ni streaming ni semi-duplex
  matériels complets homologués par ces deux prises.
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
  Aucun correctif source en 05C ; build/installation propre restent les preuves de 05B.
  Suite exécutée sous interdiction réseau macOS dans le checkout et depuis un wheel
  neuf installé avec hashes, entièrement hors ligne cette fois, chemin avec espaces.
  Imports inertes sans extras ; 28 entrées wheel dont control.html, aucun actif privé.
  Ruff/format, mypy 21 fichiers, trois locks et build wheel/sdist réussis.
- Moteurs réels : STT CPU float32, warmup puis quatre fixtures Office (silence/bruit
  synthétique/deux phrases). Silence/bruit non transcrits ; 0 erreur sur 37 mots
  synthétiques, N insuffisant pour homologation. Inférences parole 2,328 s et 5,173 s.
  Qwen3/profil Office : deux segments, 8,96 s PCM, RTF 0,573/0,565, aucune lecture.
- API réelle : **1 tentative sur 20**, HTTP 200, Flash sans réflexion, 256 tokens maximum,
  44 tokens produits. Premier contenu 0,915 s ; segment 1,350 s ; PCM livré depuis la
  requête 1,915 s. Aucune latence micro → sortie locale ni percentile matériel revendiqué.
  Test supplémentaire Qwen réel/LLM simulé lent : PCM à 0,524 s, texte terminé à 3,106 s ;
  progression avant fin prouvée sans requête API supplémentaire.
- Serveur local réellement exercé en pause : moteurs prêts, micro fermé ; accès tiers,
  absence de cookie et GET de contrôle refusés ; reconnexion sans appel/capture.
  Arrêt code 0. Connexion réseau explicitement refusée par macOS dans les deux runtimes.
- Conversation micro → sortie locale Mac : **NOT_RUN**, zéro tour réel dans ce jalon.
  Après validation de la phrase isolée : `run --arm --seconds 30 --turns 2`.
  Démarrage normal `run` en pause ; arrêt par Arrêter/Ctrl+C. Aucun enregistrement conservé.
- Qualifications conservées : turbo provisoire, **NO_ACCEPTABLE_STT** au banc strict
  antérieur ; 50 prises/transcriptions humaines manquantes. Timbre/écoute du replay
  confirmés humainement, qualification E2E et droits vocaux toujours ouverts ; inconnues
  binaires/provenance conservées dans les notices.
  Lectures V1 historiquement refusées non contournées ; segmenteur autonome réutilisé.
- V1 : Git et quatre lanceurs ciblés sans différence avant/après (2 dirty/20 non suivis
  préexistants, lanceurs non chargés). Contrôle limité, pas d'audit global ou affirmation
  d'immuabilité exhaustive ; aucun service/modèle/réglage V1 modifié par Office.
- Git : `codex/05-voice-loop` depuis `3c2e333`, qui contient `7481ee4`.
  Phase 05B sur `codex/05b-mac-audio-validation` depuis `7117d39` ; code exécuté `3f660bd`,
  [PR #6](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/6) vers `codex/05-voice-loop`.
  Clôture 05C partielle : documentation/preuves seulement, aucune fusion.
  PR #2/#3/#4 déjà fusionnées dans leurs bases respectives ; main ne contient pas la
  phase 04. PR de cette phase vers `codex/03-stt`, sans réécrire/fusionner les précédentes.
  Checkpoint code `f4afda1` poussé ; [PR #5](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/5)
  ouverte vers la base contenant la phase 04, deux contrôles CI réussis. Aucune fusion.
  Suppression utilisateur de
  `.env.example` préservée hors commit ; `.env` et clé privée non suivis.
- Rapport structuré privé et inventaire unique actualisés, mémoires Serena durables
  mises à jour. Budget partagé sous config privée, sans reset/retry automatique.
  Aucun réglage Codex global, LaunchAgent, release, phase 06 ou déploiement Echo Show.
