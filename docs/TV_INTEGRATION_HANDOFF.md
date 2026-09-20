# Handoff — lot Jarvis Office TV v1

Date : 17 septembre 2026
Contrat : `00_CONTRAT_COMMUN_TV_V1` (17 septembre 2026)
Intégration finale : worktree `jarvis-office-tv-final`, branche `codex/tv-integration-private`.
Base : release active via le pointeur `current` (wheel vérifiée, activation atomique).
Validation finale : 278 tests Python, dashboard JavaScript, Ruff/format et Mypy réussis.
Statut : **DEPLOYED_PRIVATE_WITH_KNOWN_LIMITATIONS**, non homologué acoustiquement.

Release privée active via `current` (wheel vérifiée, activation atomique).
La branche privée `codex/tv-integration-private` est poussée. Echo reste OFF,
microphone fermé.
Récepteur TV et AVT debug installés sur la Philips ; AVT habituel conservé.

Le client DeepSeek garde deux connexions HTTP bornées : une requête vocale au
premier plan reste possible pendant l'annulation d'un rollup mémoire. Cela évite
le `PoolTimeout` qui pouvait interrompre un tour Echo avant sa réponse.
Appairage TLS réel et Binder réel validés. La campagne HTTPS sans ADB a validé
AVT search/play/pause/resume/seek/stop ; la qualification physique complète est
suivie dans `jarvis-tv/docs/QUALIFICATION_2026-09-17.md`.

Dernière correction logicielle préparée : un résultat de recherche AVT conserve
son profil lors de la sélection. La confirmation d'un lancement exige le même
playback_id renvoyé par la commande, le contenu complet (saison/épisode inclus),
le profil attendu et l'état playing frais. Une observation différente reste
absente du résultat vocal, même après expiration de l'attente. Ses tests ont
échoué avant la correction puis réussi ; correction activée après l'endurance TV.

## Ce qui est livré ici

Listener TLS LAN `/tv/v1` dans le processus Office privé, file de commandes
bornée, appairage/révocation propriétaire, dispatcher vocal après le filtre
d'adresse Jarvis, UI minimale sur le dashboard loopback.

### Routes appareil (TLS LAN, pas loopback)

| Méthode | Chemin | Auth |
|---|---|---|
| POST | `/tv/v1/pair` | `pairing_id` + `code` (usage unique) |
| POST | `/tv/v1/session` | `Authorization: Bearer` |
| GET | `/tv/v1/commands?connection_id=` | Bearer ; identifiant jamais dans l'URL |
| POST | `/tv/v1/events` | Bearer |

Corps JSON UTF-8 ≤ 64 KiB. Attente `/commands` : 20 s. Heartbeat annoncé : 45 s.
`schema_version` = 1. `device_id`, `command_id`, `event_id`, `server_epoch`,
`connection_id` : UUID canonique. Le testhost `device_id=tv-test` **n'est pas**
un daemon de production et n'est pas accepté par ce serveur.

### Routes propriétaire (dashboard 127.0.0.1)

`POST /api/tv` : cookie + CSRF + Host/Origin/Fetch Metadata. Pas d'`Authorization`.
Actions : `enable`, `disable`, `defaults`, `pair` (`confirm: true`), `revoke`
(`confirm: true` + `device_id`). Le document d'appairage n'apparaît que dans
cette réponse POST (fichier téléchargé). `GET /api/state` expose un snapshot
sans secret : `tv.enabled`, port, empreinte publique, capacités, connexion,
dernier résultat, profondeur de file.

Un Bearer TV ou Echo sur `/api/*` reste 403.

## Configuration (aucun secret dans le dépôt)

Section TOML privée, hors Git, mode 0600 :

```toml
[tv]
bind = "192.168.x.x"          # explicite, jamais 0.0.0.0
port = 8769                   # ≠ port Echo, ≠ 8768
cert = "chemin/cert.pem"
key = "chemin/key.pem"        # 0600
avt_allowed_cert_sha256 = []  # empreintes publiques SHA-256, optionnel
wake_mac = "AA:BB:CC:DD:EE:FF" # MAC Wi-Fi/LAN de la Philips, hors secret
wake_broadcast = "255.255.255.255"
wake_timeout_s = 20
```

`bind`/`cert`/`key` peuvent omettre et réutiliser ceux d'Echo s'ils conviennent.
Le listener ne démarre que si `[tv]` est présent **et** le produit privé est
actif. La fonction reste inactive tant que le propriétaire n'a pas cliqué
Activer. Réutiliser un certificat Echo n'élève pas le jeton TV.

Quand la TV est en veille, l'APK ne peut plus recevoir le long-poll. Jarvis
envoie un paquet Wake-on-LAN à `wake_mac`, attend une reconnexion authentifiée,
vérifie `get_state`, puis envoie `home` avant la commande média. Activez « Wake
on Network/LAN » dans la Philips et réservez son adresse DHCP. Sans MAC
configurée, la commande répond `WAKE_UNCONFIGURED` et ne joue pas à l'aveugle.

Registre : préférence SQLite `tv_registry` (schéma 2, **pas de migration**).

### Recherche SmartTube

Le serveur Office résout une demande textuelle avec `yt-dlp` installé localement
(`/opt/homebrew/bin/yt-dlp` sur ce Mac, ou un binaire trouvé dans `PATH`). Il
demande au maximum dix entrées `ytsearch`, ne télécharge aucun média, applique un
délai de 15 secondes et ne conserve que les identifiants YouTube de onze caractères.
Le premier résultat retourné est choisi de façon déterministe puis envoyé à
SmartTube par l'intent HTTPS déjà qualifié. Si le binaire manque, expire ou renvoie
une réponse invalide, aucune commande de lecture n'est émise et Jarvis l'annonce.

## Actions et capacités

| App | Action | Côté Jarvis | Récepteur actuel |
|---|---|---|---|
| smarttube | `play_content` youtube_video | émet, dit `dispatched` sans inventer l'image | intent officiel, `dispatched` |
| smarttube | `search` | résout localement via `yt-dlp`, conserve les résultats et lance explicitement le premier identifiant réel | `play_content` officiel |
| smarttube | pause/resume/stop/seek | exige playback frais | session média / notifications |
| avt | search / play / transport | refuse si non installé / non lié | Binder absent |
| * | `get_state` | autorisé si déclaré | selon l'app |
| * | `home` | revient à l'accueil après un état vérifié | intent Android HOME |

`completed` n'est **jamais** déduit d'un intent envoyé. Un `command_result`
`completed` pour `play_content` sans playback frais concordant est ramené à
`dispatched`. Reconnexion : commandes non lues → `unknown`, pas de replay.

Voix : parseur local fermé (variantes `mets-moi`, `trouve`, `regarde`, `lance`,
formules polies), sinon extraction JSON via `collect_text` (budget nominal,
`unexpected_tool_call` inchangé). Le JSON n'est pas lu au TTS ; le modèle reçoit
une consigne d'étape vérifiable plutôt qu'un refus global.
Annuler/Pause invalide le tour TV ; Effacer oublie les candidats.

## État des autres lots après raccordement

1. Testhost corrigé : il produit désormais un UUID canonique. Le serveur
   conserve sa validation stricte ; l'ancien identifiant `tv-test` reste refusé.
2. SmartTube : l'APK stable ne fournit toujours pas de recherche structurée.
   Jarvis utilise donc le binaire local `yt-dlp` (`ytsearchN`, sans téléchargement),
   valide les identifiants YouTube, conserve les candidats et lance le premier
   résultat dans SmartTube. La recherche UI de SmartTube n'est jamais pilotée.
3. AVT : service Binder réel implémenté, candidats compilés. Le récepteur choisit
   explicitement le package habituel ou debug et vérifie sa signature. L'état
   lié/non lié doit provenir des capacités observées, jamais de cette documentation.
4. Campagne physique de l'APK debug actuel : non faite (voir
   `/Users/zeldris/Developer/jarvis-tv/docs/HARDWARE_CAMPAIGN.md`).
5. Signature production APK manquante (clé debug seulement).

## Procédure de test isolé (cette candidate)

Depuis ce worktree, **sans** démarrer le service de production :

```sh
cd /Users/zeldris/Developer/jarvis-office-tv
uv sync --locked --extra private --no-python-downloads
uv run --no-sync python -m unittest \
  tests.test_tv_protocol tests.test_tv_voice \
  tests.test_dashboard tests.test_voice tests.test_chat tests.test_storage -v
uv run --no-sync node --test tests/test_dashboard.mjs
uv run --no-sync ruff check src/jarvis_office tests/test_tv_protocol.py tests/test_tv_voice.py
uv run --no-sync ruff format --check src/jarvis_office/tv tests/test_tv_protocol.py tests/test_tv_voice.py
uv run --no-sync mypy
```

Contre le **contrat** : `tests.test_tv_protocol` (appairage, Bearer, file,
expiration, reconnexion sans replay, événement tardif, Host/Forwarded, TLS
épinglé, révocation).

Contre le **client TV** (autre dépôt, ne pas le modifier ici) : générer un
document d'appairage via `POST /api/tv`, l'importer dans l'APK, vérifier pair
→ session → long-poll. Le testhost Python du dépôt TV reste un simulateur
**client** ; il ne remplace pas ce serveur.

Ne pas lancer `jarvis-office` / `private_service` depuis le checkout.

## Ordre de qualification commune (portes encore ouvertes)

1. Revue de ce candidat Jarvis (auth, file, voix, dashboard).
2. Lot AVT : vrai `JarvisMediaService`, empreinte publique réelle dans
   `avt_allowed_cert_sha256`, pas de double dans le runtime.
3. Corrections TV listées ci-dessus si un écart de protocole apparaît sur Philips.
4. Installation non permanente de l'APK **sur la campagne**, confirmation humaine
   image/son, endurance, puis seulement une activation de release Office distincte.

Tant que 2–4 manquent : la chaîne n'est pas homologuée.

## Grammaire locale des commandes (lot 2)

Parseur déterministe dans `src/jarvis_office/tv/parser.py`, raccordé à
`dispatch` / `dispatch_command`. Aucun réseau, moteur ou appareil. Ce n'est
pas une compréhension du français ni une preuve acoustique.

Décisions (codes stables, distincts des phrases d'affichage) :

| Décision | Effet | Extracteur IA |
|---|---|---|
| `MATCH` | Une commande ou sélection unique | Non |
| `REJECT` | Interdit (négation, non-commande, entrée invalide) | Non |
| `AMBIGUOUS` | Précision nécessaire, aucun choix arbitraire | Non |
| `NO_MATCH` | Aucune règle locale | Seulement si demande média plausible |

Formulations reconnues (après casse, accents, espaces, apostrophes, ponctuation
terminale, `mets-moi`) : `pause` / `mets en pause` ; `reprends` / `continue` ;
`arrête` / `stop` ; `suivante` / `morceau suivant` / `passe au suivant` ;
playlists orales 1→index 0 (`playlist une`, `première playlist`, `mets la
playlist numéro 1`) ; `avance de 30 secondes` / `recule de 2 minutes` ;
recherches déjà couvertes (`lance`, `cherche … sur YouTube`, préambules
polies testés) ; identifiants YouTube conservés tels quels.

Refus sans fallback : `ne mets pas en pause`, `mets pas en pause`,
`n'arrête pas`, `ne lance pas Interstellar`, `je fais une pause`,
`elle a dit pause`, `mets la table`, `arrête de parler`. Un mot dans un
titre n'est pas une commande (`joue Ne me quitte pas sur YouTube`,
`cherche Pause sur YouTube`). `cherche Dune 2 sur YouTube` reste une
recherche même s'il existe des candidats.

Ambigu : `mets la playlist` (numéro manquant), `pause puis reprends`
(deux actions), sélection sans candidats ou hors liste. `playlist 0` est
un refus, jamais l'index -1.

Limites : grammaire bornée, pas toutes les négations du français ; une
formulation hors règles reste `NO_MATCH` (fallback média possible) plutôt
qu'une action inventée. `QUERY_MAX` et 500 caractères : pas de troncature
qui enlèverait une négation pour exécuter le reste. Corpus versionné :
`tests/tv_command_corpus.py`.

## Fichiers Jarvis concernés

- `src/jarvis_office/tv/` (`protocol.py`, `hub.py`, `intent.py`, `parser.py`)
- `src/jarvis_office/dashboard.py`, `static/dashboard/*`
- `src/jarvis_office/echo/live.py`, `echo/gateway.py`
- `src/jarvis_office/voice.py`, `deepseek.py`, `storage.py`
- `tests/test_tv_protocol.py`, `tests/test_tv_voice.py`, `tests/test_tv_parser.py`,
  `tests/tv_command_corpus.py` (+ extensions dashboard/storage)
- `docs/TV_INTEGRATION_HANDOFF.md`, `PRIVATE_CONTRACT.md`, `PRIVATE_OPERATIONS.md`,
  `PROJECT_STATE.md`
