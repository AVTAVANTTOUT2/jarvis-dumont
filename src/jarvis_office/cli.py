"""CLI with allowlisted output, including configuration and usage errors."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Never

from jarvis_office.config import Chat, ConfigError, load_config
from jarvis_office.diagnostics import Check, asset_checks, doctor_checks, report


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ConfigError("invalid_arguments")


async def chat_command(settings: Chat, text: str, destination: Path | None) -> int:
    from jarvis_office.assets import atomic_json, private_root
    from jarvis_office.credentials import load_key
    from jarvis_office.deepseek import ChatError, DeepSeek

    if destination is not None and (
        destination.exists()
        or destination.is_symlink()
        or not destination.resolve().is_relative_to(private_root().resolve() / "reports")
    ):
        raise ConfigError("chat_report_requires_new_private_path")
    client = DeepSeek(load_key(), settings)
    metrics: dict[str, object] = {"status": "FAIL", "requests": 0}
    code = 1
    try:
        async with client.turn(text) as turn:
            try:
                async for event in turn:
                    if event.kind == "delta":
                        print(event.text, end="", flush=True)
                turn.confirm(turn.delivered_text, channel="displayed", complete=True)
                code = 0
            except ChatError:
                pass
            except asyncio.CancelledError:
                await turn.cancel()
                code = 130
            finally:
                metrics = turn.metrics
                print()
    finally:
        await client.close()
    if destination is not None:
        atomic_json(destination, metrics)
    print(json.dumps(metrics, ensure_ascii=True), file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = Parser(
        prog="jarvis-office", description="Jarvis Office: passive diagnostics and explicit tests"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status", help="passive owned-instance health; no engines")
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--config", type=Path)
    service = sub.add_parser("service", help="owned user LaunchAgent, always starts paused")
    service.add_argument(
        "action", choices=("install", "start", "stop", "restart", "status", "uninstall")
    )
    service.add_argument("--config", type=Path)
    service.add_argument("--json", action="store_true")
    release = sub.add_parser("release", help="verified local candidate lifecycle")
    release.add_argument("action", choices=("build", "verify", "list", "activate", "rollback"))
    release.add_argument("name", nargs="?")
    release.add_argument("--source", type=Path, default=Path.cwd())
    release.add_argument("--uv", type=Path)
    release.add_argument("--config", type=Path)
    release.add_argument("--json", action="store_true")
    voice = sub.add_parser("run", help="paused local voice controller; explicit bounded arming")
    voice.add_argument("--config", type=Path)
    voice.add_argument("--arm", action="store_true")
    voice.add_argument("--seconds", type=int, choices=range(1, 301))
    voice.add_argument("--turns", type=int, choices=range(1, 11))
    voice.add_argument("--text", help="explicit addressed synthetic test; no microphone")
    voice.add_argument(
        "--no-play", action="store_true", help="with --text: synthesize/discard PCM only"
    )
    voice.add_argument("--report", type=Path, help="new private metadata-only report")
    chat = sub.add_parser("chat")
    chat.add_argument("--text", required=True)
    chat.add_argument("--report", type=Path, help="new private metadata-only JSON report")
    chat.add_argument("--config", type=Path)
    provision = sub.add_parser("configure-deepseek")
    provision.add_argument("--from-env", type=Path, required=True)
    provision.add_argument("--config", type=Path)
    doctor = sub.add_parser("doctor")
    assets = sub.add_parser("assets").add_subparsers(dest="action", required=True)
    inspect = assets.add_parser("inspect")
    importer = assets.add_parser("import")
    importer.add_argument("--dry-run", action="store_true", help="inspect and hash; no writes")
    stt_importer = assets.add_parser("import-stt")
    for name in ("model", "vad", "notice"):
        stt_importer.add_argument("--" + name, type=Path, required=True)
    stt_importer.add_argument("--target", choices=("benchmark", "selected"), default="benchmark")
    stt_importer.add_argument("--dry-run", action="store_true")
    stt = sub.add_parser("stt-test")
    stt.add_argument("--input", type=Path, required=True)
    bench = sub.add_parser("stt-bench")
    for name in ("manifest", "small", "turbo"):
        bench.add_argument("--" + name, type=Path, required=True)
    bench.add_argument("--repeat", type=int, default=3)
    mic = sub.add_parser("mic-check")
    inputs = sub.add_parser("input-list", help="passive PortAudio input formats; no capture")
    outputs = sub.add_parser("output-list", help="passive PortAudio output formats; no playback")
    corpus = sub.add_parser("corpus-init")
    corpus.add_argument("--output", type=Path, required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--seconds", type=float, required=True)
    capture.add_argument("--rate", type=int, choices=(16000, 44100, 48000))
    for command in (mic, capture):
        command.add_argument("--device")
    for command in (stt, bench, capture):
        command.add_argument("--report", type=Path)
    tts = sub.add_parser("tts-test")
    tts.add_argument("--text", required=True)
    tts.add_argument("--output", required=True, type=Path)
    tts.add_argument("--repeat", type=int, default=1)
    tts.add_argument("--report", type=Path, help="explicit private JSON metrics destination")
    for command in (
        doctor,
        inspect,
        importer,
        stt_importer,
        tts,
        stt,
        bench,
        mic,
        capture,
        corpus,
        inputs,
        outputs,
    ):
        command.add_argument("--json", action="store_true", help="structured redacted output")
        command.add_argument("--config", type=Path, help="explicit local TOML configuration")
    command_name = "cli"
    try:
        args = parser.parse_args(argv)
        command_name = args.command if args.command != "assets" else "assets " + args.action
        config = load_config(args.config)
        if command_name in {"status", "service", "release"}:
            from jarvis_office import release as releases
            from jarvis_office.runtime import status
            from jarvis_office.service import manage

            if command_name == "status":
                operation = status()
            elif command_name == "service":
                operation = manage(args.action)
            elif args.action == "build":
                if args.uv is None or not args.uv.is_file():
                    raise ConfigError("explicit_uv_executable_required")
                operation = releases.build(args.source, args.uv.absolute(), config)
            elif args.action == "list":
                operation = {
                    "status": "PASS",
                    "current": releases.active_name(),
                    "releases": sorted(
                        p.name
                        for p in releases.releases().glob("*")
                        if releases.NAME.fullmatch(p.name)
                    ),
                }
            elif args.action == "rollback":
                operation = releases.rollback()
            elif args.name is None:
                raise ConfigError("release_name_required")
            else:
                operation = (releases.verify if args.action == "verify" else releases.activate)(
                    args.name
                )
            print(json.dumps(operation, sort_keys=True))
            return 1 if operation["status"] == "FAIL" else 0
        if command_name == "run":
            from jarvis_office.assets import private_root
            from jarvis_office.voice import run_command

            config = replace(
                config,
                voice=replace(
                    config.voice,
                    arm_seconds=args.seconds or config.voice.arm_seconds,
                    arm_turns=args.turns or config.voice.arm_turns,
                ),
            )
            return asyncio.run(
                run_command(
                    config,
                    args.config or private_root() / "config.toml",
                    text=args.text,
                    no_play=args.no_play,
                    arm=args.arm,
                    report=args.report,
                )
            )
        if command_name == "chat":
            return asyncio.run(chat_command(config.chat, args.text, args.report))
        if command_name == "configure-deepseek":
            from jarvis_office.credentials import import_key

            print(json.dumps(import_key(args.from_env)))
            return 0
        if command_name == "corpus-init":
            from jarvis_office.corpus import prepare_human_corpus

            print(json.dumps(prepare_human_corpus(args.output)))
            return 0
        if command_name in {
            "stt-test",
            "stt-bench",
            "mic-check",
            "capture",
            "input-list",
            "output-list",
        }:
            from jarvis_office.speech_cli import launch

            launch(config, vars(args))
        if command_name == "assets import-stt":
            from jarvis_office.assets import AssetError, atomic_json, import_stt, private_root

            destination = private_root() / (
                "benchmarks/stt/assets" if args.target == "benchmark" else "assets/stt"
            )
            inspected = import_stt(args.model, args.vad, args.notice, destination, dry_run=True)
            if (
                args.target == "selected"
                and destination.exists()
                and any(p.name != inspected["bundle_id"] for p in destination.iterdir())
            ):
                raise AssetError("production_allows_one_stt_model")
            result = (
                inspected
                if args.dry_run
                else import_stt(args.model, args.vad, args.notice, destination)
            )
            if not args.dry_run:
                inventory_file = private_root() / "inventory.phase01.json"
                if inventory_file.is_symlink() or (
                    inventory_file.exists() and inventory_file.stat().st_size > 4 * 1024 * 1024
                ):
                    raise AssetError("invalid_inventory")
                inventory = (
                    json.loads(inventory_file.read_text()) if inventory_file.exists() else {}
                )
                imports = inventory.setdefault("phase03", {}).setdefault("imports", {})
                imports[args.target + ":" + result["bundle_id"]] = {
                    **result,
                    "source_model": str(args.model.resolve()),
                    "source_vad": str(args.vad.resolve()),
                    "source_notice": str(args.notice.resolve()),
                    "destination": str(destination / result["bundle_id"]),
                }
                atomic_json(inventory_file, inventory)
            print(json.dumps({**result, "status": "PASS", "exit_code": 0}))
            return 0
        if command_name == "assets import":
            from jarvis_office.assets import import_bundle, private_root, record_import

            result = import_bundle(config, private_root() / "assets", dry_run=args.dry_run)
            if not args.dry_run:
                record_import(private_root() / "inventory.phase01.json", config, result)
            result.update(status="PASS", exit_code=0, command=command_name)
            print(json.dumps(result, sort_keys=True))
            return 0
        if command_name == "tts-test":
            from jarvis_office.assets import atomic_json, private_root
            from jarvis_office.tts import synthesize_to_wav

            source = args.config if args.config is not None else private_root() / "config.toml"
            if args.report is not None:
                if args.report.exists() or args.report.is_symlink():
                    raise ConfigError("report_already_exists")
                if args.report.resolve() == args.output.resolve():
                    raise ConfigError("report_conflicts_with_output")
                for asset in (config.assets.tts_model, config.assets.voice_profile):
                    if asset and args.report.resolve().is_relative_to(asset.resolve()):
                        raise ConfigError("report_conflicts_with_assets")
            result = asyncio.run(
                synthesize_to_wav(config, source, args.text, args.output, repeats=args.repeat)
            )
            if args.report is not None:
                atomic_json(args.report, result)
            print(json.dumps(result, sort_keys=True))
            return 0
        checks = [
            Check(
                "configuration",
                "PASS" if config.source_present else "NOT_RUN",
                "configuration_loaded" if config.source_present else "default_configuration_absent",
            )
        ]
        checks += doctor_checks(config) if args.command == "doctor" else asset_checks(config)
        result = report(command_name, checks)
    except ConfigError as exc:
        result = report(
            command_name,
            [Check("configuration", "BLOCKED_USER" if exc.blocked else "FAIL", exc.reason)],
            config_error=not exc.blocked,
        )
    except Exception as exc:
        # Library/OS errors may include private filenames or input text. Never emit
        # str(exc); stable error codes are only accepted from our own error classes.
        from jarvis_office.assets import AssetError
        from jarvis_office.tts import TTSError

        reason = str(exc) if isinstance(exc, (AssetError, TTSError)) else "operation_failed"
        result = report(command_name, [Check(command_name, "FAIL", reason)])
    # JSON is also the default; a single output contract is easier to automate safely.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return int(str(result["exit_code"]))


if __name__ == "__main__":
    sys.exit(main())
