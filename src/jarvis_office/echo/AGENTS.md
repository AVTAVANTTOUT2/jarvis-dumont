# Parallel Echo scope

User-authorized ECHO-01 exception to the parent phase-06 deferral: implement only isolated Echo gateway modules and dedicated tests. Keep Android in its separate repository. Do not modify shared voice/runtime/service files or merge this branch into release without coordination.

ECHO-02C explicitly authorizes the minimal shared VoiceLoop/audio-source adaptation in its isolated stacked worktree. Keep the selected STT, VAD policy, DeepSeek model, TTS engine, release current and main LaunchAgent unchanged. No copied VoiceLoop. Echo requires an explicit Ethernet endpoint and fails closed if that path disappears. The qualified profile is PCM16 mono, 16 kHz up / 48 kHz down, 20 ms, original gain, MEDIA playback, 100 ms prefill; do not tune it to mask Wi-Fi jitter. The independent acoustic capture campaign owns its processes and hardware windows.

No duplicate AI pipeline, secret logging, passive transcripts in logs, disk audio, LAN scan or wildcard bind. Authenticate control and audio before accepting PCM. A lost control channel invalidates audio and all late callbacks. Bounded queues and exact turn/session ownership are mandatory. Hardware qualification cannot be inferred from tests.

On the audited June 2026 checkers firmware, `dumpsys media.audio_flinger` crashes the vendor audio HAL in Device::debug. Do not invoke it or full Android bugreports in qualification. Use logcat, AudioService and the debug EchoService metadata dump; do not alter firmware to pass this phase.
