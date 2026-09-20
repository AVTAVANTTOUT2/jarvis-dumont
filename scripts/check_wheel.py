"""Check the wheel produced by one build invocation.

Fake .whl zip archives used in tests are not product builds. A matching
SHA-256 identifies the checked file; it does not prove binary reproducibility
or hardware behaviour.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

FORBIDDEN_SUFFIXES = (".wav", ".safetensors", ".env", ".toml")
CHUNK = 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent


class WheelCheckError(Exception):
    """Explicit package-check failure for this invocation."""


@dataclass(frozen=True)
class WheelCheck:
    path: Path
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def wheels_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.iterdir() if path.is_file() and path.suffix == ".whl"]


def require_unique_wheel(output_dir: Path) -> Path:
    found = wheels_in(output_dir)
    if not found:
        raise WheelCheckError("wheel_fail: no wheel in build output")
    if len(found) != 1:
        names = ", ".join(sorted(path.name for path in found))
        raise WheelCheckError(f"wheel_fail: expected 1 wheel, found {len(found)}: {names}")
    return found[0]


def validate_wheel_contents(path: Path) -> None:
    with ZipFile(path) as archive:
        names = archive.namelist()
    if "jarvis_office/control.html" not in names:
        raise WheelCheckError("missing jarvis_office/control.html")
    if not all(name.startswith("jarvis_office/") or ".dist-info/" in name for name in names):
        raise WheelCheckError("unexpected path prefix")
    if any(name.endswith(FORBIDDEN_SUFFIXES) for name in names):
        raise WheelCheckError("forbidden private asset")


def check_output_dir(output_dir: Path) -> WheelCheck:
    wheel = require_unique_wheel(output_dir)
    digest = sha256_file(wheel)
    try:
        validate_wheel_contents(wheel)
    except WheelCheckError as exc:
        raise WheelCheckError(f"wheel_fail {wheel.name} sha256:{digest} {exc}") from exc
    return WheelCheck(path=wheel.resolve(), sha256=digest)


def format_ok(result: WheelCheck) -> str:
    return f"wheel_ok {result.path.name} sha256:{result.sha256}"


def prepare_out_dir(requested: Path | None) -> tuple[Path, bool]:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="jarvis-office-package.")), True
    requested.mkdir(parents=True, exist_ok=True)
    leftovers = wheels_in(requested)
    if leftovers:
        names = ", ".join(sorted(path.name for path in leftovers))
        raise WheelCheckError(f"wheel_fail: output directory already contains wheels: {names}")
    return requested, False


def build_command(out_dir: Path, custom: str | None) -> list[str]:
    if custom:
        return [*shlex.split(custom), "--out-dir", str(out_dir)]
    return [
        "uv",
        "build",
        "--no-python-downloads",
        "--out-dir",
        str(out_dir),
        "--no-create-gitignore",
    ]


def run_build(out_dir: Path, custom: str | None) -> None:
    command = build_command(out_dir, custom)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise WheelCheckError(f"wheel_fail: build exited {completed.returncode}")


def build_and_check(
    out_dir: Path | None = None,
    keep: bool = False,
    build_cmd: str | None = None,
) -> WheelCheck:
    cleanup = False
    created: Path | None = None
    try:
        created, auto_cleanup = prepare_out_dir(out_dir)
        cleanup = auto_cleanup and not keep
        run_build(created, build_cmd)
        return check_output_dir(created)
    finally:
        if cleanup and created is not None:
            shutil.rmtree(created, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the wheel from one build invocation.")
    parser.add_argument("--out-dir", default=os.environ.get("VERIFY_OUT_DIR") or None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    keep = bool(args.keep or os.environ.get("VERIFY_OUT_DIR"))
    custom = os.environ.get("VERIFY_BUILD_CMD") or None
    try:
        result = build_and_check(
            out_dir=Path(args.out_dir) if args.out_dir else None,
            keep=keep,
            build_cmd=custom,
        )
    except WheelCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(format_ok(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
