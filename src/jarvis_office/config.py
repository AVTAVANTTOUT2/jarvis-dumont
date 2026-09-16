"""Strict non-secret configuration; no dotenv or V1 imports."""

import math
import stat
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

MAX_CONFIG_BYTES = 65_536


class ConfigError(Exception):
    """Intentionally excludes user-controlled values and OS exception text."""

    def __init__(self, reason: str, *, blocked: bool = False) -> None:
        self.reason = reason
        self.blocked = blocked
        super().__init__(reason)


@dataclass(frozen=True)
class Assets:
    tts_model: Path | None = None
    stt_model: Path | None = None
    voice_profile: Path | None = None
    vad_model: Path | None = None


@dataclass(frozen=True)
class TTS:
    python: Path | None = None
    language: str = "french"
    clone_mode: str = "icl"
    temperature: float = 0.5
    top_p: float = 0.9
    top_k: int = 30
    streaming_interval: float = 0.4
    repetition_penalty: float = 1.05
    max_tokens: int = 4096
    startup_timeout: float = 180.0
    fragment_timeout: float = 60.0
    drain_timeout: float = 15.0


@dataclass(frozen=True)
class Speech:
    python: Path | None = None
    compute_type: str = "float32"
    cpu_threads: int = 4
    beam_size: int = 1
    input_device: str = ""
    input_rate: int = 48000
    vad_threshold: float = 0.5
    pre_roll_ms: int = 300
    terminal_silence_ms: int = 1500
    min_speech_ms: int = 96
    max_utterance_s: int = 30
    max_input_s: int = 60
    queue_blocks: int = 32
    reconnect_attempts: int = 2


@dataclass(frozen=True)
class Chat:
    model: str = "deepseek-flash"
    max_tokens: int = 256
    connect_timeout: float = 10.0
    first_content_timeout: float = 20.0
    idle_timeout: float = 10.0
    total_timeout: float = 60.0
    segment_timeout: float = 0.8
    input_chars: int = 4096
    output_chars: int = 4096
    history_turns: int = 4
    context_chars: int = 12000
    queue_events: int = 64


@dataclass(frozen=True)
class Voice:
    output_device: str = ""
    output_rate: int = 48000
    output_channels: int = 2
    port: int = 8768
    acoustic_delay: float = 0.35
    text_queue_chars: int = 2048
    pcm_seconds: float = 2.0
    prefill_seconds: float = 0.12
    arm_seconds: int = 60
    arm_turns: int = 3
    stt_timeout: float = 60.0


@dataclass(frozen=True)
class Config:
    assets: Assets = Assets()
    source_present: bool = False
    tts: TTS = TTS()
    speech: Speech = Speech()
    chat: Chat = Chat()
    voice: Voice = Voice()


def load_config(path: Path | None = None) -> Config:
    explicit = path is not None
    try:
        source = (
            (
                path
                if path is not None
                else Path.home() / "Library/Application Support/JarvisOffice/config.toml"
            )
            .expanduser()
            .absolute()
        )
        try:
            if not stat.S_ISREG(source.stat().st_mode):
                raise ConfigError("configuration_not_regular")
            with source.open("rb") as handle:
                raw = handle.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            if explicit or source.is_symlink():
                raise ConfigError("configuration_missing") from None
            return Config()
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError("configuration_too_large")
        data = tomllib.loads(raw.decode("utf-8"))
        if set(data) - {"assets", "tts", "speech", "chat", "voice"}:
            raise ConfigError("unknown_configuration_field")
        assets = data.get("assets", {})
        if not isinstance(assets, dict) or set(assets) - set(Assets.__dataclass_fields__):
            raise ConfigError("invalid_assets_configuration")
        paths: dict[str, Path | None] = {}
        for key, value in assets.items():
            if not isinstance(value, str) or "\x00" in value or "://" in value:
                raise ConfigError("invalid_local_path")
            if not value.strip():
                paths[key] = None
                continue
            target = Path(value).expanduser()
            paths[key] = target if target.is_absolute() else source.parent / target
        values = data.get("tts", {})
        if not isinstance(values, dict) or set(values) - set(TTS.__dataclass_fields__):
            raise ConfigError("invalid_tts_configuration")
        settings = TTS(**values)
        if settings.language != "french" or settings.clone_mode != "icl":
            raise ConfigError("tts_requires_french_icl")
        for value, lower, upper in (
            (settings.temperature, 0.01, 2.0),
            (settings.top_p, 0.01, 1.0),
            (settings.streaming_interval, 0.05, 5.0),
            (settings.repetition_penalty, 1.0, 3.0),
            (settings.startup_timeout, 0.1, 600.0),
            (settings.fragment_timeout, 0.1, 300.0),
            (settings.drain_timeout, 0.1, 60.0),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ConfigError("invalid_tts_parameter")
        if type(settings.top_k) is not int or not 1 <= settings.top_k <= 100:
            raise ConfigError("invalid_tts_parameter")
        if type(settings.max_tokens) is not int or not 1 <= settings.max_tokens <= 4096:
            raise ConfigError("invalid_tts_parameter")
        if settings.python is not None:
            if (
                not isinstance(settings.python, str)
                or not settings.python
                or "\x00" in settings.python
                or "://" in settings.python
            ):
                raise ConfigError("invalid_tts_python")
            python = Path(settings.python).expanduser()
            values["python"] = python if python.is_absolute() else source.parent / python
        speech_values = data.get("speech", {})
        if not isinstance(speech_values, dict) or set(speech_values) - set(
            Speech.__dataclass_fields__
        ):
            raise ConfigError("invalid_speech_configuration")
        speech = Speech(**speech_values)
        if speech.compute_type not in {"float32", "int8", "int8_float32"}:
            raise ConfigError("invalid_cpu_compute_type")
        if (
            not isinstance(speech.input_device, str)
            or len(speech.input_device) > 256
            or any(ord(c) < 32 for c in speech.input_device)
        ):
            raise ConfigError("invalid_input_device")
        if type(speech.vad_threshold) not in (int, float) or not 0 < speech.vad_threshold < 1:
            raise ConfigError("invalid_vad_threshold")
        for value, low, high in (
            (speech.cpu_threads, 1, 16),
            (speech.beam_size, 1, 5),
            (speech.pre_roll_ms, 0, 1000),
            (speech.terminal_silence_ms, 100, 2000),
            (speech.min_speech_ms, 32, 500),
            (speech.max_utterance_s, 1, 60),
            (speech.max_input_s, 1, 120),
            (speech.queue_blocks, 2, 128),
            (speech.reconnect_attempts, 0, 2),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ConfigError("invalid_speech_parameter")
        if type(speech.input_rate) is not int or speech.input_rate not in {16000, 44100, 48000}:
            raise ConfigError("invalid_input_rate")
        if speech.python is not None:
            value = speech.python
            if not isinstance(value, str) or not value or "\x00" in value or "://" in value:
                raise ConfigError("invalid_speech_python")
            target = Path(value).expanduser()
            speech_values["python"] = target if target.is_absolute() else source.parent / target
        chat_values = data.get("chat", {})
        if not isinstance(chat_values, dict) or set(chat_values) - set(Chat.__dataclass_fields__):
            raise ConfigError("invalid_chat_configuration")
        chat = Chat(**chat_values)
        if chat.model != "deepseek-flash":
            raise ConfigError("chat_requires_single_flash_model")
        for value, low, high in (
            (chat.max_tokens, 1, 256),
            (chat.input_chars, 128, 4096),
            (chat.output_chars, 256, 8192),
            (chat.history_turns, 0, 8),
            (chat.context_chars, 1024, 24000),
            (chat.queue_events, 2, 128),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ConfigError("invalid_chat_limit")
        for value in (
            chat.connect_timeout,
            chat.first_content_timeout,
            chat.idle_timeout,
            chat.total_timeout,
            chat.segment_timeout,
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0.01 <= value <= 120
            ):
                raise ConfigError("invalid_chat_timeout")
        voice_values = data.get("voice", {})
        if not isinstance(voice_values, dict) or set(voice_values) - set(
            Voice.__dataclass_fields__
        ):
            raise ConfigError("invalid_voice_configuration")
        voice = Voice(**voice_values)
        if (
            not isinstance(voice.output_device, str)
            or len(voice.output_device) > 256
            or any(ord(c) < 32 for c in voice.output_device)
        ):
            raise ConfigError("invalid_output_device")
        for value, low, high in (
            (voice.port, 1024, 65535),
            (voice.text_queue_chars, 256, 4096),
            (voice.arm_seconds, 1, 300),
            (voice.arm_turns, 1, 10),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ConfigError("invalid_voice_limit")
        if (
            type(voice.output_rate) is not int
            or voice.output_rate not in {24000, 44100, 48000}
            or type(voice.output_channels) is not int
            or voice.output_channels not in {1, 2}
        ):
            raise ConfigError("invalid_output_format")
        for value, lower, upper in (
            (voice.acoustic_delay, 0.1, 2.0),
            (voice.pcm_seconds, 0.5, 4.0),
            (voice.prefill_seconds, 0.04, 0.4),
            (voice.stt_timeout, 1.0, 120.0),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ConfigError("invalid_voice_parameter")
        config = Config(
            Assets(**paths),
            source_present=True,
            tts=TTS(**values),
            speech=Speech(**speech_values),
            chat=chat,
            voice=voice,
        )
        # Installed wheels resolve their sibling environments, never development
        # interpreters left in the shared private TOML. Assets/settings stay shared.
        release = Path(sys.prefix).parent
        if (release / "release.json").is_file():
            config = replace(
                config,
                tts=replace(config.tts, python=release / "tts/bin/python"),
                speech=replace(config.speech, python=release / "stt/bin/python"),
            )
        return config
    except PermissionError:
        raise ConfigError("configuration_permission_denied", blocked=True) from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ConfigError("invalid_toml") from None
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("configuration_unreadable") from None
