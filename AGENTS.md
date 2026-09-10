# Jarvis Office — règles durables

- Livraison privée explicitement autorisée : pipeline Echo live unique, APK Kotlin,
  dashboard loopback et SQLite locale consultable hors release. Les restrictions
  historiques Phase 06 ci-dessous ne bloquent plus ces interfaces, stockage et
  déploiement. Profil STT/TTS/audio nominal inchangé ; aucun banc/training dans
  le runtime. Statut visé DEPLOYED_PRIVATE_WITH_KNOWN_LIMITATIONS ; anciens
  échecs STT/lecture conservés. TLS et authentification appareil obligatoires.
- Contrat commun : PRIVATE_CONTRACT.md. Historique adressé activable/rétention
  configurable ; contexte passif RAM visible, archivage distinct désactivé.
  Aucun PCM conservé par défaut. Consultation sans réinjection automatique.
  Budget nominal choisi dans l'UI, compteur diagnostic 9/10 préservé séparément.

- Périmètre minimal : une conversation, un tour actif, semi-duplex. Un STT, un TTS local, un LLM distant ; Silero détecte la parole. Aucun orchestrateur au produit.
- Phase 06 : profiling avant optimisation, stabilité et release locale Mac. Budget distinct de 20 nouvelles tentatives DeepSeek maximum, compteur phase 05 conservé. TV/Echo Show : DEFERRED. Essais micro explicitement armés et bornés, aucun nouveau poids ni changement de sortie globale. STT_QUALIFICATION_PENDING et les inconnues de provenance restent ouverts sans preuve.
- Release : wheel non editable, environnements créés à leur emplacement final, actifs Office partagés et vérifiés. Activation atomique après vérification, aucune suppression automatique. LaunchAgent utilisateur distinct démarrant en pause ; verrou d'instance avant moteurs/capture et arrêt limité aux enfants Office. Aucun runtime depuis le checkout.
- V1 strictement en lecture seule : aucun import de ses modules, script, modification d'environnement, Git, service, permissions ou périphérique. Ne jamais sourcer ses fichiers .env.
- Secrets, voix, transcripts, poids, inventaires et chemins personnels hors Git. Ne jamais afficher les valeurs de configuration ou les exceptions brutes dans les diagnostics.
- Aucun téléchargement au runtime, routeur multimodèle ou repli silencieux. Pas de RAG, MCP produit ou infrastructure supplémentaire au périmètre privé ci-dessus.
- Développement sous ~/Developer/jarvis-office. Données privées sous ~/Library/Application Support/JarvisOffice ; logs sous ~/Library/Logs/JarvisOffice. Production future sous releases/<version>-<sha> avec pointeur current, jamais depuis le checkout.
- Bibliothèque standard et dépendances existantes d'abord ; aucun module audio vide ou abstraction anticipée.
- Import atomique vérifié, sans lien mutable vers V1. Le worker TTS utilise son lock séparé ; réseau bloqué et caches Office. Un warmup PCM non vide ne valide jamais le timbre humain.
- Annuler coupe la livraison puis draine la requête identifiée sous verrou ; terminer seulement l'enfant possédé si sa frontière est incertaine. Fermer explicitement générateurs et worker.
- STT : un seul modèle sélectionné en mémoire, CPU CTranslate2 vérifié ; comparaison hors trajet nominal. Silero seul segmente, trames 512/16 kHz ; SoXR continu, pré-roll et files bornées. Toute perte invalide la prise, jamais de collage entre sessions.
- Capture déclenchée et limitée seulement après contrôle permission/conflits ; aucun changement TCC ou sortie globale. Référence humaine indépendante obligatoire pour homologation ; synthèse, WER ou avg_logprob ne la remplacent pas. Aucun journal des paroles rejetées en usage normal.
- Un seul rédacteur par fichier. Préserver les changements utilisateur.
- Boucle vocale : départ en pause, capture fermée avant STT/réponse, réarmement borné après lecture et délai acoustique. Annuler/Pause ne réarment pas ; Effacer invalide session et événements. Jamais de transcription sans adresse affichée, journalisée ou envoyée.
- Sortie explicite et unique : aucun repli ou changement global. Confirmer seulement un préfixe de segments terminés selon une échéance DAC explicitement estimée ; PCM/affichage ne prouvent pas une écoute. Files texte/PCM bornées en caractères/octet au format réel.
- UI loopback seulement : Host/Origin/Fetch Metadata, cookie de session, aucun effet sur GET ni contenu LLM rendu comme HTML. Second onglet/reconnexion n'initialise pas de moteur ou de capture. Finir l'arrêt des enfants avant de fermer la tâche de contrôle.
- DeepSeek : un HTTPX asynchrone réutilisé, endpoint TLS fixe, redirections/proxies d'environnement refusés, Flash non réfléchissant et aucun retry automatique. SSE réellement réassemblé, files bornées ; saturation/annulation ferment le flux sans rejouer de texte.
- Historique du modèle en RAM ; archive locale explicitement activée indépendante. Confirmation explicite du préfixe réellement livré, un échec n'est jamais une réponse complète. Les secrets sont importés explicitement, une seule clé vers config/deepseek.env (0600), jamais depuis V1 au runtime. Rapports techniques sans prompts, réponses ni Authorization.
- Tests et revue des fichiers suivis/exclusions avant chaque checkpoint et push. Ne jamais présenter un test simulé comme une preuve matérielle.
- Ne pas changer les réglages globaux Codex, l'identité Git globale ou les protections distantes. Publication privée uniquement avec autorisation.
- PROJECT_STATE.md est mis à jour en place. Les connaissances stables vont dans .serena/memories ; aucun secret ou journal transitoire.
