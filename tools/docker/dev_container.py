#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Reuse a development container and persistent caches for each worktree."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


LABEL = "org.asterinas.dev"
SCHEMA = "1"
BUILD_LOCK = "/root/.rustup/.asterinas-build.lock"
OFFLINE_NIX_CONFIG = (
    "substitute = false\nconnect-timeout = 1\ndownload-attempts = 0\n"
    "tarball-ttl = 2592000"
)


def docker(*args, capture=False, check=True):
    return subprocess.run(
        ["docker", *args],
        text=True,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def inspect_container(name):
    result = docker("container", "inspect", name, capture=True, check=False)
    if result.returncode:
        # Distinguish an absent container from a disconnected Docker daemon.
        docker("info", "--format", "{{.ServerVersion}}", capture=True)
        if (
            "No such container" not in result.stderr
            and "No such object" not in result.stderr
        ):
            raise RuntimeError(result.stderr.strip())
        return None
    return json.loads(result.stdout)[0]


def identity(workspace, image_id):
    image_key = image_id.removeprefix("sha256:")[:12]
    worktree_key = hashlib.sha256(os.fsencode(workspace)).hexdigest()[:12]
    prefix = f"asterinas-dev-v{SCHEMA}-{image_key}"
    return f"{prefix}-{worktree_key}", prefix


def mount(source, target, *, bind=False, readonly=False):
    if any(character in source for character in (",", "\n", "\r")):
        raise ValueError("mount source cannot contain commas or newlines")
    return f"type={'bind' if bind else 'volume'},source={source},target={target}" + (
        ",readonly" if readonly else ""
    )


def mount_plan(workspace, name, prefix):
    return [
        mount(str(workspace), "/root/asterinas", bind=True),
        mount("/dev", "/dev", bind=True),
        mount(f"{name}-cargo", "/root/.cargo"),
        mount(f"{prefix}-registry", "/root/.cargo/registry"),
        mount(f"{prefix}-git", "/root/.cargo/git"),
        mount(f"{prefix}-rustup", "/root/.rustup"),
        mount(f"{prefix}-nix", "/nix"),
        mount(f"{prefix}-nix-cache", "/root/.cache/nix"),
    ]


def seed_cargo_cache(workspace, image_id, prefix, source):
    """Copy an existing download cache once without installing its binaries."""
    if not source:
        return
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", source):
        raise ValueError("seed_cargo_volume must be a Docker volume name")
    docker("volume", "inspect", source, capture=True)
    seed_key = hashlib.sha256(source.encode()).hexdigest()[:8]
    name = f"{prefix}-seed-{seed_key}"
    existing = inspect_container(name)
    if (
        existing
        and existing["State"]["Status"] == "exited"
        and existing["State"]["ExitCode"] == 0
    ):
        return
    if existing and existing["State"]["Running"]:
        raise RuntimeError(f"cache seed is already running: {name}")
    if not existing:
        args = ["create", "--name", name, "--label", f"{LABEL}.schema={SCHEMA}"]
        for spec in (
            mount(str(workspace), "/root/asterinas", bind=True, readonly=True),
            mount(source, "/seed", readonly=True),
            mount(f"{prefix}-registry", "/cache/registry"),
            mount(f"{prefix}-git", "/cache/git"),
        ):
            args += ["--mount", spec]
        args += [
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-ec",
            (
                "for part in registry git; do "
                'if test -d "/seed/$part"; then '
                'cp -an "/seed/$part/." "/cache/$part/"; fi; done'
            ),
        ]
        docker(*args, capture=True)
    docker("start", "--attach", name)
    state = inspect_container(name)["State"]
    if state["ExitCode"] != 0:
        raise RuntimeError(f"cache seed failed: {name}; inspect docker logs")


def container_labels(workspace, image_id):
    return {
        f"{LABEL}.schema": SCHEMA,
        f"{LABEL}.workspace": str(workspace),
        f"{LABEL}.image": image_id,
    }


def validate_container(existing, workspace, image_id, name):
    expected = container_labels(workspace, image_id)
    labels = existing["Config"].get("Labels") or {}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"container identity mismatch: {name}")


def ensure_container(workspace, image_id, name, prefix):
    existing = inspect_container(name)
    if existing:
        validate_container(existing, workspace, image_id, name)
    else:
        print(
            f"Creating {name}; initializing persistent volumes from the local image",
            file=sys.stderr,
        )
        args = [
            "create",
            "--name",
            name,
            "--privileged",
            "--network=host",
            "--init",
            "--workdir",
            "/root/asterinas",
        ]
        for key, value in container_labels(workspace, image_id).items():
            args += ["--label", f"{key}={value}"]
        for spec in mount_plan(workspace, name, prefix):
            args += ["--mount", spec]
        # Override image commands that might run apt on every startup.
        args += ["--entrypoint", "/bin/sleep", image_id, "infinity"]
        docker(*args, capture=True)
    if not existing or not existing["State"]["Running"]:
        docker("start", name, capture=True)


def stop_container(workspace, image_id, name):
    existing = inspect_container(name)
    if not existing:
        return
    validate_container(existing, workspace, image_id, name)
    if not existing["State"]["Running"]:
        return
    # The lock also detects work left running after a client disconnected.
    docker("exec", name, "flock", "--nonblock", BUILD_LOCK, "true")
    rows = docker("top", name, "-eo", "pid,comm", capture=True).stdout.splitlines()[1:]
    processes = [row.split(maxsplit=1)[1].strip() for row in rows]
    if sorted(processes) != ["docker-init", "sleep"]:
        raise RuntimeError("container has active commands; wait before stopping it")
    docker("stop", name, capture=True)


def command_args(name, command, offline, interactive):
    args = ["exec", "-i"]
    if interactive:
        args += ["-t"]
    if offline:
        args += [
            "--env",
            "CARGO_NET_OFFLINE=true",
            "--env",
            f"NIX_CONFIG={OFFLINE_NIX_CONFIG}",
        ]
        # Also cover Rustup and any fetchers invoked by build scripts.
        command = ["unshare", "--net", "--", *command]
    return [*args, name, "flock", "--exclusive", BUILD_LOCK, *command]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, help="worktree to mount (default: this repository)"
    )
    parser.add_argument(
        "--image", help="existing local development image; never automatically pulled"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="build using cached dependencies in an isolated network namespace",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--status",
        action="store_true",
        help="show configuration without starting a container",
    )
    actions.add_argument(
        "--stop",
        action="store_true",
        help="stop this worktree's idle container; keep all data",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- command [args]; default: bash"
    )
    args = parser.parse_args(argv)
    workspace = (args.workspace or Path(__file__).resolve().parents[2]).resolve()
    if not (workspace / "rust-toolchain.toml").is_file():
        raise ValueError(f"not an Asterinas worktree: {workspace}")

    config_path = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "asterinas/docker.json"
    )
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    devcontainer = (workspace / ".devcontainer/devcontainer.json").read_text()
    default_image = re.search(r'"image"\s*:\s*"([^"]+)"', devcontainer)
    image = (
        args.image
        or config.get("image")
        or (default_image.group(1) if default_image else None)
    )
    if not image:
        raise ValueError("no image configured; use --image")
    image_id = docker(
        "image", "inspect", image, "--format", "{{.Id}}", capture=True
    ).stdout.strip()
    name, prefix = identity(workspace, image_id)
    if args.status:
        existing = inspect_container(name)
        print(
            json.dumps(
                {
                    "container": name,
                    "state": existing["State"]["Status"] if existing else "not-created",
                    "image": image,
                    "image_id": image_id,
                    "workspace": str(workspace),
                    "mounts": mount_plan(workspace, name, prefix),
                    "config": str(config_path),
                },
                indent=2,
            )
        )
        return 0

    cache_dir = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "asterinas/docker"
    )
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f"Waiting for the {prefix} lifecycle lock", file=sys.stderr)
    with (cache_dir / f"{prefix}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.stop:
            stop_container(workspace, image_id, name)
            return 0
        seed_cargo_cache(workspace, image_id, prefix, config.get("seed_cargo_volume"))
        ensure_container(workspace, image_id, name, prefix)

        command = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        command = command or ["bash"]
        print(
            f"Using {name} (persistent caches; waiting for build lock if busy)",
            file=sys.stderr,
        )
        return docker(
            *command_args(name, command, args.offline, sys.stdin.isatty()), check=False
        ).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"dev-container: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)
