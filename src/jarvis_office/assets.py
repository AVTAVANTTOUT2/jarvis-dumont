"""Explicit imports only: verified independent files, atomic bundle publication."""

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_office.config import Config
from jarvis_office.diagnostics import inspect_stt, inspect_tts, inspect_vad, inspect_voice


class AssetError(Exception):
    pass


def stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def private_root() -> Path:
    explicit = os.environ.get("JARVIS_OFFICE_DATA")
    return Path(explicit) if explicit else Path.home() / "Library/Application Support/JarvisOffice"


def fingerprint(path: Path) -> dict[str, Any]:
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise AssetError("asset_not_regular")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        opened = os.fstat(handle.fileno())
    after = path.stat()
    if stat_identity(before) != stat_identity(opened) or stat_identity(before) != stat_identity(
        after
    ):
        raise AssetError("source_changed_during_inspection")
    return {"size": before.st_size, "sha256": digest.hexdigest()}


def validate_profile(voice: Path) -> dict[str, Any]:
    inspect_voice(voice)
    metadata = json.loads((voice / "metadata.json").read_text(encoding="utf-8"))
    if (
        metadata.get("reference_audio") != "reference.wav"
        or metadata.get("reference_text") != "transcript.txt"
        or metadata.get("language") not in {"fr", "fr-FR", "french"}
    ):
        raise AssetError("voice_metadata_mismatch")
    with wave.open(str(voice / "reference.wav"), "rb") as wav:
        details = {
            "sample_rate": wav.getframerate(),
            "channels": wav.getnchannels(),
            "duration_s": wav.getnframes() / wav.getframerate(),
            "sample_width": wav.getsampwidth(),
        }
    for key in ("sample_rate", "channels", "sample_width"):
        if key in metadata and metadata[key] != details[key]:
            raise AssetError("voice_metadata_mismatch")
    return details


def source_files(model: Path, voice: Path) -> dict[str, Path]:
    inspect_tts(model)
    validate_profile(voice)
    result = {}
    for folder, label in ((model, "model"), (voice, "voice")):
        for base, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".cache", ".git", "__pycache__"})
            if any((Path(base) / d).is_symlink() for d in dirs):
                raise AssetError("source_directory_link")
            for name in sorted(files):
                if label == "voice" and name not in {
                    "reference.wav",
                    "transcript.txt",
                    "metadata.json",
                }:
                    if not name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                        continue
                file = Path(base) / name
                if file.suffix in {".npy", ".pyc"}:
                    continue
                result[f"{label}/{file.relative_to(folder).as_posix()}"] = file
    return result


def stt_sources(model: Path, vad: Path, notice: Path) -> dict[str, Path]:
    inspect_stt(model)
    inspect_vad(vad)
    if (
        not notice.is_file()
        or notice.stat().st_size > 1024 * 1024
        or not notice.read_bytes().strip()
    ):
        raise AssetError("vad_notice_required")
    sources = {"vad/silero.onnx": vad, "vad/LICENSE": notice}
    for base, dirs, files in os.walk(model, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".cache", ".git", "__pycache__"})
        if any((Path(base) / d).is_symlink() for d in dirs):
            raise AssetError("source_directory_link")
        for name in sorted(files):
            file = Path(base) / name
            sources["model/" + file.relative_to(model).as_posix()] = file
    return sources


def atomic_json(path: Path, value: object) -> None:
    """Replace only an explicitly selected private report, never a source asset."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_bundle(bundle: Path) -> dict[str, Any]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise AssetError("bundle_missing_or_linked")
    bundle = bundle.resolve()
    manifest_file = bundle / "manifest.json"
    if manifest_file.is_symlink() or manifest_file.stat().st_size > 1024 * 1024:
        raise AssetError("invalid_import_manifest")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise AssetError("invalid_import_manifest")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries or len(entries) > 4096:
        raise AssetError("invalid_import_manifest")
    if (
        manifest.get("bundle_id")
        != hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    ):
        raise AssetError("invalid_import_manifest")
    for name, expected in entries.items():
        if not isinstance(name, str) or not name:
            raise AssetError("invalid_import_manifest")
        rel = Path(name)
        if (
            not rel.parts
            or rel.is_absolute()
            or ".." in rel.parts
            or rel.parts[0] not in {"model", "voice", "vad"}
        ):
            raise AssetError("invalid_import_manifest")
        file = bundle / rel
        if file.resolve() != file.absolute() or file.stat().st_nlink != 1:
            raise AssetError("import_is_not_independent")
        if fingerprint(file) != expected:
            raise AssetError("import_checksum_mismatch")
    if manifest.get("kind", "tts") == "tts":
        actual = source_files(bundle / "model", bundle / "voice")
    elif manifest.get("kind") == "stt":
        actual = stt_sources(bundle / "model", bundle / "vad/silero.onnx", bundle / "vad/LICENSE")
    else:
        raise AssetError("unknown_bundle_kind")
    if set(actual) != set(entries):
        raise AssetError("import_file_list_mismatch")
    return manifest


def import_bundle(config: Config, destination: Path, *, dry_run: bool = False) -> dict[str, Any]:
    model, voice = config.assets.tts_model, config.assets.voice_profile
    if model is None or voice is None:
        raise AssetError("model_and_voice_required")
    destination = destination.expanduser().resolve()
    if any(destination.is_relative_to(folder.resolve()) for folder in (model, voice)):
        raise AssetError("destination_overlaps_source")
    return _import_files(lambda: source_files(model, voice), destination, dry_run=dry_run)


def import_stt(
    model: Path, vad: Path, notice: Path, destination: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    if destination.is_relative_to(model.resolve()) or any(
        file.resolve().is_relative_to(destination) for file in (vad, notice)
    ):
        raise AssetError("destination_overlaps_source")
    return _import_files(
        lambda: stt_sources(model, vad, notice), destination, kind="stt", dry_run=dry_run
    )


def _import_files(
    enumerate_sources: Callable[[], dict[str, Path]],
    destination: Path,
    *,
    kind: str = "tts",
    dry_run: bool = False,
) -> dict[str, Any]:
    sources = enumerate_sources()
    identities = {
        name: (stat_identity(file.stat()), str(file.resolve())) for name, file in sources.items()
    }
    entries = {name: fingerprint(file) for name, file in sources.items()}
    bundle_id = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    target = destination / bundle_id
    manifest = {"schema_version": 1, "bundle_id": bundle_id, "files": entries}
    if kind != "tts":
        manifest["kind"] = kind
    result = {
        "bundle_id": bundle_id,
        "files": len(entries),
        "bytes": sum(row["size"] for row in entries.values()),
        "reused": target.exists(),
        "dry_run": dry_run,
    }
    if target.exists():
        if verify_bundle(target) != manifest:
            raise AssetError("existing_bundle_mismatch")
        return result
    existing = destination
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < result["bytes"] + 64 * 1024 * 1024:
        raise AssetError("insufficient_disk_space")
    if dry_run:
        return result
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=destination) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir(mode=0o700)
        for name, source in sources.items():
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, dest, follow_symlinks=True)
            dest.chmod(0o600)
            if os.path.samefile(source, dest) or dest.is_symlink() or dest.stat().st_nlink != 1:
                raise AssetError("import_is_not_independent")
            if (
                fingerprint(dest) != entries[name]
                or fingerprint(source) != entries[name]
                or (stat_identity(source.stat()), str(source.resolve())) != identities[name]
            ):
                raise AssetError("source_changed_during_copy")
        atomic_json(staging / "manifest.json", manifest)
        verify_bundle(staging)
        if enumerate_sources() != sources:
            raise AssetError("source_file_list_changed")
        for name, source in sources.items():
            if (
                fingerprint(source) != entries[name]
                or (stat_identity(source.stat()), str(source.resolve())) != identities[name]
            ):
                raise AssetError("source_changed_during_copy")
        # Both directories are on the destination filesystem. Existing bundles are
        # nonempty: rename cannot overwrite a concurrently published valid bundle.
        try:
            staging.rename(target)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if verify_bundle(target) != manifest:
                raise AssetError("existing_bundle_mismatch") from None
            result["reused"] = True
    return result


def record_import(inventory_path: Path, config: Config, result: dict[str, Any]) -> None:
    """Keep private provenance in the existing inventory, not in public CLI output."""
    if inventory_path.is_symlink():
        raise AssetError("inventory_must_not_be_a_link")
    if inventory_path.exists():
        if inventory_path.stat().st_size > 4 * 1024 * 1024:
            raise AssetError("invalid_inventory")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if not isinstance(inventory, dict):
            raise AssetError("invalid_inventory")
    else:
        inventory = {"schema_version": 1, "private": True}
    section = inventory.setdefault("phase02", {})
    if not isinstance(section, dict):
        raise AssetError("invalid_inventory")
    section["import"] = {
        **result,
        "source_model": str(config.assets.tts_model),
        "source_voice": str(config.assets.voice_profile),
        "copy_method": "independent regular files; SHA256 checked before/after; atomic rename",
        "profile": validate_profile(config.assets.voice_profile)
        if config.assets.voice_profile
        else None,
        "voice_identity": "NOT_RUN",
        "provenance": inventory.get("provenance", {"revision": "UNKNOWN"}),
    }
    atomic_json(inventory_path, inventory)
