# Produit privé Office

Le contrat commun est PRIVATE_CONTRACT.md : protocole PCM 1 conservé, private_state_v1 et playback_envelope_v1. Les modes demandé/confirmé et les faits physiques sont distincts. Reconnexion et redémarrage reviennent OFF ; ouvrir une interface ne crée jamais un moteur ni une capture.

Le gateway live possède l'unique VoiceLoop. Le dashboard aiohttp est intégré au même processus, propriétaire loopback sur 127.0.0.1:8768 ; le jeton Echo ne donne aucun droit administrateur. Le transport Echo en release exige WSS, CA privée limitée à l'application, validation SAN/expiration et pin, sans fallback clair.

La santé du gateway privé lie le `server_epoch` existant au verrou possédé et à l'identité PID/démarrage/exécutable. Ne jamais le confondre avec `VoiceLoop.session` (conversation renouvelable) ou `Session.id` (connexion Echo). Une ancienne métadonnée sans époque est refusée ; ne pas réparer un verrou actif en copiant une réponse HTTP. Le correctif doit être activé par le démarrage normal d'une release autorisée.

OfficeStore utilise une SQLite locale hors releases, file bornée et thread d'écriture dédié, journal DELETE. Le contenu passif reste RAM et l'archive passive est désactivée initialement. Les conversations deviennent persistantes seulement après activation visible ; une génération invalide les écritures tardives après purge/désactivation. Les sauvegardes utilisent SQLite.backup, restauration isolée. L'activation vérifie sqlite_schema_max dans le manifeste ; ne jamais rollback vers un ancien schéma implicitement.

Le budget nominal configuré par le propriétaire est distinct du compteur diagnostic historique. Le bouton preview_voice utilise seulement TTS local, micro fermé. Le smoke PASSIVE est une action UI explicite avec au plus un appel diagnostic, sans plafond nominal choisi automatiquement.

Le LaunchAgent com.jarvisoffice.private supervise la release non editable via current. PRIVATE_OPERATIONS.md est la référence d'exploitation. live-status.json contient sessions[].metrics (underruns, playback_completed_streams), jamais une preuve acoustique complète. Éviter dumpsys media.audio_flinger sur le firmware Echo (HAL fragile). Pour l'appairage, le sélecteur Android ACTION_OPEN_DOCUMENT accepte le JSON privé ; ne pas exposer son contenu et retirer sa copie temporaire après import. UIAutomator peut échouer à obtenir l'état inactif pendant les animations : vérifier son succès et utiliser une capture fraîche, jamais un ancien dump.
