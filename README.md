# Jarvis Office

Assistant vocal personnel indépendant. Phase 05 : boucle semi-duplex locale intégrée,
avec DeepSeek en streaming et Qwen3 isolé. Qualification STT/voix et essai micro → sortie Mac
restent ouverts ; voir PROJECT_STATE.md. Aucun service de production installé.

Le code original n'est assorti d'aucune licence publique. Voir THIRD_PARTY_NOTICES.md pour les composants tiers et les inconnues.

## Installation et vérification

Python 3.12.13 et uv 0.11.29 ont été vérifiés localement. Le paquet n'a aucune dépendance
d'exécution obligatoire ; l'extra `chat` installe HTTPX. Dépendances et outils sont verrouillés
dans `uv.lock`, avec leurs
empreintes de distributions. Le backend de build est épinglé séparément dans
`pyproject.toml`. TTS et STT possèdent chacun un environnement distinct ; la V1 reste inchangée.

```sh
uv sync --locked --extra chat --no-python-downloads
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

## DeepSeek textuel direct

Contrat vérifié le 7 septembre 2026 dans la [documentation officielle](https://api-docs.deepseek.com/),
le [contrat Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
et le [mode de réflexion](https://api-docs.deepseek.com/guides/thinking_mode/) :
`POST https://api.deepseek.com/chat/completions`, `model=deepseek-v4-flash`, `stream=true`,
`thinking={"type":"disabled"}`, `max_tokens=256`. HTTPX transmet ces champs directement
dans le JSON, sans `extra_body` (spécifique aux SDK) ni `reasoning_effort`. Aucun SDK,
clé OpenAI ou autre fournisseur. L'alias officiel annonce Flash-0731 à cette date ; seuls
le nom demandé/retourné et la date sont observables, pas des poids distants figés.

```sh
uv sync --locked --extra chat --no-python-downloads
# Copier seulement la clé du fichier .env explicitement fourni pour Office :
jarvis-office configure-deepseek --from-env "/chemin privé/.env"
jarvis-office chat --text "Explique en deux phrases ce qu’est un réseau local." \
  --report "$HOME/Library/Application Support/JarvisOffice/reports/chat-test.json"
```

Le secret est conservé sous `~/Library/Application Support/JarvisOffice/config/deepseek.env`
(`DEEPSEEK_API_KEY=…`, fichier 0600, dossier 0700), jamais dans le TOML. L'import ne source
pas le shell, refuse liens, valeurs ambiguës et substitutions ; il ne copie aucune autre
clé et ne remplace pas un secret Office existant valide. Le runtime lit seulement ce fichier,
sans recherche dans V1 ou d'autres coffres. Clé absente/permissions inadéquates : code 3.
Ne jamais envoyer de clé dans le chat. `.env` est ignoré par Git, vérification des suivis incluse.

La réponse s'affiche progressivement sur stdout. Les métadonnées de latence vont sur stderr
et, avec `--report`, dans un nouveau JSON privé sous `reports/`, sans question, réponse,
historique, headers ou exception réseau brute. Seul le texte fourni explicitement et les
quelques échanges confirmés en RAM sont envoyés, jamais de fichier audio/profil vocal.
TLS et hostname vérifiés, endpoint fixe, redirections et proxies d'environnement désactivés.
Compression refusée avant décodage pour borner le flux ; espaces français insécables
normalisés en espaces ordinaires, caractères de contrôle du terminal retirés.
401/403/402/429/5xx, réseau, délais, flux tronqué/invalide, réponse vide, limite de tokens,
saturation et annulation ont des raisons distinctes. Codes : succès 0, échec 1, configuration
2, secret indisponible 3, Ctrl-C géré 130. Aucun retry automatique, aucun poll conversationnel.

```python
import asyncio
from jarvis_office.credentials import load_key
from jarvis_office.deepseek import DeepSeek


async def demo():
    client = DeepSeek(load_key())
    try:
        async with client.turn("Question explicitement adressée à Jarvis") as turn:
            async for event in turn:
                if event.kind == "delta":
                    print(event.text, end="", flush=True)
                # run livre les événements segment au TTS ; chat reste textuel.
            turn.confirm(turn.delivered_text, channel="displayed", complete=True)
    finally:
        await client.close()


asyncio.run(demo())
```

Un client réutilisable, un tour actif, un identifiant et un lecteur réseau asynchrones.
La file de 64 événements permet au lecteur et au consommateur d'avancer indépendamment ;
si elle sature, fermeture du flux et erreur explicite, aucun tampon illimité. Entrée/sortie
4096 caractères, contexte total 12000, quatre tours confirmés au maximum. `reset()` annule
le tour et efface l'historique ; `close()` ferme aussi HTTPX. Aucun résumé ou stockage mémoire.
Un tour généré n'entre pas seul dans l'historique. `delivered_text` signifie remis au
consommateur, pas automatiquement affiché ; la confirmation relève du consommateur.
Après annulation/échec, `confirm(extrait, channel="spoken", complete=False)` accepte seulement
un préfixe des segments déjà remis et note explicitement l'interruption dans le contexte.
Le lecteur devra confirmer ce qu'il a réellement prononcé ; aucune lecture sonore ici.
L'annulation arrête la livraison locale et ferme HTTP, sans prétendre arrêter le calcul
ou la facturation chez le fournisseur. Aucun fragment de ce tour ne rejoint le suivant.

SSE : octets UTF-8, lignes CR/LF/CRLF, commentaires, événements complets et `[DONE]` sont
réassemblés ; rôle, usage et deltas sans contenu ne sont pas prononcés. La documentation
actuelle place l'usage sur le dernier chunk ; une trame d'usage sans `choices` est également
acceptée. Raisonnement/outils inattendus provoquent une erreur sans être prononcés. Les
limites de connexion, premier contenu, inactivité de contenu (keepalive non suffisant) et
durée totale sont distinctes. Une limite `finish_reason=length` n'est pas une réponse complète.

Segmentation française autonome : phrases, première proposition utile, décimales,
abréviations et séparation nombre/unité protégées ; Markdown/liens/code supprimés de façon
bornée. Le timer de 0,8 s ne livre que des mots terminés, garde le dernier demi-mot et
n'annule pas la lecture réseau en cours. Flush final unique seulement après une vraie fin.
Token Markdown bloqué à 512 caractères, tampon de parole à 1024, segments visés ≤256 ;
un mot insécable anormal peut dépasser cette cible jusqu'à la limite du tampon, jamais
être découpé artificiellement. Revue du code/tests V1 refusée par l'outil : pas de copie,
équivalence V1 non affirmée, blocage conservé dans PROJECT_STATE.md.

Mesures distinctes : requête → premier contenu utile, premier segment disponible en file,
fin explicite du texte. **Aucune n'est une latence vocale**. Les tests HTTPX simulés n'utilisent
aucun réseau ; les tests API réels de cette phase se limitent à trois requêtes synthétiques
sur cinq autorisées, 256 tokens maximum chacune. Résultats dans l'inventaire privé et l'état.

## Boucle vocale et contrôle local — phase 05

```sh
# Démarrage en pause, micro fermé. Aucun téléchargement au démarrage.
.venv/bin/jarvis-office run
# Ouvrir http://127.0.0.1:8768 puis Reprendre pour un essai borné.
# Autre déclenchement explicite : 30 secondes d'armement, deux demandes au maximum.
.venv/bin/jarvis-office run --arm --seconds 30 --turns 2
# Test synthétique sans micro ET sans lecture, un seul tour :
.venv/bin/jarvis-office run --text "Jarvis, explique le réseau local." --no-play \
  --report "$HOME/Library/Application Support/JarvisOffice/reports/integration-test.json"
```

Renseigner `[speech].input_device` et `[voice].output_device` dans le TOML privé avec
les **noms exacts et uniques** du micro branché et de la sortie locale Mac choisis.
Les périphériques sont revérifiés, avec fréquence et canaux configurés ; aucun repli,
changement de volume ou sortie globale. Sans sélection explicite, la
lecture et l'armement échouent. Retirer `--no-play` du test textuel autorise sa lecture
sur cette sortie seulement. Un rapport exige un nouveau chemin privé explicite.

Phase 05B : TV **DEFERRED — future phase**, pas un blocage de ce jalon Mac.
`input-list --json` et `output-list --json` interrogent PortAudio dans le runtime audio
existant : noms, canaux, fréquence par défaut et formats testés (16/24/44,1/48 kHz,
int16/float32, un ou deux canaux selon disponibilité). Aucun stream, moteur ou appel
API n'est démarré. Un format accepté par l'interrogation ne prouve ni ouverture ni écoute.
`mic-check --json` vérifie séparément permissions et accès micro concurrents ; même
le vumètre des Réglages Système peut maintenir une entrée active. Fermer ce panneau
manuellement, sans contourner le contrôle ou arrêter un service tiers.

La page est servie uniquement sur `127.0.0.1`, port configurable 8768 ; collision = erreur,
jamais arrêt du propriétaire. Aucun build frontend ni dépendance ajoutée. Amorçage via
POST de même origine ; lecture et commandes exigent Host/Origin/Fetch Metadata stricts,
en-tête local et cookie HttpOnly/SameSite=Strict. Aucune action sur GET, CORS, secret dans
l'URL ou journal d'accès. Les réponses sont rendues avec `textContent`. Le second onglet
ou une reconnexion relit le même état sans ouvrir de micro, modèle ou conversation.

Topologie : contrôleur HTTPX/UI ; enfant STT/Silero/sounddevice/SoXR en Python 3.12 ;
enfant Qwen3 MLX en Python 3.14. Les deux locks audio restent isolés et inchangés.
Moteurs chargés et préchauffés une fois par session, pas par phrase. Une signature du
code des workers vérifie le checkout réellement exécuté ; aucun `sys.path` vers V1.
Workers sans clé ni réseau (sandbox macOS plus garde Python), caches Office seulement.

Le départ est en pause. Reprendre arme un essai limité par `arm_seconds` et `arm_turns`.
Le STT traite **localement toute parole** pendant cet armement ; seule une adresse en
début de transcription, « Jarvis », peut déclencher DeepSeek. Ce n'est ni un wake word
acoustique ni une identification du locuteur : une voix diffusée peut prononcer l'adresse.
Les propos sans adresse sont abandonnés sans affichage ni journalisation. Un simple
« Jarvis » donne un état local, sans LLM, son ou mesure de vraie réponse.

La capture est fermée avant STT/réponse : aucun backlog ou barge-in. Un énoncé Silero
déjà finalisé ne repasse pas par un second VAD. Après la lecture, délai acoustique de
0,35 s par défaut, nouvelles capture/file/normalisation et remise à zéro Silero. Une
pause volontaire n'est jamais annulée par un `finally`. Pause et Annuler arrêtent le
tour et restent en pause ; Reprendre est explicite. Effacer invalide la session et son
historique RAM, sans prétendre supprimer les données déjà envoyées à DeepSeek.
Arrêter ou Ctrl+C ferme le serveur et seulement les enfants possédés.

Le lecteur SSE progresse indépendamment du TTS ; segments en ordre, file de texte
limitée à 2048 caractères (segment actif inclus). Saturation = erreur, aucun mot perdu.
PCM : un fragment IPC borné en vol et un tampon float32 de deux secondes au format de
sortie réel. Crédits de capacité avant chaque envoi, attente maximale trois secondes ;
consommateur bloqué ou dépassement = erreur. Le SoXR de sortie conserve sa traîne entre
segments ; un seul stream sounddevice par réponse, aucun WAV temporaire ou `afplay`.
Sous-alimentation, discontinuité ou contention sont visibles et interdisent une
confirmation complète. Fin normale : drain/stop/close de l'objet possédé ; annulation :
abort/close et purge, fermeture HTTP, drainage Qwen borné ou arrêt du seul worker Office.
Interrompre la livraison n'est pas une preuve d'arrêt immédiat du calcul MLX.

L'historique confirme au plus les **segments entièrement terminés** selon l'échéance
DAC estimée. Généré, remis au TTS, PCM remis au pilote et lecture estimée sont distincts.
Une annulation conserve au plus le préfixe de segments terminé, marqué incomplet.
Ni pourcentage d'octets ni affichage de la réponse ne prouvent les mots entendus.
Les callbacks utilisent un tampon préalloué et ne font ni inférence, réseau, disque
ou attente bloquante. Les opérations natives/pipe bloquantes sont hors boucle de contrôle.

Les horloges ADC/application sont rapprochées explicitement ; si indisponibles, pas de
latence depuis la parole. Le délai VAD fait partie du temps ressenti. PCM produit par
MLX (relatif à sa synthèse), premier PCM livré, remise au pilote et échéance DAC estimée
ne sont pas un son acoustiquement vérifié. Les objectifs p50 ≤2,5 s/p95 ≤4 s ne sont pas
revendiqués : aucun tour micro → sortie locale Mac validé pour l'instant. Dix tours avec doubles ne
sont pas dix conversations matérielles. L'identité vocale reste une validation humaine.

Budget phase 05 : **20 tentatives réelles maximum**, commun à `chat`, `run` et aux
relances, réservé avant HTTP dans `config/phase05-api-budget.json` privé (0600).
Pas de reset/retry automatique ; un crash peut surcompter, jamais renouveler le quota.
Utiliser seulement des demandes synthétiques/non sensibles de validation, 256 tokens
maximum. Les tests usuels utilisent un transport simulé sans réserver de requête réelle.
Rapports explicites : métadonnées seulement, jamais historique courant, clés ou propos
du bureau. Codes `run` : succès/arrêt normal 0, opération 1, configuration 2, clé 3,
interruption par signal du test 130. Un arrêt normal n'homologue pas le matériel.

Les mesures distinguent maintenant le premier PCM converti du premier callback de
sortie, le format réellement rapporté par le stream, l'éligibilité au réarmement et
la réouverture effective suivante. La capture rapporte RMS, pertes, pré-roll mesuré,
silence terminal et fréquence normalisée sans sauvegarder l'audio. Ces données ne
remplacent ni confirmation d'écoute ni qualification humaine du STT/du timbre.

## Configuration et vie privée

Le fichier par défaut est `~/Library/Application Support/JarvisOffice/config.toml`.
`config.toml.example` décrit quatre chemins locaux optionnels, vides au départ. Les chemins
relatifs sont résolus depuis le dossier du fichier TOML ; espaces et `~` sont acceptés.
Les chemins doivent être textuels/locaux ; les paramètres TTS sont typés et bornés.
Les clés inconnues et URL sont refusées. `.env.example` contient
seulement `DEEPSEEK_API_KEY=` ; seuls l'import explicitement déclenché et le fichier secret
Office dédié sont lus par le chemin DeepSeek. Les diagnostics ne lisent aucune clé.

Développement : `~/Developer/jarvis-office`. Inventaire privé unique :
`~/Library/Application Support/JarvisOffice/inventory.phase01.json`. Journaux futurs :
`~/Library/Logs/JarvisOffice/`. Audio, transcripts, poids, secrets, caches et inventaires
restent hors Git. Qwen3/tokenizers, le profil utile, les deux candidats STT de benchmark
et Silero ont été copiés indépendamment ; seul le STT retenu rejoint les actifs sélectionnés.
L'inventaire contient les chemins
résolus et les SHA-256 calculés en flux, avec état d'instabilité ; PROJECT_STATE.md en
donne uniquement une synthèse expurgée.

Les requêtes DeepSeek transmettent uniquement la transcription adressée à
Jarvis et un historique limité, jamais le flux audio ou le profil vocal. La production
future sera sous `Application Support/JarvisOffice/releases/<version>-<sha>/` avec
pointeur `current`, sans exécuter durablement le checkout de développement.

## Jalons

01 fondations/inventaire ; 02 import privé et Qwen3 isolé ; 03 capture/VAD et sélection
d'un seul STT ; 04 DeepSeek textuel en streaming ; 05 pipeline vocal ; 06 qualification,
release et service utilisateur. Une conversation, un tour actif, semi-duplex, sans
interruption pendant la réponse. L'état vérifié et les blocages sont dans PROJECT_STATE.md.
