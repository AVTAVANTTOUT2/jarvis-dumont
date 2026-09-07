"""CLI with allowlisted output, including configuration and usage errors."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from jarvis_office.config import ConfigError, load_config
from jarvis_office.diagnostics import Check, asset_checks, doctor_checks, report


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ConfigError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = Parser(
        prog="jarvis-office", description="Offline diagnostics, explicit asset import and local TTS"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    assets = sub.add_parser("assets").add_subparsers(dest="action", required=True)
    inspect = assets.add_parser("inspect")
    importer = assets.add_parser("import")
    importer.add_argument("--dry-run", action="store_true", help="inspect and hash; no writes")
    tts = sub.add_parser("tts-test")
    tts.add_argument("--text", required=True)
    tts.add_argument("--output", required=True, type=Path)
    tts.add_argument("--repeat", type=int, default=1)
    tts.add_argument("--report", type=Path, help="explicit private JSON metrics destination")
    for command in (doctor, inspect, importer, tts):
        command.add_argument("--json", action="store_true", help="structured redacted output")
        command.add_argument("--config", type=Path, help="explicit local TOML configuration")
    command_name = "cli"
    try:
        args = parser.parse_args(argv)
        command_name = args.command if args.command != "assets" else "assets " + args.action
        config = load_config(args.config)
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
