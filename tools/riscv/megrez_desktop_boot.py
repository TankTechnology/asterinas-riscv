# SPDX-License-Identifier: MPL-2.0

"""Bounded, content-addressed Megrez desktop boot workflow."""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import zlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Sequence


GENERATION_ROOT = "/home/debian/asterinas/boot"
ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
BOOTARGS = " ".join(
    (
        "console=ttyS0",
        "loglevel=info",
        "asterinas.klog_capture=info",
        "init=/init",
        "asterinas.mmc_write_partition2",
        "asterinas.reboot_after=300",
        "systemd.mask=asterinas-browser-web-evidence.service",
        "systemd.mask=asterinas-desktop-m5-network.service",
        "systemd.mask=serial-getty@ttyS0.service",
        "systemd.mask=console-getty.service",
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
        "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9",
        "--",
        "--root-init=systemd",
        "--debug-console=isolated-root",
        "--volatile-home",
    )
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{16,64}\Z")
_BASE_URL = re.compile(r"https?://[A-Za-z0-9._:-]+\Z")


class DesktopBootError(RuntimeError):
    pass


class PrepareOperations(Protocol):
    publication_transcript: bytes

    def open(self, timeout: float) -> None: ...

    def boot_rockos(self, timeout: float) -> None: ...

    def login(self, username: str, password: str, timeout: float) -> None: ...

    def publish(
        self, commands: Sequence[str], password: str, timeout: float
    ) -> None: ...

    def reboot_and_recover(self, password: str, timeout: float) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DesktopBootArtifact:
    name: str
    source: str
    basename: str
    size: int
    sha256: str
    crc32: str
    load_address: int
    mmc_path: str = ""


@dataclass(frozen=True)
class DesktopBootManifest:
    schema_version: int
    stage1_protocol_version: int
    plan_sha256: str
    expected_root_sha256: str
    bootargs: str
    artifacts: dict[str, DesktopBootArtifact]
    generation_sha256: str
    generation_directory: str

    @classmethod
    def from_plan(cls, plan_path: Path) -> "DesktopBootManifest":
        payload = plan_path.read_bytes()
        try:
            plan: dict[str, Any] = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DesktopBootError(f"invalid plan: {error}") from error
        rows = plan.get("artifacts")
        if plan.get("schema_version") != 2 or not isinstance(rows, list):
            raise DesktopBootError("unsupported desktop boot plan")
        names = [row.get("name") for row in rows if isinstance(row, dict)]
        if len(names) != len(set(names)):
            raise DesktopBootError("plan contains duplicate artifact names")
        by_name = {row.get("name"): row for row in rows if isinstance(row, dict)}
        missing = set((*ARTIFACT_NAMES, "root_image")) - by_name.keys()
        if missing:
            raise DesktopBootError(f"plan is missing artifacts: {sorted(missing)}")

        artifacts: dict[str, DesktopBootArtifact] = {}
        for name in ARTIFACT_NAMES:
            row = by_name[name]
            source = Path(row.get("path", ""))
            try:
                metadata = source.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise DesktopBootError(f"{name}: source is a symbolic link")
                content = source.read_bytes()
            except OSError as error:
                raise DesktopBootError(f"{name}: cannot read source: {error}") from error
            actual_sha = hashlib.sha256(content).hexdigest()
            actual_crc = f"{zlib.crc32(content):08x}"
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != row.get("size")
                or actual_sha != row.get("sha256")
                or actual_crc != row.get("crc32")
            ):
                raise DesktopBootError(f"{name}: identity mismatch")
            suffix = {"kernel": "Image", "initramfs": "cpio", "megrez_dtb": "dtb"}[name]
            basename = f"{name}-{actual_sha[:16]}.{suffix}"
            artifacts[name] = DesktopBootArtifact(
                name=name,
                source=str(source.resolve()),
                basename=basename,
                size=metadata.st_size,
                sha256=actual_sha,
                crc32=actual_crc,
                load_address=row.get("load_address"),
            )
        root_sha = by_name["root_image"].get("sha256")
        if not isinstance(root_sha, str) or _SHA256.fullmatch(root_sha) is None:
            raise DesktopBootError("root image identity is invalid")
        identity = {
            "schema_version": 1,
            "stage1_protocol_version": 1,
            "plan_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_root_sha256": root_sha,
            "bootargs": BOOTARGS,
            "artifacts": {
                name: _published_artifact(artifacts[name], include_path=False)
                for name in ARTIFACT_NAMES
            },
        }
        generation_sha = hashlib.sha256(_canonical(identity)).hexdigest()
        generation_directory = f"{GENERATION_ROOT}/{generation_sha[:16]}"
        artifacts = {
            name: replace(
                artifact,
                mmc_path=f"{generation_directory}/{artifact.basename}",
            )
            for name, artifact in artifacts.items()
        }
        return cls(
            **identity | {
                "artifacts": artifacts,
                "generation_sha256": generation_sha,
                "generation_directory": generation_directory,
            }
        )

    def canonical_bytes(self) -> bytes:
        return _canonical(
            {
                "schema_version": self.schema_version,
                "stage1_protocol_version": self.stage1_protocol_version,
                "plan_sha256": self.plan_sha256,
                "expected_root_sha256": self.expected_root_sha256,
                "bootargs": self.bootargs,
                "artifacts": {
                    name: _published_artifact(self.artifacts[name], include_path=True)
                    for name in ARTIFACT_NAMES
                },
                "generation_sha256": self.generation_sha256,
                "generation_directory": self.generation_directory,
            }
        )


def _canonical(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _published_artifact(
    artifact: DesktopBootArtifact, *, include_path: bool
) -> dict[str, Any]:
    result = {
        "name": artifact.name,
        "basename": artifact.basename,
        "size": artifact.size,
        "sha256": artifact.sha256,
        "crc32": artifact.crc32,
        "load_address": artifact.load_address,
    }
    if include_path:
        result["mmc_path"] = artifact.mmc_path
    return result


def publication_script(
    manifest: DesktopBootManifest, base_url: str, nonce: str
) -> str:
    """Return a fail-closed RockOS script for one immutable p3 generation."""

    if _BASE_URL.fullmatch(base_url) is None or _NONCE.fullmatch(nonce) is None:
        raise DesktopBootError("unsafe publication transport input")
    final = manifest.generation_directory
    manifest_sha = hashlib.sha256(manifest.canonical_bytes()).hexdigest()
    checks = [
        f"{artifact.sha256}  $WORK/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    final_checks = [
        f"{artifact.sha256}  {final}/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    downloads = [
        f"curl -fSs --max-time 180 {base_url}/{artifact.basename} -o $WORK/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    return "\n".join(
        (
            "set -eu",
            f"GENERATION_ROOT={GENERATION_ROOT}",
            f"FINAL={final}",
            "mkdir -p -- $GENERATION_ROOT",
            "if test -d $FINAL; then",
            f"  printf '%s  %s\\n' {manifest_sha} $FINAL/manifest.json | sha256sum -c -",
            *(f"  printf '%s\\n' '{line}' | sha256sum -c -" for line in final_checks),
            f"  echo ASTERINAS_DESKTOP_GENERATION_READY generation={manifest.generation_sha256} status=idempotent",
            "  exit 0",
            "fi",
            f"WORK=$(mktemp -d $GENERATION_ROOT/.{manifest.generation_sha256[:16]}.{nonce}.XXXXXX)",
            "trap 'rm -rf -- $WORK' EXIT HUP INT TERM",
            *downloads,
            f"curl -fSs --max-time 30 {base_url}/manifest.json -o $WORK/manifest.json",
            *(f'printf \'%s\\n\' "{line}" | sha256sum -c -' for line in checks),
            f"printf '%s  %s\\n' {manifest_sha} $WORK/manifest.json | sha256sum -c -",
            "sync $WORK",
            "mv -T -- $WORK $FINAL",
            "trap - EXIT HUP INT TERM",
            "sync $GENERATION_ROOT",
            f"echo ASTERINAS_DESKTOP_GENERATION_READY generation={manifest.generation_sha256} status=published",
            "",
        )
    )


def prepare_generation(
    manifest: DesktopBootManifest,
    operations: PrepareOperations,
    *,
    username: str,
    password: str,
    base_url: str,
    nonce: str,
) -> bytes:
    """Publish one generation through RockOS and recover to U-Boot."""

    script = publication_script(manifest, base_url, nonce)
    script_name = f"publish-{manifest.generation_sha256[:16]}.sh"
    script_sha = hashlib.sha256(script.encode()).hexdigest()
    launcher = (
        f"curl -fSs --max-time 30 {base_url}/{script_name} "
        "-o /tmp/asterinas-desktop-publish.sh "
        f"&& printf '%s  %s\\n' {script_sha} /tmp/asterinas-desktop-publish.sh "
        "| sha256sum -c - && sh /tmp/asterinas-desktop-publish.sh"
    )
    must_recover = False
    try:
        operations.open(30)
        operations.boot_rockos(240)
        operations.login(username, password, 60)
        must_recover = True
        try:
            operations.publish((launcher,), password, 600)
        finally:
            operations.reboot_and_recover(password, 180)
            must_recover = False
        transcript = operations.publication_transcript
        marker = (
            "ASTERINAS_DESKTOP_GENERATION_READY "
            f"generation={manifest.generation_sha256}"
        ).encode()
        if marker not in transcript:
            raise DesktopBootError("RockOS did not confirm the desktop generation")
        return transcript
    finally:
        if must_recover:
            # This path is only reachable when recovery itself raised; avoid a
            # second blind serial command and release the exclusive descriptor.
            pass
        operations.close()


def uboot_load_commands(manifest: DesktopBootManifest) -> tuple[str, ...]:
    return tuple(
        f"ext4load mmc 1:3 0x{artifact.load_address:x} {artifact.mmc_path}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    )


@contextlib.contextmanager
def _publication_server(
    manifest: DesktopBootManifest, address: str, port: int, nonce: str
):
    with tempfile.TemporaryDirectory(prefix="asterinas-desktop-publish-") as raw:
        directory = Path(raw)
        for name in ARTIFACT_NAMES:
            artifact = manifest.artifacts[name]
            (directory / artifact.basename).symlink_to(artifact.source)
        (directory / "manifest.json").write_bytes(manifest.canonical_bytes())
        script_name = f"publish-{manifest.generation_sha256[:16]}.sh"
        (directory / script_name).write_text(
            publication_script(manifest, f"http://{address}:{port}", nonce)
        )
        handler = functools.partial(
            SimpleHTTPRequestHandler, directory=str(directory)
        )
        server = ThreadingHTTPServer((address, port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or start one bounded Megrez Asterinas desktop boot"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser(
        "prepare", help="publish one immutable generation to RockOS partition 3"
    )
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--device", required=True)
    prepare.add_argument("--host-address", default="10.100.19.216")
    prepare.add_argument("--port", type=int, default=18080)
    prepare.add_argument("--username", default="debian")
    credential = prepare.add_mutually_exclusive_group(required=True)
    credential.add_argument("--factory-login", action="store_true")
    credential.add_argument("--password-fd", type=int)
    prepare.add_argument("--output", type=Path, required=True)
    return parser


def _read_password(descriptor: int | None, factory_login: bool) -> str:
    if factory_login:
        return "debian"
    if descriptor is None:
        raise DesktopBootError("RockOS password source is required")
    payload = os.read(descriptor, 1024)
    if len(payload) == 1024 or b"\x00" in payload:
        raise DesktopBootError("RockOS password is invalid")
    try:
        password = payload.decode().rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise DesktopBootError("RockOS password is not UTF-8") from error
    if not password or "\n" in password or "\r" in password:
        raise DesktopBootError("RockOS password is invalid")
    return password


def _prepare_main(args: argparse.Namespace) -> int:
    from tools.riscv.megrez_rockos_attestation import (
        RealRockOsAttestationOperations,
    )

    manifest = DesktopBootManifest.from_plan(args.plan)
    password = _read_password(args.password_fd, args.factory_login)
    nonce = secrets.token_hex(16)
    base_url = f"http://{args.host_address}:{args.port}"
    operations = RealRockOsAttestationOperations(args.device)
    with _publication_server(manifest, args.host_address, args.port, nonce):
        transcript = prepare_generation(
            manifest,
            operations,
            username=args.username,
            password=password,
            base_url=base_url,
            nonce=nonce,
        )
    _atomic_write(args.output / "manifest.json", manifest.canonical_bytes())
    _atomic_write(args.output / "publication.serial.log", transcript)
    sums = (
        f"{hashlib.sha256(manifest.canonical_bytes()).hexdigest()}  manifest.json\n"
        f"{hashlib.sha256(transcript).hexdigest()}  publication.serial.log\n"
    ).encode()
    _atomic_write(args.output / "sha256sums.txt", sums)
    print(
        "prepared Megrez desktop generation "
        f"{manifest.generation_sha256} at {manifest.generation_directory}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            return _prepare_main(args)
        raise AssertionError(args.action)
    except (DesktopBootError, OSError, RuntimeError, TimeoutError) as error:
        print(f"Megrez desktop boot {args.action} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
