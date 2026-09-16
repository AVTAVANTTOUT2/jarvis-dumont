"""Local wheel releases: fixed locks, final-location venvs, verified atomic pointers."""

import io
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis_office.assets import atomic_json, fingerprint, private_root, verify_bundle
from jarvis_office.config import Config, ConfigError

NAME = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}")


def releases() -> Path:
    return private_root() / "releases"


def target(name: str) -> Path:
    if not NAME.fullmatch(name):
        raise ConfigError("invalid_release_name")
    path = releases() / name
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigError("release_symlink_refused")
    return path


def command(args: list[str], *, cwd: Path, timeout: int = 300) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("release_command_unavailable_or_timeout") from None
    if result.returncode:
        # No raw child exception/arguments in reports: package managers may expose paths.
        raise ConfigError("release_command_failed")
    return result.stdout.strip()


def asset_contract(config: Config) -> dict[str, Any]:
    contracts = {}
    for asset in (
        config.assets.tts_model,
        config.assets.voice_profile,
        config.assets.stt_model,
        config.assets.vad_model,
    ):
        if asset is None or not asset.resolve().is_relative_to(
            (private_root() / "assets").resolve()
        ):
            raise ConfigError("release_requires_office_assets")
        bundle = next((p for p in [asset, *asset.parents] if (p / "manifest.json").is_file()), None)
        if bundle is None:
            raise ConfigError("release_requires_verified_bundle")
        relative = str(bundle.relative_to(private_root()))
        if relative not in contracts:
            verify_bundle(bundle)
            contracts[relative] = fingerprint(bundle / "manifest.json")
    return contracts


def files_contract(directory: Path) -> dict[str, Any]:
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = str(path.relative_to(directory))
        if relative == "release.json" or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            files[relative] = {"link": os.readlink(path), "resolved": fingerprint(path.resolve())}
        elif path.is_file():
            files[relative] = fingerprint(path)
    return files


def verify(name: str, *, assets: bool = True) -> dict[str, Any]:
    directory = target(name)
    try:
        data = json.loads((directory / "release.json").read_text())
        if data["name"] != name or not re.fullmatch(r"[0-9a-f]{40}", data["sha"]):
            raise ConfigError("invalid_release_manifest")
        if files_contract(directory) != data["files"]:
            raise ConfigError("release_checksum_mismatch")
        if assets:
            for relative, expected in data["assets"].items():
                bundle = private_root() / relative
                if not bundle.resolve().is_relative_to((private_root() / "assets").resolve()):
                    raise ConfigError("release_asset_escape")
                if fingerprint(bundle / "manifest.json") != expected:
                    raise ConfigError("release_asset_manifest_changed")
                verify_bundle(bundle)
        return {
            "status": "PASS",
            "name": name,
            "sha": data["sha"],
            "assets_verified": assets,
            "contract": "REPRODUCIBLE_INSTALL_CONTRACT",
            "classification": "RELEASE_CANDIDATE",
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError("release_manifest_unavailable_or_invalid") from None


def active_name() -> str | None:
    pointer = private_root() / "current"
    if not pointer.is_symlink():
        if pointer.exists():
            raise ConfigError("current_is_not_release_pointer")
        return None
    resolved = pointer.resolve()
    if resolved.parent != releases().resolve() or not NAME.fullmatch(resolved.name):
        raise ConfigError("current_pointer_invalid")
    return resolved.name


def activate(name: str) -> dict[str, Any]:
    from jarvis_office.runtime import Instance

    # The same ownership lock as run: no pointer change while any Office engine runs.
    lock = Instance()
    try:
        result = verify(name)
        database = private_root() / "data/office.sqlite3"
        if database.exists():
            manifest = json.loads((target(name) / "release.json").read_text())
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                schema = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema > manifest.get("sqlite_schema_max", 0):
                raise ConfigError("release_database_schema_incompatible")
        previous = active_name()
        if previous != name:
            if previous is not None:
                verify(previous)
                atomic_json(private_root() / "state/previous-release.json", {"name": previous})
            temporary = private_root() / (".current-" + uuid.uuid4().hex)
            try:
                temporary.symlink_to(Path("releases") / name)
                os.replace(temporary, private_root() / "current")
            finally:
                temporary.unlink(missing_ok=True)
        return {**result, "active": name, "previous": previous}
    finally:
        lock.close()


def rollback() -> dict[str, Any]:
    try:
        previous = json.loads((private_root() / "state/previous-release.json").read_text())["name"]
    except (OSError, ValueError, KeyError):
        raise ConfigError("no_verified_previous_release") from None
    return activate(previous)


def build(source: Path, uv: Path, config: Config) -> dict[str, Any]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ConfigError("release_requires_native_apple_silicon")
    source = source.resolve()
    sha = command(["git", "rev-parse", "HEAD"], cwd=source)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ConfigError("release_requires_git_commit")
    # Build only the committed tree. Unrelated user changes remain untouched.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", sha], cwd=source, capture_output=True, check=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="jarvis-office-build-") as scratch:
        tree = Path(scratch)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        version = tomllib.loads((tree / "pyproject.toml").read_text())["project"]["version"]
        name = version + "-" + sha[:12]
        directory = target(name)
        if directory.exists():
            return verify(name)
        assets = asset_contract(config)
        releases().mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(mode=0o700)
        command([str(uv), "build", "--offline", "--no-python-downloads"], cwd=tree)
        wheel = next((tree / "dist").glob("*.whl"))
        import shutil

        shutil.copyfile(wheel, directory / wheel.name)
        environment_info = {}
        for role, project, executable in (
            ("main", tree, Path(sys.executable)),
            ("stt", tree / "runtime/stt", config.speech.python),
            ("tts", tree / "runtime/tts", config.tts.python),
        ):
            if executable is None:
                raise ConfigError("release_python_missing")
            base = command(
                [str(executable), "-I", "-B", "-c", "import sys; print(sys._base_executable)"],
                cwd=tree,
            )
            if Path(base).is_relative_to(source):
                raise ConfigError("release_python_depends_on_checkout")
            requirements = directory / (role + "-requirements.txt")
            args = [
                str(uv),
                "export",
                "--locked",
                "--offline",
                "--no-dev",
                "--no-emit-project",
                "--no-emit-package",
                "jarvis-office",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ]
            if role == "main":
                args += ["--extra", "private"]
            command(args, cwd=project)
            shutil.copyfile(project / "uv.lock", directory / (role + ".lock"))
            command(
                [
                    str(uv),
                    "venv",
                    "--offline",
                    "--no-python-downloads",
                    "--python",
                    base,
                    str(directory / role),
                ],
                cwd=tree,
            )
            python = directory / role / "bin/python"
            command(
                [
                    str(uv),
                    "pip",
                    "sync",
                    "--offline",
                    "--require-hashes",
                    "--python",
                    str(python),
                    str(requirements),
                ],
                cwd=tree,
            )
            command(
                [
                    str(uv),
                    "pip",
                    "install",
                    "--offline",
                    "--no-deps",
                    "--python",
                    str(python),
                    str(directory / wheel.name),
                ],
                cwd=tree,
            )
            info = command(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-c",
                    "import json,sys,jarvis_office; from importlib.metadata import distributions; "
                    "print(json.dumps({'python':sys.version,'module':jarvis_office.__file__,"
                    "'packages':sorted((d.metadata['Name'],d.version,"
                    "d.metadata.get('License-Expression') "
                    "or d.metadata.get('License') or 'UNKNOWN') for d in distributions())}))",
                ],
                cwd=Path("/private/tmp"),
            )
            decoded = json.loads(info)
            if not Path(decoded["module"]).is_relative_to(directory / role):
                raise ConfigError("release_import_not_isolated")
            environment_info[role] = decoded
        data = {
            "name": name,
            "sha": sha,
            "version": version,
            "sqlite_schema_max": 2,
            "assets": assets,
            "built_utc": datetime.now(UTC).isoformat(),
            "platform": platform.platform(),
            "uv": command([str(uv), "--version"], cwd=tree),
            "environments": environment_info,
            "classification": "RELEASE_CANDIDATE_FROM_" + sha,
            "contract": "REPRODUCIBLE_INSTALL_CONTRACT",
            "files": files_contract(directory),
        }
        atomic_json(directory / "release.json", data)
    return verify(name)
