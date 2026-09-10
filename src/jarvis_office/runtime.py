"""Owned-instance lock, metadata-only rotating log and passive local health.

The kernel lock, not a PID file, arbitrates ownership. Never unlink a lock inode:
doing so would let a third process lock a different inode while the owner runs.
"""

import fcntl
import http.client
import json
import logging
import os
import re
import stat
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from jarvis_office.assets import private_root
from jarvis_office.config import ConfigError


def identity(pid: int) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "uid=,lstart=,comm="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}  # Permission unavailable: never claim verified PID ownership.
    parts = result.stdout.strip().split(maxsplit=6)
    if len(parts) != 7:
        return {}
    return {"uid": parts[0], "started": " ".join(parts[1:6]), "executable": parts[6]}


def lock_path() -> Path:
    return private_root() / "run/instance.lock"


def _open_lock() -> int:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.stat()
    if path.parent.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("instance_directory_not_private")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ConfigError("instance_lock_not_private")
    return fd


class Instance:
    def __init__(self) -> None:
        self.fd = _open_lock()
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise ConfigError("office_instance_already_running") from None
        self.record: dict[str, Any] = {
            "pid": os.getpid(),
            "identity": identity(os.getpid()),
            "prefix": str(Path(sys.prefix).resolve()),
            "session": "",
            "port": None,
        }
        self.publish("", None)

    def publish(self, session: str, port: int | None) -> None:
        self.record.update(session=session, port=port)
        raw = json.dumps(self.record).encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, raw)
        os.ftruncate(self.fd, len(raw))
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def health(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Strict allowlist: no transcript, response, voice or secret in status/logs."""
    audio = bool(snapshot.get("engines_ready") and snapshot.get("output_verified"))
    conversation = bool(snapshot.get("conversation_ready"))
    return {
        "alive": True,
        "audio_ready": audio,
        "conversation_ready": conversation,
        "state": snapshot.get("state"),
        "paused": snapshot.get("state") == "paused",
        "listening": snapshot.get("microphone") == "open" and snapshot.get("armed") is True,
        "microphone": snapshot.get("microphone"),
        "error": (
            snapshot.get("error")
            if snapshot.get("error") is None
            or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", str(snapshot.get("error")))
            else "unclassified_error"
        ),
        "qualification": "STT_QUALIFICATION_PENDING / NO_ACCEPTABLE_STT",
    }


def status() -> dict[str, Any]:
    if not lock_path().exists():
        return {"status": "NOT_RUN", "alive": False, "lock": "absent"}
    fd = _open_lock()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"status": "NOT_RUN", "alive": False, "lock": "stale_unlocked"}
        except BlockingIOError:
            pass
        record = json.loads(os.read(fd, 4096))
        pid = record.get("pid")
        if (
            type(pid) is not int
            or pid <= 0
            or not record.get("identity")
            or identity(pid) != record.get("identity")
        ):
            return {"status": "FAIL", "alive": False, "error": "instance_identity_mismatch"}
        result: dict[str, Any] = {
            "status": "PENDING",
            "alive": True,
            "pid": pid,
            "owner_verified": True,
            "release": Path(record["prefix"]).parent.name,
            "http_loopback": False,
            "audio_ready": False,
            "conversation_ready": False,
        }
        port = record.get("port")
        if type(port) is not int or not 1024 <= port <= 65535:
            return result
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Sec-Fetch-Site": "same-origin",
            "X-Jarvis-Local": "1",
            "Content-Type": "application/json",
        }
        try:
            private = record.get("dashboard") == "private-v1"
            connection.request(
                "GET" if private else "POST",
                "/api/bootstrap" if private else "/bootstrap",
                None if private else "{}",
                headers,
            )
            response = connection.getresponse()
            cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]
            if response.status != 200 or len(cookie) > 256:
                return result
            response.read(1024)
            headers["Cookie"] = cookie
            connection.request(
                "GET" if private else "POST",
                "/api/state" if private else "/snapshot",
                None if private else "{}",
                headers,
            )
            response = connection.getresponse()
            data = response.read(65537)
            if response.status != 200 or len(data) > 65536:
                return result
            snapshot = json.loads(data)
            if snapshot.get("session") != record.get("session"):
                return {**result, "status": "FAIL", "error": "instance_session_mismatch"}
            return {**result, **health(snapshot), "http_loopback": True, "status": "PASS"}
        except (OSError, ValueError, http.client.HTTPException):
            return result
        finally:
            connection.close()
    except (ValueError, KeyError, TypeError):
        return {"status": "PENDING", "alive": True, "error": "instance_starting_or_invalid"}
    finally:
        os.close(fd)


class RuntimeLog:
    def __init__(self, directory: Path | None = None) -> None:
        directory = directory or Path.home() / "Library/Logs/JarvisOffice"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ConfigError("log_directory_not_private")
        path = directory / "office.log"
        for candidate in [path, *(directory / f"office.log.{n}" for n in range(1, 4))]:
            if candidate.is_symlink():
                raise ConfigError("log_symlink_refused")
        self.handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
        path.chmod(0o600)
        self.logger = logging.Logger("jarvis_office.runtime")
        self.logger.addHandler(self.handler)
        self.previous: tuple[Any, ...] = ()

    def update(self, snapshot: dict[str, Any]) -> None:
        state = health(snapshot)
        marker = (snapshot.get("session"), snapshot.get("turn"), state["state"], state["error"])
        if marker != self.previous:
            self.previous = marker
            self.logger.warning(json.dumps({"session": marker[0], "turn": marker[1], **state}))

    def close(self) -> None:
        self.handler.close()
