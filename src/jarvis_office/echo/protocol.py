"""Protocol 1: independent control envelopes and 48-byte binary PCM headers."""

import json
import struct
from dataclasses import dataclass
from typing import Any

VERSION = 1
HEADER = struct.Struct("!4sBBHQIQQIBBHI")
MAX_PAYLOAD = 3840
FRAME_MS = 20


@dataclass(frozen=True)
class Packet:
    stream: int
    sequence: int
    captured_ns: int
    enqueued_ns: int
    rate: int
    channels: int
    pcm: bytes
    flags: int = 0

    def validate(self) -> None:
        if not (0 < self.stream < 2**63 and 0 <= self.sequence < 2**32):
            raise ValueError("AUDIO_HEADER_INVALID")
        if not (0 <= self.captured_ns <= self.enqueued_ns < 2**63):
            raise ValueError("AUDIO_TIMESTAMP_INVALID")
        if self.rate not in (16000, 48000) or self.channels not in (1, 2):
            raise ValueError("AUDIO_FORMAT_UNSUPPORTED")
        if self.flags not in (0, 1) or not 0 < len(self.pcm) <= MAX_PAYLOAD:
            raise ValueError("AUDIO_LENGTH_INVALID")
        if (
            len(self.pcm) % (2 * self.channels)
            or len(self.pcm) > self.rate * self.channels * 2 // 50
        ):
            raise ValueError("AUDIO_LENGTH_INVALID")

    def encode(self) -> bytes:
        self.validate()
        return (
            HEADER.pack(
                b"ECHO",
                VERSION,
                self.flags,
                HEADER.size,
                self.stream,
                self.sequence,
                self.captured_ns,
                self.enqueued_ns,
                self.rate,
                self.channels,
                1,
                0,
                len(self.pcm),
            )
            + self.pcm
        )

    @classmethod
    def decode(cls, data: bytes) -> "Packet":
        if not HEADER.size < len(data) <= HEADER.size + MAX_PAYLOAD:
            raise ValueError("AUDIO_LENGTH_INVALID")
        magic, version, flags, size, stream, seq, cap, enq, rate, channels, fmt, reserved, n = (
            HEADER.unpack_from(data)
        )
        if (magic, version, size, fmt, reserved) != (b"ECHO", VERSION, HEADER.size, 1, 0):
            raise ValueError("AUDIO_HEADER_INVALID")
        if n != len(data) - HEADER.size:
            raise ValueError("AUDIO_LENGTH_INVALID")
        result = cls(stream, seq, cap, enq, rate, channels, data[HEADER.size :], flags)
        result.validate()
        return result


class Sequence:
    def __init__(self) -> None:
        self.next = 0

    def accept(self, value: object) -> None:
        if type(value) is not int or value != self.next:
            raise ValueError("SEQUENCE_GAP")
        self.next += 1


def control(data: str | bytes, session: str, sequence: Sequence) -> dict[str, Any]:
    if not isinstance(data, str) or len(data) > 8192:
        raise ValueError("CONTROL_INVALID")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("protocol") != VERSION:
        raise ValueError("PROTOCOL_INCOMPATIBLE")
    if (
        not isinstance(value.get("type"), str)
        or not isinstance(value.get("payload"), dict)
        or type(value.get("timestamp")) is not int
        or value["timestamp"] < 0
        or value.get("session_id") != session
    ):
        raise ValueError("CONTROL_INVALID")
    sequence.accept(value.get("sequence"))
    return value
