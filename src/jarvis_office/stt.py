"""Concrete faster-whisper adapter and explicit qualification, never a model router."""

import contextlib
import gc
import json
import math
import resource
import statistics
import sys
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from jarvis_office.assets import fingerprint, verify_bundle
from jarvis_office.audio_input import AudioError, Samples, Silero, read_wav, segment_audio
from jarvis_office.config import Speech


def words(text: str) -> list[str]:
    """WER: Unicode NFKC + casefold, punctuation/apostrophes -> spaces.

    Accents and digits are retained. No equivalence between spoken and digit numbers,
    no substring/ghost phrase blacklist, and no stopword/negation removal.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c if c.isalnum() else " " for c in text).split()


def word_errors(reference: str, hypothesis: str) -> dict[str, int | float]:
    expected, actual = words(reference), words(hypothesis)
    row = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        next_row = [i]
        for j, right in enumerate(actual, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (left != right)))
        row = next_row
    return {
        "errors": row[-1],
        "reference_words": len(expected),
        "wer": row[-1] / len(expected) if expected else float(bool(actual)),
    }


def accept_text(text: str) -> tuple[bool, str]:
    # VAD decides whether speech exists; a probability is not a confidence percent.
    return (
        (True, "nonempty_silero_speech_transcript") if text.strip() else (False, "empty_transcript")
    )


def critical_errors(reference: str, text: str, terms: list[str]) -> list[str]:
    """Conservative qualification flags, never a production speech blacklist."""
    expected, actual = words(reference), words(text)
    negations = {"ne", "n", "pas", "jamais", "plus", "aucun", "aucune", "sans", "ni"}
    errors = []
    if Counter(w for w in expected if w in negations) != Counter(
        w for w in actual if w in negations
    ):
        errors.append("negation_tokens_changed")
    if [w for w in expected if any(c.isdigit() for c in w)] != [
        w for w in actual if any(c.isdigit() for c in w)
    ]:
        errors.append("digit_tokens_changed")
    for term in terms:
        target = words(term)
        counts = [
            sum(sequence[i : i + len(target)] == target for i in range(len(sequence)))
            for sequence in (expected, actual)
        ]
        if counts[0] != counts[1]:
            errors.append("annotated_term_changed:" + term)
    return errors


class Recognizer:
    def __init__(self, model: Path, vad: Path, settings: Speech) -> None:
        import ctranslate2
        from faster_whisper import WhisperModel

        if model.name != "model" or vad != model.parent / "vad/silero.onnx":
            raise AudioError("verified_stt_bundle_required")
        if verify_bundle(model.parent).get("kind") != "stt":
            raise AudioError("verified_stt_bundle_required")
        available = sorted(ctranslate2.get_supported_compute_types("cpu"))
        if settings.compute_type not in available:
            raise AudioError("cpu_compute_type_unavailable")
        self.settings = settings
        self.vad = Silero(vad)
        started = time.perf_counter()
        self.model = WhisperModel(
            str(model),
            device="cpu",
            compute_type=settings.compute_type,
            cpu_threads=settings.cpu_threads,
            num_workers=1,
            local_files_only=True,
        )
        self.backend = {
            "engine": "faster-whisper",
            "device": self.model.model.device,
            "compute_type": self.model.model.compute_type,
            "available_cpu_compute_types": available,
            "ctranslate2_version": ctranslate2.__version__,
            "metal": "NOT_USED",
        }
        if self.backend["device"] != "cpu" or self.backend["compute_type"] != settings.compute_type:
            self.close()
            raise AudioError("unexpected_stt_backend")
        self.load_s = time.perf_counter() - started
        started = time.perf_counter()
        try:
            self._decode(np.zeros(16000, np.float32))
            self.vad.score(np.zeros(512, np.float32))
            self.vad.reset()
        except Exception:
            self.close()
            raise AudioError("stt_warmup_failed") from None
        self.warmup_s = time.perf_counter() - started
        self.lock = threading.Lock()

    def _decode(self, audio: Samples) -> tuple[str, list[dict[str, Any]]]:
        segments, _ = self.model.transcribe(
            audio,
            language="fr",
            task="transcribe",
            beam_size=self.settings.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            compression_ratio_threshold=None,
            log_prob_threshold=None,
            no_speech_threshold=None,
            word_timestamps=False,
            max_new_tokens=440,  # Leave room for the decoder's fixed language/task prompt.
        )
        rows = []
        with contextlib.closing(segments):
            for segment in segments:
                rows.append(
                    {
                        "text": segment.text,
                        "start_s": segment.start,
                        "end_s": segment.end,
                        "avg_logprob": segment.avg_logprob,
                        "no_speech_prob": segment.no_speech_prob,
                    }
                )
        return " ".join(row["text"].strip() for row in rows).strip(), rows

    def transcribe(self, audio: Samples) -> dict[str, Any]:
        if (
            audio.ndim != 1
            or len(audio) > self.settings.max_input_s * 16000
            or not np.isfinite(audio).all()
        ):
            raise AudioError("invalid_stt_audio")
        if not self.lock.acquire(blocking=False):
            raise AudioError("one_stt_request_at_a_time")
        started = time.perf_counter()
        try:
            utterances = segment_audio(audio, self.vad, self.settings)
            outputs: list[dict[str, Any]] = []
            inference = 0.0
            for utterance in utterances:
                if utterance.reason == "max_duration":
                    outputs.append(
                        {
                            "text": "",
                            "accepted": False,
                            "reason": "utterance_truncated",
                            "timing": utterance.timing(),
                            "inference_s": 0,
                            "segments": [],
                        }
                    )
                    continue
                before = time.perf_counter()
                text, segments = self._decode(utterance.audio)
                elapsed = time.perf_counter() - before
                inference += elapsed
                accepted, reason = accept_text(text)
                outputs.append(
                    {
                        "text": text,
                        "accepted": accepted,
                        "reason": reason,
                        "segments": segments,
                        "timing": utterance.timing(),
                        "inference_s": elapsed,
                        "stt_queue_wait_s": None,
                    }
                )
            text = " ".join(row["text"] for row in outputs if row["accepted"])
            truncated = any(row["reason"] == "utterance_truncated" for row in outputs)
            if truncated:
                text = ""
            accepted = bool(text)
            reason = (
                "speech_transcribed"
                if accepted
                else ("no_silero_speech" if not utterances else "no_accepted_utterance")
            )
            if truncated:
                reason = "utterance_truncated"
            return {
                "raw_text": " ".join(row["text"] for row in outputs),
                "text": text,
                "accepted": accepted,
                "reason": reason,
                "utterances": outputs,
                "duration_s": len(audio) / 16000,
                "inference_s": inference,
                "processing_s": time.perf_counter() - started,
                "hardware_latency": "NOT_RUN",
                "avg_logprob_interpretation": "mean_log_probability_not_percentage",
            }
        finally:
            self.lock.release()

    def close(self) -> None:
        self.model.model.unload_model()
        if self.model.model.model_is_loaded:
            raise AudioError("stt_model_did_not_unload")


def load_corpus(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise AudioError("corpus_manifest_missing_or_large")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("items") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= 200
    ):
        raise AudioError("invalid_corpus_manifest")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in ids:
            raise AudioError("invalid_corpus_item")
        ids.add(row["id"])
        if not isinstance(row.get("reference"), str) or len(row["reference"]) > 10000:
            raise AudioError("human_reference_required")
        if row.get("source") in {"human", "synthetic"} and not row["reference"].strip():
            raise AudioError("human_reference_required")
        if row.get("condition") not in {
            "quiet",
            "office_noise",
            "silence",
            "synthetic_noise",
        } or row.get("split") not in {"dev", "holdout"}:
            raise AudioError("invalid_corpus_condition_or_split")
        if row.get("source") not in {"human", "synthetic", "synthetic_noise", "silence"}:
            raise AudioError("invalid_corpus_provenance")
        file = row.get("file")
        if not isinstance(file, str) or not file or "://" in file or "\x00" in file:
            raise AudioError("invalid_corpus_audio_path")
        row["path"] = (path.parent / file).resolve()
        terms = row.get("critical_terms", [])
        if (
            not isinstance(terms, list)
            or len(terms) > 50
            or not all(isinstance(t, str) and t.strip() for t in terms)
        ):
            raise AudioError("invalid_critical_terms")
    return rows


def benchmark(
    model: Path, vad: Path, settings: Speech, corpus: list[dict[str, Any]], repeats: int
) -> dict[str, Any]:
    if not 1 <= repeats <= 5:
        raise AudioError("invalid_repeat_count")
    engine = Recognizer(model, vad, settings)
    rows = []
    hashes = {}
    try:
        for item in corpus:
            for repeat in range(repeats):
                try:
                    before = fingerprint(item["path"])
                    audio = read_wav(item["path"], settings.max_input_s)
                    result = engine.transcribe(audio)
                    if fingerprint(item["path"]) != before:
                        raise AudioError("corpus_audio_changed")
                    hashes[item["id"]] = before
                    text = result["text"]
                    missing = critical_errors(
                        item["reference"], text, item.get("critical_terms", [])
                    )
                    rows.append(
                        {
                            "id": item["id"],
                            "repeat": repeat + 1,
                            "status": "PASS",
                            "condition": item["condition"],
                            "split": item["split"],
                            "source": item["source"],
                            "reference": item["reference"],
                            "critical_errors": missing,
                            "lost_phrase": bool(item["reference"].strip())
                            and not result["accepted"],
                            **result,
                            **word_errors(item["reference"], text),
                        }
                    )
                except Exception:
                    rows.append(
                        {
                            "id": item["id"],
                            "repeat": repeat + 1,
                            "status": "FAIL",
                            "condition": item["condition"],
                            "split": item["split"],
                            "source": item["source"],
                            "lost_phrase": bool(item["reference"].strip()),
                            "reason": "sample_failed",
                            **word_errors(item["reference"], ""),
                        }
                    )
        summary = summarize(rows)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "backend": engine.backend,
            "load_s": engine.load_s,
            "warmup_s": engine.warmup_s,
            "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
            "corpus_files": len(corpus),
            "attempts": len(rows),
            "audio_fingerprints": hashes,
            "normalization": "NFKC_casefold_punctuation_spaces_keep_accents_digits",
            "summary": summary,
            "rows": rows,
        }
    finally:
        engine.close()
        del engine
        gc.collect()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    for key in sorted({(r["condition"], r["split"]) for r in rows}):
        subset = [r for r in rows if (r["condition"], r["split"]) == key]
        latencies = sorted(r["inference_s"] for r in subset if r["status"] == "PASS")
        reference_words = sum(r["reference_words"] for r in subset)
        groups["/".join(key)] = {
            "attempts": len(subset),
            "failures": sum(r["status"] == "FAIL" for r in subset),
            "wer": sum(r["errors"] for r in subset) / reference_words if reference_words else None,
            "reference_words": reference_words,
            "critical_errors": sum(len(r.get("critical_errors", [])) for r in subset),
            "lost_phrases": sum(r["lost_phrase"] for r in subset),
            "invented_outputs": sum(
                bool(r.get("text"))
                for r in subset
                if r.get("source") in {"silence", "synthetic_noise"}
            ),
            "inference_p50_s": statistics.median(latencies) if latencies else None,
            "inference_p95_s": latencies[math.ceil(len(latencies) * 0.95) - 1]
            if latencies
            else None,
            "percentile_method": "nearest_rank; no_hardware_latency",
        }
    return groups


def select_candidate(candidates: dict[str, Any]) -> dict[str, Any]:
    """Qualification recommendation only; never changes the production model."""
    for name in ("small", "turbo"):
        groups = candidates[name]["summary"]
        quiet = [v for k, v in groups.items() if k.startswith("quiet/")]
        if (
            quiet
            and all(g["wer"] is not None and g["wer"] <= 0.05 for g in quiet)
            and all(
                not any(
                    g[k]
                    for k in ("failures", "critical_errors", "invented_outputs", "lost_phrases")
                )
                for g in groups.values()
            )
        ):
            return {
                "status": "PROVISIONAL",
                "model": name,
                "scope": "observed_corpus_only",
                "latency_acceptance": "REVIEW_REQUIRED",
            }
    return {"status": "NO_ACCEPTABLE_STT", "model": None, "scope": "observed_corpus_only"}
