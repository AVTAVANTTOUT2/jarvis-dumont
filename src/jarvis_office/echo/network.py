"""Explicit Mac interface path; never changes host routes or interface settings."""

import ipaddress
import re
import socket
import subprocess
import sys


def require_ethernet(interface: str, address: str) -> int:
    if sys.platform != "darwin" or not re.fullmatch(r"en[0-9]+", interface):
        raise ValueError("NETWORK_PATH_UNAVAILABLE")
    if ipaddress.ip_address(address).version != 4:
        raise ValueError("NETWORK_PATH_UNAVAILABLE")
    try:
        result = subprocess.run(
            ["/sbin/ifconfig", interface], capture_output=True, text=True, timeout=2, check=True
        ).stdout
        flags = result.splitlines()[0]
        addresses = re.findall(r"\binet (\S+)", result)
        if (
            "UP" not in flags
            or "RUNNING" not in flags
            or "status: active" not in result
            or address not in addresses
        ):
            raise ValueError("NETWORK_PATH_UNAVAILABLE")
        return socket.if_nametoindex(interface)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("NETWORK_PATH_UNAVAILABLE") from None


def ethernet_listener(interface: str, address: str, port: int) -> socket.socket:
    index = require_ethernet(interface, address)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Darwin netinet/in.h: inherited by accepted sockets. No TCP tuning.
        listener.setsockopt(socket.IPPROTO_IP, 25, index)  # IP_BOUND_IF
        listener.bind((address, port))
        listener.listen(8)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise
