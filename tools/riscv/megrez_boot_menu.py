# SPDX-License-Identifier: MPL-2.0

"""Prepare and publish one immutable, four-mode Megrez extlinux selector."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import zlib

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_boot_manifest import BootManifestError, MAX_EXTLINUX_BYTES
from tools.riscv.megrez_board_session import MEGREZ_FRAMEBUFFER


MODES = ("basic", "probe", "desktop")
PATH = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BASE_ARGS = "console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init"
MODE_ARGS = {
    "basic": BASE_ARGS + " -- --root-init=basic",
    "probe": BASE_ARGS + " asterinas.reboot_after=90 -- --root-init=probe-auto",
}
ARTIFACTS = ("kernel", "stage1", "dtb", "desktop_stage1")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BootManifestError(message)


def vendor_default(data: str) -> tuple[str, str, dict[str, str]]:
    """Preserve the selected vendor stanza and reject inherited boot directives."""
    require(
        isinstance(data, str) and 0 < len(data.encode()) <= MAX_EXTLINUX_BYTES,
        "vendor configuration has an invalid size",
    )
    require(all(ord(c) >= 32 or c in "\n\t" for c in data), "vendor control character")
    default = None
    stanzas: dict[str, list[str]] = {}
    label = None
    for raw in data.splitlines(keepends=True):
        line = raw.strip()
        if not line or line.startswith("#"):
            if label is not None:
                stanzas[label].append(raw)
            continue
        words = line.split(None, 1)
        require(len(words) == 2, "vendor directive lacks a value")
        key, value = words
        if key == "label":
            require(
                LABEL.fullmatch(value) is not None and value not in stanzas,
                "duplicate or unsafe vendor label",
            )
            label = value
            stanzas[label] = [raw]
        elif label is None:
            require(
                key in ("default", "menu", "prompt", "timeout"),
                "unsupported vendor global directive",
            )
            if key == "default":
                require(default is None, "ambiguous vendor default")
                default = value
        else:
            stanzas[label].append(raw)
    require(default in stanzas, "missing vendor default stanza")
    require(
        not default.startswith("asterinas-"), "vendor label collides with Asterinas"
    )
    stanza = "".join(stanzas[default])
    fields = {}
    for raw in stanza.splitlines()[1:]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split(None, 1)
        require(
            key in ("menu", "linux", "initrd", "fdt", "fdtdir", "append"),
            "unsupported vendor boot directive",
        )
        require(key not in fields, "duplicate vendor boot directive")
        if key == "menu":
            require(value.startswith("label "), "unsupported vendor menu directive")
        fields[key] = value
    require(
        all(key in fields for key in ("linux", "initrd", "append")),
        "incomplete RockOS boot stanza",
    )
    require(("fdt" in fields) != ("fdtdir" in fields), "ambiguous RockOS DTB")
    for key in ("linux", "initrd", "fdt", "fdtdir"):
        if key in fields:
            require(
                PATH.fullmatch(fields[key].rstrip("/")) is not None,
                "unsafe RockOS artifact path",
            )
    require("${" not in fields["append"], "unsupported RockOS argument expansion")
    return default, stanza, fields


def identity(data: bytes, path: str) -> dict:
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32": f"{zlib.crc32(data):08x}",
    }


def _fdt_property(path: Path, kind: str, node: str, name: str, label: str) -> list[str]:
    try:
        result = subprocess.run(
            ["fdtget", "-t", kind, str(path), node, name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError as error:
        raise BootManifestError(f"cannot inspect prepared DTB: {error}") from error
    require(result.returncode == 0, f"prepared DTB lacks {label}")
    return result.stdout.split()


def validate_prepared_dtb(path: Path) -> None:
    """Reject a desktop selector whose immutable DTB lacks the hardware handoff."""

    require(path.is_file() and not path.is_symlink(), "prepared DTB is not a file")
    framebuffer = MEGREZ_FRAMEBUFFER
    expected = (
        (
            "s",
            framebuffer.node_path,
            "compatible",
            ["simple-framebuffer"],
            "framebuffer compatible",
        ),
        (
            "x",
            framebuffer.node_path,
            "reg",
            ["0", f"{framebuffer.address:x}", "0", f"{framebuffer.size:x}"],
            "framebuffer registers",
        ),
        (
            "u",
            framebuffer.node_path,
            "width",
            [str(framebuffer.width)],
            "framebuffer width",
        ),
        (
            "u",
            framebuffer.node_path,
            "height",
            [str(framebuffer.height)],
            "framebuffer height",
        ),
        (
            "u",
            framebuffer.node_path,
            "stride",
            [str(framebuffer.stride)],
            "framebuffer stride",
        ),
        (
            "s",
            framebuffer.node_path,
            "format",
            [framebuffer.pixel_format],
            "framebuffer format",
        ),
        ("s", framebuffer.node_path, "status", ["okay"], "framebuffer status"),
        (
            "s",
            "/chosen",
            "asterinas,usb-host",
            [
                "/soc/usb0@50480000/dwc3@50480000",
                "/soc/usb1@50490000/dwc3@50490000",
            ],
            "USB host handoff",
        ),
    )
    for kind, node, name, values, label in expected:
        require(
            _fdt_property(path, kind, node, name, label) == values,
            f"prepared DTB has invalid {label}",
        )


def validate(document: dict) -> None:
    require(
        isinstance(document, dict)
        and set(document)
        == {"schema_version", "vendor", "artifacts", "desktop_args", "extlinux"},
        "invalid menu manifest fields",
    )
    require(document["schema_version"] == 3, "menu publication requires schema 3")
    vendor_default(document["vendor"])
    require(
        isinstance(document["artifacts"], dict)
        and set(document["artifacts"]) == set(ARTIFACTS),
        "invalid menu artifacts",
    )
    seen = {}
    for item in document["artifacts"].values():
        require(
            isinstance(item, dict) and set(item) == {"path", "size", "sha256", "crc32"},
            "invalid artifact identity fields",
        )
        require(
            isinstance(item["path"], str) and PATH.fullmatch(item["path"]) is not None,
            "unsafe artifact path",
        )
        require(
            type(item["size"]) is int and 0 < item["size"] <= 64 * 1024 * 1024,
            "invalid artifact size",
        )
        require(
            isinstance(item["sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is not None,
            "invalid artifact SHA-256",
        )
        require(
            isinstance(item["crc32"], str)
            and re.fullmatch(r"[a-f0-9]{8}", item["crc32"]) is not None,
            "invalid artifact CRC32",
        )
        require(
            item["sha256"][:12] in Path(item["path"]).name,
            "artifact basename is not immutable",
        )
        require(
            item["path"] not in seen or seen[item["path"]] == item,
            "conflicting shared artifact identity",
        )
        seen[item["path"]] = item
    args = document["desktop_args"]
    require(
        isinstance(args, str)
        and len(args) <= 4096
        and re.fullmatch(r"[A-Za-z0-9 ._=/,:@+%~-]+", args) is not None,
        "unsafe desktop arguments",
    )
    tokens = args.split()
    require(
        tokens.count("--") == 1 and tokens.count("init=/init") == 1,
        "invalid desktop init",
    )
    require(
        tokens[tokens.index("--") + 1 :]
        in (
            ["--root-init=systemd"],
            ["--root-init=systemd", "--debug-console=root"],
            ["--root-init=systemd", "--debug-console=root", "--volatile-home"],
        ),
        "invalid desktop root-init",
    )
    require(
        not any(t.startswith("asterinas.reboot_after") for t in tokens),
        "desktop must not have an automatic reboot deadline",
    )
    require(document["extlinux"] == render(document), "selector identity mismatch")


def render(document: dict) -> str:
    default, stanza, _ = vendor_default(document["vendor"])
    output = f"default {default}\nmenu title Megrez operating system\nprompt 1\ntimeout 100\n\n"
    output += stanza.rstrip("\n") + "\n\n"
    artifacts = document["artifacts"]
    for mode in MODES:
        stage1 = artifacts["desktop_stage1" if mode == "desktop" else "stage1"]
        args = document["desktop_args"] if mode == "desktop" else MODE_ARGS[mode]
        output += (
            f"label asterinas-{mode}\nmenu label Asterinas {mode.title()}\n"
            f"linux {artifacts['kernel']['path']}\ninitrd {stage1['path']}\n"
            f"fdt {artifacts['dtb']['path']}\nappend {args}\n\n"
        )
    require(len(output.encode()) <= MAX_EXTLINUX_BYTES, "selector is too large")
    return output


def prepare(
    vendor: Path, sources: dict[str, Path], desktop_args: str, output: Path
) -> dict:
    validate_prepared_dtb(sources["dtb"])
    output.mkdir(parents=True, exist_ok=False)
    document = {
        "schema_version": 3,
        "vendor": vendor.read_text(),
        "artifacts": {},
        "desktop_args": desktop_args,
    }
    payloads = {}
    for name in ARTIFACTS:
        data = sources[name].read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        prefix, suffix = (
            ("asterinas", "booti")
            if name == "kernel"
            else (("megrez", "dtb") if name == "dtb" else ("stage1", "cpio"))
        )
        path = f"/{prefix}-{sha[:12]}.{suffix}"
        document["artifacts"][name] = identity(data, path)
        payloads[path[1:]] = data
    document["extlinux"] = render(document)
    validate(document)
    with PinnedOutputDirectory(output) as publication:
        for name, data in payloads.items():
            publication.atomic_write(name, data, mode=0o444)
        publication.atomic_write("manifest.json", encode(document), mode=0o444)
        publication.atomic_write(
            "asterinas-menu.conf", document["extlinux"].encode(), mode=0o444
        )
    return document


def encode(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def prepare_dtb(source: Path, output: Path) -> None:
    """Move the existing framebuffer/USB handoff from each boot to publication."""
    require(
        not output.exists() and not output.is_symlink(), "prepared DTB already exists"
    )
    shutil.copyfile(source, output)
    fb = MEGREZ_FRAMEBUFFER

    def put(node: str, property_name: str, kind: str, *values: str) -> None:
        subprocess.run(
            ["fdtput", "-t", kind, str(output), node, property_name, *values],
            check=True,
        )

    subprocess.run(["fdtput", "-p", "-c", str(output), fb.node_path], check=True)
    put(fb.node_path, "compatible", "s", "simple-framebuffer")
    put(fb.node_path, "reg", "x", "0", f"{fb.address:x}", "0", f"{fb.size:x}")
    for key, value in (
        ("width", fb.width),
        ("height", fb.height),
        ("stride", fb.stride),
    ):
        put(fb.node_path, key, "i", str(value))
    put(fb.node_path, "format", "s", fb.pixel_format)
    put(fb.node_path, "status", "s", "okay")
    put(
        "/chosen",
        "asterinas,usb-host",
        "s",
        "/soc/usb0@50480000/dwc3@50480000",
        "/soc/usb1@50490000/dwc3@50490000",
    )


def publication_script(document: dict, base_url: str) -> str:
    validate(document)
    require(
        re.fullmatch(r"http://[0-9.]+:[0-9]+(?:/[A-Za-z0-9._-]+)*", base_url)
        is not None,
        "unsafe publication URL",
    )
    config_sha = hashlib.sha256(document["extlinux"].encode()).hexdigest()
    vendor_sha = hashlib.sha256(document["vendor"].encode()).hexdigest()
    lines = [
        "#!/bin/sh",
        "set -eu",
        "umask 022",
        '[ "$(findmnt -n -o SOURCE -- /boot)" = /dev/mmcblk1p1 ]',
        f"printf '%s  %s\\n' {vendor_sha} /boot/extlinux/extlinux.conf | sha256sum -c -",
        "work=$(mktemp -d /tmp/asterinas-menu.XXXXXX)",
        'trap \'rm -f -- "$work/item"; rmdir -- "$work"\' EXIT',
        'verify() { test -f "$1" && test ! -L "$1" && '
        '[ "$(stat -c %s -- "$1")" = "$2" ] && '
        'printf "%s  %s\\n" "$3" "$1" | sha256sum -c -; }',
    ]
    installed = set()
    for item in document["artifacts"].values():
        if item["path"] in installed:
            continue
        installed.add(item["path"])
        destination = "/boot" + item["path"]
        lines += [
            f"if test -e {destination}; then",
            f"  verify {destination} {item['size']} {item['sha256']}",
            "else",
            f'  curl -fSs {shlex.quote(base_url + item["path"])} -o "$work/item"',
            f'  verify "$work/item" {item["size"]} {item["sha256"]}',
            f'  install -D -m 0444 "$work/item" {destination}',
            "fi",
        ]
    _, _, rockos = vendor_default(document["vendor"])
    for key in ("linux", "initrd", "fdt", "fdtdir"):
        if key in rockos:
            lines.append(
                f"test {'-d' if key == 'fdtdir' else '-s'} {shlex.quote('/boot' + rockos[key])}"
            )
    config = f"/boot/extlinux/asterinas-menu-{config_sha[:12]}.conf"
    lines += [
        f'curl -fSs {shlex.quote(base_url + "/asterinas-menu.conf")} -o "$work/item"',
        f'verify "$work/item" {len(document["extlinux"].encode())} {config_sha}',
        f'install -m 0444 "$work/item" {config}.part',
        "sync",
        f"mv -f {config}.part {config}",
        "sync",
        f"verify {config} {len(document['extlinux'].encode())} {config_sha}",
        f"echo ASTERINAS_MENU_CANARY_READY sha256={config_sha}",
    ]
    return "\n".join(lines) + "\n"


def promotion_script(document: dict, evidence: Path) -> str:
    """Require distinct, logged physical cycles before replacing the selector."""
    validate(document)
    sha = hashlib.sha256(document["extlinux"].encode()).hexdigest()
    required = {"rockos": 3, "fallback": 3, "basic": 3, "probe": 3, "desktop": 2}
    counts = dict.fromkeys(required, 0)
    run_ids = set()
    for path in evidence.glob("*/result.json"):
        result = json.loads(path.read_text())
        if result.get("action") != "cycle" or result.get("menu_sha256") != sha:
            continue
        if result.get("passed") is not True or result.get("recovered") is not True:
            continue
        run_id = result.get("run_id")
        require(
            isinstance(run_id, str)
            and re.fullmatch(r"[a-f0-9]{32}", run_id) is not None,
            "invalid qualification run ID",
        )
        require(run_id not in run_ids, "duplicate qualification run")
        run_ids.add(run_id)
        data = (path.parent / "serial.log").read_bytes()
        require(
            hashlib.sha256(data).hexdigest() == result.get("serial_sha256"),
            "qualification log identity mismatch",
        )
        mode = result["mode"]
        require(mode in counts, "unknown qualification mode")
        require(b"OpenSBI" in data and b"U-Boot " in data, "missing recovery epoch")
        counts[mode] += 1
    require(
        all(counts[name] >= count for name, count in required.items()),
        f"physical qualification incomplete: {counts}, required: {required}",
    )
    source = f"/boot/extlinux/asterinas-menu-{sha[:12]}.conf"
    vendor_sha = hashlib.sha256(document["vendor"].encode()).hexdigest()
    artifact_checks = []
    for item in {
        item["path"]: item for item in document["artifacts"].values()
    }.values():
        path = "/boot" + item["path"]
        artifact_checks.extend(
            [
                f"test -f {path} && test ! -L {path}",
                f'[ "$(stat -c %s -- {path})" = {item["size"]} ]',
                f"printf '%s  %s\\n' {item['sha256']} {path} | sha256sum -c -",
            ]
        )
    return "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            '[ "$(findmnt -n -o SOURCE -- /boot)" = /dev/mmcblk1p1 ]',
            f"printf '%s  %s\\n' {vendor_sha} /boot/extlinux/extlinux.conf | sha256sum -c -",
            f"test -f {source} && test ! -L {source}",
            f"printf '%s  %s\\n' {sha} {source} | sha256sum -c -",
            *artifact_checks,
            "temporary=$(mktemp /boot/extlinux/.asterinas-menu.XXXXXX)",
            "trap 'rm -f -- \"$temporary\"' EXIT",
            f'install -m 0444 {source} "$temporary"',
            # Preserve the prior selector for manual rollback; do not retire files.
            "previous=$(sha256sum /boot/extlinux/asterinas.conf)",
            "previous=${previous%% *}",
            'install -m 0444 /boot/extlinux/asterinas.conf "/boot/extlinux/asterinas-backup-$previous.conf"',
            'sync; mv -f "$temporary" /boot/extlinux/asterinas.conf; sync',
            f"printf '%s  %s\\n' {sha} /boot/extlinux/asterinas.conf | sha256sum -c -",
            f"echo ASTERINAS_MENU_PUBLISHED sha256={sha}",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--vendor", required=True, type=Path)
    for name in ARTIFACTS:
        create.add_argument(
            "--" + name.replace("_", "-"), required=name != "desktop_stage1", type=Path
        )
    create.add_argument("--desktop-args", required=True)
    create.add_argument("--output", required=True, type=Path)
    publish = commands.add_parser("publication-script")
    publish.add_argument("--manifest", required=True, type=Path)
    publish.add_argument("--base-url", required=True)
    publish.add_argument("--output", type=Path)
    dtb = commands.add_parser("prepare-dtb")
    dtb.add_argument("--source", required=True, type=Path)
    dtb.add_argument("--output", required=True, type=Path)
    promote = commands.add_parser("promotion-script")
    promote.add_argument("--manifest", required=True, type=Path)
    promote.add_argument("--evidence", required=True, type=Path)
    promote.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            document = prepare(
                args.vendor,
                {name: getattr(args, name) or args.stage1 for name in ARTIFACTS},
                args.desktop_args,
                args.output,
            )
            print(hashlib.sha256(encode(document)).hexdigest())
        elif args.action == "prepare-dtb":
            prepare_dtb(args.source, args.output)
        elif args.action == "promotion-script":
            document = json.loads(args.manifest.read_text())
            script = promotion_script(document, args.evidence)
            with args.output.open("x") as stream:
                stream.write(script)
        else:
            document = json.loads(args.manifest.read_text())
            script = publication_script(document, args.base_url)
            if args.output:
                with args.output.open("x") as stream:
                    stream.write(script)
            else:
                print(script, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"boot menu: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
