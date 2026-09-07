# Jarvis Office — règles durables

- Périmètre minimal : une conversation, un tour actif, semi-duplex. Un STT, un TTS local, un LLM distant ; Silero détecte la parole. Aucun orchestrateur au produit.
- Phase 01 uniquement : diagnostics sans capture, lecture sonore, chargement de moteur, requête DeepSeek ou téléchargement de modèle.
- V1 strictement en lecture seule : aucun import de ses modules, script, modification d'environnement, Git, service, permissions ou périphérique. Ne jamais sourcer ses fichiers .env.
- Secrets, voix, transcripts, poids, inventaires et chemins personnels hors Git. Ne jamais afficher les valeurs de configuration ou les exceptions brutes dans les diagnostics.
- Aucun téléchargement au runtime, routeur multimodèle ou repli silencieux. Pas de RAG, MCP produit, mémoire persistante, base de données, Android ou infrastructure supplémentaire.
- Développement sous ~/Developer/jarvis-office. Données privées sous ~/Library/Application Support/JarvisOffice ; logs sous ~/Library/Logs/JarvisOffice. Production future sous releases/<version>-<sha> avec pointeur current, jamais depuis le checkout.
- Bibliothèque standard et dépendances existantes d'abord ; aucun module audio vide ou abstraction anticipée.
- Un seul rédacteur par fichier. Préserver les changements utilisateur.
- Tests et revue des fichiers suivis/exclusions avant chaque checkpoint et push. Ne jamais présenter un test simulé comme une preuve matérielle.
- Ne pas changer les réglages globaux Codex, l'identité Git globale ou les protections distantes. Publication privée uniquement avec autorisation.
- PROJECT_STATE.md est mis à jour en place. Les connaissances stables vont dans .serena/memories ; aucun secret ou journal transitoire.
