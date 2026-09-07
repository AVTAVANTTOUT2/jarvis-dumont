# Jarvis Office

Assistant vocal personnel indépendant. Phase 01 : inventaire privé et diagnostics locaux uniquement.

Le code original n'est assorti d'aucune licence publique. Voir THIRD_PARTY_NOTICES.md pour les composants tiers et les inconnues.

## Installation et vérification

Python 3.12.13 et uv 0.11.29 ont été vérifiés localement. Le paquet n'a aucune dépendance
d'exécution ; les outils de développement sont verrouillés dans `uv.lock`, avec leurs
empreintes de distributions. Le backend de build est épinglé séparément dans
`pyproject.toml`. Ne pas fusionner cet environnement avec celui de la V1 ou du futur TTS.

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
fondations : les actifs ne seront importés/configurés qu'aux phases suivantes.

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

## Configuration et vie privée

Le fichier par défaut est `~/Library/Application Support/JarvisOffice/config.toml`.
`config.toml.example` décrit quatre chemins locaux optionnels, vides au départ. Les chemins
relatifs sont résolus depuis le dossier du fichier TOML ; espaces et `~` sont acceptés.
Les clés inconnues, valeurs non textuelles et URL sont refusées. `.env.example` contient
seulement `DEEPSEEK_API_KEY=` ; aucun `.env` n'est chargé par cette phase.

Développement : `~/Developer/jarvis-office`. Inventaire privé unique :
`~/Library/Application Support/JarvisOffice/inventory.phase01.json`. Journaux futurs :
`~/Library/Logs/JarvisOffice/`. Audio, transcripts, poids, secrets, caches et inventaires
restent hors Git. Aucun actif de la V1 n'a été copié. L'inventaire contient les chemins
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
