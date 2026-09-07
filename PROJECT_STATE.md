# État du projet

**PHASE_02_TTS_WORKING** — synthèse Qwen3 autonome mesurée sur M4 ; pas de qualification
du timbre, de la TV ou de production. Phase 03 non commencée.

- Code terminé : import explicite `assets import [--dry-run]`, copie indépendante
  idempotente vérifiée/atomique ; worker Qwen3 persistant, protocole borné, annulation
  avec drainage, `tts-test --text … --output … [--repeat 3] [--report …]`.
  Diagnostics toujours passifs ; aucun micro, STT, LLM distant, téléchargement de poids
  ou repli vocal. Une conversation, un tour actif, semi-duplex ; pas d'orchestrateur.
- Environnements : contrôleur Python 3.12 sans dépendance runtime ; worker Python 3.14.6,
  mlx-audio 0.4.5, MLX/Metal 0.31.2, mlx-lm 0.31.3, lock séparé. 3 296 fichiers Python
  comparés à l'environnement de référence : aucun écart ; RECORD installés vérifiés.
  Cela ne prouve pas l'absence de tout patch natif V1.
- Actifs réels : 17 fichiers, 1 852 083 849 octets importés et revérifiés sans lien partagé.
  Qwen3 révision MLX `4e44ed4bcee28a0f89a493e07bde16e6dccd43eb`, tokenizer complet,
  profil sélectionné par configuration : WAV mono 24 kHz, 16,02 s et transcript non vide.
  Inventaire unique enrichi en place, trois WAV et mesures dans les rapports privés.
- Tests simulés : 22 unittest réussis, également depuis le wheel installé hors ligne
  dans un venv propre sans dépendances ; import du CLI/worker sans moteur vérifié.
  Ruff, format, mypy (8 fichiers), build wheel/sdist réussis. Commandes dans README.md.
- Tests M4 réels : trois longueurs × trois répétitions, zéro erreur, 24 kHz mono.
  Premier PCM livré après warmup : court 348–351 ms, moyen 351–355 ms, long 402–413 ms.
  Calcul : 0,98–1,11 / 5,74–6,35 / 17,00–17,72 s ; audio : 1,52–1,76 /
  11,12–12,32 / 33,04–34,56 s. RTF 0,51–0,65 ; pic RSS enfant ~2,07 Go,
  pic MLX 3,45 Go (mesures distinctes, pas à additionner).
  Démarrages de processus + warmup : 2,71–5,84 s, sans purge du cache disque.
- Cycle de vie réel : annulation après un fragment, drainage 3,54 s, seconde requête
  réussie dans le même enfant, arrêt complet constaté. Calcul non interrompu pendant
  drainage. Tentative socket réellement refusée par sandbox macOS et test audit Python.
  Mesures de contention échantillonnées : aucun processus candidat V1 détecté par chemins ;
  autres charges système non entièrement attribuées, aucun service arrêté.
- V1 : HEAD `67968ed5bd4dee2cf402efe5088f6ac48bc94d19`, état Git inchangé (deux dirty,
  vingt non suivis préexistants). 41 fichiers et 74 actifs comparés au relevé phase 01 :
  aucune différence ; quatre lanceurs inchangés/non chargés. Contrôle partiel : logs/DB
  exclus, quatre adaptateurs non relus et une empreinte dirty initiale indisponible.
- Blocage d'audit **BLOCKED_USER** : l'outil refuse la lecture détaillée de
  `native_audio/qwen3_local.py`, `native_audio/sidecar_protocol.py`,
  `jarvis/audio/tts/backends/qwen3_local.py` et `jarvis/audio/tts/backends/sidecar.py`.
  Aucun contournement. Paramètres constatés + API MLX officielle utilisés ; aucun code V1
  copié, équivalence exhaustive de sa recette non affirmée. Action minimale : autoriser
  leur lecture ciblée dans le périmètre de l'outil pour clôturer cet audit.
- Licences : cartes Qwen Apache-2.0 préservées ; amont exact de conversion, certains
  composants binaires et droits vocaux restent non vérifiés (THIRD_PARTY_NOTICES.md).
  Dépôt privé sans octroi de licence publique ; poids/voix jamais dans le push.
- Matériel/validation humaine : lecture sonore **NOT_RUN**, TV non qualifiée, timbre
  **NOT_RUN**. Trois démos disponibles pour une écoute explicite ultérieure.
- Git : phase 01 fusionnée par PR #1 (`268271e`). Phase 02 sur `codex/02-tts`,
  checkpoint code `de4e00c` poussé ; [PR #2](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/2).
  CI Linux du code réussie ; chaque mise à jour doit passer avant fusion.
  Aucune protection ou visibilité modifiée.
- Mémoires Serena : séparation runtime, API Qwen, import et cycle de vie actualisés.
  Navigation symbolique indisponible (serveur sans langage actif), inspection ciblée.
  Aucun réglage global Codex modifié. Prochaine étape indépendante : revue/écoute des
  démos et clôture de l'audit bloqué ; phase 03 uniquement sur nouvelle demande.
