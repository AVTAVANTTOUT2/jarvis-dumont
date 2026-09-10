"""Capture sources for the existing audio worker; remote PCM stays binary and in RAM."""

import os
import queue
import struct
import threading
import time
import uuid
from typing import Any

from jarvis_office.config import Speech

PIPE_HEADER = struct.Struct("!16sdH")  # listen generation, server receipt, PCM byte count
REMOTE_RATE = 16000


class LocalMacIngress:
    def __init__(self, sd: Any) -> None:
        self.sd = sd

    def open(self, settings: Speech) -> tuple[Any, dict[str, Any], Any]:
        from jarvis_office.audio_input import AudioError
        from jarvis_office.capture import CaptureQueue, microphone_preflight, resolve_input

        check = microphone_preflight()
        if check["status"] != "PASS":
            raise AudioError(str(check["reason"]))
        index, device = resolve_input(self.sd, settings.input_device, settings.input_rate)
        channel = CaptureQueue(settings.queue_blocks)
        stream = self.sd.InputStream(
            device=index,
            samplerate=settings.input_rate,
            channels=1,
            dtype="float32",
            blocksize=round(settings.input_rate * 0.02),
            callback=channel.callback,
            extra_settings=self.sd.CoreAudioSettings(change_device_parameters=False),
        )
        return channel, device, stream


class PipeStream:
    """One listen generation. No data from an earlier generation can be replayed."""

    samplerate = REMOTE_RATE
    channels = 1

    def __init__(self) -> None:
        from jarvis_office.capture import CaptureQueue

        self.channel = CaptureQueue(10)
        self.token = uuid.uuid4().bytes
        self.active = False

    def start(self) -> None:
        self.active = True

    def abort(self, **_: Any) -> None:
        self.active = False
        while True:
            try:
                self.channel.blocks.get_nowait()
            except queue.Empty:
                break

    close = abort


class RemotePipeIngress:
    """Inherited anonymous pipe, read inside the same sandboxed STT worker."""

    def __init__(self, fd: int) -> None:
        self.reader = os.fdopen(fd, "rb", buffering=0)
        self.current: PipeStream | None = None
        self.closed = False
        threading.Thread(target=self._read, daemon=True, name="remote-pcm-ingress").start()

    def open(self, settings: Speech) -> tuple[Any, dict[str, Any], PipeStream]:
        from jarvis_office.audio_input import AudioError

        if self.closed or settings.input_rate != REMOTE_RATE:
            raise AudioError("remote_ingress_unavailable")
        if self.current is not None:
            self.current.abort()
        stream = self.current = PipeStream()
        return (
            stream.channel,
            {
                "name": "Remote audio",
                "sample_rate": REMOTE_RATE,
                "channels": 1,
                "ingress_token": stream.token.hex(),
                "clock_basis": "server_receive_estimate",
            },
            stream,
        )

    def _exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            data = self.reader.read(size - len(result))
            if not data:
                raise EOFError
            result.extend(data)
        return bytes(result)

    def _read(self) -> None:
        import numpy as np

        from jarvis_office.capture import Block

        try:
            while True:
                token, received, size = PIPE_HEADER.unpack(self._exact(PIPE_HEADER.size))
                if size != REMOTE_RATE * 2 // 50:
                    raise ValueError("remote_pcm_format")
                pcm = self._exact(size)
                stream = self.current
                if stream is None or not stream.active or stream.token != token:
                    continue
                channel = stream.channel
                if not 0 <= time.perf_counter() - received <= 0.2:
                    channel.lost = True
                    continue
                samples = (
                    np.frombuffer(pcm, dtype="<i2").astype(np.float32).reshape(-1, 1) / 32768.0
                )
                start = channel.samples
                channel.sequence += 1
                channel.samples += len(samples)
                try:
                    channel.blocks.put_nowait(
                        Block(
                            channel.sequence,
                            start,
                            start / REMOTE_RATE,
                            received,
                            samples,
                            start / REMOTE_RATE,
                        )
                    )
                except queue.Full:
                    channel.dropped += 1
                    channel.lost = True
        except (EOFError, OSError, ValueError):
            self.closed = True
            if self.current is not None:
                self.current.channel.lost = True
        finally:
            self.reader.close()
