# ECHO-02C live pipeline

The Ethernet A/B reference passed 70 repeated clips without a during-data underrun, against nine events in ten Mac Wi-Fi clips. Ethernet is a transport candidate for the frozen profile; the precise Wi-Fi component is not identified. Physical live qualification remains pending until the installed APK, human turns and stability run are observed. `REMOTE_STT_QUALIFIED=NO`, `DEV_INSECURE_LAN`, `NOT_SECURE_RELEASE`.

`VoiceLoop` is reused with `RemoteEchoAudio`. `AudioClient` retains the existing offline worker and selected `Recognizer`; an inherited anonymous binary pipe supplies `RemotePipeIngress` to the same `AudioEngine.listen` normalization/VAD/STT code. `LocalMacIngress` retains native local capture. The remote worker refuses local output operations. The normal local output path remains `AudioClient` / `Playback`.

`RemoteEchoEgress` adapts streamed Qwen PCM to PCM16 mono 48 kHz and sends 20 ms binary packets. All linguistic segments in one assistant turn share one stream. Its existing 24 kB source credit is an actual bound, including PCM in flight (500 ms at the configured 24 kHz TTS source). The sender drains independently so synthesis can start the next segment before the preceding one finishes playing. A producer pause rebases the pacing clock; elapsed silence is never repaid as a burst of packets. Source chunks, pipe, ingress queue and fractional downlink frame are bounded; there is no WAV, replay or whole-response accumulation. Playback acknowledgements, session identity, uplink generation and turn identity are checked before delivery. Segment confirmation is conservative: only a fully drained remote response confirms its segments.

The gateway alone selects ACTIVE/PASSIVE. Non-addressed ACTIVE speech is discarded; non-addressed PASSIVE speech enters the bounded RAM context with no DeepSeek call. Recent passive context is attached as a separate, explicitly labelled, ephemeral user-data message only on an addressed turn, and is excluded from retained question history. Clear purges that buffer. No passive transcript enters normal metadata reports.

During response preparation/playback the ingress token is withdrawn; the Echo closes capture on stream start. After playback acknowledgement, a separate Echo acoustic tail (initially 600 ms, not yet physically qualified) precedes a fresh microphone stream and VAD generation. OFF and disconnect invalidate authorization; reconnect requires a visible user activation. Individual capture windows remain bounded to five minutes and renew only within the same already authorized live connection, with the existing workers and remaining turn cap. The qualification profile allows at most five addressed turns per mode activation. One Office instance lock excludes a simultaneous local microphone conversation. Context clear also invalidates an in-flight passive utterance from the previous context generation.

## Private configuration and run

Use the existing mode-0600 TOML pairing configuration. Add explicit fields under `[echo]`:

```toml
network_path = "ETHERNET_REQUIRED"
network_interface = "en0"
live_pipeline = true
playback_prefill_ms = 100
acoustic_tail_ms = 600
voice_config = "/PRIVATE/voice.toml"
api_budget_file = "/PRIVATE/echo-api-budget.json"
report_file = "/PRIVATE/echo-live-status.json"
```

`bind` must be the actual Ethernet IPv4 address, with a dedicated explicit port; never an implicit wildcard. The private voice config preserves the selected model/assets and VAD/STT settings, selects 16 kHz remote input and installed worker Python paths, and keeps the bounded arm policy. Do not commit that config or its addresses, pairing, reports, speech or model assets.

The listener uses Darwin `IP_BOUND_IF`, checks the inherited interface on accepted sockets and monitors link/address availability. Loss emits `NETWORK_PATH_UNAVAILABLE`, closes the owned sessions/listener and never switches to Wi-Fi. It does not alter global macOS routing or Echo Wi-Fi. Before each physical window verify the interface/link/address and actual socket endpoint again.

Run an installed non-editable wheel, with matching code signatures in main/STT/TTS environments, using `python -I -B -m jarvis_office.echo.gateway --config /PRIVATE/echo.toml`. Preserve each existing worker interpreter/dependency set and model bundle. The release pointer and main LaunchAgent are untouched. SIGINT/SIGTERM close owned sessions and workers. Startup never opens a microphone; device mode starts OFF. A separate private, persistent ECHO-02C request budget reserves before HTTP, at most eight real attempts across restarts, with no automatic retry or reset; phase 06's budget stays unchanged.

Protocol 1 requires explicit `live_turns` capability on both sides. Older clients receive `PROTOCOL_INCOMPATIBLE`; there is no silent live fallback. MEDIA usage and 100 ms prefill are announced and checked by the development APK. AudioTrack allocation, gain, frame size and TCP settings are unchanged from the A/B reference.

## Validation and limits

Provider contract update observed on 2026-09-10: the configured `deepseek-v4-flash` request receives the canonical `deepseek-flash` response identifier. The official [model documentation](https://api-docs.deepseek.com/quick_start/pricing) states that the legacy Flash model is retired and the alias is served by Flash V4.1. The parser accepts that exact canonical identifier alongside the former Flash response names, records the actual returned name, and still rejects Pro/unknown names or a model change within a turn. The configured request, non-thinking policy, STT, TTS and audio profile are unchanged. Functional reports must disclose the provider's model transition rather than claim the retired backend was preserved.

CI uses fake HTTP/TTS/recognizer boundaries and no models, real API, LAN or device. `python -m unittest discover -s tests` covers remote PCM through the existing AudioEngine, repeated VoiceLoop turns, single-stream response routing, passive context and zero non-addressed requests, stale callbacks/PCM, network loss, queue generations/age, and an independent request ceiling. Run Ruff and mypy as usual, plus the Echo requirements.

Numeric private reports separate server pipeline timestamps, server remote send, Android local durations and RTT. A remote socket send is never labelled an AudioTrack write or acoustic onset. During-data and drain/teardown underruns require the full physical traces; heartbeat clock estimates alone do not qualify the inter-machine latency budget.

Live pacing RCA: a measured 391 ms producer gap was followed by sub-millisecond sends, filling the Android queue and aborting playback. Rebasing prevents that burst, while the bounded source credit permits synthesis/playback overlap. A subsequent real two-segment TTS test preserved all 560640 PCM bytes through AudioTrack, with matching CRC, zero during-data underruns and a maximum server-send gap of 21.9 ms. Android prefill/allocation, PCM profile and gain were unchanged. This individual test does not qualify 30-minute stability or the p95 latency budget: PCM-ready-to-send measurements must include source queue residence. Earlier controlled replays also exposed one 128 ms receive gap despite regular server sends; retain that observation rather than declaring Ethernet infallible.

Live acoustic confirmations, at least two actual turns without restart, live passive recall/clear, self-echo exclusion, reconnect OFF and 30-minute stability remain physical gates. A concurrent `paired_capture_server.py` or acoustic collector is not an abandoned process; wait for its owner or use an isolated endpoint, and never seize the device while its capture is armed.

Merge coordination: shared adaptation touches `voice.py`, `audio_worker.py`, `audio_client.py`, `credentials.py` and the optional ephemeral-context/request-budget parameters in `deepseek.py`. These may overlap later release/cancellation work. No STT research branch has been imported, and no cancellation-race fix is included.
