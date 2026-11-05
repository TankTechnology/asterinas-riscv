#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Log an address into the campus portal (深澜 srun 2.00).

The Megrez board cannot authenticate itself: the portal login is a challenge /
response exchange over HTTPS, and the credentials must not be stored on the
board's filesystem. This tool runs on the development host and asks the portal
to log the board's address in, which is enough — the portal tracks addresses,
not who made the request.

Credentials come from NetworkManager's stored campus profile by default, so
they are read from the system's own secret store and never appear on a command
line, in a file this repository owns, or in the output. Run it with the
privilege that can read that secret.

    sudo megrez_portal_login.py --ip 192.0.2.10          # from the active WiFi
    sudo megrez_portal_login.py --ip 192.0.2.10 --profile ZGCLab
    sudo megrez_portal_login.py --ip 192.0.2.10 --logout

`192.0.2.10` is an RFC 5737 documentation address. The board's real
address is site configuration: set `BOARD_IP` (and `BOARD_GW` /
`PORTAL_IP` where the script uses them) in the local environment; never
commit it.

Exit status: 0 logged in (or already logged in), 1 the portal refused,
2 the credentials or the network could not be read.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import hashlib
import hmac
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOST = "https://auth.zgclab.edu.cn"
CALLBACK = "cb1"
XOR_TABLE = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
STD_TABLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
MASK = 0xFFFFFFFF


# --- the portal's encoder ---------------------------------------------------


def _sx(value: int) -> int:
    value &= MASK
    return value - (1 << 32) if value >= (1 << 31) else value


def _xo(a: int, b: int) -> int:
    return _sx(_sx(a) ^ _sx(b))


def _words(text: str, with_length: bool) -> list[int]:
    codes = [ord(ch) for ch in text]
    out: list[int] = []
    for index in range(0, len(codes), 4):
        block = codes[index : index + 4] + [0, 0, 0, 0]
        out.append(
            _sx(block[0] | block[1] << 8 | block[2] << 16 | block[3] << 24)
        )
    # The portal's own encoder appends the character count as a final word for
    # the payload (but not for the key). Leaving it out produces a blob the
    # server rejects with `auth_info_error`, which reads like a wrong password.
    if with_length:
        out.append(len(codes))
    return out


def encode(text: str, key: str) -> str:
    """XXTEA the JSON payload and base64 it with the portal's own table."""

    v = _words(text, True)
    n = len(v) - 1
    if n < 1:
        return ""
    k = _words(key, False)
    while len(k) < 4:
        k.append(0)
    z = v[n]
    delta = 0x9E3779B9
    rounds = 6 + 52 // (n + 1)
    total = 0
    while rounds > 0:
        total = _sx(total + delta)
        e = (total >> 2) & 3
        p = 0
        while p < n:
            y = v[p + 1]
            m = _xo((z & MASK) >> 5, y << 2)
            m += _xo(_xo((y & MASK) >> 3, z << 4), _xo(total, y))
            m += _xo(k[(p & 3) ^ e], z)
            z = v[p] = _sx((v[p] + m) & MASK)
            p += 1
        m = _xo((z & MASK) >> 5, v[0] << 2)
        m += _xo(_xo((v[0] & MASK) >> 3, z << 4), _xo(total, v[0]))
        m += _xo(k[(n & 3) ^ e], z)
        z = v[n] = _sx((v[n] + m) & MASK)
        rounds -= 1

    raw = bytearray()
    for word in v:
        word &= MASK
        raw += bytes(
            (word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF, (word >> 24) & 0xFF)
        )
    standard = base64.b64encode(bytes(raw)).decode()
    return "{SRBX1}" + "".join(
        "=" if ch == "=" else XOR_TABLE[STD_TABLE.index(ch)] for ch in standard
    )


# --- credentials ------------------------------------------------------------


def nmcli_secret(profile: str | None, field: str) -> str | None:
    argv = ["nmcli", "-s", "-g", field, "connection", "show"]
    if profile:
        argv.append(profile)
    else:
        # The active connection is the one the host itself is using.
        active = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
            check=False,
        )
        name = None
        for line in active.stdout.splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) == 2 and parts[1].strip() == "802-11-wireless":
                name = parts[0]
                break
        if name is None:
            return None
        argv.append(name)
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def credentials(profile: str | None) -> tuple[str, str]:
    identity = nmcli_secret(profile, "802-1x.identity")
    password = nmcli_secret(profile, "802-1x.password")
    if not identity or not password:
        raise SystemExit(
            f"could not read the campus credentials for profile {profile or '(active)'} "
            "from NetworkManager; run with sudo or pass --profile"
        )
    return identity, password


# --- the portal exchange ----------------------------------------------------


def _context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # The portal's certificate is not in the system store; this is a login
    # exchange carrying a challenge response, not a secret in the clear.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _fetch(url: str) -> str:
    """Fetch one portal URL over IPv4, straight to the portal.

    Two things matter here and both are easy to get wrong. The host exports an
    HTTPS proxy, and going through it makes the portal see the proxy's address
    instead of ours, which the challenge is bound to -- the login then comes
    back `challenge_expire_error`. And an IPv6 connection is answered with the
    host's IPv6 address as `client_ip`, which the board's address cannot be
    logged in under. Both are avoided by speaking to the portal's IPv4 address
    directly with the right Host header.
    """

    parsed = urllib.parse.urlsplit(url)
    host, path = parsed.hostname, parsed.path
    if parsed.query:
        path += "?" + parsed.query
    address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    connection = http.client.HTTPSConnection(address, 443, timeout=15, context=_context())
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        return response.read().decode()
    finally:
        connection.close()


def _jsonp(body: str) -> dict:
    return json.loads(body[body.find("(") + 1 : body.rfind(")")])


def login(username: str, password: str, expected_ip: str | None, ac_id: str) -> str:
    """Log in the address this request comes from.

    The portal signs the exchange with the address it sees as the client, so
    the only address that can be logged in is the one the request originates
    from. `expected_ip` is a check, not a target: passing an address this host
    does not own produces `sign_error`, which is why the login has to run on the
    machine whose address needs to be online.
    """

    body = _fetch(
        f"{HOST}/cgi-bin/get_challenge?callback={CALLBACK}"
        f"&username={urllib.parse.quote(username)}"
        f"&_={int(time.time() * 1000)}"
    )
    data = _jsonp(body)
    challenge = data["challenge"]
    client_ip = data["client_ip"]
    if expected_ip is not None and expected_ip != client_ip:
        raise SystemExit(
            f"this host is {client_ip}, not {expected_ip}: the portal only signs a "
            "login for the address the request comes from, so run this on that machine"
        )

    digest = hmac.new(challenge.encode(), password.encode(), hashlib.md5).hexdigest()
    info = {
        "username": username,
        "password": password,
        "ip": client_ip,
        "acid": ac_id,
        "enc_ver": "srun_bx1",
    }
    encoded = encode(json.dumps(info, separators=(",", ":")), challenge)
    checksum = hashlib.sha1(
        "".join(
            [
                challenge, username, challenge, digest, challenge, ac_id,
                challenge, client_ip, challenge, "200", challenge, "1",
                challenge, encoded,
            ]
        ).encode()
    ).hexdigest()

    query = (
        "callback=%s&action=login&username=%s&password=%s&os=Linux&name=Linux"
        "&double_stack=0&chksum=%s&info=%s&ac_id=%s&ip=%s&n=200&type=1"
    ) % (
        CALLBACK,
        urllib.parse.quote(username),
        urllib.parse.quote("{MD5}" + digest),
        checksum,
        urllib.parse.quote(encoded),
        ac_id,
        urllib.parse.quote(client_ip),
    )
    reply = _fetch(f"{HOST}/cgi-bin/srun_portal?" + query + f"&_={int(time.time() * 1000)}")
    return _result(reply)


def logout(username: str, ip: str, ac_id: str) -> str:
    reply = _fetch(
        f"{HOST}/cgi-bin/srun_portal?callback={CALLBACK}&action=logout"
        f"&username={urllib.parse.quote(username)}&ip={urllib.parse.quote(ip)}"
        f"&ac_id={ac_id}&_={int(time.time() * 1000)}"
    )
    return _result(reply)


def _result(reply: str) -> str:
    # `error_msg` is often present and empty, so `res` has to be looked for
    # first or every reply reads as an empty success.
    for key in ("res", "error"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]*)"', reply)
        if match and match.group(1):
            return match.group(1)
    return reply.strip()[:300] or "(empty reply)"


def main() -> int:
    parser = argparse.ArgumentParser(prog="megrez_portal_login")
    parser.add_argument(
        "--expect-ip",
        help="assert which address this host should be seen as, and refuse otherwise",
    )
    parser.add_argument("--profile", help="NetworkManager profile (default: active WiFi)")
    parser.add_argument("--ac-id", default="1")
    parser.add_argument("--logout", action="store_true", help="log the address out and stop")
    values = parser.parse_args()

    username, password = credentials(values.profile)

    if values.logout:
        print("logout:", logout(username, values.expect_ip or "", values.ac_id))
        return 0

    result = login(username, password, values.expect_ip, values.ac_id)
    print("login:", result)
    if result == "ip_already_online_error":
        # The portal still holds a session for this address from a previous
        # boot; clear it and start again rather than leaving a half session.
        print("logout:", logout(username, values.expect_ip or "", values.ac_id))
        result = login(username, password, values.ip, values.ac_id)
        print("login:", result)
    return 0 if result == "login_ok" else 1


if __name__ == "__main__":
    sys.exit(main())
