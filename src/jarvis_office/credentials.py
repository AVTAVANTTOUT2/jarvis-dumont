"""Explicit single-key import; never source dotenv, inspect a vault or log a value."""

import json
import os
import stat
from pathlib import Path

from jarvis_office.assets import private_root
from jarvis_office.config import ConfigError


def secret_path() -> Path:
    return private_root() / "config/deepseek.env"


def _read_key(path: Path, *, private: bool) -> str:
    try:
        if path.is_symlink():
            raise ConfigError("secret_file_link_refused", blocked=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ConfigError("secret_file_invalid", blocked=True)
            if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ConfigError("secret_permissions_require_0600", blocked=True)
            raw = handle.read(65537).decode("utf-8")
        values = []
        for line in raw.splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "DEEPSEEK_API_KEY":
                value = value.strip()
                if value[:1] in {"'", '"'}:
                    quote = value[0]
                    end = value.find(quote, 1)
                    trailing = value[end + 1 :].strip()
                    if end < 0 or (trailing and not trailing.startswith("#")):
                        raise ConfigError("secret_value_invalid", blocked=True)
                    value = value[1:end]
                else:
                    value = value.split(" #", 1)[0].strip()
                if value:
                    values.append(value)
        if not values:
            raise ConfigError("deepseek_key_missing", blocked=True)
        if len(set(values)) != 1:
            raise ConfigError("deepseek_key_ambiguous", blocked=True)
        key = values[0]
        if not 8 <= len(key) <= 512 or not all(
            c.isascii() and (c.isalnum() or c in "-_.") for c in key
        ):
            raise ConfigError("secret_value_invalid", blocked=True)
        return key
    except ConfigError:
        raise
    except (OSError, UnicodeError):
        raise ConfigError("deepseek_key_unreadable", blocked=True) from None


def load_key() -> str:
    """Only the Office private file; no implicit V1 or environment lookup at runtime."""
    return _read_key(secret_path(), private=True)


def reserve_validation_request() -> int:
    """Phase 06 hard ceiling, shared by chat/run and restarts. Phase 05 is preserved.

    Reserve BEFORE HTTP, including attempts that fail or are cancelled. A crash may
    overcount; it can never silently replenish the budget. No automatic reset.
    """
    import fcntl

    path = private_root() / "config/phase06-api-budget.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
            raise ConfigError("validation_budget_permissions")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > 4096
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ConfigError("validation_budget_invalid")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raw = handle.read(4097)
            data = json.loads(raw) if raw else {"phase": 6, "limit": 20, "attempts": 0}
            if (
                not isinstance(data, dict)
                or data.get("phase") != 6
                or data.get("limit") != 20
                or type(data.get("attempts")) is not int
                or not 0 <= data["attempts"] <= 20
            ):
                raise ConfigError("validation_budget_invalid")
            if data["attempts"] >= 20:
                raise ConfigError("validation_budget_exhausted")
            data["attempts"] += 1
            handle.seek(0)
            json.dump(data, handle)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            return int(data["attempts"])
    except (OSError, ValueError):
        raise ConfigError("validation_budget_unavailable") from None


def import_key(source: Path) -> dict[str, object]:
    """User-triggered provisioning; refuse overwrite and copy exactly one key."""
    target = secret_path()
    if target.exists() or target.is_symlink():
        load_key()
        return {"status": "PASS", "reused": True, "secret_value_emitted": False}
    key = _read_key(source, private=False)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink() or target.parent.stat().st_mode & 0o077:
        raise ConfigError("secret_directory_requires_0700", blocked=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("DEEPSEEK_API_KEY=" + key + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"status": "PASS", "reused": False, "secret_value_emitted": False}
