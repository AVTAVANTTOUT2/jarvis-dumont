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


def classify_playback(trace: list[dict]) -> dict:
    """Classify observed counter increments, never equate the total with audible glitches."""
    phases = {row["phase"]: row for row in trace if row["phase"] != "U2"}
    frames = [row for row in trace if row["phase"] == "U2"]
    required = {"U0", "U1", "U5", "U6_AFTER_PAUSE_FLUSH", "END_DECLARED"}
    if not required <= phases.keys():
        return {"complete": False}
    last = next(
        (row for row in frames if row["sequence"] == phases["END_DECLARED"]["last_sequence"]), None
    )
    if last is None:
        return {"complete": False}
    if [row["sequence"] for row in frames] != list(range(last["sequence"] + 1)):
        return {"complete": False}
    counts = {key: phases[key]["underruns"] for key in ("U0", "U1", "U5")}
    counts.update(
        U3=last["underruns_before"],
        U4=last["underruns"],
        U6=phases["U6_AFTER_PAUSE_FLUSH"]["underruns"],
    )
    if any(counts[key] < 0 for key in counts) or not (
        counts["U0"] <= counts["U1"] <= counts["U3"] <= counts["U4"] <= counts["U5"] <= counts["U6"]
    ):
        return {"complete": False, "counts": counts}
    return {
        "complete": True,
        "counts": counts,
        "frames": len(frames),
        "during_data": counts["U4"] - counts["U1"],
        "after_last_write": counts["U5"] - counts["U4"],
        "teardown": counts["U6"] - counts["U5"],
        "queue_max": max(row["queue_depth"] for row in frames),
        "u6_operation": "pause_flush_before_release",
        "audible_artifact": "HUMAN_REQUIRED",
    }


async def replay_ram(
    raw: bytearray, in_rate: int, out_rate: int, send, *, gain: float = 1
) -> tuple[int, float]:
    """Diagnostic only: bounded capture and converted PCM are wiped even if playback fails."""
    import numpy as np
    import soxr

    capture = np.frombuffer(raw, dtype="<i2").copy()
    raw[:] = b"\0" * len(raw)
    raw.clear()
    converted = None
    pcm = bytearray()
    try:
        converted = (
            capture.copy() if in_rate == out_rate else soxr.resample(capture, in_rate, out_rate)
        )
        applied_gain = 1.0
        if gain < 1:
            applied_gain = gain
            np.multiply(converted, applied_gain, out=converted, casting="unsafe")
        if gain > 1:
            peak = max(abs(int(converted.min(initial=0))), abs(int(converted.max(initial=0))))
            applied_gain = min(float(gain), 8192 / peak) if peak else 1.0
            np.multiply(converted, applied_gain, out=converted, casting="unsafe")
        pcm.extend(converted.astype("<i2", copy=False).tobytes())
        capture.fill(0)
        converted.fill(0)
        await send(pcm)
        return len(pcm), applied_gain
    finally:
        capture.fill(0)
        if converted is not None:
            converted.fill(0)
        pcm[:] = b"\0" * len(pcm)
        pcm.clear()


def spectrum_metrics(raw: bytearray, rate: int) -> dict:
    """Whole-capture energy bands, not a speech classifier; retain no waveform or FFT."""
    import numpy as np

    values = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    spectrum = None
    try:
        if values.size == 0:
            return {"dc_pcm": 0.0, "bands": {}}
        dc = float(values.mean())
        values -= dc
        values *= np.hanning(values.size)
        spectrum = np.fft.rfft(values)
        power = np.abs(spectrum) ** 2
        frequencies = np.fft.rfftfreq(values.size, 1 / rate)
        total = float(power.sum())
        return {
            "dc_pcm": dc,
            "bands": {
                f"{low}_{high}_hz": float(power[(frequencies >= low) & (frequencies < high)].sum())
                / total
                if total
                else 0.0
                for low, high in ((0, 80), (80, 250), (250, 4000), (4000, rate // 2 + 1))
            },
        }
    finally:
        values.fill(0)
        if spectrum is not None:
            spectrum.fill(0)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=[
            "profile",
            "transport",
            "tone",
            "continuous",
            "matrix",
            "capture",
            "micro_replay",
            "passive",
            "outage",
            "recreate",
        ],
        required=True,
    )
    parser.add_argument("--rate", type=int, choices=[16000, 48000], default=16000)
    parser.add_argument("--source", type=int, choices=[1, 7], default=7)
    parser.add_argument("--playback-usage", type=int, choices=[1, 2], default=2)
    parser.add_argument("--seconds", type=int, choices=range(1, 31), default=6)
    parser.add_argument(
        "--prefill-ms", type=int, choices=[20, 40, 60, 80, 100, 120, 140], default=20
    )
    parser.add_argument("--capture-file", type=Path)
    parser.add_argument("--replay-gain", type=int, choices=[1, 2, 4, 8], default=1)
    parser.add_argument("--compare-levels", action="store_true")
    args = parser.parse_args()
    if args.replay_gain != 1 and args.phase != "micro_replay":
        parser.error("replay gain is only available for micro_replay")
    if args.compare_levels and (args.phase != "micro_replay" or args.replay_gain != 1):
        parser.error("level comparison requires micro_replay with original gain 1")
    if args.phase == "micro_replay" and (args.capture_file or args.seconds > 8):
        parser.error("micro_replay is RAM-only and limited to eight seconds")
    settings = replace(
        Settings.load(args.config), test_audio=True, playback_prefill_ms=args.prefill_ms
    )
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
            timeout = (
                None
                if args.phase == "micro_replay" and command[:3] == ("shell", "am", "instrument")
                else 90
            )
            async with asyncio.timeout(timeout):
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
    clipping = 0
    silent_frames = 0
    frame_count = 0
    sample_rates: set[int] = set()

    def frame_received(session: Session, frame: Packet, received_ns: int) -> None:
        nonlocal energy, samples, peak, clipping, silent_frames, frame_count
        if frame.flags:
            return
        sample_rates.add(frame.rate)
        values = struct.unpack(f"<{len(frame.pcm) // 2}h", frame.pcm)
        frame_energy = sum(v * v for v in values)
        energy += frame_energy
        samples += len(values)
        peak = max(peak, max(abs(v) for v in values))
        frame_count += 1
        if args.phase == "micro_replay" and frame_count == 1:
            print("MIC_CAPTURE_STARTED", flush=True)
        clipping += sum(abs(v) >= 32767 for v in values)
        silent_frames += frame_energy / len(values) < 32768**2 * 1e-6  # Below -60 dBFS RMS.
        limit = args.rate * 2 * (args.seconds if args.phase == "micro_replay" else 5)
        if (args.capture_file or args.phase == "micro_replay") and len(raw) < limit:
            raw.extend(frame.pcm[: limit - len(raw)])

    class ReplayGateway(EchoGateway):
        async def tone(self, session, *, duration_ms=400, replay_pcm=None):
            async def send(pcm):
                print("MIC_REPLAY_STARTED", flush=True)
                await super(ReplayGateway, self).tone(session, replay_pcm=pcm)
                if args.compare_levels and session.alive:
                    await asyncio.sleep(1)

                    async def send_quieter(data):
                        print("MIC_REPLAY_MINUS6DB_STARTED", flush=True)
                        await super(ReplayGateway, self).tone(session, replay_pcm=data)

                    quieter = bytearray(pcm)
                    report["comparison_bytes"], report["comparison_gain"] = await replay_ram(
                        quieter, session.down_rate, session.down_rate, send_quieter, gain=0.5
                    )

            try:
                report["microphone_spectrum"] = spectrum_metrics(raw, args.rate)
                report["ram_replayed_bytes"], report["ram_replay_gain"] = await replay_ram(
                    raw, args.rate, session.down_rate, send, gain=args.replay_gain
                )
            finally:
                report["ram_capture_destroyed"] = not raw

    gateway_type = ReplayGateway if args.phase == "micro_replay" else EchoGateway
    gateway = gateway_type(settings, on_frame=frame_received)
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
            "playback_usage",
            str(args.playback_usage),
            "-e",
            "seconds",
            str(args.seconds),
            "-e",
            "replays",
            "2" if args.compare_levels else "1",
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
            with contextlib.suppress(RuntimeError, json.JSONDecodeError):
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
        if args.phase in {"capture", "micro_replay", "passive"}:
            report["pass"] = report["pass"] and samples >= args.rate * max(1, args.seconds - 1)
        if args.phase == "outage":
            report["pass"] = report["pass"] and outage_done and len(sessions) >= 2
        report["server_sessions"] = [
            {"state": s.snapshot(), "metrics": s.metrics, "timings": s.timing_summary()}
            for s in sessions.values()
        ]
        report["underrun_classification"] = [
            classify_playback(trace)
            for trace in report.get("android", {}).get("playback_traces", [])
        ]
        if args.phase in {"tone", "continuous", "matrix", "micro_replay"}:
            expected = (
                2
                if args.compare_levels
                else 10
                if args.phase == "matrix"
                else 5
                if args.phase == "tone"
                else 1
            )
            complete = len(report["underrun_classification"]) == expected and all(
                item.get("complete") for item in report["underrun_classification"]
            )
            report["audio_stable"] = complete and all(
                item.get("complete") and item.get("during_data") == 0
                for item in report["underrun_classification"]
            )
            report["pass"] = (
                report["pass"]
                and complete
                and sum(s.metrics.get("playback_completed_streams", 0) for s in sessions.values())
                == expected
            )
        report["prefill_ms"] = args.prefill_ms
        report["playback_usage"] = args.playback_usage
        report["microphone"] = {
            "samples": samples,
            "rates": sorted(sample_rates),
            "peak": peak,
            "frames": frame_count,
            "duration_seconds": samples / args.rate,
            "clipped_samples": clipping,
            "silent_frames_below_minus60_dbfs": silent_frames,
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
        raw[:] = b"\0" * len(raw)
        raw.clear()
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
        + (" / audio_stable=" + str(report["audio_stable"]) if "audio_stable" in report else "")
    )


if __name__ == "__main__":
    asyncio.run(main())
