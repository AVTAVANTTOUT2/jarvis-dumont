# État du projet

**PHASE_03_CODE_TESTED — QUALIFICATION_BLOCKED_USER**. Capture/VAD/STT autonomes
fonctionnels sur M4 ; aucun STT homologué, phase 04 non commencée.

- Code terminé : import STT explicite/idempotent/atomique avec SHA-256 ; un adaptateur
  faster-whisper CPU, modèle chargé/préchauffé une fois, Silero ONNX seul segmente.
  Capture sounddevice bornée, SoXR continu, pré-roll 320 ms et silence terminal 512 ms,
  files/pertes/reconnexion contrôlées. Aucun réseau, fallback, serveur V1 ou LLM distant.
- Environnement STT neuf : Python 3.12.13, faster-whisper 1.2.1, CTranslate2 4.8.1,
  ONNX Runtime 1.27.0, sounddevice 0.5.5, SoXR 1.1.0, NumPy 2.5.1 ; lock séparé.
  Backend effectif CPU float32, 4 threads, beam 1 ; ni CUDA ni Metal. Paramètres privés
  d'énoncé/entrée limités à 60 s pour le corpus ; capture explicite limitée à 30 s.
- Actifs : small (révision 536b066…) et turbo (0a363e9…) CTranslate2/tokenizers complets,
  Silero 6.2.1 ONNX et LICENSE copiés indépendamment sous les benchmarks privés.
  Seul large-v3-turbo est aussi copié sous assets/stt et configuré, **PROVISOIRE pour
  développement**, jamais homologué. Aucun nouveau poids téléchargé ou modèle V1 effacé.
- Mesures réelles finales : 5 fichiers distincts, dont seulement 3 phrases synthétiques
  Qwen et 2 silence/bruit artificiel, 3 répétitions par candidat (15 essais chacun).
  WER synthétique small 9,03 %, turbo 2,78 % ; dev/holdout séparés dans le rapport privé.
  P95 inférence dev/holdout : small 1,726/3,460 s ; turbo 5,476/6,947 s.
  Zéro erreur d'inférence, phrase perdue ou sortie sur silence/bruit de ce petit jeu.
  Aucun bruit de bureau réel ni parole humaine qualifiés par ces résultats.
- Critères stricts : **NO_ACCEPTABLE_STT** (sortie benchmark 1 attendue) : small dépasse
  5 % ; les deux candidats signalent trois alertes critiques de représentation des nombres
  sur les répétitions de la phrase longue (« 1, 2, 3, 4 » versus mots). Aucune négation
  perdue observée ; interprétation sémantique à revoir humainement. Les seuils ne sont pas
  relâchés. Turbo reste seulement le meilleur candidat technique, plus lent ; latence
  conversationnelle globale non validée. Pic RSS processus ~1,64/4,46 Go, maximum cumulé.
- Micro M4 réel : Blue Snowball, permission déjà accordée, aucune autre entrée active au
  contrôle. Trois prises explicites de 2 s à 48/44,1/16 kHz réussies, chacune 32 000
  échantillons mono 16 kHz après conversion ; signal non nul, aucun débordement/reconnexion,
  aucune parole détectée. Postflight sans entrée active. Pas de modification de fréquence
  globale, permission, sortie ou service. Contrôles instantanés, pas surveillance permanente.
- Tests : 40 unittest réussis dans le checkout ET dans le wheel installé hors ligne dans
  un venv neuf (NumPy/SoXR seulement pour tests). Ruff/format, mypy 13 fichiers et build
  wheel/sdist hors ligne réussis. Imports CLI sans moteur/NumPy vérifiés. Socket native
  réellement refusée sous sandbox et garde Python testée. doctor/assets inspect et
  stt-test du seul modèle sélectionné réussis. Commandes reproductibles dans README.md.
- Corpus humain **BLOCKED_USER** : manifeste privé de 50 prises planifiées (25 phrases
  calme/bruit, 10 holdout), aucune fausse référence et aucun enregistrement automatique.
  Action minimale : prises déclenchées individuellement, transcription/validation humaine,
  annotation des noms/nombres/négations et vocabulaire métier, puis benchmark inchangé.
- V1 : HEAD 67968ed5bd4dee2cf402efe5088f6ac48bc94d19, état Git inchangé (2 dirty et
  20 non suivis préexistants) ; 41 fichiers et 74 actifs comparés sans différence.
  Quatre lanceurs inchangés/non chargés. Contrôle partiel : logs/DB exclus, quatre
  adaptateurs refusés par l'outil et une empreinte dirty initiale indisponible.
- Phase 02 conservée : Qwen3 local isolé fonctionne (3 longueurs × 3 répétitions,
  annulation/drainage réels testés). Écoute/TV/timbre **NOT_RUN**, droits vocaux inconnus.
  Audit détaillé de quatre adaptateurs V1 toujours **BLOCKED_USER**, aucun contournement ;
  autoriser leur lecture ciblée pour clôture. Notices de conversion/binaires incomplètes,
  voir THIRD_PARTY_NOTICES.md. Aucun octroi de licence publique, aucun WAV dans Git.
- Git : phase 01 fusionnée par PR #1 ; phase 02 PR #2 ouverte, CI réussie.
  Phase 03 sur codex/03-stt, checkpoint code bbf5926 poussé ;
  [PR #3](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/3) ouverte, empilée sur
  codex/02-tts. Les deux exécutions CI Linux du code ont réussi ; aucune fusion effectuée.
  Aucune protection/visibilité/identité globale modifiée.
- Inventaire privé unique mis à jour en place ; mesures et corpus hors Git. Mémoires
  Serena actualisées, navigation symbolique toujours indisponible (aucun langage actif).
  Prochain jalon : qualification humaine STT et revues restantes ; pas de client DeepSeek.
