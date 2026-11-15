#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Build one immutable Firefox actor+dmesg diagnostic rootfs offline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.riscv.debian.rootfs.dev_overlay import materialize_rootfs
from tools.riscv.diagnostics.firefox_actor_overlay import (
    SOURCE_SHA256,
    replace_once,
    transform_archive,
)
from tools.riscv.diagnostics.firefox_dmesg_experiment import (
    BASELINE_SHA256,
    GUEST_HELPER,
    build_guest_source,
    load_baseline,
)

SNAPSHOT_COLLECTOR = (
    Path(__file__).resolve().parents[1] / "debian/rootfs/firefox_diagnostic_snapshot.py"
)


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def debugfs_cat(image, path):
    result = subprocess.run(
        ["debugfs", "-R", f"cat {path}", str(image)],
        capture_output=True,
        check=True,
    )
    return result.stdout


def prepare(base, source_archive, payload, output, *, unbuffered_socket=False):
    base, source_archive = Path(base), Path(source_archive)
    payload, output = Path(payload), Path(output)
    if payload.exists() or output.exists():
        raise FileExistsError("preserve existing Firefox dmesg diagnostic artifacts")
    base_image = base / "debian-root.ext2"
    installed = debugfs_cat(base_image, "/usr/lib/firefox-esr/omni.ja")
    if digest_bytes(installed) != SOURCE_SHA256:
        raise ValueError("installed Firefox archive does not match the pinned source")

    payload.mkdir(parents=True)
    provenance = transform_archive(
        source_archive,
        payload / "omni.ja",
        unbuffered_socket=unbuffered_socket,
    )
    wrapper = debugfs_cat(base_image, "/usr/lib/asterinas/browser-web-firefox").decode(
        "utf-8"
    )
    modified_wrapper = replace_once(
        wrapper,
        '        /usr/bin/chmod 0600 -- "$temporary"',
        '''        if [[ "${ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS:-0}" == 1 ]]; then
            printf '%s\\n' 'user_pref("browser.dom.window.dump.enabled", true);' >>"$temporary"
        fi
        /usr/bin/chmod 0600 -- "$temporary"''',
    )
    wrapper_path = payload / "browser-web-firefox"
    wrapper_path.write_text(modified_wrapper)
    subprocess.run(["bash", "-n", str(wrapper_path)], check=True)

    driver = build_guest_source(load_baseline())
    compile(driver, "firefox-dmesg-driver.py", "exec")
    driver_path = payload / "firefox-dmesg-driver.py"
    driver_path.write_text(driver)
    helper_path = payload / "firefox_dmesg_guest.py"
    shutil.copyfile(GUEST_HELPER, helper_path)
    collector_path = payload / "firefox-diagnostic-snapshot"
    shutil.copyfile(SNAPSHOT_COLLECTOR, collector_path)

    provenance.update(
        {
            "wrapper_source_sha256": digest_bytes(wrapper.encode()),
            "wrapper_derived_sha256": digest_bytes(modified_wrapper.encode()),
            "checkpoint_baseline_sha256": BASELINE_SHA256,
            "dmesg_driver_sha256": digest_bytes(driver.encode()),
            "dmesg_guest_helper_sha256": digest_bytes(helper_path.read_bytes()),
            "snapshot_collector_sha256": digest_bytes(collector_path.read_bytes()),
        }
    )
    (payload / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    spec = {
        "schema_version": 1,
        "profile": "browser-web",
        "files": [
            {
                "source": "omni.ja",
                "destination": "/usr/lib/firefox-esr/omni.ja",
                "mode": "0644",
            },
            {
                "source": "browser-web-firefox",
                "destination": "/usr/lib/asterinas/browser-web-firefox",
                "mode": "0755",
            },
            {
                "source": "firefox-dmesg-driver.py",
                "destination": "/usr/lib/asterinas/firefox-dmesg-driver.py",
                "mode": "0755",
                "create": True,
            },
            {
                "source": "firefox_dmesg_guest.py",
                "destination": "/usr/lib/asterinas/firefox_dmesg_guest.py",
                "mode": "0644",
                "create": True,
            },
            {
                "source": "firefox-diagnostic-snapshot",
                "destination": "/usr/lib/asterinas/firefox-diagnostic-snapshot",
                "mode": "0755",
            },
        ],
    }
    overlay = payload / "overlay.json"
    overlay.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    result = materialize_rootfs(base, overlay, output)
    (payload / "packaging-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unbuffered-socket", action="store_true")
    args = parser.parse_args()
    result = prepare(
        args.base,
        args.source_archive,
        args.payload,
        args.output,
        unbuffered_socket=args.unbuffered_socket,
    )
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
