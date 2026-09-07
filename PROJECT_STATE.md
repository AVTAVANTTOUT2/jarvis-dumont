# État du projet

**PHASE_01_READY** — fondations implémentées et vérifiées localement et en CI Linux.
Ce verdict ne qualifie pas le fonctionnement vocal.

- Décisions : paquet `jarvis_office`, CLI `jarvis-office`, Python 3.12 dédié, uv.lock,
  aucune dépendance d'exécution. Un choix par fonction ; aucun STT Office sélectionné.
  Production future par releases privées/versionnées, pas depuis le checkout.
- Code terminé : `doctor --json`, `assets inspect --json`, configuration TOML typée,
  sortie expurgée et codes 0/1/2/3 documentés. Aucun import V1 ou moteur, aucune clé lue.
- Tests simulés : 9 tests unittest réussis, puis les mêmes 9 depuis un wheel installé
  hors ligne dans un venv propre sans dépendances. Ruff réussi, mypy réussi (5 fichiers),
  build sdist/wheel réussi. Commandes reproductibles dans README.md.
- Fichiers réels : diagnostics des actifs explicites réussis (code 0) ; configuration
  absente correctement signalée (code 3). M4 arm64, 32 Gio ; Python MLX 3.14 séparé,
  MLX 0.31.2 / mlx-audio 0.4.5 / mlx-lm 0.31.3. Qwen3 et son tokenizer présents ;
  small et turbo présents dans le cache STT CTranslate2 réellement utilisé par V1.
  Trois téléchargements incomplets dans un autre cache HF, conservés sans modification.
- Matériel : interrogation des périphériques réussie, microphone USB et sorties audio
  identifiés. Sortie par défaut = haut-parleurs du Mac ; sortie TV non qualifiée.
  Capture, lecture sonore, inférence et DeepSeek : **NOT_RUN**.
- V1 : HEAD constaté `67968ed5bd4dee2cf402efe5088f6ac48bc94d19`, deux fichiers suivis
  modifiés et vingt fichiers non suivis préexistants. Lanceurs ciblés non chargés dans
  le domaine utilisateur ; supervisor/ingestion désactivés. HEAD, branche, état Git,
  45 empreintes de fichiers, 74 empreintes d'actifs et quatre lanceurs identiques à leurs
  relevés initiaux. Comparaison partielle : logs/DB exclus ; empreinte initiale d'un
  fichier dirty indisponible après une erreur de parsing du chemin, explicitée dans le JSON.
- Inventaire détaillé : JSON privé unique hors Git dans Application Support/JarvisOffice.
  Aucun actif copié. Droits vocaux, révision amont exacte de conversion et certaines
  notices binaires inconnus : voir THIRD_PARTY_NOTICES.md, à résoudre avant leur réutilisation.
- Git : dépôt privé `AVTAVANTTOUT2/jarvis-office`, amorçage `0dd7f7c` sur main,
  travail sur `codex/01-bootstrap`, code livré au checkpoint `d7e9336`.
  Push effectué ; [PR #1](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/1) créée.
  CI push et PR réussies pour le code ; la CI de chaque mise à jour doit passer avant fusion.
  Main constatée sans protection ; aucune protection ou visibilité modifiée. L'API de
  rulesets répond 403 (offre GitHub), la protection de branche a été vérifiée séparément.
- Validation humaine : écoute et choix matériel non exécutés ; aucune qualification vocale.
  Prochain jalon, sur nouvelle demande : phase 02, import privé des seuls actifs utiles
  et Qwen3 isolé. Cette phase n'est pas commencée.
- Mémoires Serena : conventions de diagnostic, séparation des environnements et commandes
  fiables enregistrées. Navigation symbolique indisponible dans cette session (serveur
  sans langage actif) ; inspection locale ciblée utilisée. Aucun réglage global Codex modifié.
