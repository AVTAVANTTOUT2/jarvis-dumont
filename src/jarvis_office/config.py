"""Strict non-secret configuration; no dotenv or V1 imports."""

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
class Config:
    assets: Assets = Assets()
    source_present: bool = False


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
        if set(data) - {"assets"}:
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
        return Config(Assets(**paths), source_present=True)
    except PermissionError:
        raise ConfigError("configuration_permission_denied", blocked=True) from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ConfigError("invalid_toml") from None
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("configuration_unreadable") from None
