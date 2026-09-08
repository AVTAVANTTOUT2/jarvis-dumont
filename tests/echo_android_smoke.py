"""Opt-in emulator-only instrumentation harness. No AI, real microphone or LAN scan."""

import asyncio
import json
import secrets
import sys
from pathlib import Path

from jarvis_office.echo.gateway import EchoGateway, Settings


async def main() -> None:
    android_repo, report = map(Path, sys.argv[1:3])
    adb = "/opt/homebrew/bin/adb"
    device = "emulator-5580"

    async def command(*args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            adb, "-s", device, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async with asyncio.timeout(90):
            out, err = await process.communicate()
        if process.returncode:
            raise RuntimeError("ADB_COMMAND_FAILED")
        return (out + err).decode()

    secret = secrets.token_urlsafe(32)
    gateway = EchoGateway(
        Settings(
            "127.0.0.1",
            18771,
            {"instrumentation": secret},
            enabled=True,
            security="DEV_INSECURE_LAN",
            test_audio=True,
        )
    )
    server = await gateway.start()
    result: dict[str, object] = {"hardware": "EMULATOR_ONLY", "ai_requests": 0}
    try:
        await command(
            "install", "-r", "-g", str(android_repo / "app/build/outputs/apk/debug/app-debug.apk")
        )
        await command(
            "install",
            "-r",
            str(android_repo / "app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"),
        )
        await command("reverse", "tcp:18771", "tcp:18771")
        text = await command(
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "pairing",
            secret,
            "com.jarvisoffice.echo.test/com.jarvisoffice.echo.SmokeInstrumentation",
        )
        result["instrumentation"] = text.replace(secret, "[redacted]")
        result["pass"] = "INSTRUMENTATION_CODE: -1" in text and "PASS:" in text
    finally:
        await command("shell", "am", "force-stop", "com.jarvisoffice.echo")
        await command("reverse", "--remove", "tcp:18771")
        await gateway.close()
        server.close()
        await server.wait_closed()
        report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report.write_text(json.dumps(result, indent=2))
        report.chmod(0o600)
    print(str(report) + " — emulator instrumentation " + ("PASS" if result.get("pass") else "FAIL"))


if __name__ == "__main__":
    asyncio.run(main())
