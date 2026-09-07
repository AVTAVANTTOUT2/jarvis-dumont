"""Bounded Markdown removal and conservative French streaming boundaries.

Independent implementation from requirements. The V1 segmenter/test source review is
blocked by tool scope; no V1 code or configuration is copied or imported.
"""

import re


class TextError(Exception):
    pass


class Markdown:
    def __init__(self) -> None:
        self.pending = ""
        self.code = False

    def feed(self, text: str, *, final: bool = False) -> str:
        self.pending += text
        output = []
        while self.pending:
            value = self.pending
            if self.code:
                end = value.find("```")
                if end < 0:
                    self.pending = "" if final else value[-2:]
                    break
                self.pending, self.code = value[end + 3 :], False
                output.append(" ")
                continue
            if value.startswith("```"):
                self.pending, self.code = value[3:], True
                continue
            if not final and (
                value in {"`", "``"}
                or any(
                    prefix.startswith(value.lower()) for prefix in ("http://", "https://", "www.")
                )
            ):
                break
            if re.match(r"(?:https?://|www\.)", value, re.I):
                url_end = re.search(r"\s", value)
                if url_end is None and not final:
                    break
                self.pending = value[url_end.start() :] if url_end else ""
                output.append(" ")
                continue
            if value.startswith("["):
                close = value.find("]")
                if close < 0 or (close == len(value) - 1 and not final):
                    if final:
                        self.pending = ""
                    break
                if value[close + 1 :].startswith("("):
                    end = value.find(")", close + 2)
                    if end < 0:
                        if final:
                            self.pending = ""
                        break
                    output.append(value[1:close])
                    self.pending = value[end + 1 :]
                    continue
            char = value[0]
            self.pending = value[1:]
            if char not in "`*_#~[]" and (char.isprintable() or char.isspace()):
                output.append(char)
        if len(self.pending) > 512:
            raise TextError("markdown_token_limit")
        return "".join(output)


ABBREVIATIONS = {
    "m",
    "mme",
    "mmes",
    "mlle",
    "dr",
    "pr",
    "st",
    "ste",
    "etc",
    "env",
    "ex",
    "cf",
    "av",
    "n°",
}


class Pronounce:
    def __init__(self) -> None:
        self.markdown = Markdown()
        self.pending = ""
        self.first = True
        self.finished = False

    def _word_cut(self, limit: int) -> int:
        prefix = self.pending[:limit]
        spaces = list(re.finditer(r"\s+", prefix))
        if not spaces:
            return 0
        cut = spaces[-1].end()
        # Keep a final complete number with its not-yet-complete following unit.
        match = re.search(r"(?:^|\s)\d+(?:[.,:]\d+)*\s*$", prefix[:cut])
        if match:
            cut = match.start()
        return cut

    def _take(self, *, timed: bool = False, final: bool = False) -> list[str]:
        result = []
        while self.pending:
            cut = 0
            for match in re.finditer(r"[.!?…;:]+[\"»”)]*(?=\s)", self.pending):
                token = re.search(r"([\w°]+)\.$", self.pending[: match.end()])
                if token and (token[1].casefold() in ABBREVIATIONS or len(token[1]) == 1):
                    continue
                if (
                    match[0].startswith(":")
                    and match.start()
                    and self.pending[match.start() - 1].isdigit()
                ):
                    continue
                cut = match.end()
                break
            if self.first and not cut:
                for match in re.finditer(r",(?=\s)", self.pending):
                    if match.start() >= 40 and len(self.pending[: match.start()].split()) >= 4:
                        cut = match.end()
                        break
            if cut > 256 or (not cut and len(self.pending) > 256):
                cut = self._word_cut(256)
            if not cut and timed:
                candidate = self._word_cut(len(self.pending))
                if candidate >= 40 and len(self.pending[:candidate].split()) >= 4:
                    cut = candidate
            if not cut and final:
                cut = len(self.pending)
            if not cut:
                break
            value = self.pending[:cut]
            self.pending = self.pending[cut:]
            value = re.sub(r"(?m)^\s*[-+]\s+", "", value)
            value = " ".join(value.split())
            if value:
                result.append(value)
                self.first = False
        if len(self.pending) > 1024:
            raise TextError("speech_buffer_limit")
        return result

    def feed(self, text: str) -> list[str]:
        if self.finished:
            raise TextError("text_after_finish")
        self.pending += self.markdown.feed(text)
        return self._take()

    def timed(self) -> list[str]:
        return [] if self.finished else self._take(timed=True)

    def finish(self) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        self.pending += self.markdown.feed("", final=True)
        return self._take(final=True)
