# Jarvis Office

Assistant vocal personnel indépendant. Phase 03 : capture bornée, Silero et STT local,
en complément des diagnostics, de l'import vérifié et de Qwen3 isolé.

Le code original n'est assorti d'aucune licence publique. Voir THIRD_PARTY_NOTICES.md pour les composants tiers et les inconnues.

## Installation et vérification

Python 3.12.13 et uv 0.11.29 ont été vérifiés localement. Le paquet n'a aucune dépendance
d'exécution ; les outils de développement sont verrouillés dans `uv.lock`, avec leurs
empreintes de distributions. Le backend de build est épinglé séparément dans
`pyproject.toml`. TTS et STT possèdent chacun un environnement distinct ; la V1 reste inchangée.

```sh
uv sync --locked --no-python-downloads
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv build --no-python-downloads
```

L'installation des outils/build nécessite leur présence dans le cache ou un accès au
registre. Une fois installé, le diagnostic fonctionne hors réseau. Le lock ne constitue
pas une promesse de build identique octet pour octet sur tous les systèmes.

## Diagnostics

```sh
.venv/bin/jarvis-office doctor --json
.venv/bin/jarvis-office assets inspect --json
.venv/bin/jarvis-office assets inspect --config "/chemin privé/config.toml" --json
```

Le JSON est aussi la sortie par défaut : `schema_version`, `command`, `status`,
`exit_code`, `checks` (`name`, `status`, `reason`). Les erreurs n'affichent aucun chemin,
contenu, identifiant de compte, valeur d'environnement ou exception système brute.

| Code | Signification |
| --- | --- |
| 0 | Contrôles structurels exécutés réussis ; les `NOT_RUN` restent non vérifiés. |
| 1 | `FAIL` : actif manquant/incomplet/invalide ou plateforme incompatible. |
| 2 | Arguments ou configuration explicite absents/invalides. |
| 3 | `BLOCKED_USER` : chemin d'actif non configuré ou permission refusée. |

Un `FAIL` prend priorité sur `BLOCKED_USER`. L'absence de configuration par défaut
est `NOT_RUN` ; ses actifs non configurés sont `BLOCKED_USER`. Ainsi, sans configuration,
les deux commandes retournent 3 sur le Mac cible. Cela ne constitue pas un échec des
fondations : les chemins nécessaires ne sont simplement pas encore configurés.

`doctor` vérifie Python/macOS arm64, les métadonnées des distributions du Python courant
et les actifs configurés. MLX et les moteurs absents sont `NOT_RUN`, car optionnels pour
cette phase. Leur présence ne prouve ni un import réussi ni le fonctionnement du matériel.
La clé DeepSeek n'est ni recherchée ni lue. Micro, lecture sonore, réseau et inférence
restent explicitement `NOT_RUN`. Le diagnostic ne crée aucun dossier, log ou fichier.

`assets inspect` inspecte seulement les chemins explicitement configurés. Il contrôle les
fichiers requis de Qwen3 MLX (y compris le tokenizer vocal et les limites des fichiers
safetensors), un dossier STT CTranslate2, un profil WAV/transcript/métadonnées et un fichier
Silero ONNX/JIT. Les liens vers des fichiers sont autorisés, notamment les snapshots HF ;
les liens cassés, sous-dossiers liés et marqueurs de téléchargement incomplet sont refusés.
Les fichiers JSON sont bornés à 16 Mio et la traversée à 4096 entrées. Aucun moteur ne
valide les tenseurs ou les poids binaires STT/VAD : `PASS` signifie structure locale
contrôlée, pas authenticité, qualité vocale ou aptitude à l'inférence. Les droits vocaux
ne sont jamais déduits d'un champ de métadonnées.

## Import explicite et TTS isolé

```sh
# Python 3.14.6 arm64 déjà installé ; aucun téléchargement de Python ou de poids.
uv sync --project runtime/tts --locked --python /chemin/python3.14 --no-python-downloads
jarvis-office assets import --config "/chemin privé/source.toml" --dry-run --json
jarvis-office assets import --config "/chemin privé/source.toml" --json
jarvis-office tts-test --text "Bonjour, le système vocal est prêt." \
  --output "/chemin privé/demo.wav" --repeat 3 --report "/chemin privé/mesures.json"
```

Le TOML source renseigne seulement `assets.tts_model` et `assets.voice_profile` réels.
L'import publie `assets/<bundle_id>/` sous Application Support/JarvisOffice : `model`,
`voice` et manifeste SHA-256. Il copie les fichiers des symlinks HF, jamais leurs liens,
ignore les caches `.npy`, conserve les avis et refuse les fichiers instables. Publication
du dossier temporaire par renommage atomique, sans hardlink partagé avec la source.
Une copie existante est revérifiée, pas dupliquée. `--dry-run` hache/contrôle mais n'écrit
rien. L'import met à jour l'inventaire privé existant.

Dans le TOML Office, pointer vers les deux dossiers importés et définir `tts.python`
vers `runtime/tts/.venv/bin/python`. Le contrôleur ne télécharge ni n'installe rien.
`runtime/tts/uv.lock` fige les versions observées : mlx-audio 0.4.5, MLX/Metal 0.31.2,
mlx-lm 0.31.3 et leurs dépendances. Aucun écart sur les 3 296 fichiers Python comparés
avec l'environnement de référence ; cette vérification ne couvre pas les patches natifs.

Une commande supervise un enfant persistant, chargé/préchauffé une fois pour ses répétitions.
La référence WAV et son transcript sont obligatoires ; français explicite, ICL, température
0,5, top-p 0,9, top-k 30, intervalle 0,4 s. La pénalité configurée 1,05 est effectivement
bornée à 1,5 par ICL dans mlx-audio 0.4.5. Aucun repli vers une voix générique.
Chargement, warmup PCM non vide et identité vocale humaine sont trois états distincts.

Le protocole borné est documenté dans `tts.py` : identifiant, PCM S16LE mono 24 kHz,
fin et erreur distinctes ; stderr drainé en continu, rétention maximale 32 Kio.
Une requête à la fois. L'annulation coupe la livraison, puis draine sous verrou ; le calcul
MLX continue jusqu'à la fin ou au délai maximal, après lequel seul l'enfant possédé est
terminé. Utiliser `contextlib.aclosing(client.stream(...))` et toujours `await client.close()`.
Aucun tampon de traîne ni seuil d'amplitude ne retire les consonnes faibles.

Les caches appartiennent à Office, l'environnement de l'enfant est expurgé. Sur macOS,
`sandbox-exec` interdit le réseau ; un audit Python interdit aussi sockets/sous-processus.
Un test de connexion réellement refusée complète les variables offline, qui ne sont pas
une preuve à elles seules. Les diagnostics n'importent toujours aucun moteur.

`tts-test` exige un nouveau WAV explicite et ne lit jamais de son (`--play` non proposé à
ce jalon). Répétitions 1 à 5, dernier WAV conservé, toutes les mesures dans le JSON :
chargement, warmup, premier PCM MLX/converti/livré, calcul, durée, RTF et pics RSS/MLX.
Le premier PCM utilisateur depuis la commande inclut le démarrage. « Froid » signifie
nouveau processus, pas cache disque macOS vidé. RTF inférieur à 1 = calcul plus rapide
que la durée audio, pas garantie de fidélité vocale. Sortie/rapport existants refusés ;
erreurs d'opération = 1, configuration/arguments = 2. Aucune preuve d'écoute humaine.

## Capture, VAD et STT local

```sh
uv sync --project runtime/stt --locked --no-python-downloads
jarvis-office assets import-stt --model "/source/candidat" --vad "/source/silero.onnx" \
  --notice "/source/LICENSE" --target benchmark --dry-run --json
# Retirer --dry-run pour importer. --target selected est une décision explicite :
# un seul bundle sélectionné autorisé, aucun remplacement ou téléchargement automatique.
jarvis-office mic-check --device "nom exact du microphone" --json
jarvis-office capture --seconds 5 --rate 48000 \
  --output "$HOME/Library/Application Support/JarvisOffice/corpus/prise.wav" --json
jarvis-office stt-test --input "/chemin privé/prise.wav" \
  --report "$HOME/Library/Application Support/JarvisOffice/reports/stt.json" --json
jarvis-office stt-bench --manifest "/chemin privé/corpus/manifest.json" \
  --small "/chemin privé/benchmark-small/model" --turbo "/chemin privé/benchmark-turbo/model" \
  --repeat 3 --report "$HOME/Library/Application Support/JarvisOffice/reports/qualification.json"
```

Configurer `speech.python`, `assets.stt_model` et `assets.vad_model` avec la copie sélectionnée.
Le lock STT reprend faster-whisper 1.2.1, CTranslate2 4.8.1, ONNX Runtime 1.27.0,
sounddevice 0.5.5, SoXR 1.1.0 et NumPy 2.5.1. Le backend réellement mesuré est CPU
float32, quatre threads, beam 1 ; aucune accélération Metal revendiquée. Chargement et
warmup hors transcription ; le benchmark décharge explicitement le candidat précédent.
Seuls les imports explicites copient des actifs, indépendamment et après vérification SHA-256.
Les candidats sont sous `benchmarks/stt/assets`, le seul sélectionné sous `assets/stt`.

Le micro est résolu par nom exact non ambigu, pas par indice persistant. `mic-check`
interroge formats, permission AVFoundation, activité d'entrée CoreAudio et quatre services
V1 ciblés ; il ne capture pas. Ce contrôle est un instantané, pas une garantie contre un
autre programme démarrant ensuite. Un conflit ou une permission manquante retourne 3
(`BLOCKED_USER`), sans solliciter/modifier TCC ni arrêter V1. L'inspection des commandes
de processus est indisponible dans le sandbox ; cette limite est signalée.

`capture` ouvre seulement sounddevice, pour 0,25 à 30 secondes, jamais en continu. Le callback
copie dans une file bornée ; SoXR HQ conserve son état pour convertir 44,1/48 kHz vers mono
16 kHz. Une perte, un saut d'horodatage ou une déconnexion invalide toute la prise ; au plus
deux reprises repartent de zéro. La fréquence globale du périphérique n'est pas changée.
Silero ONNX conserve état et contexte par session : toutes les trames complètes de 512
échantillons (32 ms) sont traitées, les restes conservés. Pré-roll effectif 320 ms, silence
terminal 512 ms aux réglages fournis. Durée maximale bornée ; troncature refusée pour STT.
Les temps de parole/finalisation viennent des compteurs d'échantillons. Un replay de WAV
marque la latence matérielle `NOT_RUN` et l'attente STT non mesurée ; aucun faux chrono micro.

Un seul VAD nominal, `vad_filter=False` dans faster-whisper ; français explicite, température
unique, pas de cascade ni liste noire de sous-chaînes. Les deux exemples français du cahier
des charges sont conservés par les tests. `avg_logprob` reste une log-probabilité moyenne,
jamais un pourcentage de confiance. Les rapports explicites privés gardent texte brut,
acceptation et raison ; stdout n'expose que des métadonnées, aucune parole rejetée journalisée.
Les nouveaux WAV/rapports existants ne sont pas écrasés. Aucun son n'est joué, aucun réseau
requis : sandbox macOS et garde sockets Python actifs dans le runtime.

WER : Unicode NFKC, minuscules, ponctuation/apostrophes remplacées par des espaces,
accents et chiffres conservés ; aucune conversion chiffres/mots. Noms et nombres écrits
en lettres sont à annoter dans `critical_terms`, contrôlés en plus des chiffres/négations.
Ces alertes conservatrices demandent une revue humaine, ne modifient pas la transcription.
Rapports séparés par calme/bruit et dev/holdout, erreurs, phrases perdues, sorties inventées,
p50 médiane et p95 rang le plus proche. Le pic RSS du second candidat est le maximum du
processus depuis son démarrage, pas une mesure isolée de sa mémoire à lui seul.
La recommandation préfère small seulement si les critères observés passent, sinon turbo ;
`NO_ACCEPTABLE_STT`/code 1 si aucun ne passe. Aucun changement automatique de configuration.
Une recommandation reste `PROVISIONAL`, avec latence globale à valider, jamais homologuée.

```sh
jarvis-office corpus-init \
  --output "$HOME/Library/Application Support/JarvisOffice/corpus/human/manifest.json"
```

Le manifeste prépare 25 phrases × calme/bruit de bureau, dont dix prises réservées au
holdout. Il ne crée aucun enregistrement et laisse `reference` vide/`human_verified=false`.
Déclencher chaque prise individuellement avec `capture`, écouter puis transcrire humainement
ce qui a réellement été dit et annoter les termes critiques/vocabulaire métier. Ne pas utiliser
la sortie STT comme vérité. Un manifeste incomplet n'est pas une référence utilisable.
Ne pas régler les seuils sur le holdout. Moins de 50 prises humaines vérifiées, ou couverture
insuffisante calme/bruit/holdout : validation `BLOCKED_USER`, même avec une belle WER synthétique.

## Configuration et vie privée

Le fichier par défaut est `~/Library/Application Support/JarvisOffice/config.toml`.
`config.toml.example` décrit quatre chemins locaux optionnels, vides au départ. Les chemins
relatifs sont résolus depuis le dossier du fichier TOML ; espaces et `~` sont acceptés.
Les chemins doivent être textuels/locaux ; les paramètres TTS sont typés et bornés.
Les clés inconnues et URL sont refusées. `.env.example` contient
seulement `DEEPSEEK_API_KEY=` ; aucun `.env` n'est chargé par cette phase.

Développement : `~/Developer/jarvis-office`. Inventaire privé unique :
`~/Library/Application Support/JarvisOffice/inventory.phase01.json`. Journaux futurs :
`~/Library/Logs/JarvisOffice/`. Audio, transcripts, poids, secrets, caches et inventaires
restent hors Git. Qwen3/tokenizers, le profil utile, les deux candidats STT de benchmark
et Silero ont été copiés indépendamment ; seul le STT retenu rejoint les actifs sélectionnés.
L'inventaire contient les chemins
résolus et les SHA-256 calculés en flux, avec état d'instabilité ; PROJECT_STATE.md en
donne uniquement une synthèse expurgée.

Les futures requêtes DeepSeek transmettront uniquement la transcription adressée à
Jarvis et un historique limité, jamais le flux audio ou le profil vocal. La production
future sera sous `Application Support/JarvisOffice/releases/<version>-<sha>/` avec
pointeur `current`, sans exécuter durablement le checkout de développement.

## Jalons

01 fondations/inventaire ; 02 import privé et Qwen3 isolé ; 03 capture/VAD et sélection
d'un seul STT ; 04 DeepSeek textuel en streaming ; 05 pipeline vocal ; 06 qualification,
release et service utilisateur. Une conversation, un tour actif, semi-duplex, sans
interruption pendant la réponse. L'état vérifié et les blocages sont dans PROJECT_STATE.md.
