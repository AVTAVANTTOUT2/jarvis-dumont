"""User-owned private gateway supervisor: three attempts, always reconnect OFF."""

import argparse
import contextlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import UTC, datetime
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
    attempt = 0
    code: int | None = None
    phase = "SETUP"
    events: deque[dict[str, object]] = deque(maxlen=64)
    dropped = 0
    write_failures = 0
    rotated = False

    def record(
        event: str,
        *,
        error: Exception | None = None,
        supervisor_exit_code: int | None = None,
        persist: bool = True,
    ) -> None:
        nonlocal dropped, write_failures, rotated
        # Closed categories only: even custom exception class names can contain private text.
        exception_class = next(
            (
                cls.__name__
                for cls in (ConfigError, ProcessLookupError, PermissionError, OSError, ValueError)
                if isinstance(error, cls)
            ),
            "Exception" if error is not None else None,
        )
        dropped += int(len(events) == events.maxlen)
        events.append(
            {
                "event": event,
                "utc": datetime.now(UTC).isoformat(),
                "monotonic_s": time.monotonic(),
                "phase": phase,
                "attempt": attempt,
                "supervisor_pid": os.getpid(),
                "child_pid": child.pid if child is not None else None,
                "child_exit_code": code,
                "supervisor_exit_code": supervisor_exit_code,
                "stop_requested": stopped,
                "error_code": "SUPERVISOR_ERROR" if error is not None else None,
                "exception_class": exception_class,
            }
        )
        if not persist:
            return
        try:
            directory = root / "state"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or directory.stat().st_mode & 0o077:
                raise OSError("private_supervisor_directory_required")
            path = directory / "private-supervisor-events.json"
            if not rotated:
                if path.exists() or path.is_symlink():
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.stat().st_mode & 0o077
                        or path.stat().st_size > 65536
                    ):
                        raise OSError("private_supervisor_journal_required")
                    os.replace(path, directory / "private-supervisor-events.previous.json")
                rotated = True
            snapshot = {
                "schema_version": 1,
                "events": list(events),
                "dropped_events": dropped,
                "write_failures": write_failures,
            }
            if len(json.dumps(snapshot, ensure_ascii=True, indent=2).encode()) + 1 > 65536:
                raise ValueError("supervisor_journal_bound")
            atomic_json(path, snapshot)
        except (OSError, ValueError):
            write_failures += 1

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped, phase
        stopped = True
        # Do no disk I/O in a signal handler; the next supervisor event persists this fact.
        record("STOP_REQUESTED", persist=False)
        previous_phase = phase
        phase = "STOP_POLL"
        if child is not None and child.poll() is None:
            phase = "STOP_TERMINATE"
            child.terminate()
        phase = previous_phase

    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        record("SUPERVISOR_START")
        for next_attempt in range(1, 4):
            if stopped:
                break
            attempt = next_attempt
            child = None
            code = None
            phase = "STATE_START"
            record("ATTEMPT")
            atomic_json(
                root / "state/private-service.json",
                {
                    "state": "STARTING",
                    "attempt": attempt,
                    "supervisor_pid": os.getpid(),
                },
            )
            phase = "CHILD_START"
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
            record("CHILD_STARTED")
            phase = "CHILD_WAIT"
            code = child.wait()
            record("CHILD_WAIT_RETURNED")
            phase = "STATE_EXIT"
            atomic_json(
                root / "state/private-service.json",
                {
                    "state": "STOPPED" if stopped or code == 0 else "DEGRADED",
                    "attempt": attempt,
                    "exit_code": code,
                },
            )
            if stopped or code == 0:
                record("SUPERVISOR_EXIT", supervisor_exit_code=code)
                return code
            phase = "RETRY_DELAY"
            deadline = time.monotonic() + 10
            while not stopped and time.monotonic() < deadline:
                time.sleep(0.2)
        record("SUPERVISOR_EXIT", supervisor_exit_code=1)
        return 1
    except Exception as error:
        exit_code = 1 if isinstance(error, (ConfigError, OSError, ValueError)) else None
        record("SUPERVISOR_ERROR", error=error, supervisor_exit_code=exit_code)
        try:
            atomic_json(
                root / "state/private-service.json",
                {
                    "state": "DEGRADED",
                    "attempt": attempt,
                    "supervisor_pid": os.getpid(),
                    "child_pid": child.pid if child is not None else None,
                    "exit_code": code,
                    "error": "SUPERVISOR_ERROR",
                    "phase": phase,
                    "stop_requested": stopped,
                },
            )
        except (OSError, ValueError):
            phase = "STATE_ERROR"
            record("TERMINAL_STATE_WRITE_FAILED")
        raise


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
