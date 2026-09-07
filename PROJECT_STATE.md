# État du projet

**PHASE_04_TEXT_WORKING — AUDIT_V1_PARTIEL**. DeepSeek textuel et segmentation testés ;
aucun microphone, lecture sonore ou pipeline de phase 05 démarré.

- Code terminé : un client HTTPX asynchrone réutilisable, SSE réellement réassemblé,
  événements delta/segment identifiés, annulation et fermeture explicites. File bornée,
  saturation en erreur, délais connexion/premier contenu/inactivité/total, aucun retry.
  Raisonnement/outils inattendus refusés, jamais transmis comme texte prononçable.
- Contrat officiel revérifié le 7 septembre 2026 : POST api.deepseek.com/chat/completions,
  deepseek-v4-flash, stream=true, thinking.type=disabled, max_tokens=256. Aucun SDK,
  reasoning_effort, clé OpenAI ou fournisseur de secours. Alias distant non figé en poids ;
  modèle retourné, date et contrat consignés sans contenu de conversation.
- Installation : extra chat optionnel, HTTPX 0.28.1, httpcore 1.0.9, anyio 4.15.1,
  h11 0.16.0, certifi 2026.7.22, idna 3.19, uv.lock. Contrôleur toujours importable sans
  extra ni moteur. Locks TTS/STT actualisés uniquement pour métadonnées du paquet Office,
  sans changement de leurs versions installées ni installation dans V1.
- Secret : seule DEEPSEEK_API_KEY du .env explicitement fourni pour Office a été copiée
  vers config/deepseek.env privé (0600, dossier 0700). Source non écrasée, aucun secret V1
  recherché, aucune valeur affichée. Aucun secret, audio ou rapport privé dans Git.
- Conversation : quatre tours confirmés en RAM, contexte 12000 caractères ; entrée/sortie
  4096, file 64 événements. Généré, remis au consommateur et effectivement affiché/prononcé
  restent distincts. Confirmation explicite complète/partielle ; interruption notée dans
  le contexte, aucun échec promu en réponse complète. Reset invalide les confirmations.
- Texte prononçable : première proposition/phrase, décimales, nombre/unité et abréviations,
  Markdown/code/URL bornés, timer 0,8 s sans demi-mot, flush final unique. Un lecteur lent
  fait échouer le tour au plafond de file, sans duplication ni accumulation sans borne.
- Tests simulés : 62 unittest réussis dans le checkout ET depuis le wheel neuf installé
  avec dépendances vérifiées par leurs hashes. Suite propre exécutée sous interdiction
  réseau macOS ; imports sans dépendances inertes, TLS/hostname/redirects testés.
  Ruff/format, mypy 16 fichiers, trois locks et build wheel/sdist réussis. Le wheel contient
  22 entrées code/métadonnées seulement. L'installation initiale entièrement hors ligne
  manquait d'un artefact de cache ; installation des dépendances autorisée via registre,
  puis tests sans réseau réussis. Aucun appel API ajouté par ces tests.
- API réelle : **3 requêtes synthétiques sur 5 autorisées**, toutes HTTP 200/PASS,
  modèle retourné deepseek-v4-flash, fin explicite stop, 193 tokens de sortie au total.
  Requête → premier contenu : 0,564–0,895 s ; premier segment disponible : 0,693–1,076 s ;
  fin du texte : 1,100–1,421 s. Ce ne sont pas des latences vocales. La troisième requête
  a vérifié le transport final sans compression. Métadonnées privées, aucun historique
  personnel ou audio envoyé. Commande chat réellement exécutée ; pas de poll payant.
- V1 : Git inchangé (HEAD 67968ed5…, 2 dirty/20 non suivis préexistants), 40 fichiers et
  74 actifs comparables sans différence. Quatre lanceurs inchangés/non chargés au contrôle
  final. Contrôle partiel contre inventaire antérieur ; logs/DB et fichiers refusés exclus.
  Pas de nouveau relevé de services avant phase 04, donc aucune affirmation avant/après
  exhaustive. Aucun service, modèle, environnement ou réglage V1 modifié par Office.
- Audit de réutilisation **BLOCKED_USER** : lecture de jarvis/audio/tts/segmenter.py et
  tests/test_tts_segmenter.py refusée par l'outil, comme quatre adaptateurs en phase 02.
  Aucun contournement/copie ; segmentation autonome, équivalence V1 non affirmée.
  Action minimale : autoriser ces lectures ciblées dans le périmètre de l'outil.
- Blocages précédents conservés : STT turbo seulement provisoire, NO_ACCEPTABLE_STT aux
  critères stricts ; 50 prises/transcriptions humaines manquantes. Écoute/TV/timbre non
  validés, droits vocaux et certaines notices/provenances binaires inconnus. Dépôt privé
  sans licence publique automatique ; THIRD_PARTY_NOTICES.md actualisé en place.
- Git : codex/04-deepseek basée sur codex/03-stt (PR #3 ouverte, dépendante de PR #2).
  Checkpoint code 7fb8655 poussé ; [PR #4](https://github.com/AVTAVANTTOUT2/jarvis-office/pull/4)
  ouverte. Premier contrôle CI arrêté sur le format du bloc Python README, corrigé ;
  contrôles relancés. Aucune fusion, protection ou visibilité modifiée.
  Suppression locale utilisateur de .env.example préservée hors commit ; .env reste ignoré.
- Inventaire privé unique mis à jour ; mémoires Serena actualisées pour les contrats
  durables. Navigation symbolique indisponible, aucun langage actif. Aucun réglage global
  Codex modifié. Prochaine étape : revue du checkpoint et clôture des validations humaines/
  audits restants ; phase 05 uniquement sur nouvelle demande.
