# Exploitation privée

Le gateway et le dashboard appartiennent au même processus, avec le VoiceLoop
existant. Le Mac écoute le gateway uniquement sur l'adresse Ethernet configurée,
en WSS et avec une identité par appareil. Le dashboard reste sur
`http://127.0.0.1:8768/` ; aucune exposition LAN administrateur n'est implémentée.

Le lancement utilise `current/main/bin/python -I -B -m
jarvis_office.private_service install|start|stop|status` depuis le dossier privé
Office, jamais le checkout. Le LaunchAgent `com.jarvisoffice.private` démarre OFF
à l'ouverture de session, avec trois tentatives maximum en cas d'échec.
Le dashboard donne la readiness réelle ; aucun heartbeat n'appelle DeepSeek.

La CLI de santé vérifie d'abord le verrou détenu, son propriétaire et l'identité
PID/démarrage/exécutable. Pour le gateway privé, elle compare ensuite le
`server_epoch` publié dans ce verrou avec celui du dashboard. Cet identifiant
reste stable pendant la vie du gateway ; la session conversationnelle change
après Effacer, et la session Echo change à la reconnexion. Aucune des deux ne
remplace l'identité du processus. Une époque différente est refusée.
Une ancienne release sans époque dans le verrou est refusée explicitement par
la nouvelle CLI (`instance_epoch_missing`). Ne pas réécrire le verrou ni copier
l'identifiant reçu : seule l'activation autorisée de la candidate, avec démarrage
normal de son propriétaire, peut publier les nouvelles métadonnées.
Chaque appel de santé libère sa propre session HTTP par un POST authentifié et
protégé CSRF, sans fermer les sessions du navigateur. Les versions antérieures
conservent ces sessions jusqu'à leur expiration (12 heures) et peuvent atteindre
la limite de 16 : réutiliser l'onglet propriétaire déjà authentifié. Ne pas
redémarrer une instance ou purger ses sessions pour masquer un résultat de santé.

Avant Conversation/Écoute contextuelle, choisir le plafond nominal dans Réglages.
Le compteur de diagnostic ECHO-02C reste indépendant et n'est jamais réinitialisé.
L'historique adressé commence seulement après activation explicite (30 jours
proposés) ; le contexte passif reste en RAM sauf option d'archivage séparée.
Une archive consultée ne devient jamais automatiquement un prompt.

SQLite : `data/office.sqlite3` hors releases, schéma 1, journal DELETE,
transactions/file bornées. Les exports ont un périmètre explicite et les CSV
neutralisent les formules. La sauvegarde utilise l'API SQLite ; restauration
opérateur via `OfficeStore.restore_isolated(source,destination)` vers un chemin
absent, avec integrity_check/foreign_key_check. Ne jamais remplacer la base active
pour démontrer un retour arrière. Les exports déjà copiés ne sont pas effacés
par une purge ; les sauvegardes ont leur rétention séparée.

Couple compatible : serveur 0.3.0, APK 0.3.0-private/code 11, PCM protocole 1,
capacités private_state_v1/playback_envelope_v1, SQLite schéma 1. La bouche suit
la position AudioTrack locale et une énergie PCM ; aucune précision phonétique
ou latence acoustique n'est annoncée. Aucun nouvel STT/TTS/poids/gain/prefill.

Appairage : importer sur l'APK un document JSON privé schema_version/host/port/
device/secret/pin/ca_pem depuis Commandes → connexion. Supprimer le document de
transfert après import. La CA est limitée à l'application, avec vérification du
SAN, de l'expiration, de la chaîne et du pin ; jamais de repli en clair.
Rotation : nouveau secret serveur/appareil, certificat avec SAN correct et pin
correspondant, puis réimport explicite. Révoquer une identité signifie la retirer
du fichier devices puis redémarrer le service OFF. Les clés CA et APK/signing
sont conservées hors Git avec copie privée vérifiée, pas dans les exports métier.

Avant activation : conserver config, budget, APK existante et empreintes sous
`private-v1/before-activation`. La signature release diffère de la dev.4 ; conserver
l'APK originale et la configuration d'appairage avant migration, puis réappairer.
Le rollback applicatif standard refuse un schéma supérieur à celui du manifeste
de release. Retour à une ancienne dev : arrêter le service privé, laisser SQLite
intacte, restaurer uniquement la configuration et l'APK sauvegardées puis lancer
explicitement l'ancien gateway. Cette ancienne pile n'est pas une release privée
sécurisée et ne doit pas redevenir permanente par un rollback aveugle.

Les anciens résultats ECHO-02C/ECHO-02D restent immuables ; le catalogue Données
montre une sélection expurgée avec empreintes. Les erreurs STT et underruns
historiques restent connus. La preuve matérielle courante et les SHA réellement
activés sont dans PROJECT_STATE.md et le manifeste privé de déploiement.
