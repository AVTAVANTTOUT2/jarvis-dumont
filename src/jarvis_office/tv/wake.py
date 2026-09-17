from __future__ import annotations

import asyncio
import re
import socket

MAC_RE = re.compile(r"^[0-9a-fA-F]{12}$")


class WakeOnLanError(ValueError):
    pass


def normalize_mac(value: str) -> str:
    mac = value.replace(":", "").replace("-", "").replace(" ", "")
    if MAC_RE.fullmatch(mac) is None:
        raise WakeOnLanError("INVALID_TV_MAC")
    return mac.lower()


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    address = normalize_mac(mac)
    packet = b"\xff" * 6 + bytes.fromhex(address) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, 9))


async def wake(mac: str, broadcast: str = "255.255.255.255") -> None:
    await asyncio.to_thread(send_magic_packet, mac, broadcast)
