"""Explicit finite capture through sounddevice only; callback never runs inference."""

import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jarvis_office.assets import private_root
from jarvis_office.audio_input import AudioError, Normalizer, Samples, Segmenter, Silero
from jarvis_office.config import Speech

PREFLIGHT_SWIFT = """
import AVFoundation
import CoreAudio
import Foundation
var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var size: UInt32 = 0
var errors = 0
if AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
    &address, 0, nil, &size) != 0 { errors += 1 }
var objects = [AudioObjectID](repeating: 0, count: Int(size)/4)
if !objects.isEmpty && AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
    &address, 0, nil, &size, &objects) != 0 { errors += 1 }
var active = 0
for object in objects {
    var running: UInt32 = 0
    var count: UInt32 = 4
    address.mSelector = kAudioProcessPropertyIsRunningInput
    if AudioObjectGetPropertyData(object, &address, 0, nil, &count, &running) != 0 { errors += 1 }
    if running != 0 { active += 1 }
}
let permission = AVCaptureDevice.authorizationStatus(for: .audio).rawValue
let result: [String: Int] = ["authorization": permission,
    "active_inputs": active, "query_errors": errors]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
"""


def microphone_preflight() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "BLOCKED_USER", "reason": "microphone_requires_macos_validation"}
    cache = private_root() / "cache/swift"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = subprocess.run(
            ["/usr/bin/swift", "-module-cache-path", str(cache), "-e", PREFLIGHT_SWIFT],
            capture_output=True,
            timeout=30,
            check=False,
        )
        native = json.loads(result.stdout) if result.returncode == 0 else {}
        if native.get("query_errors") != 0:
            return {"status": "BLOCKED_USER", "reason": "microphone_conflict_query_unavailable"}
        authorization = native.get("authorization")
        if authorization != 3:
            return {
                "status": "BLOCKED_USER",
                "reason": "microphone_permission_required",
                "microphone_state": {
                    0: "NOT_DETERMINED",
                    1: "RESTRICTED",
                    2: "PERMISSION_DENIED",
                }.get(authorization if isinstance(authorization, int) else -1, "UNKNOWN"),
            }
        if native["active_inputs"]:
            return {"status": "BLOCKED_USER", "reason": "another_audio_input_active"}
        for label in (
            "com.jarvis.ingestion",
            "com.jarvis.macos-bridge",
            "com.jarvis.supervisor",
            "com.jarvis.t710-tunnel",
        ):
            service = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if service.returncode != 113:
                return {"status": "BLOCKED_USER", "reason": "v1_service_conflict_or_unknown"}
        return {
            "status": "PASS",
            "reason": "permission_granted_no_input_active_at_check",
            "authorization": "GRANTED",
            "conflict_scope": "CoreAudio input activity and targeted V1 services; instant snapshot",
            "process_command_scan": "BLOCKED_USER_in_runtime_sandbox",
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "BLOCKED_USER", "reason": "microphone_preflight_unavailable"}


def list_devices(sd: Any, direction: str) -> dict[str, Any]:
    """Query PortAudio formats only; availability is not proof of an opened stream."""
    if direction not in {"input", "output"}:
        raise AudioError("invalid_device_direction")
    devices = []
    for index, item in enumerate(sd.query_devices()):
        channels = int(item["max_" + direction + "_channels"])
        if not channels:
            continue
        formats = []
        for rate in (16000, 24000, 44100, 48000):
            for count in range(1, min(channels, 2) + 1):
                for dtype in ("int16", "float32"):
                    try:
                        getattr(sd, "check_" + direction + "_settings")(
                            device=index, channels=count, samplerate=rate, dtype=dtype
                        )
                        formats.append({"rate": rate, "channels": count, "dtype": dtype})
                    except sd.PortAudioError:
                        pass
        devices.append(
            {
                "name": item["name"],
                "max_channels": channels,
                "default_rate": item["default_samplerate"],
                "formats": formats,
                "availability": "FORMAT_SUPPORTED" if formats else "NO_TESTED_FORMAT",
                "stream": "NOT_RUN",
            }
        )
    return {"status": "PASS", "exit_code": 0, "direction": direction, "devices": devices}


def resolve_input(sd: Any, name: str, rate: int) -> tuple[int, dict[str, Any]]:
    if not name:
        raise AudioError("explicit_input_device_required")
    matches = [
        (index, item)
        for index, item in enumerate(sd.query_devices())
        if item["name"] == name and item["max_input_channels"] >= 1
    ]
    if len(matches) != 1:
        raise AudioError("input_device_missing_or_ambiguous")
    index, item = matches[0]
    rates = []
    for candidate in (16000, 44100, 48000):
        try:
            sd.check_input_settings(device=index, channels=1, dtype="float32", samplerate=candidate)
            rates.append(candidate)
        except sd.PortAudioError:
            pass
    if rate not in rates:
        raise AudioError("input_rate_unsupported")
    return index, {
        "name": item["name"],
        "hostapi": sd.query_hostapis(item["hostapi"])["name"],
        "supported_rates": rates,
        "sample_rate": rate,
        "channels": 1,
        "identity": "exact_unique_name_rechecked_not_persisted_index",
    }


@dataclass
class Block:
    sequence: int
    start_sample: int
    adc_time: float
    received_wall: float
    data: Samples
    callback_current_time: float | None = None


class CaptureQueue:
    def __init__(self, capacity: int) -> None:
        self.blocks: queue.Queue[Block] = queue.Queue(maxsize=capacity)
        self.sequence = self.samples = self.dropped = 0
        self.lost = False

    def callback(self, data: Samples, frames: int, times: Any, status: Any) -> None:
        # No inference/network/disk. Queue and one owned copy are strictly bounded.
        self.sequence += 1
        start = self.samples
        self.samples += frames
        if status or self.blocks.full():
            self.dropped += 1
            self.lost = True
            return
        try:
            self.blocks.put_nowait(
                Block(
                    self.sequence,
                    start,
                    float(times.inputBufferAdcTime),
                    time.perf_counter(),
                    data.copy(),
                    float(times.currentTime) if hasattr(times, "currentTime") else None,
                )
            )
        except queue.Full:
            self.dropped += 1
            self.lost = True

    def get(self, timeout: float = 2) -> Block:
        if self.lost:
            raise AudioError("capture_discontinuity")
        try:
            item = self.blocks.get(timeout=timeout)
        except queue.Empty:
            raise AudioError("capture_disconnected") from None
        if self.lost:
            raise AudioError("capture_discontinuity")
        return item


def capture_wav(settings: Speech, vad: Silero, output: Path, seconds: float) -> dict[str, Any]:
    import sounddevice as sd

    if (
        not 0.25 <= seconds <= 30
        or output.suffix.lower() != ".wav"
        or output.exists()
        or output.is_symlink()
    ):
        raise AudioError("capture_requires_new_wav_and_bounded_duration")
    transitions = ["NOT_STARTED"]
    reconnects = 0
    dropped = 0
    while True:
        check = microphone_preflight()
        if check["status"] != "PASS":
            return {
                **check,
                "exit_code": 3,
                "microphone_state": check.get("microphone_state", "BLOCKED_USER"),
            }
        index, device = resolve_input(sd, settings.input_device, settings.input_rate)
        channel = CaptureQueue(settings.queue_blocks)
        normalizer = Normalizer(settings.input_rate, 1)
        segmenter = Segmenter(settings, realtime=True)
        vad.reset()
        blocks: list[Samples] = []
        segments = []
        adc_first = adc_previous = None
        first_received_wall = None
        expected = sequence = 0
        queue_delays = []
        delay_max = 0.0
        target = round(seconds * settings.input_rate)
        started = time.perf_counter()
        try:
            transitions.append("READY")
            with sd.InputStream(
                device=index,
                samplerate=settings.input_rate,
                channels=1,
                dtype="float32",
                blocksize=round(settings.input_rate * 0.02),
                callback=channel.callback,
                extra_settings=sd.CoreAudioSettings(change_device_parameters=False)
                if sys.platform == "darwin" and hasattr(sd, "CoreAudioSettings")
                else None,
            ):
                transitions.append("CAPTURING")
                while expected < target:
                    if time.perf_counter() - started > seconds + 3:
                        raise AudioError("capture_disconnected")
                    block = channel.get()
                    if block.sequence != sequence + 1 or block.start_sample != expected:
                        raise AudioError("capture_discontinuity")
                    if adc_previous is not None and abs(block.adc_time - adc_previous) > 0.01:
                        raise AudioError("capture_timestamp_discontinuity")
                    sequence = block.sequence
                    adc_previous = block.adc_time + len(block.data) / settings.input_rate
                    if adc_first is None:
                        adc_first = block.adc_time
                        first_received_wall = block.received_wall
                    queue_delays.append(time.perf_counter() - block.received_wall)
                    data = block.data[: target - expected]
                    expected += len(data)
                    pcm = normalizer.feed(data)
                    delay_max = max(delay_max, normalizer.delay_samples)
                    blocks.append(pcm)
                    segments.extend(segmenter.feed(pcm, vad.score))
            pcm = normalizer.feed(np.empty((0, 1), np.float32), last=True)
            blocks.append(pcm)
            segments.extend(segmenter.feed(pcm, vad.score))
            segments.extend(segmenter.finish(vad.score))
            if channel.lost:
                raise AudioError("capture_discontinuity")
            audio = np.concatenate(blocks)
            break
        except (AudioError, sd.PortAudioError) as exc:
            if isinstance(exc, sd.PortAudioError):
                permission = microphone_preflight()
                if permission.get("reason") == "microphone_permission_required":
                    return {**permission, "exit_code": 3}
            dropped += channel.dropped
            reason = (
                str(exc) if isinstance(exc, AudioError) else "capture_device_or_permission_error"
            )
            transitions.append("DISCONTINUITY" if "discontinuity" in reason else "DISCONNECTED")
            # Entire failed attempt discarded. No samples from separate sessions join.
            if reconnects >= settings.reconnect_attempts or not isinstance(exc, AudioError):
                return {
                    "status": "FAIL",
                    "exit_code": 1,
                    "reason": reason,
                    "microphone_state": transitions[-1],
                    "reconnects": reconnects,
                    "dropped_blocks": dropped,
                }
            reconnects += 1
            time.sleep(0.1)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".capture-", dir=output.parent) as directory:
        staged = Path(directory) / "capture.wav"
        with wave.open(str(staged), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        staged.chmod(0o600)
        os.link(staged, output)
    transitions.extend(["SPEECH_DETECTED" if segments else "SILENCE_NO_SPEECH", "STOPPED"])
    return {
        "status": "PASS",
        "exit_code": 0,
        "device": device,
        "microphone_state": "STOPPED",
        "transitions": transitions,
        "duration_s": len(audio) / 16000,
        "input_samples": expected,
        "signal_peak": float(np.max(np.abs(audio))),
        "signal_rms": float(np.sqrt(np.mean(audio * audio))),
        "digital_signal": "NONZERO_SAMPLES" if np.any(audio) else "NO_SIGNAL",
        "first_adc_time": adc_first,
        "first_block_received_wall_s": first_received_wall,
        "wall_clock": "perf_counter; distinct from PortAudio ADC clock",
        "queue_delay_max_s": max(queue_delays, default=0),
        "resampler_pending_max_s": delay_max / 16000,
        "dropped_blocks": dropped,
        "reconnects": reconnects,
        "segments": [s.timing() for s in segments],
        "human_reference": "NOT_RUN",
        "speech_text_logged": False,
    }
