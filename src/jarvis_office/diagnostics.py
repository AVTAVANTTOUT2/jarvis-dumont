"""Bounded file inspection. PASS describes structure, never model inference."""

import json
import platform
import stat
import struct
import sys
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

from jarvis_office.config import Config

Status = Literal["PASS", "FAIL", "NOT_RUN", "BLOCKED_USER"]
MAX_JSON_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    reason: str


class InvalidAsset(Exception):
    pass


def regular_file(path: Path) -> int:
    """Check before opening so FIFOs/devices cannot block a diagnostic."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise InvalidAsset
    with path.open("rb") as handle:
        if handle.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise InvalidAsset
    return info.st_size


def json_object(path: Path) -> dict[str, object]:
    if regular_file(path) > MAX_JSON_BYTES:
        raise InvalidAsset
    with path.open("rb") as handle:
        value = json.loads(handle.read(MAX_JSON_BYTES + 1))
    if not isinstance(value, dict) or not value:
        raise InvalidAsset
    return value


def safetensors_file(path: Path) -> None:
    size = regular_file(path)
    with path.open("rb") as handle:
        header_size = struct.unpack("<Q", handle.read(8))[0]
        if not 2 <= header_size <= MAX_JSON_BYTES or 8 + header_size >= size:
            raise InvalidAsset
        header = json.loads(handle.read(header_size))
    if not isinstance(header, dict):
        raise InvalidAsset
    offsets = []
    for key, tensor in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(tensor, dict):
            raise InvalidAsset
        pair = tensor.get("data_offsets")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(type(n) is not int for n in pair)
            or not 0 <= pair[0] <= pair[1] <= size - 8 - header_size
        ):
            raise InvalidAsset
        offsets.append(pair)
    if not offsets or max(end for _, end in offsets) != size - 8 - header_size:
        raise InvalidAsset


def model_directory(path: Path) -> None:
    if not path.is_dir():
        raise InvalidAsset
    # Only this model directory; never recursively search a user's home/cache.
    # ponytail: bounded to 4096 entries; use a trusted manifest for larger bundles.
    pending = [path]
    count = 0
    while pending:
        for entry in pending.pop().iterdir():
            count += 1
            if count > 4096:
                raise InvalidAsset
            if entry.is_symlink():
                if not entry.exists() or not entry.is_file():
                    raise InvalidAsset
            elif entry.is_dir():
                pending.append(entry)
            if entry.name.endswith((".incomplete", ".part", ".download", ".tmp")):
                raise InvalidAsset


def inspect_tts(path: Path) -> None:
    model_directory(path)
    for name in (
        "config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "vocab.json",
        "preprocessor_config.json",
        "speech_tokenizer/config.json",
    ):
        json_object(path / name)
    regular_file(path / "merges.txt")
    weights = json_object(path / "model.safetensors.index.json").get("weight_map")
    if not isinstance(weights, dict) or not weights:
        raise InvalidAsset
    names: set[str] = set()
    for value in weights.values():
        if (
            not isinstance(value, str)
            or Path(value).name != value
            or not value.endswith(".safetensors")
        ):
            raise InvalidAsset
        names.add(value)
    for name in sorted(names):
        safetensors_file(path / name)
    safetensors_file(path / "speech_tokenizer/model.safetensors")


def inspect_stt(path: Path) -> None:
    model_directory(path)
    for name in ("config.json", "tokenizer.json"):
        json_object(path / name)
    if regular_file(path / "model.bin") < 16:
        raise InvalidAsset
    if (path / "vocabulary.json").exists():
        regular_file(path / "vocabulary.json")
    else:
        regular_file(path / "vocabulary.txt")


def inspect_voice(path: Path) -> None:
    model_directory(path)
    audio = path / "reference.wav"
    size = regular_file(audio)
    with wave.open(str(audio), "rb") as wav:
        if (
            wav.getnchannels() < 1
            or wav.getframerate() < 1
            or wav.getnframes() < 1
            or wav.getnframes() * wav.getnchannels() * wav.getsampwidth() > size
        ):
            raise InvalidAsset
        expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
        actual = 0
        while chunk := wav.readframes(4096):
            actual += len(chunk)
        if actual != expected:
            raise InvalidAsset
    text = path / "transcript.txt"
    if regular_file(text) > 65_536:
        raise InvalidAsset
    with text.open("rb") as handle:
        if not handle.read(65_537).decode("utf-8").strip():
            raise InvalidAsset
    json_object(path / "metadata.json")


def inspect_vad(path: Path) -> None:
    if path.suffix not in {".onnx", ".jit"} or regular_file(path) < 16:
        raise InvalidAsset


def asset_check(name: str, path: Path | None, inspect: Callable[[Path], None]) -> Check:
    if path is None:
        return Check(name, "BLOCKED_USER", "asset_not_configured")
    try:
        inspect(path)
    except PermissionError:
        return Check(name, "BLOCKED_USER", "asset_permission_denied")
    except FileNotFoundError:
        return Check(name, "FAIL", "asset_missing_or_broken_link")
    except (
        InvalidAsset,
        OSError,
        ValueError,
        RuntimeError,
        EOFError,
        RecursionError,
        struct.error,
        wave.Error,
    ):
        return Check(name, "FAIL", "asset_invalid_or_incomplete")
    return Check(name, "PASS", "local_structure_checked_inference_not_run")


def asset_checks(config: Config) -> list[Check]:
    assets = config.assets
    return [
        asset_check("tts_model", assets.tts_model, inspect_tts),
        asset_check("stt_model", assets.stt_model, inspect_stt),
        asset_check("voice_profile", assets.voice_profile, inspect_voice),
        asset_check("vad_model", assets.vad_model, inspect_vad),
        Check("voice_rights", "NOT_RUN", "metadata_does_not_establish_consent"),
        Check("inference", "NOT_RUN", "not_run_by_read_only_diagnostic"),
    ]


def doctor_checks(config: Config) -> list[Check]:
    checks = [
        Check("python", "PASS" if sys.version_info[:2] == (3, 12) else "FAIL", "requires_3_12"),
        Check(
            "platform",
            "PASS" if (platform.system(), platform.machine()) == ("Darwin", "arm64") else "FAIL",
            "target_macos_arm64",
        ),
    ]
    for name in ("mlx", "mlx-audio", "faster-whisper", "silero-vad", "sounddevice"):
        try:
            # Read installed distribution metadata; do not import the library.
            metadata.distribution(name)
            checks.append(Check(name, "PASS", "distribution_present_import_not_run"))
        except metadata.PackageNotFoundError:
            checks.append(
                Check(name, "NOT_RUN", "optional_distribution_absent_in_this_environment")
            )
        except (OSError, ValueError):
            checks.append(Check(name, "FAIL", "distribution_metadata_unreadable"))
    return (
        checks
        + asset_checks(config)
        + [
            Check(name, "NOT_RUN", "not_run_by_read_only_diagnostic")
            for name in ("microphone", "playback", "deepseek_key", "network")
        ]
    )


def report(command: str, checks: list[Check], *, config_error: bool = False) -> dict[str, object]:
    statuses = {check.status for check in checks}
    status: Status = (
        "FAIL" if "FAIL" in statuses else "BLOCKED_USER" if "BLOCKED_USER" in statuses else "PASS"
    )
    code = 2 if config_error else 1 if status == "FAIL" else 3 if status == "BLOCKED_USER" else 0
    return {
        "schema_version": 1,
        "command": command,
        "status": status,
        "exit_code": code,
        "checks": [asdict(check) for check in checks],
    }
