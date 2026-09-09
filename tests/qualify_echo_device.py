"""Physical qualification. Secrets use app-private stdin; no root or adb reverse."""

import argparse
import asyncio
import contextlib
import json
import math
import os
import struct
from dataclasses import replace
from pathlib import Path

from jarvis_office.echo.gateway import EchoGateway, Session, Settings
from jarvis_office.echo.protocol import Packet


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=["profile", "transport", "tone", "capture", "passive", "outage", "recreate"],
        required=True,
    )
    parser.add_argument("--rate", type=int, choices=[16000, 48000], default=16000)
    parser.add_argument("--source", type=int, choices=[1, 7], default=7)
    parser.add_argument("--seconds", type=int, default=6)
    parser.add_argument("--capture-file", type=Path)
    args = parser.parse_args()
    settings = replace(Settings.load(args.config), test_audio=True)
    device_id, secret = next(iter(settings.devices.items()))

    async def adb(*command: str, data: bytes | None = None) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "/opt/homebrew/bin/adb",
            "-s",
            args.device,
            *command,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(90):
                out, err = await process.communicate(data)
            if process.returncode:
                report["adb_failure"] = {
                    "operation": " ".join(command[:4]),
                    "exit": process.returncode,
                    "detail": err.decode().replace(secret, "[redacted]")[:500],
                }
                raise RuntimeError("ADB_COMMAND_FAILED")
            return out
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    report: dict[str, object] = {"phase": args.phase, "physical_device": True, "ai_requests": 0}
    raw = bytearray()
    sessions: dict[str, Session] = {}
    energy = 0
    samples = 0
    peak = 0
    sample_rates: set[int] = set()

    def frame_received(session: Session, frame: Packet, received_ns: int) -> None:
        nonlocal energy, samples, peak
        if frame.flags:
            return
        sample_rates.add(frame.rate)
        values = struct.unpack(f"<{len(frame.pcm) // 2}h", frame.pcm)
        energy += sum(v * v for v in values)
        samples += len(values)
        peak = max(peak, max(abs(v) for v in values))
        if args.capture_file and len(raw) < args.rate * 2 * 5:
            raw.extend(frame.pcm[: args.rate * 2 * 5 - len(raw)])

    gateway = EchoGateway(settings, on_frame=frame_received)
    server = await gateway.start()
    outage_done = False
    screen_saved = False

    async def monitor() -> None:
        nonlocal server, outage_done, screen_saved
        while True:
            for session in list(gateway.sessions.values()):
                sessions[session.id] = session
                if (
                    args.phase == "passive"
                    and not screen_saved
                    and session.metrics.get("uplink_frames", 0) >= 10
                ):
                    screen_saved = True
                    screen = args.report.with_suffix(".png")
                    screen.write_bytes(await adb("exec-out", "screencap", "-p"))
                    screen.chmod(0o600)
                    service = (
                        await adb(
                            "shell", "dumpsys", "activity", "services", "com.jarvisoffice.echo"
                        )
                    ).decode()
                    report["foreground_service"] = [
                        line.strip() for line in service.splitlines() if "isForeground=" in line
                    ]
                if (
                    args.phase == "outage"
                    and not outage_done
                    and session.metrics.get("uplink_frames", 0) >= 10
                ):
                    outage_done = True
                    server.close()
                    await server.wait_closed()
                    await asyncio.sleep(2)
                    server = await gateway.start()
            await asyncio.sleep(0.02)

    task = asyncio.create_task(monitor())
    try:
        await adb("shell", "am", "force-stop", "com.jarvisoffice.echo")
        await adb(
            "shell", "run-as", "com.jarvisoffice.echo", "rm", "-f", "files/qualification.json"
        )
        # The package must already be installed. run-as switches to its ordinary private UID.
        pairing = json.dumps(
            {"host": settings.bind, "port": settings.port, "device": device_id, "secret": secret}
        ).encode()
        await adb(
            "shell",
            "run-as",
            "com.jarvisoffice.echo",
            "sh",
            "-c",
            "'umask 077; mkdir -p files; cat > files/pairing.json'",
            data=pairing,
        )
        output = await adb(
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "phase",
            args.phase,
            "-e",
            "rate",
            str(args.rate),
            "-e",
            "source",
            str(args.source),
            "-e",
            "seconds",
            str(args.seconds),
            "com.jarvisoffice.echo.test/com.jarvisoffice.echo.QualificationInstrumentation",
        )
        report["instrumentation"] = output.decode().replace(secret, "[redacted]")
        if "INSTRUMENTATION_CODE: -1" in output.decode():
            device_report = await adb(
                "exec-out", "run-as", "com.jarvisoffice.echo", "cat", "files/qualification.json"
            )
            report["android"] = json.loads(device_report)
            report["pass"] = (
                bool(report["android"]["pass"]) and report["android"]["phase"] == args.phase
            )
        else:
            report["pass"] = False
            with contextlib.suppress(RuntimeError):
                report["android"] = json.loads(
                    await adb(
                        "exec-out",
                        "run-as",
                        "com.jarvisoffice.echo",
                        "cat",
                        "files/qualification.json",
                    )
                )
        if args.phase == "transport":
            report["pass"] = report["pass"] and any(
                s.metrics.get("synthetic_verified_frames") == 50 for s in sessions.values()
            )
        if args.phase in {"capture", "passive"}:
            report["pass"] = report["pass"] and samples >= args.rate * max(1, args.seconds - 1)
        if args.phase == "outage":
            report["pass"] = report["pass"] and outage_done and len(sessions) >= 2
        report["server_sessions"] = [
            {"state": s.snapshot(), "metrics": s.metrics, "timings": s.timing_summary()}
            for s in sessions.values()
        ]
        report["microphone"] = {
            "samples": samples,
            "rates": sorted(sample_rates),
            "peak": peak,
            "rms": math.sqrt(energy / samples) if samples else None,
            "rms_dbfs": 20 * math.log10(math.sqrt(energy / samples) / 32768)
            if energy and samples
            else None,
        }
        if args.capture_file and raw:
            # Explicit, bounded qualification only: at most five seconds. Not the gateway default.
            fd = os.open(args.capture_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as capture:
                capture.write(raw)
            report["private_capture_bytes"] = len(raw)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await adb("shell", "am", "force-stop", "com.jarvisoffice.echo")
        await gateway.close()
        server.close()
        await server.wait_closed()
        args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.report.write_text(json.dumps(report, indent=2))
        args.report.chmod(0o600)
    print(
        str(args.report)
        + " — physical "
        + args.phase
        + " "
        + ("PASS" if report.get("pass") else "FAIL")
    )


if __name__ == "__main__":
    asyncio.run(main())
