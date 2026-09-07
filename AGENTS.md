# Jarvis Office — règles durables

- Périmètre minimal : une conversation, un tour actif, semi-duplex. Un STT, un TTS local, un LLM distant ; Silero détecte la parole. Aucun orchestrateur au produit.
- Phase 04 : DeepSeek textuel direct uniquement, Flash sans réflexion ; au plus cinq requêtes synthétiques de validation, aucun micro, moteur audio ou phase 05. Diagnostics toujours passifs. Aucun nouveau poids.
- V1 strictement en lecture seule : aucun import de ses modules, script, modification d'environnement, Git, service, permissions ou périphérique. Ne jamais sourcer ses fichiers .env.
- Secrets, voix, transcripts, poids, inventaires et chemins personnels hors Git. Ne jamais afficher les valeurs de configuration ou les exceptions brutes dans les diagnostics.
- Aucun téléchargement au runtime, routeur multimodèle ou repli silencieux. Pas de RAG, MCP produit, mémoire persistante, base de données, Android ou infrastructure supplémentaire.
- Développement sous ~/Developer/jarvis-office. Données privées sous ~/Library/Application Support/JarvisOffice ; logs sous ~/Library/Logs/JarvisOffice. Production future sous releases/<version>-<sha> avec pointeur current, jamais depuis le checkout.
- Bibliothèque standard et dépendances existantes d'abord ; aucun module audio vide ou abstraction anticipée.
- Import atomique vérifié, sans lien mutable vers V1. Le worker TTS utilise son lock séparé ; réseau bloqué et caches Office. Un warmup PCM non vide ne valide jamais le timbre humain.
- Annuler coupe la livraison puis draine la requête identifiée sous verrou ; terminer seulement l'enfant possédé si sa frontière est incertaine. Fermer explicitement générateurs et worker.
- STT : un seul modèle sélectionné en mémoire, CPU CTranslate2 vérifié ; comparaison hors trajet nominal. Silero seul segmente, trames 512/16 kHz ; SoXR continu, pré-roll et files bornées. Toute perte invalide la prise, jamais de collage entre sessions.
- Capture déclenchée et limitée seulement après contrôle permission/conflits ; aucun changement TCC ou sortie globale. Référence humaine indépendante obligatoire pour homologation ; synthèse, WER ou avg_logprob ne la remplacent pas. Aucun journal des paroles rejetées en usage normal.
- Un seul rédacteur par fichier. Préserver les changements utilisateur.
- DeepSeek : un HTTPX asynchrone réutilisé, endpoint TLS fixe, redirections/proxies d'environnement refusés, Flash non réfléchissant et aucun retry automatique. SSE réellement réassemblé, files bornées ; saturation/annulation ferment le flux sans rejouer de texte.
- Historique en RAM seulement et confirmation explicite du texte réellement affiché/prononcé. Un échec n'est jamais une réponse complète. Les secrets sont importés explicitement, une seule clé vers config/deepseek.env (0600), jamais depuis V1 au runtime. Rapports textuels sans prompts, réponses ni Authorization.
- Tests et revue des fichiers suivis/exclusions avant chaque checkpoint et push. Ne jamais présenter un test simulé comme une preuve matérielle.
- Ne pas changer les réglages globaux Codex, l'identité Git globale ou les protections distantes. Publication privée uniquement avec autorisation.
- PROJECT_STATE.md est mis à jour en place. Les connaissances stables vont dans .serena/memories ; aucun secret ou journal transitoire.
