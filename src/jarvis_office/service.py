"""User LaunchAgent only; never a shell command, global kill or auto-armed capture."""

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from jarvis_office.assets import private_root
from jarvis_office.config import ConfigError
from jarvis_office.release import active_name, verify
from jarvis_office.runtime import status as application_status

LABEL = "com.jarvisoffice.voice"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def specification() -> dict[str, Any]:
    root = private_root()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / "current/main/bin/python"),
            "-I",
            "-B",
            "-m",
            "jarvis_office",
            "run",
            "--config",
            str(root / "config.toml"),
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": False,
        "KeepAlive": False,
        "ThrottleInterval": 30,
        "ExitTimeOut": 25,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"JARVIS_OFFICE_DATA": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        # Application-owned rotating JSON log is authoritative. Never accumulate
        # unbounded native stderr/stdout in launchd-managed files.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "Umask": 0o077,
    }


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin":
        raise ConfigError("launchagent_requires_macos")
    try:
        return subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("launchctl_unavailable_or_timeout") from None


def owned_plist() -> bool:
    path = plist_path()
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise ConfigError("launchagent_not_owned")
        if plistlib.loads(path.read_bytes()) != specification():
            raise ConfigError("launchagent_conflicts_with_existing_file")
    except (OSError, ValueError):
        raise ConfigError("launchagent_invalid") from None
    return True


def status() -> dict[str, Any]:
    installed = owned_plist()
    job = launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    pid = re.search(r"^\s*pid = (\d+)\s*$", job.stdout, re.MULTILINE)
    last_exit = re.search(r"^\s*last exit code = (\d+)\s*$", job.stdout, re.MULTILINE)
    app = application_status()
    return {
        "status": "PASS"
        if installed
        and job.returncode == 0
        and pid
        and app.get("pid") == int(pid[1])
        and app.get("http_loopback")
        else "NOT_RUN",
        "installed": installed,
        "launchd_loaded": job.returncode == 0,
        "pid": int(pid[1]) if pid else None,
        "last_exit_code": int(last_exit[1]) if last_exit else None,
        "current": active_name(),
        "application": app,
    }


def manage(action: str) -> dict[str, Any]:
    if action == "status":
        return status()
    domain, job = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{LABEL}"
    installed = owned_plist()
    if action == "install":
        name = active_name()
        if name is None:
            raise ConfigError("activate_verified_release_first")
        verify(name)
        if not installed:
            path = plist_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".jarvis-office-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    plistlib.dump(specification(), handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {"status": "PASS", "installed": True, "started": False, "auto_arm": False}
    if not installed:
        raise ConfigError("launchagent_not_installed")
    if action in {"stop", "restart", "uninstall"}:
        if launchctl("print", job).returncode == 0:
            if launchctl("bootout", job).returncode:
                raise ConfigError("launchagent_stop_failed")
            deadline = time.monotonic() + 30
            while application_status().get("alive") and time.monotonic() < deadline:
                time.sleep(0.1)
            if application_status().get("alive"):
                raise ConfigError("office_shutdown_unverified")
        if action == "uninstall":
            plist_path().unlink()  # Only our exact verified plist; private data retained.
        if action != "restart":
            return {
                "status": "PASS",
                "loaded": False,
                "installed": action != "uninstall",
                "private_data_retained": True,
            }
    if action in {"start", "restart"}:
        name = active_name()
        if name is None:
            raise ConfigError("activate_verified_release_first")
        verify(name)
        if application_status().get("alive"):
            raise ConfigError("office_instance_already_running")
        if launchctl("print", job).returncode != 0:
            if launchctl("bootstrap", domain, str(plist_path())).returncode:
                raise ConfigError("launchagent_bootstrap_failed")
        if launchctl("kickstart", job).returncode:
            raise ConfigError("launchagent_start_failed")
        return {"status": "PASS", "startup": "PENDING", "auto_arm": False}
    raise ConfigError("unknown_service_action")
