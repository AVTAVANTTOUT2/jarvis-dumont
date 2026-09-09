# Isolated Echo gateway

This module is an ECHO-01 **transport qualification sink**, not a second voice engine. No local Mac microphone, STT, TTS or cloud client is started. PCM is counted and released in memory. The passive buffer and address policy are tested with synthetic text, but real STT is not yet connected. Android displays TRANSPORT_ONLY explicitly.

This work is isolated from phase 06. New Echo modules/tests and one CI dependency-install step are added; VoiceLoop, AudioClient, audio worker, runtime, service and existing UI are unchanged. The shared CI step is necessary so the new tests/type checks can import the optional transport dependency. Do not merge before coordinating the shared pipeline integration.

## Run

```sh
uv sync --locked --extra chat --no-python-downloads
uv pip install --python .venv/bin/python -r src/jarvis_office/echo/requirements.txt
.venv/bin/python -m jarvis_office.echo.gateway --config /PRIVATE/echo.toml --pair echo-desk --bind EXPLICIT_LAN_IP
.venv/bin/python -m jarvis_office.echo.gateway --config /PRIVATE/echo.toml
```

Pairing generates a random 256-bit secret in a new mode-0600 file; it is not printed, logged or committed. Enter it into the native Android Settings page. Device IDs are explicit user choices, not harvested hardware identifiers. The file is private TOML:

```toml
[echo]
enabled = true
bind = "EXPLICIT_PRIVATE_IP"
port = 8771
security = "DEV_INSECURE_LAN"
test_audio = false
max_audio_ms = 200
context_seconds = 1800
context_chars = 20000
context_utterances = 100

[devices]
# Provisioned by --pair; never put a real secret in this example.
```

No wildcard bind or port 8768. Configuration is mandatory. `SECURE_RELEASE` additionally requires `cert` and `key` PEM file paths. TLS uses the normal Python server context (TLS 1.2 minimum); Android also requires a certificate pin and standard platform trust. TLS deployment/hardening remains unqualified. No global trust or macOS audio settings are changed.

The `/control` and `/audio` WebSocket upgrades require device authentication. Audio also binds to a live control session. Maximum message 8192 bytes, receive queue four frames, write high-water mark 4096 bytes, no compression, timeout-bound sending. A gap or increasing stream delay ends the session. Active ownership within this transport gateway is exclusive; because no production engine is connected, it cannot open competing Blue Snowball and Echo turns.

`Session.snapshot()` exposes device, mode, connected, RTT, uplink/downlink state, passive count and current turn without transcripts. State messages carry this data to the terminal; no second web frontend is added. Ctrl+C/SIGTERM closes owned sockets/tasks and purges RAM context.

The [Android repository](https://github.com/AVTAVANTTOUT2/jarvis-office-echo) contains the complete protocol table and app/build instructions. Protocol 1 uses 48-byte big-endian headers and little-endian PCM16, 20 ms frames, random stream IDs, exact sequence checks and no replay. Acknowledged AudioTrack drainage gates diagnostic completion; no claim of human audibility follows from this acknowledgement.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -p test_echo.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src/jarvis_office/echo tests/test_echo.py tests/echo_android_smoke.py
.venv/bin/mypy src/jarvis_office/echo
```

The pinned WebSocket dependency is isolated here to avoid concurrent edits to phase 06's package/lock files. Install it explicitly before these tests and in the dedicated gateway wheel environment. A permanent development gateway must run from a non-editable wheel outside the checkout, with a separate user LaunchAgent; it must not use or restart the phase-06 service.

`tests/echo_android_smoke.py ANDROID_REPO PRIVATE_REPORT` is opt-in and only targets `emulator-5580`. It starts a temporary loopback gateway, generates ephemeral pairing, installs the debug and instrumentation APKs, uses an ADB reverse, then removes that reverse, force-stops the test app and closes the gateway. It does not validate the Echo or LAN latency.

For explicit physical qualification, install the Android APK plus instrumentation built with `-PechoQualification=true`, then:

```sh
.venv/bin/python tests/qualify_echo_device.py --device EXPLICIT_ADB_ADDRESS \
  --config /PRIVATE/echo.toml --report /PRIVATE/transport.json --phase transport
```

Phases: profile, transport (50 known PCM frames), tone (five quiet 400 ms signals), capture, passive, outage (listener unavailable for two seconds), recreate. This uses the real LAN, no ADB reverse or scan. Secrets are provisioned via stdin into ordinary app-private storage; Android encrypts/deletes the temporary pairing. Capture only computes sample counts/RMS/peak in RAM. Explicit `--capture-file /PRIVATE/test.pcm` permits at most five seconds of private diagnostic PCM; the running gateway never saves audio. The harness refuses stale results after an instrumentation crash and stops its app/listener in finally. A human must confirm audibility and prescribed speech; transport counts alone do not prove them.

The real Android 11 Echo accepts native 16 kHz mono uplink and 48 kHz mono playback. Its AudioRecord timestamps are unreliable; read-end-minus-frame estimates must be labeled. Local durations, bounded timing quantiles, synchronized estimates and clock uncertainty are separate. The Android report contains the full sanitized hardware qualification; raw inventory and device identifiers stay outside Git.

## Next integration boundary

Reuse the existing `VoiceLoop` audio-client injection and its single-turn ownership. The current `AudioClient`/worker protocol owns local capture/STT and playback; `_respond` already routes through the selected audio client, preserving turn/session checks. Implement shared LocalMicroIngress/RemoteEchoIngress and LocalMacEgress/RemoteEchoEgress at that boundary after physical transport qualification. Reuse existing Normalizer, Silero, Segmenter, Recognizer, DeepSeek turn and TTS stream; do not copy them here. Any late event must remain bound to `(device, session, turn, stream)`.

PassiveContextBuffer is RAM-only, expires by monotonic age, and evicts oldest entries on the first of time/character/count limits. PassivePolicy calls the existing address detector; unaddressed ACTIVE text is dropped, unaddressed PASSIVE text can enter the buffer, SPEAKING/OFF text is ignored. Only an addressed request can return a bounded, labeled passive excerpt (default 4000 characters / 20 utterances) for explicit insertion by the shared engine. No automatic cloud summarization or transmission exists. The synthetic tests make zero real DeepSeek calls.
