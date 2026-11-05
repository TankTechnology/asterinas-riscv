#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Status: manual -- guest-side DHCP probe. See tools/riscv/LAYOUT.md.
"""Minimal DHCP client, enough to learn which network the wired port is on.

The image never ships a DHCP client (the board normally boots with a static
`asterinas.net=` address). When the campus switch moves the port into a
quarantine VLAN that address is meaningless, so this script asks for a lease
and reports what came back: the offered address, the router and the server.

    dhcp_probe.py [interface]
"""

import random
import socket
import struct
import sys
import time

SERVER_PORT = 67
CLIENT_PORT = 68

MAGIC = b"\x63\x82\x53\x63"

OPT_SUBNET = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_SERVER_ID = 54
OPT_MESSAGE_TYPE = 53

DISCOVER, OFFER, REQUEST, ACK = 1, 2, 3, 5


def build(xid: bytes, mac: bytes, msg_type: int, extra=b"", requested=None) -> bytes:
    packet = struct.pack("!BBBBIHH", 1, 1, 6, 0, int.from_bytes(xid, "big"), 0, 0)
    packet += b"\x00" * 16                    # ciaddr
    packet += b"\x00" * 4                     # yiaddr
    packet += b"\x00" * 4                     # siaddr
    packet += b"\x00" * 4                     # giaddr
    packet += mac + b"\x00" * 10              # chaddr
    packet += b"\x00" * 192                   # sname + file
    packet += MAGIC
    packet += bytes([OPT_MESSAGE_TYPE, 1, msg_type])
    if requested is not None:
        packet += bytes([50, 4]) + socket.inet_aton(requested)
    packet += extra
    packet += b"\xff"
    return packet


def parse(data: bytes) -> dict:
    fields = {}
    fields["yiaddr"] = socket.inet_ntoa(data[16:20])
    options = data[240:]
    index = 0
    while index < len(options):
        tag = options[index]
        if tag == 255:
            break
        if tag == 0:
            index += 1
            continue
        length = options[index + 1]
        value = options[index + 2:index + 2 + length]
        if tag == OPT_MESSAGE_TYPE and length == 1:
            fields["type"] = value[0]
        elif tag == OPT_SERVER_ID and length == 4:
            fields["server"] = socket.inet_ntoa(value)
        elif tag == OPT_SUBNET and length == 4:
            fields["mask"] = socket.inet_ntoa(value)
        elif tag == OPT_ROUTER and length >= 4:
            fields["router"] = socket.inet_ntoa(value[:4])
        elif tag == OPT_DNS and length >= 4:
            fields["dns"] = ", ".join(socket.inet_ntoa(value[i:i + 4]) for i in range(0, length - 3, 4))
        index += 2 + length
    return fields


def main() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", CLIENT_PORT))
    sock.settimeout(5)

    mac = b"\x00\x48\x54\x71\x00\x47"
    xid = bytes(random.getrandbits(8) for _ in range(4))

    print("DISCOVER", flush=True)
    discover = build(xid, mac, DISCOVER, extra=bytes([55, 4, OPT_SUBNET, OPT_ROUTER, OPT_DNS, OPT_SERVER_ID]))
    offer = None
    for _ in range(3):
        sock.sendto(discover, ("255.255.255.255", SERVER_PORT))
        try:
            data, address = sock.recvfrom(1024)
        except socket.timeout:
            print("  no offer yet", flush=True)
            continue
        fields = parse(data)
        if fields.get("type") == OFFER:
            offer = fields
            print(f"OFFER from {address[0]}: {fields}", flush=True)
            break

    if offer is None:
        print("NO_OFFER", flush=True)
        return 1

    print("REQUEST", flush=True)
    request = build(xid, mac, REQUEST,
                    extra=bytes([OPT_SERVER_ID, 4]) + socket.inet_aton(offer["server"]),
                    requested=offer["yiaddr"])
    for _ in range(3):
        sock.sendto(request, ("255.255.255.255", SERVER_PORT))
        try:
            data, address = sock.recvfrom(1024)
        except socket.timeout:
            continue
        fields = parse(data)
        if fields.get("type") == ACK:
            print(f"ACK from {address[0]}: {fields}", flush=True)
            return 0
    print("NO_ACK", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
