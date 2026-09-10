# ECHO-02D — buffering RCA, blocked

The Ethernet reference reproduced one during-data underrun in 40 physical
streams. At PCM16 mono 48 kHz, transport is 96,000 bytes/s and each 20 ms packet
contains 1,920 PCM bytes. The source credit is 24,000 bytes at the actual internal
24 kHz rate: 500 ms, including the sender's in-flight input. It is not 500 ms of
startup latency or 500 ms of PCM already available on Android.

The Echo physically prefills 4,800 frames (100 ms) before calling `play()`. Its
20,772-byte allocation can hold 216.375 ms, but the reference constrains usable
space to 4,800 frames. The failing reference trace had only 64 ms left before a
205.49 ms callback gap. UI, kernel socket capacity, native allocation and actual
accepted PCM must not be conflated.

A bounded placement candidate kept startup prefill unchanged, sent up to 240 ms
ahead with 2 ms refill spacing, and expanded usable AudioTrack space after the
playback head advanced. The local 100-stream burst probe passed, including pauses
up to 200 ms. The 100-stream Ethernet campaign still had two during-data
underruns at separate natural gaps around 0.5 seconds. A second candidate held a
Wi-Fi high-performance lock only while playing. An underrun still occurred with 84 ms
available before a 103.79 ms gap from a real-time source. Testing stopped after
44 completed streams under the agreed two-candidate limit.

The rejected changes are **not retained** in the gateway, settings or playback
profile. This PR only extends the existing qualification report and its tests.
`buffer_observations()` uses Android-local receive times and written/head counts;
it reports missing observations as unknown, and does not subtract a Mac clock.
The reserve associated with a gap is the preceding write sample, not an invented
exact gap-start value. A gap alone does not imply an underrun.

The read-only TCP counters showed retransmissions. Reserve sampling continued
while callbacks stalled, so a global stop of the playback thread was not observed.
Neither a specific Wi-Fi driver/AP nor a universal GC cause has been isolated.
Socket send-buffer occupancy includes unacknowledged bytes; it cannot alone
measure PCM already acknowledged by the kernel but not dispatched by OkHttp.

Verdict: **ECHO_02D_BLOCKED / PLAYBACK_RELIABILITY=FAIL / LATENCY_QUALIFIED=NO**.
Budget remained 9/10. PASSIVE, real STT/TTS, local TTS qualification, and a new
30-minute stability run were not performed after the failed synthetic gate.
All detailed hardware traces and candidate patches remain private. Next action:
`OWNER_DEVICE_ARCHITECTURE_DECISION`. No merge or secure-release claim.
