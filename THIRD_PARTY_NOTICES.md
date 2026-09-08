# Composants tiers — constats des phases 01 à 05

Phase 05B, 8 septembre 2026 : sortie locale sounddevice/SoXR exercée avec Qwen3
et le profil Office existants ; aucun paquet, poids, tokenizer ou code tiers ajouté.
Lecture technique et arrêt/reprise de sortie testés ; reconnaissance humaine du timbre
encore en attente. Les inconnues de droits/provenance ci-dessous restent ouvertes.

Vérifié le 7 septembre 2026. Aucun octroi automatique de licence publique au code original.
Le dépôt privé ne dispense d'aucune obligation tierce. Aucun extrait de la V1 n'est repris.

Phase 05 : intégration originale des composants Office, sans nouvel extrait V1,
dépendance Python/binaire, poids ou tokenizer. Les versions et avis ci-dessous restent
applicables. La page HTML/JS originale est incluse dans le wheel ; aucun framework,
asset tiers ou runtime d'orchestration ajouté. sounddevice 0.5.5 est désormais raccordé
à la sortie progressive (formats interrogés, objets stop/abort/close inspectés) ; aucune
lecture physique réalisée tant que la sortie n'est pas explicitement sélectionnée.
Les droits vocaux, notices binaires et provenances inconnues ne sont pas résolus par
l'intégration. Aucune redistribution publique ni release de production dans cette phase.

Phase 04 : le segmenteur Office est nouveau. Lecture de `jarvis/audio/tts/segmenter.py`
et `tests/test_tts_segmenter.py` V1 refusée par l'outil ; aucune reprise de code ni
équivalence affirmée. Le contrat HTTP est implémenté à partir de la documentation officielle.
Les métadonnées/licences installées et fichiers de révision ont été lus sans importer les
moteurs. Les avis fournis restent dans leurs distributions existantes. Ce tableau est un
inventaire, pas une autorisation de redistribution des actifs ou de la voix.

## Poids, tokenizers et code source examiné

Tous ces actifs restent hors Git et hors du wheel Office. Qwen3/tokenizers, le profil,
Silero et les deux candidats STT ont des copies privées indépendantes. Les cartes STT
figées sont conservées séparément des bundles immuables ; aucun LICENSE autonome dans
les snapshots STT locaux. Une annonce MIT n'établit pas toute la provenance de conversion.

| Composant / révision constatée | Licence constatée et source officielle | Usage / limites |
| --- | --- | --- |
| JarvisAPI `67968ed5bd4dee2cf402efe5088f6ac48bc94d19`, avec modifications locales | Aucune licence racine ; avis séparés dans `integrations/opencode/`. [Source](https://github.com/AVTAVANTTOUT2/JarvisAPI/tree/67968ed5bd4dee2cf402efe5088f6ac48bc94d19). Droits de reprise inconnus. | Lecture seule ; aucun code copié. |
| Qwen3-TTS Base 0.6B MLX 6bit `4e44ed4bcee28a0f89a493e07bde16e6dccd43eb` | Apache-2.0 annoncée dans la [fiche de cette révision](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-0.6B-Base-6bit/blob/4e44ed4bcee28a0f89a493e07bde16e6dccd43eb/README.md) ; aucun fichier LICENSE autonome dans la liste des fichiers. | TTS futur ; poids locaux et tokenizer vocal identifiés, SHA-256 égaux aux etags locaux. Conversion annoncée avec mlx-audio 0.3.0, distinct du runtime installé 0.4.5. |
| Qwen3 Base source `5d83992436eae1d760afd27aff78a71d676296fc` (révision vérifiée, pas provenance exacte de conversion) | Apache-2.0 dans la [fiche figée](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base/blob/5d83992436eae1d760afd27aff78a71d676296fc/README.md), aucun LICENSE autonome listé. | Révision amont effectivement utilisée par la conversion inconnue ; modèle source non téléchargé. |
| Tokenizers Qwen : `vocab.json`, `merges.txt`, configuration et `speech_tokenizer` à la même révision MLX | Même fiche Apache-2.0 ; aucun avis distinct constaté dans les fichiers locaux du tokenizer. | Fichiers identifiés, dont les poids du tokenizer vocal ; provenance amont précise à compléter. |
| faster-whisper-small et tokenizer `536b0662742c02347bc0e980a01041f333bce120` | MIT annoncée dans la [fiche figée](https://huggingface.co/Systran/faster-whisper-small/blob/536b0662742c02347bc0e980a01041f333bce120/README.md). | Copie CTranslate2 complète, benchmark seulement ; amont exact de conversion inconnu. |
| faster-whisper-large-v3-turbo et tokenizer `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` | MIT annoncée dans la [fiche figée](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/blob/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/README.md). | Copie CTranslate2 complète ; seul actif STT sélectionné, provisoire/non homologué. Amont exact de conversion inconnu. |
| Silero VAD 6.2.1, ONNX SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` | MIT, LICENSE local conservé dans chaque copie ; [source versionnée](https://github.com/snakers4/silero-vad/tree/v6.2.1). | Inférence ONNX CPU, interface Office écrite pour cet ABI ; paquet Torch/Silero non installé dans Office. |
| Profil vocal local (WAV, transcript, métadonnées) | Droits et consentement **non vérifiés**, distincts de la licence Qwen. La métadonnée déclare un usage local fourni par le propriétaire ; ce n'est pas une preuve indépendante. | Copie et synthèses privées demandées par l'utilisateur ; aucune diffusion, écoute humaine non effectuée. |

## Environnements audio existants — inventaire de phase 01

Les versions multiples correspondent à des environnements séparés. Les paquets et leurs
binaires restent dans la V1/runtime MLX ; ils ne sont ni vendus ni embarqués par cette phase.

| Composant/version | Licence constatée / source officielle | Usage |
| --- | --- | --- |
| MLX + mlx-metal 0.31.2 ; mlx-audio 0.4.5 ; mlx-lm 0.31.3 | MIT, métadonnées et licences installées ; [MLX](https://github.com/ml-explore/mlx), [mlx-audio](https://github.com/Blaizzy/mlx-audio), [mlx-lm](https://github.com/ml-explore/mlx-lm). | Runtime Python 3.14 séparé ; `libmlx` et `libjaccl` présents. Notices transitives exactes à revoir avant distribution. |
| faster-whisper 1.2.1 ; CTranslate2 4.8.1 (extension et dylib incluses) | MIT dans métadonnées ; [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [CTranslate2](https://github.com/OpenNMT/CTranslate2). | STT existant, Python 3.12. |
| PyAudio 0.2.14 ; sounddevice 0.5.5 | MIT dans métadonnées ; [PyAudio](https://people.csail.mit.edu/hubert/pyaudio/), [sounddevice](https://github.com/spatialaudio/python-sounddevice). | Audio existant, jamais importé ici. |
| PortAudio Homebrew 19.7.0 ; dylibs embarquées par sounddevice | MIT dans formule Homebrew ; [source](https://www.portaudio.com/). Version exacte des dylibs embarquées inconnue. | Binaires présents ; aucun stream ouvert. |
| soundfile 0.14.0 ; libsndfile embarquée | BSD-3-Clause pour wrapper ; COPYING LGPL-2.1 pour libsndfile, version binaire inconnue. [Source](https://github.com/bastibe/python-soundfile), [libsndfile](https://github.com/libsndfile/libsndfile). | Lecture/écriture audio future ; non importés. |
| PyAV 18.0.0 | BSD-3-Clause dans sa licence ; [source](https://github.com/PyAV-Org/PyAV). | Décodeur STT existant ; cette licence ne couvre pas seule ses codecs. |
| FFmpeg Homebrew 8.1.2_1 ; FFmpeg embarqué PyAV (avcodec 62.28.102, avformat 62.12.102) | GPL-3.0-or-later dans formule Homebrew ; conditions exactes du build PyAV **inconnues**. [Source](https://ffmpeg.org/legal.html). | Binaires présents, pas distribués. Ne pas confondre les deux builds. |
| Codecs PyAV : SVT-AV1 4.1.0, dav1d, LAME, opencore-amrnb/wb, Opus, SharpYUV, VPX, WebP/WebPMux, x264 ABI 165, x265 ABI 216 | Licences et versions complètes des binaires embarqués **inconnues** ; [projet d'empaquetage officiel](https://github.com/PyAV-Org/PyAV). Les noms/ABI ne prouvent pas les versions source. | Liste locale relevée dans l'inventaire ; obligations de tous ces codecs à résoudre avant redistribution. |
| NumPy 2.5.1 / 2.4.6 ; SciPy 1.17.1 / 1.18.0 | NumPy : BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 ; SciPy : avis BSD et tiers fournis. [NumPy](https://numpy.org/), [SciPy](https://scipy.org/). | Calcul existant ; notices des bibliothèques natives fournies à conserver. |
| Torch 2.13.0 / 2.11.0 ; torchaudio 2.11.0 ; torchcodec 0.15.0 | Torch 2.13 : expression Apache-2.0, LLVM-exception, BSD-2/3, BSL-1.0 et MIT ; autres builds : avis installés, qualification transitive incomplète. [Source](https://pytorch.org/). | Présence inventoriée ; compatibilité des versions non testée. |
| ONNX Runtime 1.27.0 / 1.23.2 ; transformers 4.57.6 / 5.14.1 / 5.9.0 ; tokenizers 0.22.2 ; librosa 0.11.0 | Détails dans métadonnées privées ; notices transitives de ces builds à compléter avant réutilisation. [ONNX](https://github.com/microsoft/onnxruntime), [Transformers](https://github.com/huggingface/transformers), [Tokenizers](https://github.com/huggingface/tokenizers), [librosa](https://github.com/librosa/librosa). | Dépendances existantes uniquement, pas de nouvelle installation Office. |

## Outils installés pour Office

Le wheel Office contient uniquement le paquet Python original ; aucun outil ci-dessous
n'y est embarqué. `uv.lock` verrouille les dépendances de développement. Les licences
accompagnant les distributions installées sont conservées dans l'environnement dédié.

| Composant/version | Licence constatée / source officielle | Distribution / usage |
| --- | --- | --- |
| CPython 3.12.13 | PSF et notices tierces de l'interpréteur ; [source](https://www.python.org/). | Interpréteur système réutilisé, non copié dans le wheel. |
| uv / uv_build 0.11.29 | MIT OR Apache-2.0, [source](https://github.com/astral-sh/uv/tree/0.11.29). | uv déjà installé ; backend de build épinglé, pas de moteur audio. |
| Ruff 0.16.6 | MIT, licence installée ; [source](https://github.com/astral-sh/ruff). | Binaire de lint/format, développement uniquement. |
| mypy 2.3.1 ; mypy_extensions 1.1.0 | MIT, licences installées ; [mypy](https://github.com/python/mypy), [extensions](https://github.com/python/mypy_extensions). | Typage, dont extension compilée mypy. |
| ast_serialize 0.10.0 ; librt 0.15.0 | MIT, licences installées ; [ast_serialize](https://github.com/mypyc/ast_serialize), [librt](https://github.com/mypyc/librt). | Extensions binaires transitives de mypy, développement uniquement. |
| pathspec 1.1.1 | MPL-2.0 dans LICENSE installé ; [source](https://github.com/cpburnz/python-pathspec). | Dépendance transitive de mypy, non embarquée. |
| typing_extensions 4.16.0 | PSF-2.0, métadonnées installées ; [source](https://github.com/python/typing_extensions). | Dépendance transitive de mypy, non embarquée. |

## Runtime TTS installé pour Office — phase 02

Python 3.14.6, environnement neuf verrouillé dans `runtime/tts/uv.lock`. Les paquets
et avis restent dans cet environnement local, aucun binaire ni poids dans le wheel/Git.
3 296 fichiers Python MLX/MLX-LM/mlx-audio/Transformers/Tokenizers identiques aux paquets
épinglés réinstallés : aucun patch Python détecté dans ce périmètre. Le code Office est
nouveau, utilise l'API publique ; pas de copie du code V1. Lecture détaillée des quatre
adaptateurs V1 bloquée par le périmètre de l'outil, donc équivalence exhaustive non affirmée.

| Composants installés / versions | Licence constatée / source officielle | Usage et limites |
| --- | --- | --- |
| mlx / mlx-metal 0.31.2 ; mlx-audio 0.4.5 ; mlx-lm 0.31.3 | MIT ; [MLX](https://github.com/ml-explore/mlx), [Audio versionnée](https://github.com/Blaizzy/mlx-audio/tree/v0.4.5), [LM](https://github.com/ml-explore/mlx-lm). | Moteur local ; extension MLX, libmlx/libjaccl et bibliothèque Metal installées. |
| transformers 5.9.0 ; huggingface-hub 1.16.4 ; tokenizers 0.22.2 ; safetensors 0.7.0 ; hf-xet 1.5.0 | Apache-2.0 ; [Transformers](https://github.com/huggingface/transformers), [Hub](https://github.com/huggingface/huggingface_hub), [Tokenizers](https://github.com/huggingface/tokenizers), [Safetensors](https://github.com/huggingface/safetensors), [Xet](https://github.com/huggingface/xet-core). | Chargement local ; extensions Rust installées, réseau interdit. Notices transitives Rust non exhaustivement qualifiées. |
| sentencepiece 0.2.1 | Licence absente du wheel constaté : **inconnue pour ce binaire** ; [source](https://github.com/google/sentencepiece). | Extension native installée transitivement ; pas une autorisation de redistribution. |
| numpy 2.4.6 ; scipy 1.18.0 | NumPy : BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 ; SciPy : BSD et avis tiers installés. [NumPy](https://numpy.org), [SciPy](https://scipy.org). | Extensions natives ; avis installés conservés, pas réduits à la licence du wrapper. |
| OpenBLAS/LAPACK ; libgcc/libgfortran/libquadmath des wheels NumPy/SciPy | BSD-3-Clause / BSD-3-Clause-Open-MPI ; GPL-3.0-or-later WITH GCC-exception-3.1 ; LGPL-2.1-or-later dans leurs LICENSE.txt. [OpenBLAS](https://www.openblas.net), [LAPACK](https://www.netlib.org/lapack/), [GCC](https://gcc.gnu.org/onlinedocs/libstdc++/manual/license.html). | Binaires installés ; versions source exactes **inconnues**. ABI des fichiers et empreintes privées ne les établissent pas. |
| miniaudio 1.71 ; sounddevice 0.5.5 | MIT wrappers ; [miniaudio Python](https://github.com/irmen/pyminiaudio), [sounddevice](https://github.com/spatialaudio/python-sounddevice). | Décodage WAV par miniaudio ; aucun stream audio ouvert. Version/avis exacts du cœur miniaudio embarqué et de PortAudio **inconnus**. |
| cffi 2.1.0 ; protobuf 7.35.0 ; PyYAML 6.0.3 ; regex 2026.5.9 ; MarkupSafe 3.0.3 | MIT-0 ; BSD-3-Clause ; MIT ; Apache-2.0 AND CNRI-Python ; BSD-3-Clause. [CFFI](https://github.com/python-cffi/cffi), [Protobuf](https://github.com/protocolbuffers/protobuf), [YAML](https://pyyaml.org), [Regex](https://github.com/mrabarnett/mrab-regex), [MarkupSafe](https://github.com/pallets/markupsafe). | Extensions binaires transitives installées ; versions natives embarquées non déduites des versions Python. |
| certifi 2026.5.20 ; tqdm 4.67.3 ; packaging 26.2 ; typing_extensions 4.15.0 | MPL-2.0 ; MPL-2.0 AND MIT ; Apache-2.0 OR BSD-2-Clause ; PSF-2.0. [Certifi](https://github.com/certifi/python-certifi), [tqdm](https://github.com/tqdm/tqdm), [Packaging](https://github.com/pypa/packaging), [Typing](https://github.com/python/typing_extensions). | Dépendances Python, certificats inclus mais aucun appel réseau. |
| anyio 4.13.0 ; h11 0.16.0 ; httpcore 1.0.9 ; httpx 0.28.1 ; idna 3.16 | MIT ; MIT ; BSD-3-Clause ; BSD-3-Clause ; BSD-3-Clause. [AnyIO](https://github.com/agronholm/anyio), [h11](https://github.com/python-hyper/h11), [Core](https://github.com/encode/httpcore), [HTTPX](https://github.com/encode/httpx), [IDNA](https://github.com/kjd/idna). | Transitifs du Hub, installés mais réseau interdit. |
| click 8.4.1 ; fsspec 2026.4.0 ; Jinja2 3.1.6 ; pycparser 3.0 ; Pygments 2.20.0 | BSD-3-Clause, sauf Jinja2 : BSD (métadonnée), Pygments : BSD-2-Clause. [Click](https://github.com/pallets/click), [Fsspec](https://github.com/fsspec/filesystem_spec), [Jinja](https://github.com/pallets/jinja), [Parser](https://github.com/eliben/pycparser), [Pygments](https://pygments.org). | Utilitaires Python transitifs. |
| annotated-doc 0.0.4 ; filelock 3.29.0 ; markdown-it-py 4.2.0 ; mdurl 0.1.2 ; rich 15.0.0 ; typer 0.25.1 ; shellingham 1.5.4 | MIT sauf shellingham ISC. [Annotated](https://github.com/fastapi/annotated-doc), [Filelock](https://github.com/tox-dev/py-filelock), [Markdown](https://github.com/executablebooks/markdown-it-py), [Mdurl](https://github.com/executablebooks/mdurl), [Rich](https://github.com/Textualize/rich), [Typer](https://github.com/fastapi/typer), [Shellingham](https://github.com/sarugaku/shellingham). | Utilitaires Python transitifs. |

Qwen3/tokenizers : fiche Apache-2.0 copiée intacte ; absence de LICENSE autonome confirmée
aux révisions vérifiées. Révision amont exacte de conversion toujours inconnue. Le texte
Apache-2.0 de référence est conservé à côté du bundle privé, sans inventer d'avis titulaire.
Avant redistribution/release : résoudre les notices binaires manquantes et les droits
du profil. La licence de modèle ne vaut ni consentement vocal ni validation humaine.

## Runtime STT installé pour Office — phase 03

Python 3.12.13, environnement neuf et versions effectives verrouillées dans
`runtime/stt/uv.lock`. Les versions existantes ont été lues comme métadonnées, sans
copie du venv ou du code V1. Aucun de ces paquets, binaires, poids ou tokenizers n'est
embarqué dans Git ou le wheel Office ; ils sont installés localement avec leurs avis.
Cette absence de redistribution ne dispense pas de leurs conditions applicables.

| Composants installés / versions | Licence constatée / source officielle | Usage et limites |
| --- | --- | --- |
| faster-whisper 1.2.1 ; CTranslate2 4.8.1 | MIT métadonnées ; [FW versionné](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1), [CT2](https://github.com/OpenNMT/CTranslate2/tree/v4.8.1). | STT CPU, extension et libctranslate2 installées ; pas de LICENSE autonome constaté dans le wheel CT2. |
| ONNX Runtime 1.27.0 | MIT, LICENSE et ThirdPartyNotices installés ; [source](https://github.com/microsoft/onnxruntime). | Silero local, extension et libonnxruntime ; notices transitives conservées. |
| sounddevice 0.5.5 ; PortAudio embarqué | MIT wrapper ; [source](https://github.com/spatialaudio/python-sounddevice/tree/0.5.5), [PortAudio](https://www.portaudio.com/). | Capture CoreAudio réelle ; version source exacte du dylib PortAudio inconnue. PyAudio non installé. |
| soxr 1.1.0 ; libsoxr/PFFFT embarqués | LGPL-2.1-or-later, COPYING.LGPL, LICENSE-libsoxr.txt, LICENSE-PFFFT.txt et LICENSE.txt installés ; [wrapper](https://github.com/dofuuz/python-soxr), [libsoxr](https://sourceforge.net/projects/soxr/). | Rééchantillonnage continu ; versions natives exactes inconnues, ne pas réduire à la licence Python. |
| NumPy 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 ; [source](https://numpy.org/). | Extensions natives et avis tiers installés ; aussi dépendance de tests du contrôleur, pas dépendance de son wheel. |
| PyAV 18.0.0 ; FFmpeg et codecs embarqués | BSD-3-Clause wrapper ; licences natives exactes **inconnues**, voir inventaire ci-dessus et [PyAV](https://github.com/PyAV-Org/PyAV), [FFmpeg](https://ffmpeg.org/legal.html). | 19 dylibs du wheel installées transitivement : avcodec 62.28.102, avformat 62.12.102, avdevice 62.3.102, avfilter 11.14.102, avutil 60.26.102, swresample 6.3.102, swscale 9.5.102, SVT-AV1 4.1.0, dav1d, LAME, opencore-amrnb/wb, Opus, SharpYUV, VPX, WebP/WebPMux, x264 ABI 165, x265 ABI 216. Qualification requise avant distribution. |
| huggingface-hub 0.36.2 ; tokenizers 0.22.2 ; hf-xet 1.5.1 ; flatbuffers 25.12.19 | Apache-2.0 métadonnées/classifiers ; [Hub](https://github.com/huggingface/huggingface_hub), [Tokenizers](https://github.com/huggingface/tokenizers), [Xet](https://github.com/huggingface/xet-core), [FlatBuffers](https://github.com/google/flatbuffers). | Extensions Rust installées ; pas de LICENSE autonome dans le wheel Tokenizers constaté. Notices transitives Rust à compléter ; réseau interdit. |
| cffi 2.1.0 ; protobuf 6.33.6 ; PyYAML 6.0.3 ; charset-normalizer 3.4.9 | MIT-0 ; BSD-3-Clause ; MIT ; MIT, avis installés ; [CFFI](https://github.com/python-cffi/cffi), [Protobuf](https://github.com/protocolbuffers/protobuf), [YAML](https://pyyaml.org/), [Charset](https://github.com/jawah/charset_normalizer). | Extensions natives transitives, aucune identité de version native inventée. |
| requests 2.34.2 ; urllib3 2.7.0 ; certifi 2026.6.17 ; idna 3.18 | Apache-2.0 ; MIT ; MPL-2.0 ; BSD-3-Clause ; [Requests](https://github.com/psf/requests), [urllib3](https://github.com/urllib3/urllib3), [Certifi](https://github.com/certifi/python-certifi), [IDNA](https://github.com/kjd/idna). | Transitifs du Hub installés, aucun appel réseau nominal. |
| filelock 3.29.7 ; fsspec 2026.6.0 ; packaging 26.2 ; pycparser 3.0 ; setuptools 83.0.0 | MIT ; BSD-3-Clause ; Apache-2.0 OR BSD-2-Clause ; BSD-3-Clause ; MIT ; [Filelock](https://github.com/tox-dev/py-filelock), [Fsspec](https://github.com/fsspec/filesystem_spec), [Packaging](https://github.com/pypa/packaging), [Parser](https://github.com/eliben/pycparser), [Setuptools](https://github.com/pypa/setuptools). | Avis des utilitaires et sous-paquets vendored setuptools conservés, inventaire transitif non exhaustivement qualifié. |
| tqdm 4.68.4 ; typing_extensions 4.16.0 | MPL-2.0 AND MIT ; PSF-2.0 ; [tqdm](https://github.com/tqdm/tqdm), [Typing](https://github.com/python/typing_extensions). | Utilitaires Python transitifs. |

## Extra HTTP installé pour Office — phase 04

Un seul transport, HTTPX 0.28.1 déjà présent dans le runtime TTS, réinstallé séparément
dans le contrôleur via l'extra `chat`. Versions effectives figées dans `uv.lock` ; aucun
SDK OpenAI, aucune seconde bibliothèque cliente utilisée. Les six distributions ci-dessous
sont Python/données, sans nouveau moteur ou binaire natif audio. Le TLS utilise l'interpréteur
CPython existant ; ses notices ne sont pas remplacées par celles de HTTPX. Les fichiers de
licence installés sont conservés. Le wheel Office ne les embarque pas, il déclare l'extra.

| Composant/version | Licence constatée / source officielle | Utilisation / distribution |
| --- | --- | --- |
| httpx 0.28.1 ; httpcore 1.0.9 | BSD-3-Clause, LICENSE.md installés ; [HTTPX](https://github.com/encode/httpx/tree/0.28.1), [Core](https://github.com/encode/httpcore). | HTTP/SSE asynchrone direct ; installation optionnelle locale, non embarquée. |
| anyio 4.15.1 ; h11 0.16.0 | MIT, LICENSE/ LICENSE.txt installés ; [AnyIO](https://github.com/agronholm/anyio), [h11](https://github.com/python-hyper/h11). | Concurrence et protocole HTTP transitifs, non embarqués. |
| certifi 2026.7.22 | MPL-2.0, LICENSE installé ; [source](https://github.com/certifi/python-certifi). | Certificats CA pour TLS vérifié, non embarqués. |
| idna 3.19 | BSD-3-Clause, LICENSE.md installé ; [source](https://github.com/kjd/idna). | Noms d'hôte transitifs, non embarqué. |
| DeepSeek API, alias deepseek-v4-flash observé le 7 septembre 2026 | Service distant ; aucun octroi de licence de poids déduit du contrat technique. [Documentation officielle](https://api-docs.deepseek.com/), [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion). Conditions contractuelles du compte non auditées ici. | Trois requêtes synthétiques autorisées, aucun poids distribué/téléchargé. Alias annoncé Flash-0731 ; révision effective des poids distants non vérifiable. |
