"""User-owned private gateway supervisor: three attempts, always reconnect OFF."""

import argparse
import contextlib
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from jarvis_office.assets import atomic_json, private_root
from jarvis_office.config import ConfigError
from jarvis_office.release import active_name, verify

LABEL = "com.jarvisoffice.private"


def specification() -> dict[str, object]:
    root = private_root()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / "current/main/bin/python"),
            "-I",
            "-B",
            "-m",
            "jarvis_office.private_service",
            "run",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ThrottleInterval": 30,
        "ExitTimeOut": 35,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"JARVIS_OFFICE_DATA": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "Umask": 0o077,
    }


def supervise() -> int:
    root = private_root()
    stopped = False
    child: subprocess.Popen[bytes] | None = None

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for attempt in range(1, 4):
        if stopped:
            break
        atomic_json(
            root / "state/private-service.json",
            {
                "state": "STARTING",
                "attempt": attempt,
                "supervisor_pid": os.getpid(),
            },
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "jarvis_office.echo.gateway",
                "--config",
                str(root / "config/private-gateway.toml"),
            ],
            cwd=root,
        )
        code = child.wait()
        atomic_json(
            root / "state/private-service.json",
            {
                "state": "STOPPED" if stopped or code == 0 else "DEGRADED",
                "attempt": attempt,
                "exit_code": code,
            },
        )
        if stopped or code == 0:
            return code
        deadline = time.monotonic() + 10
        while not stopped and time.monotonic() < deadline:
            time.sleep(0.2)
    return 1


def manage(action: str) -> None:
    if sys.platform != "darwin":
        raise ConfigError("launchagent_requires_macos")
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    job = f"gui/{os.getuid()}/{LABEL}"
    if action == "install":
        name = active_name()
        if name is None:
            raise ConfigError("activate_verified_release_first")
        verify(name)
        if path.exists() and plistlib.loads(path.read_bytes()) != specification():
            raise ConfigError("private_launchagent_conflict")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(specification()))
        path.chmod(0o600)
    elif action == "start":
        loaded = subprocess.run(["launchctl", "print", job], capture_output=True).returncode == 0
        args = (
            ["launchctl", "kickstart", job]
            if loaded
            else [
                "launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(path),
            ]
        )
        if subprocess.run(args, capture_output=True).returncode:
            raise ConfigError("private_service_start_failed")
    elif action == "stop":
        subprocess.run(["launchctl", "bootout", job], capture_output=True)
    elif action == "status":
        result = subprocess.run(["launchctl", "print", job], capture_output=True)
        print("LOADED" if result.returncode == 0 else "NOT_LOADED")
        print("CURRENT=" + str(active_name()))
        print("DASHBOARD=http://127.0.0.1:8768/")
    else:
        raise ConfigError("invalid_service_action")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "install", "start", "stop", "status"])
    action = parser.parse_args().action
    try:
        if action == "run":
            return supervise()
        manage(action)
        return 0
    except (ConfigError, OSError, ValueError):
        print("PRIVATE_SERVICE_OPERATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
