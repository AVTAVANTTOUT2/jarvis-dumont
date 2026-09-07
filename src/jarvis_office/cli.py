"""CLI with allowlisted output, including configuration and usage errors."""

import argparse
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
    parser = Parser(prog="jarvis-office", description="Read-only, offline diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    assets = sub.add_parser("assets").add_subparsers(dest="action", required=True)
    inspect = assets.add_parser("inspect")
    for command in (doctor, inspect):
        command.add_argument("--json", action="store_true", help="structured redacted output")
        command.add_argument("--config", type=Path, help="explicit local TOML configuration")
    command_name = "cli"
    try:
        args = parser.parse_args(argv)
        command_name = "doctor" if args.command == "doctor" else "assets inspect"
        config = load_config(args.config)
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
    # JSON is also the default; a single output contract is easier to automate safely.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return int(str(result["exit_code"]))


if __name__ == "__main__":
    sys.exit(main())
