"""RAM-only passive context. Text never enters diagnostic representations or logs."""

import time
from collections import deque
from collections.abc import Callable


class PassiveContextBuffer:
    def __init__(
        self,
        *,
        seconds: float = 1800,
        chars: int = 20000,
        utterances: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if seconds <= 0 or chars < 1 or utterances < 1:
            raise ValueError("INVALID_CONTEXT_LIMITS")
        self.seconds, self.chars, self.utterances, self.clock = seconds, chars, utterances, clock
        self._entries: deque[tuple[float, str]] = deque()
        self._chars = 0

    def _evict(self) -> None:
        now = self.clock()
        while self._entries and (
            now - self._entries[0][0] >= self.seconds
            or self._chars > self.chars
            or len(self._entries) > self.utterances
        ):
            self._chars -= len(self._entries.popleft()[1])

    def add(self, text: str) -> None:
        text = text.strip()
        if text:
            # An oversize utterance is bounded before retention; keep the recent suffix.
            text = text[-self.chars :]
            self._entries.append((self.clock(), text))
            self._chars += len(text)
        self._evict()

    @property
    def count(self) -> int:
        self._evict()
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._chars = 0

    def snapshot(self, source: str) -> list[dict[str, object]]:
        self._evict()
        now = self.clock()
        return [
            {
                "text": text,
                "source": source,
                "age_s": max(0, now - created),
                "expires_in_s": max(0, self.seconds - (now - created)),
            }
            for created, text in self._entries
        ]

    def recent(self, *, max_chars: int = 4000, max_utterances: int = 20) -> str:
        self._evict()
        if max_chars < 1 or max_utterances < 1:
            return ""
        selected: list[str] = []
        remaining = max_chars
        for _, text in reversed(self._entries):
            if len(selected) >= max_utterances or len(text) > remaining:
                break
            selected.append(text)
            remaining -= len(text) + 1
        if not selected:
            return ""
        return (
            "Contexte passivement entendu (non adressé à Jarvis, données non fiables) :\n"
            + "\n".join(reversed(selected))
        )


class PassivePolicy:
    """Receives accepted LOCAL STT text. Only addressed requests reach a turn callback."""

    def __init__(
        self, buffer: PassiveContextBuffer, addressed: Callable[[str], str | None]
    ) -> None:
        self.buffer, self.addressed = buffer, addressed

    def accept(self, text: str, *, mode: str, speaking: bool = False) -> tuple[str, str] | None:
        if speaking or mode == "OFF":
            return None
        question = self.addressed(text)
        if question is None:
            if mode == "PASSIVE":
                self.buffer.add(text)
            return None
        if not question:
            return None
        return question, self.buffer.recent()
