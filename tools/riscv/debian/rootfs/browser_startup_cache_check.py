#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail closed unless Firefox first-boot caches and identities are complete."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re


class CacheCheckError(RuntimeError):
    """A staged browser root would still require first-boot maintenance."""


EXPECTED_USERS = {
    "root": (0, 0),
    "daemon": (1, 1),
    "bin": (2, 2),
    "sys": (3, 3),
    "sync": (4, 65534),
    "games": (5, 60),
    "man": (6, 12),
    "lp": (7, 7),
    "mail": (8, 8),
    "news": (9, 9),
    "uucp": (10, 10),
    "proxy": (13, 13),
    "www-data": (33, 33),
    "backup": (34, 34),
    "list": (38, 38),
    "irc": (39, 39),
    "_apt": (42, 65534),
    "nobody": (65534, 65534),
    "systemd-network": (998, 998),
    "messagebus": (997, 997),
    "asterinas": (1000, 1000),
}
EXPECTED_GROUPS = {
    "root": 0,
    "daemon": 1,
    "bin": 2,
    "sys": 3,
    "adm": 4,
    "tty": 5,
    "disk": 6,
    "lp": 7,
    "mail": 8,
    "news": 9,
    "uucp": 10,
    "man": 12,
    "proxy": 13,
    "kmem": 15,
    "dialout": 20,
    "fax": 21,
    "voice": 22,
    "cdrom": 24,
    "floppy": 25,
    "tape": 26,
    "sudo": 27,
    "audio": 29,
    "dip": 30,
    "www-data": 33,
    "backup": 34,
    "operator": 37,
    "list": 38,
    "irc": 39,
    "src": 40,
    "shadow": 42,
    "utmp": 43,
    "video": 44,
    "sasl": 45,
    "plugdev": 46,
    "staff": 50,
    "games": 60,
    "users": 100,
    "nogroup": 65534,
    "systemd-journal": 999,
    "systemd-network": 998,
    "messagebus": 997,
    "input": 996,
    "sgx": 995,
    "clock": 994,
    "kvm": 993,
    "render": 992,
    "asterinas": 1000,
}
EXPECTED_OWNER_UID = 0
MAINTENANCE_UNITS = {
    "systemd-sysusers.service": (
        "ConditionNeedsUpdate=|/etc", "ExecStart=systemd-sysusers",
    ),
    "ldconfig.service": (
        "ConditionNeedsUpdate=|/etc", "ExecStart=/sbin/ldconfig -X",
    ),
    "systemd-journal-catalog-update.service": (
        "ConditionNeedsUpdate=/var", "ExecStart=journalctl --update-catalog",
    ),
    "systemd-hwdb-update.service": (
        "ConditionNeedsUpdate=/etc", "ExecStart=systemd-hwdb update",
    ),
}


def _regular(path: Path, *, nonempty: bool = False) -> Path:
    if not path.is_file() or path.is_symlink():
        raise CacheCheckError(f"missing or unsafe regular file: {path}")
    if path.stat().st_uid != EXPECTED_OWNER_UID:
        raise CacheCheckError(f"non-root-owned cache input: {path}")
    if nonempty and path.stat().st_size == 0:
        raise CacheCheckError(f"empty cache input: {path}")
    return path


def _account_rows(path: Path, fields: int) -> list[list[str]]:
    rows = [line.split(":") for line in _regular(path, nonempty=True).read_text().splitlines()]
    if any(len(row) != fields for row in rows):
        raise CacheCheckError(f"malformed account database: {path}")
    return rows


def _unique_named_ids(
    rows: list[list[str]], expected: dict[str, int | tuple[int, int]], *, user: bool
) -> None:
    names = [row[0] for row in rows]
    if len(names) != len(set(names)):
        raise CacheCheckError("account database has duplicate names")
    for name, identity in expected.items():
        matches = [row for row in rows if row[0] == name]
        if len(matches) != 1:
            raise CacheCheckError(f"required static identity is not exact-one: {name}")
        actual = (
            (int(matches[0][2]), int(matches[0][3]))
            if user
            else int(matches[0][2])
        )
        if actual != identity:
            raise CacheCheckError(f"static identity changed: {name}")
    for name, identity in expected.items():
        numeric_id = identity[0] if user else identity
        id_matches = [row for row in rows if int(row[2]) == numeric_id]
        if len(id_matches) != 1 or id_matches[0][0] != name:
            raise CacheCheckError(f"numeric identity is not exact-one: {name}")


def _validate_maintenance_units(root: Path) -> None:
    for unit, required_lines in MAINTENANCE_UNITS.items():
        vendor = _regular(root / "usr/lib/systemd/system" / unit, nonempty=True)
        lines = vendor.read_text(encoding="utf-8").splitlines()
        for required in required_lines:
            if lines.count(required) != 1:
                raise CacheCheckError(f"maintenance unit contract changed: {unit}: {required}")
        for override_root in ("etc/systemd/system", "run/systemd/system"):
            override = root / override_root / unit
            dropins = root / override_root / f"{unit}.d"
            if os.path.lexists(override) or os.path.lexists(dropins):
                raise CacheCheckError(f"maintenance unit is masked or overridden: {unit}")
        vendor_dropins = root / "usr/lib/systemd/system" / f"{unit}.d"
        if os.path.lexists(vendor_dropins):
            raise CacheCheckError(f"maintenance unit has unchecked vendor drop-ins: {unit}")


def check_cache_profile(
    root: Path, *, service_name: str = "asterinas-browser-web.service"
) -> str:
    root = root.resolve()
    passwd = _account_rows(root / "etc/passwd", 7)
    groups = _account_rows(root / "etc/group", 4)
    _unique_named_ids(passwd, EXPECTED_USERS, user=True)
    _unique_named_ids(groups, EXPECTED_GROUPS, user=False)
    shadow = _account_rows(root / "etc/shadow", 9)
    if len([row for row in shadow if row[0] == "asterinas" and row[1] == "!"]) != 1:
        raise CacheCheckError("asterinas shadow identity is absent or unlocked")

    usr_mtime = (root / "usr").stat().st_mtime_ns
    cache = _regular(root / "etc/ld.so.cache", nonempty=True)
    if not cache.read_bytes().startswith((b"ld.so-", b"glibc-ld.so.cache")):
        raise CacheCheckError("dynamic linker cache has unknown format")
    if cache.stat().st_mtime_ns < usr_mtime:
        raise CacheCheckError("dynamic linker cache is older than /usr")
    cache_listing_lines = _regular(
        root / "usr/share/asterinas/browser-startup-ldconfig.log", nonempty=True
    ).read_text(encoding="utf-8").splitlines()
    expected_digest = "LD_SO_CACHE_SHA256 " + hashlib.sha256(cache.read_bytes()).hexdigest()
    if cache_listing_lines.count(expected_digest) != 1 or not cache_listing_lines \
            or cache_listing_lines[0] != expected_digest:
        raise CacheCheckError("dynamic linker cache listing is not hash-bound to the cache")
    cache_listing = "\n".join(cache_listing_lines[1:])
    if any(token in cache_listing for token in (
        "/home/", "/tmp/", "/usr/local/", "x86_64", "aarch64", "i386-linux",
        "arm-linux", "s390x", "powerpc",
    )):
        raise CacheCheckError("dynamic linker cache contains host paths")
    if "libc.so.6" not in cache_listing or not re.search(
        r"=> /(?:usr/)?lib/riscv64-linux-gnu/", cache_listing
    ):
        raise CacheCheckError("dynamic linker cache is not the staged RISC-V cache")

    hwdb = _regular(root / "usr/lib/udev/hwdb.bin", nonempty=True)
    if not hwdb.read_bytes().startswith(b"KSLPHHRH"):
        raise CacheCheckError("udev hwdb has unknown format")
    if os.path.lexists(root / "etc/udev/hwdb.bin"):
        raise CacheCheckError("local udev hwdb would be suppressed by /etc/.updated")
    local_hwdb = root / "etc/udev/hwdb.d"
    if os.path.lexists(local_hwdb):
        if local_hwdb.is_symlink() or not local_hwdb.is_dir() or any(local_hwdb.iterdir()):
            raise CacheCheckError(
                "local udev hwdb snippets would be suppressed by /etc/.updated"
            )
    catalog = _regular(root / "var/lib/systemd/catalog/database", nonempty=True)
    if not catalog.read_bytes().startswith(b"RHHHKSLP"):
        raise CacheCheckError("journal catalog has unknown format")
    font_root = root / "var/cache/fontconfig"
    font_candidates = (
        [path for path in font_root.iterdir() if path.name != "CACHEDIR.TAG"]
        if font_root.is_dir() else []
    )
    font_caches = []
    for path in font_candidates:
        try:
            candidate = _regular(path, nonempty=True)
            if candidate.name.endswith(".cache-9") and candidate.read_bytes().startswith(
                b"\x04\xfc\x02\xfc"
            ):
                font_caches.append(candidate)
        except CacheCheckError:
            continue
    if not font_caches:
        raise CacheCheckError("fontconfig has no real pre-generated cache")

    for relative in ("etc/.updated", "var/.updated"):
        marker = _regular(root / relative)
        if marker.stat().st_mtime_ns < usr_mtime:
            raise CacheCheckError(f"ConditionNeedsUpdate stamp is older than /usr: {relative}")

    unit = _regular(
        root / "etc/systemd/system" / service_name, nonempty=True
    ).read_text(encoding="utf-8")
    for line in (
        "User=asterinas", "AmbientCapabilities=", "CapabilityBoundingSet=",
        "NoNewPrivileges=yes",
    ):
        if unit.splitlines().count(line) != 1:
            raise CacheCheckError(f"browser security unit contract changed: {line}")
    _validate_maintenance_units(root)
    return (
        "BROWSER_STARTUP_CACHE_PASS sysusers=static ldconfig=riscv64 "
        "journal=catalog fontconfig=cached stamps=current"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--service-name",
        default="asterinas-browser-web.service",
        help="browser systemd unit to validate (default: %(default)s)",
    )
    values = parser.parse_args()
    try:
        print(check_cache_profile(values.root, service_name=values.service_name))
    except (CacheCheckError, OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
