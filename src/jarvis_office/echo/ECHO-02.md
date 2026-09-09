# ECHO-02 diagnostic checkpoint

**ECHO_02_BLOCKED / DEV_INSECURE_LAN / NOT_SECURE_RELEASE.** Human acoustic confirmation and reproducible downlink stability remain missing. Zero DeepSeek calls; no VoiceLoop integration or changes to model engines, shared voice code, cancellation work, release or the main LaunchAgent.

This branch adds bounded synthetic streams up to 30 seconds, explicit protocol-1 prefill capability negotiation, completed-stream accounting, and physical diagnostics. `tests/qualify_echo_device.py` supports `continuous` (one 30-second stream), `matrix` (ten 3-second streams at one selected prefill) and `micro_replay` (owner-ready microphone capture and server resampling/replay, RAM only, maximum eight seconds). Its underrun classifier separates data-window increments from final drain and pause/flush teardown; missing/reset/incomplete traces fail classification. Android remains only a PCM terminal.

Use the same existing private mode-0600 gateway config and an explicit ADB address:

```sh
.venv/bin/python tests/qualify_echo_device.py --device EXPLICIT_ADB_ADDRESS \
  --config /PRIVATE/echo.toml --report /PRIVATE/continuous.json \
  --phase continuous --prefill-ms 100
```

Other prefill choices: 20, 40, 60, 80, 120, 140 ms. Stop the dedicated Echo listener before qualification; the harness owns/stops its temporary listener and Android process. Never stop the main Jarvis service. `--phase micro_replay --seconds 6` waits for the owner to tap ACTIF and say “Jarvis, test du microphone Echo Show.”; it forbids `--capture-file`. No STT, TTS or cloud API is involved. Mutable capture and replay buffers are cleared on success/failure; no audio file is needed. The harness emits capture/replay start markers containing no audio or transcript.

Real-device results: 40/60/80 ms each failed ten-clip stability; 100 ms initially passed ten clips and a continuous 30-second stream, then failed a ten-clip repeat with three during-data increments. 120 and 140 ms each retained one increment over ten clips. At one failure, consecutive Mac sends were 19 ms apart but Android receipt was 148 ms apart. This implicates network/receiver delay but does not identify the exact driver/scheduling cause. Wi-Fi lock trials were ineffective and removed. No prefill is qualified; the default remains 20 ms. End-of-stream counter increments are tracked separately and never equated with audible glitches.

On the final 140 ms matrix: 1,500 frames, ten completed streams, no transport failure, maximum queue four frames. Android receive→write-start p50/p95 2.0/44.3 ms; Mac PCM-ready→send p95 <0.01 ms. Last-512-frame inter-clock estimate 7/73 ms p50/p95 with final offset uncertainty ~5 ms, **ESTIMATED only**. RTT 32/199 ms p50/p95 over 15 samples. Neither the added round-trip budget nor audible startup delay is qualified. The final known uplink probe passed 50/50 PCM frames without opening AudioRecord.

The owner missed the first automatic microphone window. A second trial, triggered by the owner with ACTIF, received 288 frames / 5.76 seconds, RMS −44.9 dBFS, peak 1,600/32,768 and zero clipped samples. The server replayed 552,960 PCM bytes after resampling; capture/replay RAM was cleared. Playback completed all frames, with zero during-data underruns and one pause/flush teardown increment. No audio file or transcript was created. **The owner heard no replay sound:** AUDIO_ECHO=non for this trial, MIC_ECHO not qualified, ECHO_ARTIFACT undetermined. The owner also answered no sound for the technically completed five-bip recheck.

Route comparison now accepts `--playback-usage 1|2` (MEDIA / VOICE_COMMUNICATION). A live mixer probe observed speaker routing with −23 dB voice-stream gain, but the continuous run failed with Android `AUDIO_PLAYBACK_STOP_TIMEOUT`; this is unresolved, not an acoustic PASS. Intrusive mixer polling was removed. An instrumentation crash with an empty JSON report now remains a failed result without a secondary JSON parser exception.

Timed manual speaker windows were missed and removed. Android code 4 provides a persistent debug main-screen test button, with microphone OFF. The dedicated Echo LaunchAgent now uses an installed ECHO-02 wheel and a separate private authenticated `test_audio=true`, `playback_prefill_ms=100` diagnostic configuration. The prior Echo runtime/config/plist are preserved; the main Jarvis LaunchAgent is untouched. No owner deadline or active instrumentation remains. MEDIA audibility is still pending; 100 ms is diagnostic, not a qualified default.

`--replay-gain 2|4|8` is available only for micro_replay, defaults to 1 and limits amplified output to a peak of 8,192 PCM units. This explicit server-side diagnostic gain does not alter Android AGC or system volume. It is unit-tested but amplified hardware replay has not yet run, pending the speaker recheck. The synthetic tone retains its original amplitude.

After the diagnostic gain/start-marker changes, full validation passes: 121 unit tests (including 17 Echo tests), Ruff check/format and mypy; no real models or cloud calls. Acoustic answers, live addressed turns, passive turquoise test, clear-after-live-context, no self-trigger and 30-minute stability remain required. Detailed sanitized matrix and APK identity are in the Android repository's `ECHO-02.md`; raw reports and credentials stay outside Git.
