"""Strict non-secret configuration; no dotenv or V1 imports."""

import math
import stat
import tomllib
from dataclasses import dataclass
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
class Config:
    assets: Assets = Assets()
    source_present: bool = False
    tts: TTS = TTS()


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
        if set(data) - {"assets", "tts"}:
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
        return Config(Assets(**paths), source_present=True, tts=TTS(**values))
    except PermissionError:
        raise ConfigError("configuration_permission_denied", blocked=True) from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ConfigError("invalid_toml") from None
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("configuration_unreadable") from None
