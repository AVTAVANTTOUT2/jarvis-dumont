"""Subprocess fixture; no engines, hardware or network required."""

import json
import struct
import sys
import time

HEADER = struct.Struct(">4sII")


def frame(tag, request_id, payload=b""):
    sys.stdout.buffer.write(HEADER.pack(tag, request_id, len(payload)) + payload)
    sys.stdout.buffer.flush()


mode = sys.argv[1]
if mode == "dead_start":
    raise SystemExit(1)
frame(b"LOAD", 0, b'{"load_s":0}')
if mode == "empty_warmup":
    frame(b"RDY!", 0, b'{"sample_rate":24000,"channels":1,"warmup_pcm_bytes":0}')
    raise SystemExit(0)
if mode == "startup_timeout":
    time.sleep(10)
frame(b"RDY!", 0, b'{"sample_rate":24000,"channels":1,"warmup_pcm_bytes":2}')
for line in sys.stdin.buffer:
    request = json.loads(line)
    request_id = request["id"]
    if mode == "stderr":
        sys.stderr.buffer.write(b"x" * 200_000)
        sys.stderr.buffer.flush()
    if mode == "dead":
        raise SystemExit(1)
    if mode == "large":
        sys.stdout.buffer.write(HEADER.pack(b"PCM!", request_id, 256 * 1024 + 1))
        sys.stdout.buffer.flush()
        time.sleep(10)
    if mode == "truncated":
        sys.stdout.buffer.write(HEADER.pack(b"PCM!", request_id, 100) + b"short")
        sys.stdout.buffer.flush()
        raise SystemExit(1)
    if mode == "wrong_id":
        frame(b"PCM!", request_id + 1, b"xx")
        continue
    if mode == "empty":
        frame(b"END!", request_id, b"{}")
        continue
    for number in range(5):
        # Distinct low-amplitude samples preserve weak tails and request identity.
        frame(b"PCM!", request_id, struct.pack("<h", request_id * 10 + number))
        if mode == "stalled" and number == 0:
            time.sleep(10)
        time.sleep(0.02)
    frame(b"PCM!", request_id)  # A final empty chunk must not erase the weak tail.
    frame(b"END!", request_id, b'{"pcm_bytes":12}' if mode == "bad_count" else b'{"pcm_bytes":10}')
