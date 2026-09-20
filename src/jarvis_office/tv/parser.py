"""Bounded local TV command grammar. Deterministic, no network or devices."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jarvis_office.tv.protocol import QUERY_MAX, YOUTUBE_RE

MAX_UTTERANCE = 500

YOUTUBE_IN_TEXT = re.compile(
    r"(?:youtu\.be/|v=|(?:video|youtube)\s+)([A-Za-z0-9_-]{11})\b",
    re.IGNORECASE,
)

WORDS = {
    "une": 1,
    "un": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
}

_APOSTROPHES = str.maketrans(
    {
        "\u2019": "'",
        "\u2018": "'",
        "\u02bc": "'",
        "`": "'",
    }
)


class DecisionKind(StrEnum):
    MATCH = "match"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class LocalDecision:
    kind: DecisionKind
    reason: str
    parsed: dict[str, Any] | None = None


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()


def named_app(question: str) -> str | None:
    text = fold(question)
    if "smarttube" in text or "youtube" in text:
        return "smarttube"
    if re.search(r"\bavt\b", text):
        return "avt"
    return None


def default_app(hub: Any, question: str) -> str:
    named = named_app(question)
    if named:
        return named
    text = fold(question)
    defaults = hub.defaults()
    film = defaults.get("default_film_app")
    video = defaults.get("default_video_app")
    if re.search(r"\b(film|serie|episode)\b", text):
        return film if film in {"smarttube", "avt"} else "avt"
    return video if video in {"smarttube", "avt"} else "smarttube"


def normalize(question: str) -> str:
    text = question.translate(_APOSTROPHES)
    text = fold(text)
    text = re.sub(r"(?<=[a-z0-9])[-‐‑‒–—](?=[a-z])", " ", text)
    text = re.sub(r"\bn'", "n' ", text)
    text = re.sub(r"[!?.,;:…«»\"“”()[\]]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match(reason: str, parsed: dict[str, Any]) -> LocalDecision:
    return LocalDecision(DecisionKind.MATCH, reason, parsed)


def _reject(reason: str) -> LocalDecision:
    return LocalDecision(DecisionKind.REJECT, reason)


def _ambiguous(reason: str) -> LocalDecision:
    return LocalDecision(DecisionKind.AMBIGUOUS, reason)


def _no_match() -> LocalDecision:
    return LocalDecision(DecisionKind.NO_MATCH, "NO_MATCH")


def _command(
    action: str, args: dict[str, Any] | None = None, app: str | None = None
) -> dict[str, Any]:
    return {"kind": "command", "app": app, "action": action, "args": args or {}}


_POLITE_PREFIX = re.compile(
    r"^(?:s'il te plait|s'il vous plait|peux tu|tu peux|"
    r"est ce que (?:tu peux|vous pouvez)|merci de)\s+"
)
_POLITE_SUFFIX = re.compile(r"\s+(?:s'il te plait|s'il vous plait|merci)$")
_QUERY_POLITE = re.compile(r"\s+(?:s'il te plait|s'il vous plait|merci)$")


def strip_polite(text: str) -> str:
    current = text
    previous = None
    while previous != current:
        previous = current
        current = _POLITE_PREFIX.sub("", current)
        current = _POLITE_SUFFIX.sub("", current)
    return current.strip()


_PAUSE = re.compile(r"^(?:mets? en )?pause$")
_RESUME = re.compile(r"^(?:reprends?|reprendre|continue|continuer)$")
_STOP = re.compile(r"^(?:arrete|arreter|stop)$")
_NEXT = re.compile(r"^(?:(?:passe (?:au|a la) |(?:morceau|titre|chanson|piste) )?suivant[e]?)$")
_DESCRIPTIVE = re.compile(
    r"(?:"
    r"\b(?:je|tu|il|elle|on|nous|vous|ils|elles) "
    r"(?:fais|fait|font|faisais|faisait|faisaient) (?:une )?pause\b|"
    r"\b(?:a|ont|avait|avais) dit\b|"
    r"\bsemaine (?:suivante|prochaine)\b|"
    r"\b(?:l'|le |la )?(?:annee|mois|jour) (?:suivant[e]?|prochain[e]?)\b|"
    r"\bmets (?:la table|le couvert|la nappe)\b|"
    r"\barrete de \w+|"
    r"\bj'ai (?:vu|regarde)\b"
    r")"
)
_VERB_RE = re.compile(
    r"\b(mettre|mettez|mettes|mette|mets|met|lancer|lances|lance|"
    r"joues|joue|recherche|cherches|cherche|trouver|trouve|"
    r"regarder|regarde|montrer|montre|ouvrir|ouvre|"
    r"reprendre|reprends|reprend|continuer|continue|arreter|arrete|stop|pause|"
    r"passes|passe|avancer|avance|reculer|recule)\b"
)
_SPLIT = re.compile(r"\b(?:puis|et puis|et ensuite)\b")
_SEEK = re.compile(
    r"^(avance|recule)(?:r)?(?: de)? (\d+|une|un|deux|trois|quatre|cinq) "
    r"(seconde|minute)s?$"
)
_SEEK_INVALID = re.compile(r"\b(avance|recule)(?:r)? de -")
_SEEK_INCOMPLETE = re.compile(r"^(avance|recule)(?:r)? de(?: \d+| une| un| deux| trois)?$")
_PLAYLIST = re.compile(
    r"^(?:(?:mets?|lance|joue|mettre) )?(?:moi )?(?:la |le )?"
    r"(?:(?P<pre>premier|premiere|deuxieme|second|seconde|troisieme) )?"
    r"playlists?e?"
    r"(?: (?:numero|n))?"
    r"(?: (?P<num>0|[1-9]\d*|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix))?"
    r"$"
)
_PLAYLIST_PRE = {
    "premier": 0,
    "premiere": 0,
    "deuxieme": 1,
    "second": 1,
    "seconde": 1,
    "troisieme": 2,
}
_PICK = re.compile(
    r"^(?:(?:le|la|l'|choisis|prends|numero) )?"
    r"(?P<ord>premier|premiere|1er|1ere|deuxieme|second|seconde|2e|2eme|"
    r"troisieme|3e|3eme|1|2|3)$"
)
_PICK_INDEX = {
    "premier": 0,
    "premiere": 0,
    "1er": 0,
    "1ere": 0,
    "1": 0,
    "deuxieme": 1,
    "second": 1,
    "seconde": 1,
    "2e": 1,
    "2eme": 1,
    "2": 1,
    "troisieme": 2,
    "3e": 2,
    "3eme": 2,
    "3": 2,
}
_SEARCH_RE = re.compile(
    r"\b(lance|lancer|joue|cherche|recherche|trouve|trouver|regarde|regarder|"
    r"montre|montrer|ouvre|ouvrir|mets?|mette|mettez|mettre)\b"
    r"(?:\s+(?:moi|donc))?\s+(?:le|la|l'|les)?\s*(.+)$"
)
_ALWAYS_SEARCH = ("lance", "lancer", "joue", "cherche", "recherche")
_MEDIA_DEST = re.compile(r"\b(tv|tele|film|serie|video|youtube|smarttube|avt|musique)\b")
_DEST_TAIL = re.compile(
    r"\b(sur|dans|a)\s+(?:(?:la|le|ma|mon)\s+)?"
    r"(smarttube|youtube|avt|tele|tv)\b.*$"
)


def _is_negated(text: str, start: int, end: int) -> bool:
    before = text[:start]
    after = text[end:]
    if re.search(r"(?:\bne|n')\s*$", before) and re.match(r"\s*pas\b", after):
        return True
    if re.match(r"\s*pas\b", after):
        return True
    if re.search(r"\b(?:je )?(?:ne )?veux pas\b", before):
        return True
    return bool(re.search(r"\bne\b", before) and re.match(r"\s*pas\b", after))


def _parse_playlist(core: str) -> LocalDecision | None:
    found = _PLAYLIST.fullmatch(core)
    if found is None:
        return None
    pre = found.group("pre")
    raw = found.group("num")
    if pre and raw:
        left = _PLAYLIST_PRE[pre]
        right = _playlist_number(raw)
        if right is None:
            return _reject("REJECT_PLAYLIST_INDEX")
        if left != right:
            return _ambiguous("AMBIGUOUS_PLAYLIST_INDEX")
        return _match("MATCH_PLAYLIST", _command("playlist_play", {"index": left}, "smarttube"))
    if pre:
        return _match(
            "MATCH_PLAYLIST",
            _command("playlist_play", {"index": _PLAYLIST_PRE[pre]}, "smarttube"),
        )
    if raw is None:
        return _ambiguous("AMBIGUOUS_PLAYLIST_INDEX")
    index = _playlist_number(raw)
    if index is None:
        return _reject("REJECT_PLAYLIST_INDEX")
    return _match("MATCH_PLAYLIST", _command("playlist_play", {"index": index}, "smarttube"))


def _playlist_number(raw: str) -> int | None:
    if raw.isdigit():
        value = int(raw)
        if value <= 0:
            return None
        return value - 1
    if raw in _PLAYLIST_PRE:
        return _PLAYLIST_PRE[raw]
    if raw in WORDS:
        return WORDS[raw] - 1
    return None


def _parse_seek(core: str, text: str) -> LocalDecision | None:
    if _SEEK_INVALID.search(core) or _SEEK_INVALID.search(text):
        return _reject("REJECT_INVALID_SEEK")
    if _SEEK_INCOMPLETE.fullmatch(core):
        return _ambiguous("AMBIGUOUS_SEEK")
    found = _SEEK.fullmatch(core)
    if found is None:
        return None
    raw_amount = found.group(2)
    if raw_amount.isdigit():
        amount = int(raw_amount)
    elif raw_amount in WORDS:
        amount = WORDS[raw_amount]
    else:
        return _reject("REJECT_INVALID_SEEK")
    unit = 1000 if found.group(3).startswith("second") else 60_000
    delta = amount * unit
    if found.group(1) == "recule":
        delta = -delta
    return _match("MATCH_SEEK", _command("seek", {"delta_ms": delta}))


def _parse_pick(core: str, hub: Any) -> LocalDecision | None:
    found = _PICK.fullmatch(core)
    if found is None:
        return None
    index = _PICK_INDEX[found.group("ord")]
    items = hub.active_candidates(None)
    if not items:
        return _ambiguous("AMBIGUOUS_NO_CANDIDATES")
    if index >= len(items):
        return _ambiguous("AMBIGUOUS_OUT_OF_RANGE")
    return _match("MATCH_PICK", {"kind": "pick", "index": index})


def _parse_search(text: str, question: str, hub: Any) -> dict[str, Any] | None:
    found = _SEARCH_RE.search(text)
    if found is None:
        return None
    verb = found.group(1)
    query = found.group(2).strip()
    query = _DEST_TAIL.sub("", query)
    query = _QUERY_POLITE.sub("", query).strip()
    if not query:
        return None
    media_ctx = named_app(question) or _MEDIA_DEST.search(text)
    if not (verb.startswith(_ALWAYS_SEARCH) or media_ctx):
        return None
    return _command("search", {"query": query, "limit": 5}, default_app(hub, question))


def _is_transport_core(fragment: str) -> bool:
    core = strip_polite(fragment)
    if not core:
        return False
    if any(pattern.fullmatch(core) for pattern in (_PAUSE, _RESUME, _STOP, _NEXT)):
        return True
    if _parse_playlist(core) is not None:
        return True
    return _parse_seek(core, core) is not None


def _is_multi(text: str) -> bool:
    if _SPLIT.search(text) is None:
        return False
    parts = _SPLIT.split(text)
    if len(parts) < 2:
        return False
    return _is_transport_core(parts[0]) and _is_transport_core(parts[1])


def _youtube_id(question: str, text: str) -> str | None:
    match = YOUTUBE_IN_TEXT.search(question)
    if match is None:
        return None
    identifier = match.group(1)
    if YOUTUBE_RE.fullmatch(identifier) is None:
        return None
    if "youtu" not in text and "video" not in text:
        return None
    return identifier


def decide_local(question: str, hub: Any) -> LocalDecision:
    if not question or not question.strip():
        return _reject("REJECT_EMPTY")
    if len(question) > MAX_UTTERANCE:
        return _reject("REJECT_TOO_LONG")
    text = normalize(question)
    if not text:
        return _reject("REJECT_EMPTY")
    core = strip_polite(text)
    first = _VERB_RE.search(text)
    if first is not None and _is_negated(text, first.start(), first.end()):
        return _reject("REJECT_NEGATION")
    search = _parse_search(text, question, hub)
    if search is None and _DESCRIPTIVE.search(text):
        return _reject("REJECT_NOT_COMMAND")
    if search is None and _is_multi(text):
        return _ambiguous("AMBIGUOUS_MULTI_ACTION")
    playlist = _parse_playlist(core)
    if playlist is not None:
        return playlist
    seek = _parse_seek(core, text)
    if seek is not None:
        return seek
    if _PAUSE.fullmatch(core):
        return _match("MATCH_TRANSPORT", _command("pause"))
    if _RESUME.fullmatch(core):
        return _match("MATCH_TRANSPORT", _command("resume"))
    if _STOP.fullmatch(core):
        return _match("MATCH_TRANSPORT", _command("stop"))
    if _NEXT.fullmatch(core):
        return _match("MATCH_PLAYLIST_NEXT", _command("playlist_next", app="smarttube"))
    pick = _parse_pick(core, hub)
    if pick is not None:
        return pick
    identifier = _youtube_id(question, text)
    if identifier is not None:
        return _match(
            "MATCH_YOUTUBE",
            _command(
                "play_content",
                {"content": {"kind": "youtube_video", "id": identifier}},
                "smarttube",
            ),
        )
    if search is not None:
        query = str(search["args"]["query"])
        if len(query) > QUERY_MAX:
            return _reject("REJECT_TOO_LONG")
        return _match("MATCH_SEARCH", search)
    return _no_match()
