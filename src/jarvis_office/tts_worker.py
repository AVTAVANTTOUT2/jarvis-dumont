"""MLX-only child. Heavy imports occur after offline isolation and asset validation."""

import contextlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

from jarvis_office.assets import verify_bundle
from jarvis_office.audio_client import source_signature
from jarvis_office.config import Config, load_config
from jarvis_office.tts import MAX_FRAME, MAX_REQUEST, MAX_TEXT, TTSError, write_frame


def install_network_guard() -> None:
    def guard(event: str, args: tuple[object, ...]) -> None:
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
            raise PermissionError("tts_worker_network_or_subprocess_denied")

    sys.addaudithook(guard)


def metadata_frame(out: BinaryIO, tag: bytes, request_id: int, data: object) -> None:
    write_frame(out, tag, request_id, json.dumps(data, allow_nan=False).encode())


class Engine:
    def __init__(self, config: Config) -> None:
        model, voice = config.assets.tts_model, config.assets.voice_profile
        if (
            model is None
            or voice is None
            or model.parent != voice.parent
            or model.name != "model"
            or voice.name != "voice"
        ):
            raise TTSError("imported_model_and_voice_required")
        verify_bundle(model.parent)
        profile = json.loads((voice / "metadata.json").read_text(encoding="utf-8"))
        if (
            profile.get("reference_audio") != "reference.wav"
            or profile.get("reference_text") != "transcript.txt"
            or profile.get("language") not in {"fr", "fr-FR", "french"}
        ):
            raise TTSError("voice_metadata_mismatch")
        self.transcript = (voice / "transcript.txt").read_text(encoding="utf-8").strip()
        if not self.transcript:
            raise TTSError("voice_transcript_missing")
        self.settings = config.tts
        import mlx.core as mx
        import numpy as np
        from mlx_audio.tts.utils import load_model
        from mlx_audio.utils import load_audio

        self.mx, self.np = mx, np
        self.model = load_model(model, lazy=False, strict=True, local_files_only=True)
        if (
            getattr(self.model.config, "tts_model_type", None) != "base"
            or getattr(self.model, "tokenizer", None) is None
            or getattr(self.model, "speech_tokenizer", None) is None
            or not self.model.speech_tokenizer.has_encoder
            or "french" not in self.model.get_supported_languages()
        ):
            raise TTSError("base_icl_tokenizer_or_french_unavailable")
        self.sample_rate = int(self.model.sample_rate)
        self.reference = load_audio(str(voice / "reference.wav"), sample_rate=self.sample_rate)
        mx.eval(self.reference)
        if self.reference.size == 0:
            raise TTSError("voice_reference_empty")

    def synthesize(self, text: str, request_id: int, out: BinaryIO | None) -> dict[str, Any]:
        started = time.perf_counter()
        cpu = time.process_time()
        first_model: float | None = None
        first_pcm: float | None = None
        total = 0
        chunks = 0
        settings = self.settings
        generator = self.model.generate(
            text=text,
            ref_audio=self.reference,
            ref_text=self.transcript,
            lang_code=settings.language,
            temperature=settings.temperature,
            top_p=settings.top_p,
            top_k=settings.top_k,
            repetition_penalty=settings.repetition_penalty,
            max_tokens=settings.max_tokens,
            stream=True,
            streaming_interval=settings.streaming_interval,
            streaming_context_size=25,
            verbose=False,
        )
        with contextlib.closing(generator):
            for result in generator:
                self.mx.eval(result.audio)
                if result.audio.size == 0:
                    continue
                if first_model is None:
                    first_model = time.perf_counter() - started
                audio = self.np.asarray(result.audio, dtype=self.np.float32)
                if audio.ndim != 1 or not self.np.isfinite(audio).all():
                    raise TTSError("invalid_generated_audio")
                pcm = (self.np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                if not pcm:
                    continue
                if first_pcm is None:
                    first_pcm = time.perf_counter() - started
                # Preserve the entire tail, including weak consonants. No trim,
                # fade, silence gate or lookahead delays first delivery.
                for offset in range(0, len(pcm), MAX_FRAME):
                    chunk = pcm[offset : offset + MAX_FRAME]
                    if out is not None:
                        write_frame(out, b"PCM!", request_id, chunk)
                    total += len(chunk)
                    chunks += 1
        if total == 0:
            raise TTSError("empty_synthesis")
        elapsed = time.perf_counter() - started
        duration = total / (2 * self.sample_rate)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "pcm_bytes": total,
            "chunks": chunks,
            "duration_s": duration,
            "synthesis_wall_s": elapsed,
            "cpu_s": time.process_time() - cpu,
            "first_model_audio_s": first_model,
            "first_pcm_produced_s": first_pcm,
            "rtf": elapsed / duration,
            "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
            "mlx_peak_bytes": int(self.mx.get_peak_memory()),
            "effective_icl_repetition_penalty": max(settings.repetition_penalty, 1.5),
        }

    def close(self) -> None:
        self.reference = None
        self.model = None
        self.mx.clear_cache()


def serve(config: Config, out: BinaryIO, source: BinaryIO) -> int:
    engine = None
    stage = "load"
    try:
        started = time.perf_counter()
        engine = Engine(config)
        metadata_frame(out, b"LOAD", 0, {"load_s": time.perf_counter() - started})
        stage = "warmup"
        warmup = engine.synthesize("Bonjour.", 0, None)
        if type(warmup.get("pcm_bytes")) is not int or warmup["pcm_bytes"] <= 0:
            raise TTSError("empty_warmup")
        metadata_frame(
            out,
            b"RDY!",
            0,
            {
                "sample_rate": engine.sample_rate,
                "code_signature": source_signature(),
                "channels": 1,
                "warmup_pcm_bytes": warmup["pcm_bytes"],
                "warmup": warmup,
                "voice_identity": "NOT_RUN",
            },
        )
        last_id = 0
        while line := source.readline(MAX_REQUEST + 1):
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise TTSError("invalid_request_frame")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "text"}:
                raise TTSError("invalid_request")
            request_id, text = request["id"], request["text"]
            if type(request_id) is not int or not last_id < request_id < 2**32:
                raise TTSError("invalid_request_id")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise TTSError("invalid_request_text")
            last_id = request_id
            try:
                metrics = engine.synthesize(text, request_id, out)
                metadata_frame(out, b"END!", request_id, metrics)
            except Exception as exc:
                metadata_frame(
                    out,
                    b"ERR!",
                    request_id,
                    {"reason": "synthesis_failed", "type": type(exc).__name__},
                )
                return 1  # Unknown engine state: the parent must recreate it.
        return 0
    except Exception as exc:
        metadata_frame(out, b"ERR!", 0, {"reason": stage + "_failed", "type": type(exc).__name__})
        return 1
    finally:
        if engine is not None:
            engine.close()


def main() -> int:
    # Protect the protocol even from native libraries writing directly to fd 1.
    with os.fdopen(os.dup(1), "wb", buffering=0) as out:
        sys.stdout.flush()
        os.dup2(2, 1)
        install_network_guard()
        try:
            config = load_config(Path(sys.argv[1]))
        except Exception:
            metadata_frame(out, b"ERR!", 0, {"reason": "invalid_worker_configuration"})
            return 2
        return serve(config, out, sys.stdin.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
