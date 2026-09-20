# Gateway privé — chemin réseau

Le LaunchAgent `com.jarvisoffice.private` (RunAtLoad, KeepAlive false, trois tentatives) supervise la release `current`. `ETHERNET_REQUIRED` exige une interface `enN` UP/RUNNING/active portant l’IPv4 du SAN ; le 1000baseT USB n’est plus requis. Décision propriétaire : oublier le dongle USB. Chemin vivant = Wi-Fi `en1` avec la même IPv4 d’appairage (certificat et JSON Echo inchangés). IPv4 coupée sur « USB 10/100/1G/2.5G LAN ».

Ne pas activer KeepAlive. Ne pas toucher aux LaunchAgents V1. Après reboot, vérifier que `en1` a toujours l’IPv4 SAN (réglage Wi-Fi manuel) puis `private_service start` depuis `current`, jamais depuis le checkout. Un flap DHCP peut faire quitter le gateway (fail-closed) : relancer le service une fois l’adresse revenue.
