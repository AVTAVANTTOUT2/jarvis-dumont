"""Single selected STT runtime entry; only the explicit bench accepts two candidates."""

import dataclasses
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Never

from jarvis_office.assets import AssetError, atomic_json, private_root
from jarvis_office.config import Config, load_config
from jarvis_office.tts import worker_environment


def launch(config: Config, arguments: dict[str, Any]) -> Never:
    python = config.speech.python
    if python is None or not python.is_file():
        raise AssetError("speech_python_not_configured")
    arguments = {k: str(v.absolute()) if isinstance(v, Path) else v for k, v in arguments.items()}
    arguments["config"] = arguments.get("config") or str(private_root() / "config.toml")
    command = [str(python), "-I", "-B", "-m", "jarvis_office.speech_cli", json.dumps(arguments)]
    env = worker_environment(private_root() / "cache/speech")
    # Keep private-root resolution independent of the isolated worker HOME.
    env["JARVIS_OFFICE_DATA"] = str(private_root())
    if sys.platform == "darwin":
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            *command,
        ]
    os.execve(command[0], command, env)


class BoundedMessages(io.TextIOBase):
    def __init__(self) -> None:
        self.tail = ""
        self.characters = 0

    def write(self, text: str) -> int:
        self.characters += len(text)
        self.tail = (self.tail + text)[-32768:]
        return len(text)


def run(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    command = args["command"]
    if command in {"input-list", "output-list"}:
        import sounddevice as sd

        from jarvis_office.capture import list_devices

        return list_devices(sd, command.split("-")[0])

    from jarvis_office.audio_input import AudioError, Silero, read_wav
    from jarvis_office.capture import capture_wav, microphone_preflight, resolve_input
    from jarvis_office.stt import Recognizer, benchmark, load_corpus, select_candidate

    report_path = Path(args["report"]) if args.get("report") else None
    if report_path:
        if report_path.exists() or report_path.is_symlink():
            raise AudioError("report_already_exists")
        # Raw speech test reports are explicitly confined to the private data root.
        if not report_path.resolve().is_relative_to(private_root().resolve()):
            raise AudioError("speech_report_must_be_private")
    if command == "mic-check":
        import sounddevice as sd

        name = args.get("device") or config.speech.input_device
        _, device = resolve_input(sd, name, config.speech.input_rate)
        check = microphone_preflight()
        return {
            **check,
            "exit_code": 0 if check["status"] == "PASS" else 3,
            "device": device,
            "capture": "NOT_RUN",
        }
    if config.assets.vad_model is None:
        raise AudioError("vad_model_not_configured")
    if command == "capture":
        from jarvis_office.assets import verify_bundle

        verify_bundle(config.assets.vad_model.parent.parent)
        output = Path(args["output"])
        if not output.resolve().is_relative_to(private_root().resolve() / "corpus"):
            raise AudioError("capture_output_must_be_in_private_corpus")
        vad = Silero(config.assets.vad_model)
        settings = dataclasses.replace(
            config.speech,
            input_device=args.get("device") or config.speech.input_device,
            input_rate=args.get("rate") or config.speech.input_rate,
        )
        result = capture_wav(settings, vad, output, args["seconds"])
    elif command == "stt-test":
        if config.assets.stt_model is None:
            raise AudioError("stt_model_not_configured")
        audio = read_wav(Path(args["input"]), config.speech.max_input_s)
        engine = Recognizer(config.assets.stt_model, config.assets.vad_model, config.speech)
        try:
            result = {
                "status": "PASS",
                "exit_code": 0,
                "backend": engine.backend,
                "load_s": engine.load_s,
                "warmup_s": engine.warmup_s,
                **engine.transcribe(audio),
            }
        finally:
            engine.close()
    elif command == "stt-bench":
        if report_path is None:
            raise AudioError("private_benchmark_report_required")
        corpus = load_corpus(Path(args["manifest"]))
        candidates = {}
        for name in ("small", "turbo"):
            model = Path(args[name])
            # benchmark() explicitly unloads and deletes the model before returning.
            candidates[name] = benchmark(
                model, model.parent / "vad/silero.onnx", config.speech, corpus, args["repeat"]
            )
        human = [r for r in corpus if r["source"] == "human" and r.get("human_verified") is True]
        qualified_corpus = (
            len(human) >= 50
            and sum(r["split"] == "holdout" for r in human) >= 10
            and all(
                sum(r["condition"] == condition for r in human) >= 20
                for condition in ("quiet", "office_noise")
            )
        )
        selection = select_candidate(candidates)
        result = {
            "status": "PASS" if selection["model"] else "FAIL",
            "exit_code": 0 if selection["model"] else 1,
            "candidates": candidates,
            "human_corpus_count": len(human),
            "human_validation": "NOT_RUN" if qualified_corpus else "BLOCKED_USER",
            "selection": selection,
            "criteria": {
                "quiet_wer_max": 0.05,
                "critical_errors_max": 0,
                "invented_outputs_max": 0,
            },
        }
    else:
        raise AudioError("unknown_speech_command")
    if report_path:
        atomic_json(report_path, result)
    if command == "stt-test":
        return {
            k: result[k]
            for k in (
                "status",
                "exit_code",
                "backend",
                "load_s",
                "warmup_s",
                "accepted",
                "reason",
                "duration_s",
                "inference_s",
                "processing_s",
            )
        } | {
            "transcript_chars": len(result["text"]),
            "private_report_written": report_path is not None,
        }
    if command == "stt-bench":
        return {
            "status": result["status"],
            "exit_code": result["exit_code"],
            "human_validation": result["human_validation"],
            "selection": result["selection"],
            "private_report_written": True,
            "candidates": {
                name: {
                    k: value[k]
                    for k in (
                        "backend",
                        "load_s",
                        "warmup_s",
                        "peak_rss_bytes",
                        "corpus_files",
                        "attempts",
                        "summary",
                    )
                }
                for name, value in candidates.items()
            },
        }
    return result


def deny_network() -> None:
    # Native network access is additionally blocked by macOS sandbox-exec.
    def guard(event: str, arguments: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            raise PermissionError("speech_network_denied")

    sys.addaudithook(guard)


def main() -> int:
    output, errors = sys.stdout, sys.stderr
    messages = BoundedMessages()
    sys.stdout = sys.stderr = messages
    deny_network()
    try:
        args = json.loads(sys.argv[1])
        config = load_config(Path(args["config"]))
        result = run(config, args)
    except Exception as exc:
        from jarvis_office.audio_input import AudioError
        from jarvis_office.config import ConfigError

        reason = (
            str(exc)
            if isinstance(exc, (AudioError, AssetError, ConfigError))
            else "speech_operation_failed"
        )
        result = {"status": "FAIL", "exit_code": 1, "reason": reason}
    finally:
        sys.stdout, sys.stderr = output, errors
    print(json.dumps(result, allow_nan=False))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
