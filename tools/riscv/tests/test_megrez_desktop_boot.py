# SPDX-License-Identifier: MPL-2.0

import hashlib
import io
import json
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from tools.riscv import megrez_desktop_boot as boot


class DesktopBootFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        artifacts = {}
        for name, payload in (
            ("kernel", b"kernel-image"),
            ("initramfs", b"stage1-cpio"),
            ("megrez_dtb", b"prepared-dtb"),
            ("root_image", b"root-identity-only"),
        ):
            path = self.root / name
            path.write_bytes(payload)
            artifacts[name] = {
                "name": name,
                "path": str(path),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "crc32": f"{zlib.crc32(payload):08x}",
                "load_address": {
                    "kernel": 0x80200000,
                    "initramfs": 0x83000000,
                    "megrez_dtb": 0xF0000000,
                    "root_image": 0,
                }[name],
            }
        self.plan = self.root / "plan.json"
        self.plan.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "profile": "debian-browser",
                    "smp": 4,
                    "sv39": True,
                    "reboot_after": 600,
                    "bootargs": "console=ttyS0 init=/init -- --root-init=systemd",
                    "markers": [],
                    "artifacts": list(artifacts.values()),
                }
            )
        )


class DesktopBootManifestTests(DesktopBootFixture):
    def test_desktop_boot_keeps_a_userspace_sync_recovery_window(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        self.assertIn("asterinas.reboot_after=300", manifest.bootargs)
        self.assertIn(
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=180", manifest.bootargs
        )
        self.assertLessEqual(len(manifest.bootargs.encode()) + 1, 1024)

    def test_manifest_is_canonical_and_content_addressed(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        document = json.loads(manifest.canonical_bytes())

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["stage1_protocol_version"], 1)
        self.assertEqual(document["generation_sha256"], manifest.generation_sha256)
        self.assertEqual(len(manifest.generation_sha256), 64)
        self.assertTrue(
            manifest.generation_directory.endswith(manifest.generation_sha256[:16])
        )
        self.assertEqual(manifest.canonical_bytes(), manifest.canonical_bytes())
        self.assertNotIn(str(self.root), manifest.canonical_bytes().decode())

    def test_persistent_home_is_explicit_and_has_distinct_generation(self) -> None:
        disposable = boot.DesktopBootManifest.from_plan(self.plan)
        self.assertIn("--volatile-home", disposable.bootargs)

        document = json.loads(self.plan.read_text())
        document["persistent_home"] = True
        self.plan.write_text(json.dumps(document))
        persistent = boot.DesktopBootManifest.from_plan(self.plan)
        self.assertNotIn("--volatile-home", persistent.bootargs)
        self.assertIn("--debug-console=isolated-root", persistent.bootargs)
        self.assertNotEqual(persistent.generation_sha256, disposable.generation_sha256)

        document["persistent_home"] = "true"
        self.plan.write_text(json.dumps(document))
        with self.assertRaisesRegex(boot.DesktopBootError, "persistent_home"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_generation_uses_safe_sha_prefixed_p3_paths(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        commands = boot.uboot_load_commands(manifest)

        self.assertEqual(len(commands), 3)
        self.assertTrue(
            all(command.startswith("ext4load mmc 1:3 ") for command in commands)
        )
        self.assertTrue(all(".." not in command for command in commands))
        for name in ("kernel", "initramfs", "megrez_dtb"):
            artifact = manifest.artifacts[name]
            self.assertIn(artifact.sha256[:16], artifact.basename)
            self.assertIn(manifest.generation_directory, artifact.mmc_path)

    def test_rejects_tampered_and_unsafe_plan_artifacts(self) -> None:
        document = json.loads(self.plan.read_text())
        document["artifacts"][0]["sha256"] = "0" * 64
        self.plan.write_text(json.dumps(document))
        with self.assertRaisesRegex(boot.DesktopBootError, "identity mismatch"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_rejects_symlink_source(self) -> None:
        document = json.loads(self.plan.read_text())
        source = Path(document["artifacts"][0]["path"])
        target = source.with_suffix(".real")
        source.rename(target)
        source.symlink_to(target)

        with self.assertRaisesRegex(boot.DesktopBootError, "symbolic link"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_rejects_duplicate_artifact_names(self) -> None:
        document = json.loads(self.plan.read_text())
        document["artifacts"].append(document["artifacts"][0])
        self.plan.write_text(json.dumps(document))

        with self.assertRaisesRegex(boot.DesktopBootError, "duplicate"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_publication_script_is_atomic_idempotent_and_partition3_only(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        script = boot.publication_script(
            manifest, "http://10.100.19.216:18080", "0123456789abcdef"
        )

        self.assertIn("mktemp -d", script)
        self.assertIn("trap", script)
        self.assertIn("mv -T", script)
        self.assertIn("sha256sum -c", script)
        self.assertIn("ASTERINAS_DESKTOP_GENERATION_READY", script)
        self.assertNotIn("mmcblk1p1", script)
        self.assertNotIn("mmcblk1p2", script)
        self.assertNotIn(str(self.root), script)
        self.assertNotIn("debian", script.split("GENERATION_ROOT=", 1)[0])
        self.assertIn(f'"{manifest.artifacts["kernel"].sha256}  $WORK/', script)

    def test_publication_script_rejects_unsafe_transport_inputs(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        for base_url, nonce in (
            ("http://host/a b", "0123456789abcdef"),
            ("http://host", "../escape"),
        ):
            with self.subTest(base_url=base_url, nonce=nonce):
                with self.assertRaises(boot.DesktopBootError):
                    boot.publication_script(manifest, base_url, nonce)

    def test_prepare_uses_rockos_and_always_returns_to_uboot(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            def __init__(self) -> None:
                self.calls = []
                self.publication_transcript = (
                    "ASTERINAS_DESKTOP_GENERATION_READY "
                    f"generation={manifest.generation_sha256}"
                ).encode()

            def open(self, timeout):
                self.calls.append(("open", timeout))

            def boot_rockos(self, timeout):
                self.calls.append(("boot", timeout))

            def login(self, username, password, timeout):
                self.calls.append(("login", username, password, timeout))

            def publish(self, commands, password, timeout):
                self.calls.append(("publish", commands, password, timeout))

            def reboot_and_recover(self, password, timeout):
                self.calls.append(("recover", password, timeout))

            def close(self):
                self.calls.append(("close",))

        operations = Operations()
        transcript = boot.prepare_generation(
            manifest,
            operations,
            username="debian",
            password="secret",
            base_url="http://10.100.19.216:18080",
            nonce="0123456789abcdef",
        )

        self.assertEqual(transcript, operations.publication_transcript)
        self.assertEqual(operations.calls[0], ("open", 120))
        self.assertEqual(
            [call[0] for call in operations.calls],
            ["open", "boot", "login", "publish", "recover", "close"],
        )
        launcher = operations.calls[3][1][0]
        self.assertIn(manifest.generation_sha256[:16], launcher)
        self.assertIn("sha256sum -c", launcher)
        self.assertNotIn("secret", launcher)

    def test_prepare_recovers_after_publication_failure(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            publication_transcript = b""

            def __init__(self) -> None:
                self.calls = []

            def open(self, timeout):
                self.calls.append("open")

            def boot_rockos(self, timeout):
                self.calls.append("boot")

            def login(self, username, password, timeout):
                self.calls.append("login")

            def publish(self, commands, password, timeout):
                self.calls.append("publish")
                raise RuntimeError("interrupted")

            def reboot_and_recover(self, password, timeout):
                self.calls.append("recover")

            def close(self):
                self.calls.append("close")

        operations = Operations()
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            boot.prepare_generation(
                manifest,
                operations,
                username="debian",
                password="secret",
                base_url="http://10.100.19.216:18080",
                nonce="0123456789abcdef",
            )
        self.assertEqual(operations.calls[-2:], ["recover", "close"])


class DesktopBootMakefileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repository = Path(__file__).resolve().parents[3]
        cls.makefile = (repository / "Makefile").read_text(encoding="utf-8")

    def _recipe(self, target: str) -> str:
        return self.makefile.split(f".PHONY: {target}", 1)[1].split(
            ".PHONY:", 1
        )[0]

    def test_makefile_exposes_stable_desktop_boot_defaults(self) -> None:
        self.assertIn(
            "MEGREZ_DESKTOP_BOOT_PLAN ?= "
            "$(CURDIR)/target/megrez-desktop-boot/build/plan.json",
            self.makefile,
        )
        self.assertIn(
            "MEGREZ_DESKTOP_BOOT_DEVICE ?= "
            "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0",
            self.makefile,
        )
        self.assertIn("MEGREZ_DESKTOP_BOOT_OUTPUT_ROOT ?=", self.makefile)

    def test_makefile_start_is_only_the_bounded_start_action(self) -> None:
        recipe = self._recipe("run_riscv_megrez_desktop")
        self.assertIn("tools.riscv.megrez_desktop_boot start", recipe)
        for variable in (
            "MEGREZ_DESKTOP_BOOT_PLAN",
            "MEGREZ_DESKTOP_BOOT_DEVICE",
            "MEGREZ_DESKTOP_BOOT_START_OUTPUT",
        ):
            self.assertIn(f"$({variable})", recipe)
        for forbidden in (
            " prepare ",
            "install",
            "fixture",
            "qualification",
            "performance",
        ):
            self.assertNotIn(forbidden, recipe)

    def test_makefile_exposes_explicit_desktop_device_profile(self) -> None:
        recipe = self._recipe("run_riscv_megrez_desktop")
        self.assertIn("MEGREZ_DESKTOP_BOOT_DEVICE_PROFILE ?= full", self.makefile)
        self.assertIn("--device-profile", recipe)
        self.assertIn("$(MEGREZ_DESKTOP_BOOT_DEVICE_PROFILE)", recipe)

    def test_makefile_prepare_is_explicit_and_overridable(self) -> None:
        recipe = self._recipe("prepare_riscv_megrez_desktop_boot")
        self.assertIn("tools.riscv.megrez_desktop_boot prepare", recipe)
        for variable in (
            "MEGREZ_DESKTOP_BOOT_PLAN",
            "MEGREZ_DESKTOP_BOOT_DEVICE",
            "MEGREZ_DESKTOP_BOOT_HOST_ADDRESS",
            "MEGREZ_DESKTOP_BOOT_PREPARE_OUTPUT",
        ):
            self.assertIn(f"$({variable})", recipe)
        self.assertIn("--factory-login", recipe)


class DesktopDeviceProfileTests(unittest.TestCase):
    def test_minimal_profile_disables_risky_board_devices_for_one_boot(self) -> None:
        commands = boot.uboot_device_profile_commands("minimal")

        self.assertEqual(
            commands,
            (
                "fdt set /soc/ethernet@50400000 status disabled",
                "fdt set /soc/ethernet@50410000 status disabled",
                "fdt set /soc/usb0@50480000/dwc3@50480000 status disabled",
                "fdt set /soc/usb1@50490000/dwc3@50490000 status disabled",
            ),
        )

    def test_full_profile_keeps_existing_device_tree_unchanged(self) -> None:
        self.assertEqual(boot.uboot_device_profile_commands("full"), ())

    def test_unknown_profile_is_rejected_before_serial_use(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown desktop device profile"):
            boot.uboot_device_profile_commands("unsafe")

    def test_minimal_profile_uses_offline_local_firefox_bootargs(self) -> None:
        online_args = (
            "console=ttyS0 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=17893 "
            "systemd.setenv=ASTERINAS_DESKTOP_DNS_HOST=10.100.19.216 "
            "systemd.setenv=ASTERINAS_DESKTOP_DNS_PORT=15354 "
            "-- --root-init=systemd"
        )
        minimal_args = boot.profile_bootargs("minimal", online_args)
        self.assertNotIn("ASTERINAS_DESKTOP_PROXY_HOST", minimal_args)
        self.assertNotIn("ASTERINAS_DESKTOP_PROXY_PORT", minimal_args)
        self.assertNotIn("ASTERINAS_DESKTOP_DNS_HOST", minimal_args)
        self.assertNotIn("ASTERINAS_DESKTOP_DNS_PORT", minimal_args)
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_OFFLINE=1",
            minimal_args,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct",
            minimal_args,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_START_URL="
            "file:///usr/share/asterinas/physical-graphics/index.html",
            minimal_args,
        )
        bootargs, stage1_args = minimal_args.split(" -- ", 1)
        self.assertIn("ASTERINAS_DESKTOP_OFFLINE", bootargs)
        self.assertIn("ASTERINAS_WEB_NETWORK_MODE", bootargs)
        self.assertIn("ASTERINAS_DESKTOP_START_URL", bootargs)
        self.assertIn("ASTERINAS_BROWSER_WEB_SESSION", bootargs)
        for token in (
            "ASTERINAS_DESKTOP_OFFLINE=1",
            "ASTERINAS_WEB_NETWORK_MODE=direct",
            "ASTERINAS_DESKTOP_START_URL="
            "file:///usr/share/asterinas/physical-graphics/index.html",
            "ASTERINAS_BROWSER_WEB_SESSION=1",
        ):
            self.assertIn(token, bootargs.split())
        self.assertNotIn("systemd.setenv=", stage1_args)
        self.assertFalse(any(token.startswith("ASTERINAS_") for token in stage1_args.split()))
        self.assertEqual(boot.profile_bootargs("full", "console=ttyS0"), "console=ttyS0")


class DesktopBootStartTests(DesktopBootFixture):
    def setUp(self) -> None:
        super().setUp()
        # These exercise serial orchestration with fake operations, not the
        # workstation's optional network bridge.
        for name in ("require_proxy_reachable", "warn_if_dns_unreachable"):
            patcher = mock.patch.object(boot, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_recovery_already_consumed_by_readiness_is_retained(self) -> None:
        recovery = b"OpenSBI v1.5\nU-Boot 2024\nrockos-eswin login:"
        operations = boot.RealStartOperations("unused")
        operations._boot_started = time.monotonic() - 300
        operations._serial = mock.Mock(transcript=b"guest failure\n" + recovery)
        operations._session = mock.Mock()

        self.assertEqual(operations.await_recovery(360), recovery)
        operations._session.wait_for_uboot_prompt.assert_not_called()

    def test_recovery_wait_accepts_a_direct_rockos_boundary(self) -> None:
        operations = boot.RealStartOperations("unused")
        operations._boot_started = time.monotonic()
        operations._session = mock.Mock()
        operations._session.wait_for_recovery_boundary.return_value = (
            "OpenSBI v1.5\nU-Boot 2024\nrockos-eswin login:"
        )

        recovery = operations.await_recovery(30)

        self.assertIn(b"rockos-eswin login:", recovery)
        operations._session.wait_for_recovery_boundary.assert_called_once()
        operations._session.wait_for_uboot_prompt.assert_not_called()

    def test_rockos_login_without_new_firmware_epoch_is_not_recovery(self) -> None:
        self.assertEqual(boot._recovery_plane(b"rockos-eswin login:"), "unknown")
        self.assertEqual(boot._recovery_plane(b"OpenSBI\nU-Boot\n"), "unknown")

    def _ready_transcript(self) -> bytes:
        return "\n".join(marker for _, marker in boot.PHASE_MARKERS).encode()

    def test_phase_parser_requires_exact_order_without_duplicates(self) -> None:
        observed = boot.observe_ready_phases(
            self._ready_transcript(), now=time.monotonic
        )
        self.assertEqual(tuple(observed), tuple(name for name, _ in boot.PHASE_MARKERS))

        markers = [marker for _, marker in boot.PHASE_MARKERS]
        with self.assertRaisesRegex(boot.DesktopBootError, "out of order"):
            boot.observe_ready_phases("\n".join(markers[1:] + markers[:1]).encode())
        with self.assertRaisesRegex(boot.DesktopBootError, "duplicated"):
            boot.observe_ready_phases(
                "\n".join(markers[:2] + [markers[1]] + markers[2:]).encode()
            )

    def test_start_validates_artifacts_and_read_only_admission(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            def __init__(self, transcript):
                self.transcript = transcript
                self.phase_times = {
                    name: float(index)
                    for index, (name, _) in enumerate(boot.PHASE_MARKERS)
                }
                self.boot_epoch_started = False
                self.calls = []

            def open(self, timeout):
                self.calls.append(("open", timeout))

            def load(self, supplied, timeout):
                self.calls.append(("load", timeout))
                return {
                    name: supplied.artifacts[name].size for name in boot.ARTIFACT_NAMES
                }

            def boot(self, supplied, timeout):
                self.calls.append(("boot", timeout))
                self.boot_epoch_started = True

            def wait_ready(self, timeout):
                self.calls.append(("wait_ready", timeout))
                return self.transcript

            def probe(self, timeout):
                self.calls.append(("probe", timeout))
                return {
                    "boot_id": "11111111-2222-3333-4444-555555555555",
                    "firefox_pid": 122,
                    "firefox_uid": 1000,
                    "visible_windows": 1,
                    "watchdog": 0,
                    "local_page": True,
                    "debug_console": True,
                    "x11_socket": True,
                }

            def await_recovery(self, timeout):
                self.calls.append(("recover", timeout))
                return b"OpenSBI\nU-Boot\n=> "

            def close(self):
                self.calls.append(("close",))

        operations = Operations(self._ready_transcript())
        result = boot.start_generation(manifest, operations)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["firefox_pid"], 122)
        self.assertEqual(result["phases"], operations.phase_times)
        self.assertEqual(
            [call[0] for call in operations.calls],
            ["open", "load", "boot", "wait_ready", "probe", "close"],
        )

    def test_minimal_profile_does_not_require_network_proxy(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            device_profile = "minimal"
            boot_epoch_started = False
            phase_times = {
                name: float(index) for index, (name, _) in enumerate(boot.PHASE_MARKERS)
            }
            transcript = b"\n".join(marker.encode() for _, marker in boot.PHASE_MARKERS)

            def open(self, timeout):
                return None

            def load(self, supplied, timeout):
                return {
                    name: supplied.artifacts[name].size for name in boot.ARTIFACT_NAMES
                }

            def boot(self, supplied, timeout):
                self.boot_epoch_started = True

            def wait_ready(self, timeout):
                return self.transcript

            def probe(self, timeout):
                return {
                    "boot_id": "11111111-2222-3333-4444-555555555555",
                    "firefox_pid": 122,
                    "firefox_uid": 1000,
                    "visible_windows": 1,
                    "watchdog": 0,
                    "local_page": True,
                    "debug_console": True,
                    "x11_socket": True,
                }

            def close(self):
                return None

        operations = Operations()
        with mock.patch.object(
            boot, "require_proxy_reachable", side_effect=AssertionError("proxy")
        ), mock.patch.object(
            boot, "warn_if_dns_unreachable", side_effect=AssertionError("dns")
        ):
            result = boot.start_generation(manifest, operations)
        self.assertEqual(result["status"], "pass")

    def test_start_waits_for_firmware_recovery_after_guest_failure(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            def __init__(self):
                self.transcript = (
                    b"\n".join(marker.encode() for _, marker in boot.PHASE_MARKERS[:5])
                    + b"\nASTERINAS_DESKTOP_BOOT_WAIT reason=firefox-window remaining=209\n"
                )
                self.phase_times = {
                    name: float(index)
                    for index, (name, _) in enumerate(boot.PHASE_MARKERS[:5])
                }
                self.boot_epoch_started = False
                self.calls = []

            def open(self, timeout):
                self.calls.append("open")

            def load(self, supplied, timeout):
                self.calls.append("load")
                return {
                    name: supplied.artifacts[name].size for name in boot.ARTIFACT_NAMES
                }

            def boot(self, supplied, timeout):
                self.calls.append("boot")
                self.boot_epoch_started = True

            def wait_ready(self, timeout):
                self.calls.append("wait_ready")
                raise TimeoutError("desktop readiness deadline expired")

            def probe(self, timeout):
                raise AssertionError("probe must not run")

            def await_recovery(self, timeout):
                self.calls.append(("recover", timeout))
                return b"OpenSBI\nU-Boot 2024\n=> "

            def close(self):
                self.calls.append("close")

        operations = Operations()
        result = boot.start_generation(manifest, operations)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["recovered_to_uboot"])
        self.assertEqual(result["phases"], operations.phase_times)
        self.assertEqual(
            result["reason"],
            "desktop readiness deadline expired; last readiness "
            "reason=firefox-window remaining=209",
        )
        self.assertIn(("recover", 360), operations.calls)

    def test_start_records_direct_rockos_recovery_boundary(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            device_profile = "minimal"
            def __init__(self):
                self.transcript = b"ASTERINAS_DESKTOP_BOOT_WAIT reason=browser-start remaining=90\n"
                self.phase_times = {
                    name: float(index)
                    for index, (name, _) in enumerate(boot.PHASE_MARKERS[:5])
                }
                self.boot_epoch_started = False

            def open(self, timeout):
                return None

            def load(self, supplied, timeout):
                return {
                    name: supplied.artifacts[name].size for name in boot.ARTIFACT_NAMES
                }

            def boot(self, supplied, timeout):
                self.boot_epoch_started = True

            def wait_ready(self, timeout):
                raise TimeoutError("desktop readiness deadline expired")

            def probe(self, timeout):
                raise AssertionError("probe must not run")

            def await_recovery(self, timeout):
                return b"OpenSBI v1.5\nU-Boot 2024\nrockos-eswin login:"

            def close(self):
                return None

        result = boot.start_generation(manifest, Operations())
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["recovery_boundary_observed"])
        self.assertEqual(result["recovery_plane"], "rockos")
        self.assertFalse(result["recovered_to_uboot"])

    def test_boot_command_failure_after_epoch_still_waits_for_recovery(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            transcript = b"Enter riscv_boot\n"
            phase_times = {"kernel-entered": 1.0}
            boot_epoch_started = False

            def __init__(self):
                self.calls = []

            def open(self, timeout):
                self.calls.append("open")

            def load(self, supplied, timeout):
                self.calls.append("load")
                return {
                    name: supplied.artifacts[name].size for name in boot.ARTIFACT_NAMES
                }

            def boot(self, supplied, timeout):
                self.calls.append("boot")
                self.boot_epoch_started = True
                raise TimeoutError("kernel entry marker timed out")

            def wait_ready(self, timeout):
                raise AssertionError("readiness must not run")

            def probe(self, timeout):
                raise AssertionError("probe must not run")

            def await_recovery(self, timeout):
                self.calls.append("recover")
                return b"OpenSBI\nU-Boot\n=> "

            def close(self):
                self.calls.append("close")

        operations = Operations()
        result = boot.start_generation(manifest, operations)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(operations.calls[-2:], ["recover", "close"])

    def test_preboot_failure_returns_result_without_guest_recovery(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            transcript = b"=> ext4load: file not found\n=> "
            phase_times = {}
            boot_epoch_started = False

            def __init__(self):
                self.calls = []

            def open(self, timeout):
                self.calls.append("open")

            def load(self, supplied, timeout):
                self.calls.append("load")
                raise boot.DesktopBootError("generation unavailable; run prepare")

            def boot(self, supplied, timeout):
                raise AssertionError("boot must not run")

            def wait_ready(self, timeout):
                raise AssertionError("readiness must not run")

            def probe(self, timeout):
                raise AssertionError("probe must not run")

            def await_recovery(self, timeout):
                raise AssertionError("guest recovery must not run")

            def close(self):
                self.calls.append("close")

        operations = Operations()
        result = boot.start_generation(manifest, operations)

        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["boot_epoch_started"])
        self.assertEqual(operations.calls, ["open", "load", "close"])

    def test_real_loader_reads_only_the_immutable_partition3_paths(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Session:
            def __init__(self) -> None:
                self.commands = []

            def command(self, command, timeout=15, expect=None):
                self.commands.append(command)
                return "1 bytes read\n=> "

            def _verify_loaded_artifact(self, name, address, crc32, output, pattern):
                self.assertions = (address, crc32, output, pattern)
                return manifest.artifacts[name].size

        session = Session()
        operations = boot.RealStartOperations("unused")
        operations._session = session
        sizes = operations.load(manifest, 30)

        self.assertEqual(
            sizes,
            {name: manifest.artifacts[name].size for name in boot.ARTIFACT_NAMES},
        )
        loads = [
            command for command in session.commands if command.startswith("ext4load")
        ]
        self.assertEqual(tuple(loads), boot.uboot_load_commands(manifest))
        self.assertFalse(
            any(
                forbidden in command
                for command in session.commands
                for forbidden in ("tftp", "loady", "curl", "wget", "mmc write")
            )
        )

    def test_minimal_profile_edits_only_the_volatile_boot_dtb(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Session:
            def __init__(self) -> None:
                self.commands = []

            def command(self, command, timeout=15, expect=None):
                self.commands.append(command)
                return "Enter riscv_boot\n=> " if command.startswith("booti ") else "=> "

            def start_boot_attempt(self):
                return None

        session = Session()
        operations = boot.RealStartOperations("unused", device_profile="minimal")
        operations._fd = 0
        operations._session = session
        with mock.patch(
            "tools.riscv.debian.rootfs.gate_runtime.SerialConsole",
            return_value=mock.Mock(),
        ):
            operations.boot(manifest, 30)

        profile = boot.uboot_device_profile_commands("minimal")
        self.assertEqual(tuple(session.commands[2:6]), profile)
        self.assertNotIn("saveenv", " ".join(session.commands))
        self.assertNotIn("mmc write", " ".join(session.commands))

    def test_missing_partition3_generation_has_prepare_instruction(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Session:
            def command(self, command, timeout=15):
                if command.startswith("ext4load"):
                    raise RuntimeError("U-Boot error: file not found")
                return "=> "

        operations = boot.RealStartOperations("unused")
        operations._session = Session()

        with self.assertRaisesRegex(
            boot.DesktopBootError, r"generation .* unavailable.*run .* prepare"
        ):
            operations.load(manifest, 30)

    def test_real_start_transcript_preserves_recovery_chronology(self) -> None:
        class Serial:
            transcript = b"guest-before-recovery\n"

        operations = boot.RealStartOperations("unused")
        operations._log.write("firmware-before-boot\n")
        operations._preboot_log_length = len(operations._log.getvalue())
        operations._serial = Serial()
        operations._log.write("firmware-after-recovery\n")

        self.assertEqual(
            operations.transcript,
            b"firmware-before-boot\nguest-before-recovery\nfirmware-after-recovery\n",
        )

    def test_real_start_reports_split_phase_marker_once(self) -> None:
        progress = io.StringIO()
        operations = boot.RealStartOperations("unused", progress_stream=progress)
        operations._boot_started = 90.0
        marker = b"DEBIAN_STAGE1_PROGRESS step=start "

        with mock.patch.object(boot.time, "monotonic", return_value=100.0):
            operations._observe_serial(marker[:17])
            operations._observe_serial(marker[17:] + marker)

        self.assertEqual(operations.phase_times, {"stage1-start": 10.0})
        self.assertEqual(
            progress.getvalue(),
            "Megrez desktop phase: stage1-start elapsed=10.000s\n",
        )

    def test_admission_probe_cannot_match_the_echoed_command(self) -> None:
        nonce = "0123456789abcdef"
        marker = f"__ASTERINAS_DESKTOP_ADMISSION_{nonce}__"
        end_marker = f"__ASTERINAS_DESKTOP_ADMISSION_END_{nonce}__"

        class Serial:
            def __init__(self) -> None:
                self.payload = b""
                self._transcript = b""

            @property
            def transcript(self):
                return self._transcript

            def checkpoint(self):
                return len(self._transcript)

            def send(self, payload, deadline):
                self.payload = payload
                self._transcript += payload
                self._transcript += (
                    f"{marker} boot_id=11111111-2222-3333-4444-555555555555 "
                    "firefox_pid=122 firefox_uid=1000 visible_windows=1 "
                    f"watchdog=0 debug_console=1 x11_socket=1 {end_marker}\n"
                ).encode()

            def wait_for(self, expected, deadline, start=0):
                if self._transcript.find(expected, start) < 0:
                    raise TimeoutError(expected)
                return self._transcript

        serial = Serial()
        operations = boot.RealStartOperations("unused")
        operations._serial = serial
        with mock.patch.object(boot.secrets, "token_hex", return_value=nonce):
            evidence = operations.probe(5)

        self.assertNotIn(marker.encode(), serial.payload)
        self.assertNotIn(end_marker.encode(), serial.payload)
        self.assertEqual(evidence["visible_windows"], 1)


if __name__ == "__main__":
    unittest.main()
