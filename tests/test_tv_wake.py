from __future__ import annotations

import unittest
from unittest.mock import patch

from jarvis_office.tv.wake import WakeOnLanError, normalize_mac, send_magic_packet


class _Socket:
    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def __enter__(self) -> _Socket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def setsockopt(self, *args: int) -> None:
        self.options.append(args)

    def sendto(self, packet: bytes, address: tuple[str, int]) -> None:
        self.sent.append((packet, address))


class TvWakeTests(unittest.TestCase):
    def test_magic_packet_is_broadcast(self) -> None:
        sock = _Socket()
        with patch("jarvis_office.tv.wake.socket.socket", return_value=sock):
            send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.3.255")
        self.assertEqual(len(sock.sent), 1)
        packet, address = sock.sent[0]
        self.assertEqual(address, ("192.168.3.255", 9))
        self.assertEqual(packet, b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16)

    def test_invalid_mac_is_rejected(self) -> None:
        with self.assertRaises(WakeOnLanError):
            normalize_mac("not-a-mac")


if __name__ == "__main__":
    unittest.main()
