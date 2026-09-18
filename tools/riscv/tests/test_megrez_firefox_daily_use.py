#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the one-shot physical Firefox daily-use runner."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tools.riscv.debian.rootfs.browser_daily_use_contract import FUNCTION_GROUPS
from tools.riscv.megrez_firefox_daily_use import (
    DailyUsePhysicalConfig,
    RealDailyUsePhysicalOperations,
    daily_use_physical_bootargs,
    main,
    run_firefox_daily_use,
)
from tools.riscv.megrez_physical_graphics import DailyUseTerminalStatus
from tools.riscv.tests.test_browser_daily_use_contract import complete_result


REPOSITORY_ROOT = Path(__file__).parents[3]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
FAST_CHECK = REPOSITORY_ROOT / "tools/riscv/firefox_fast_check.sh"
README = REPOSITORY_ROOT / "tools/riscv/README.md"
EXPERIMENT_ID = "fedcba9876543210fedcba9876543210"
GATE_RUN_ID = "0123456789abcdef0123456789abcdef"
COMPONENT_NAMES = (
    "browser-fixture-capture.json",
    "browser-local-capture.json",
    "browser-context-switch.json",
    "browser-composite-capture.json",
    "browser-system-time.json",
    "browser-thread-time.json",
)
RESULT_NAME = "browser-daily-use-result.json"


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def valid_pass_bundle(
    *,
    experiment_id: str = EXPERIMENT_ID,
    gate_run_id: str = GATE_RUN_ID,
    identity_changed: bool = False,
    coverage: bool = True,
) -> bytes:
    components = {
        COMPONENT_NAMES[0]: canonical({"fixture": "pass"}),
        COMPONENT_NAMES[1]: canonical({"local": "pass"}),
        COMPONENT_NAMES[2]: canonical({"context": "pass"}),
        COMPONENT_NAMES[3]: canonical(
            {
                "run_id": gate_run_id,
                "workload_start_observed_guest_monotonic_ns": 200,
                "phase_observations": [
                    {"phase": name, "observed_guest_monotonic_ns": 300 + index}
                    for index, name in enumerate(FUNCTION_GROUPS)
                ],
            }
        ),
        COMPONENT_NAMES[4]: canonical(
            {
                "intervals": [
                    {
                        "guest_monotonic_start_ns": 100 if coverage else 250,
                        "guest_monotonic_end_ns": 500,
                    }
                ]
            }
        ),
        COMPONENT_NAMES[5]: canonical(
            {
                "intervals": [
                    {
                        "guest_monotonic_start_ns": 100,
                        "guest_monotonic_end_ns": 500 if coverage else 250,
                    }
                ]
            }
        ),
    }
    result = complete_result()
    result["runId"] = gate_run_id
    if identity_changed:
        result["identities"]["firefox"]["final"]["startTimeTicks"] += 1
    result["artifacts"] = [
        {
            "name": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in components.items()
    ]
    result["attribution"] = {
        "compositeArtifact": COMPONENT_NAMES[3],
        "systemArtifact": COMPONENT_NAMES[4],
        "threadArtifact": COMPONENT_NAMES[5],
    }
    components[RESULT_NAME] = canonical(result)
    rows = [
        {
            "name": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "base64": base64.b64encode(payload).decode(),
        }
        for name, payload in components.items()
    ]
    return canonical(
        {
            "schemaVersion": 1,
            "experimentId": experiment_id,
            "gateRunId": gate_run_id,
            "outcome": "pass",
            "artifacts": rows,
        }
    )


def mutate_bundle_result(raw: bytes, mutation) -> bytes:
    bundle = json.loads(raw)
    result_row = bundle["artifacts"][-1]
    result = json.loads(base64.b64decode(result_row["base64"]))
    mutation(result)
    payload = canonical(result)
    result_row.update(
        {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "base64": base64.b64encode(payload).decode(),
        }
    )
    return canonical(bundle)


@dataclass(frozen=True)
class Artifact:
    name: str
    sha256: str


class Plan:
    plan_sha256 = "1" * 64
    bootargs = "console=ttyS0 init=/init -- --root-init=systemd"
    artifacts = (
        Artifact("kernel", "2" * 64),
        Artifact("initramfs", "3" * 64),
        Artifact("megrez_dtb", "4" * 64),
        Artifact("root_manifest", "5" * 64),
    )
    smp = 4
    sv39 = True
    profile = "debian-browser"

    def validate(self) -> None:
        return None


class FakeFixture:
    def __init__(self, owner, config) -> None:
        self.owner = owner
        self.config = config

    def start(self):
        self.owner.calls.append("fixture-start")
        return self

    def wait_for_daily_use_evidence(self, _timeout: float) -> bytes:
        if self.owner.bundle is None:
            raise TimeoutError("no upload")
        return self.owner.bundle

    def daily_use_evidence_summary(self):
        if self.owner.bundle is None:
            return None
        return {
            "bytes": len(self.owner.bundle),
            "sha256": hashlib.sha256(self.owner.bundle).hexdigest(),
        }

    def close(self) -> None:
        self.owner.calls.append("fixture-close")


class FakeOperations:
    def __init__(
        self,
        *,
        bundle: bytes | None = None,
        terminal: DailyUseTerminalStatus | None = None,
        daily_use_error: BaseException | None = None,
        recovery_error: BaseException | None = None,
    ) -> None:
        self.bundle = bundle
        self.terminal = terminal or DailyUseTerminalStatus(EXPERIMENT_ID, "pass", 0, 0)
        self.daily_use_error = daily_use_error
        self.recovery_error = recovery_error
        self.calls: list[str] = []
        self.guest_started = False
        self.transcript = "serial evidence\n"
        self.published = None

    def fixture_factory(self, config):
        self.fixture_config = config
        return FakeFixture(self, config)

    def invalidate(self) -> None:
        self.calls.append("invalidate")

    def open(self, _timeout: float) -> None:
        self.calls.append("open")

    def ensure_artifacts(self, _plan, _timeout: float):
        self.calls.append("ensure-artifacts")
        return ("kernel:cached", "initramfs:cached", "megrez_dtb:cached")

    def boot(self, _plan, _bootargs: str, _timeout: float) -> None:
        self.calls.append("boot")
        self.guest_started = True

    def prove_graphical_readiness(self, _timeout: float):
        self.calls.append("graphical-readiness")
        return SimpleNamespace(browser_pid=4242)

    def run_daily_use_profile(self, *_args):
        self.calls.append("daily-use")
        if self.daily_use_error is not None:
            raise self.daily_use_error
        return self.terminal

    def request_reboot(self, _timeout: float) -> None:
        self.calls.append("reboot")

    def await_recovery(self, _timeout: float) -> None:
        self.calls.append("recovery")
        if self.recovery_error is not None:
            raise self.recovery_error

    def publish_daily_use(self, *values) -> None:
        self.calls.append("publish")
        self.published = values

    def close(self) -> None:
        self.calls.append("close")


class DailyUseRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.plan = Plan()
        self.config = DailyUsePhysicalConfig(
            device=Path("/dev/serial/by-id/usb-test"),
            output_directory=Path(self.temporary.name) / "run",
        )

    def run_gate(self, operations: FakeOperations):
        return run_firefox_daily_use(
            self.plan,
            self.config,
            operations,
            experiment_id=EXPERIMENT_ID,
            fixture_factory=operations.fixture_factory,
            artifact_validator=lambda _plan: {},
        )

    def test_successful_run_has_exact_phase_order_and_qualified_result(self) -> None:
        operations = FakeOperations(bundle=valid_pass_bundle())

        result = self.run_gate(operations)

        self.assertTrue(result.passed)
        self.assertTrue(result.qualified)
        self.assertTrue(result.recovered)
        self.assertEqual(
            operations.calls,
            [
                "invalidate",
                "fixture-start",
                "open",
                "ensure-artifacts",
                "boot",
                "graphical-readiness",
                "daily-use",
                "reboot",
                "recovery",
                "publish",
                "fixture-close",
                "close",
            ],
        )
        self.assertEqual(operations.fixture_config.allowed_peer, "10.100.19.200")

    def test_http_receipt_without_matching_terminal_is_failure(self) -> None:
        operations = FakeOperations(
            bundle=valid_pass_bundle(),
            terminal=DailyUseTerminalStatus(EXPERIMENT_ID, "fail", 1, 0),
        )

        result = self.run_gate(operations)

        self.assertFalse(result.passed)
        self.assertFalse(result.qualified)
        self.assertIn("terminal-pass", result.reason)

    def test_failure_still_attempts_bounded_recovery_and_publishes(self) -> None:
        operations = FakeOperations(
            bundle=None, daily_use_error=TimeoutError("profile")
        )

        result = self.run_gate(operations)

        self.assertFalse(result.passed)
        self.assertIn("reboot", operations.calls)
        self.assertIn("recovery", operations.calls)
        self.assertIn("publish", operations.calls)
        self.assertEqual(operations.calls[-1], "close")

    def test_bundle_identity_coverage_and_recovery_are_qualification_inputs(self):
        cases = (
            (None, None, "bundle-pass"),
            (b"not-json", None, "failure="),
            (valid_pass_bundle(experiment_id="a" * 32), None, "failure="),
            (valid_pass_bundle(identity_changed=True), None, "identities-stable"),
            (valid_pass_bundle(coverage=False), None, "sampler-coverage"),
            (valid_pass_bundle(), RuntimeError("recovery failed"), "recovered"),
        )
        for bundle, recovery_error, reason in cases:
            with self.subTest(reason=reason, bundle=bundle):
                operations = FakeOperations(
                    bundle=bundle, recovery_error=recovery_error
                )
                result = self.run_gate(operations)
                self.assertFalse(result.qualified)
                if reason is not None:
                    self.assertIn(reason, result.reason)

    def test_bundle_and_result_gate_run_must_agree(self) -> None:
        raw = json.loads(valid_pass_bundle(gate_run_id="b" * 32))
        raw["gateRunId"] = "c" * 32
        operations = FakeOperations(bundle=canonical(raw))

        result = self.run_gate(operations)

        self.assertFalse(result.qualified)
        self.assertIn("failure=", result.reason)

    def test_failed_function_groups_and_missing_attribution_fail_closed(self) -> None:
        def fail_groups(result) -> None:
            result["state"] = "fail"
            for group in result["functionGroups"]:
                group.update({"state": "fail", "reason": "fixture-capability-failed"})

        def remove_attribution(result) -> None:
            result["attribution"].pop("threadArtifact")

        for mutation in (fail_groups, remove_attribution):
            with self.subTest(mutation=mutation.__name__):
                operations = FakeOperations(
                    bundle=mutate_bundle_result(valid_pass_bundle(), mutation)
                )
                result = self.run_gate(operations)
                self.assertFalse(result.qualified)
                self.assertIn("failure=", result.reason)

    def test_wrong_fixture_identity_and_existing_output_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DailyUsePhysicalConfig(
                device=self.config.device,
                output_directory=self.config.output_directory,
                board_peer="10.100.19.201",
            )
        self.config.output_directory.mkdir()
        operations = FakeOperations(bundle=valid_pass_bundle())
        with self.assertRaisesRegex(Exception, "output"):
            self.run_gate(operations)

    def test_base_exception_recovers_publishes_closes_and_reraises(self) -> None:
        operations = FakeOperations(bundle=None, daily_use_error=KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            self.run_gate(operations)

        self.assertIn("recovery", operations.calls)
        self.assertIn("publish", operations.calls)
        self.assertEqual(operations.calls[-2:], ["fixture-close", "close"])

    def test_daily_bootargs_are_separate_from_normal_physical_bootargs(self) -> None:
        from tools.riscv.megrez_physical_graphics import physical_bootargs

        normal = physical_bootargs(self.plan).split()
        daily = daily_use_physical_bootargs(self.plan).split()
        frozen = (
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1",
            "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
            "systemd.setenv=ASTERINAS_PHYSICAL_DAILY_USE=1",
            "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL="
            "http://10.100.19.216:17894/asterinas-network-probe.bin",
        )
        for token in frozen:
            self.assertNotIn(token, normal)
            self.assertEqual(daily.count(token), 1)

    def test_real_publication_is_private_atomic_and_manifest_last(self) -> None:
        output = self.config.output_directory
        bundle_raw = valid_pass_bundle()
        from tools.riscv.debian.rootfs.browser_daily_use_upload import parse_bundle

        bundle = parse_bundle(bundle_raw, EXPERIMENT_ID)
        result = self.run_gate(FakeOperations(bundle=bundle_raw))
        operations = RealDailyUsePhysicalOperations.__new__(
            RealDailyUsePhysicalOperations
        )
        operations._output_path = output
        operations._output = None
        operations._real = SimpleNamespace(close=lambda: None)
        operations.invalidate()
        operations.publish_daily_use(
            result,
            bundle,
            "serial\n",
            ("kernel:cached",),
            {
                "bytes": len(bundle_raw),
                "sha256": hashlib.sha256(bundle_raw).hexdigest(),
            },
        )
        names = {path.name for path in output.iterdir()}
        self.assertIn("sha256-manifest.json", names)
        self.assertFalse(any(name.startswith(".gate-") for name in names))
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        manifest = json.loads((output / "sha256-manifest.json").read_bytes())
        self.assertNotIn(
            "sha256-manifest.json", [row["name"] for row in manifest["files"]]
        )
        operations.close()

    def test_prepare_only_prints_exact_non_prepare_command_without_opening(
        self,
    ) -> None:
        plan_path = Path(self.temporary.name) / "plan.json"
        plan_path.write_text("{}", encoding="utf-8")
        output = Path(self.temporary.name) / "prepared"
        opened = []
        with (
            mock.patch(
                "tools.riscv.megrez_firefox_daily_use._read_plan",
                return_value=self.plan,
            ),
            mock.patch(
                "tools.riscv.megrez_firefox_daily_use._validate_physical_artifacts",
                return_value={},
            ),
            mock.patch(
                "tools.riscv.megrez_firefox_daily_use.RealDailyUsePhysicalOperations",
                side_effect=lambda *args, **kwargs: opened.append((args, kwargs)),
            ),
            mock.patch("builtins.print") as printed,
        ):
            status = main(
                [
                    "/dev/serial/by-id/usb-test",
                    "--plan",
                    str(plan_path),
                    "--output-directory",
                    str(output),
                    "--profile-timeout",
                    "120",
                    "--prepare-only",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(opened, [])
        command = printed.call_args.args[0]
        self.assertIn("/dev/serial/by-id/usb-test", command)
        self.assertIn(f"--plan {plan_path}", command)
        self.assertIn(f"--output-directory {output}", command)
        self.assertIn("--profile-timeout 120", command)
        self.assertNotIn("--prepare-only", command)


class DailyUseEntryPointTests(unittest.TestCase):
    def test_makefile_exposes_complete_physical_daily_use_unit_gate(self) -> None:
        source = MAKEFILE.read_text(encoding="utf-8")
        target = source.split(
            "test_riscv_firefox_daily_use_physical_unit:", 1
        )[1].split("\n.PHONY:", 1)[0]
        for module in (
            "test_browser_daily_use_contract",
            "test_browser_daily_use_gate",
            "test_browser_daily_use_upload",
            "test_megrez_network_fixture",
            "test_debian_rootfs",
            "test_megrez_physical_graphics",
            "test_megrez_firefox_daily_use",
            "test_megrez_firefox_daily_use_report",
        ):
            self.assertIn(f"tools.riscv.tests.{module}", target)

    def test_prepare_target_is_validation_only_and_fail_closed(self) -> None:
        source = MAKEFILE.read_text(encoding="utf-8")
        target = source.split("prepare_riscv_megrez_firefox_daily_use:", 1)[1].split(
            "\n.PHONY:", 1
        )[0]
        for fragment in (
            "MEGREZ_FIREFOX_DAILY_USE_PLAN",
            "MEGREZ_FIREFOX_DAILY_USE_DEVICE",
            "MEGREZ_FIREFOX_DAILY_USE_OUTPUT",
            "MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND",
            "MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER",
            "/dev/serial/by-id/",
            "10.100.19.216",
            "10.100.19.200",
            "MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL",
            "MEGREZ_FIREFOX_DAILY_USE_MMC_INITRAMFS",
            "MEGREZ_FIREFOX_DAILY_USE_MMC_DTB",
            "tools.riscv.megrez_firefox_daily_use",
            "--prepare-only",
        ):
            self.assertIn(fragment, target)
        self.assertIn('test ! -e "$(MEGREZ_FIREFOX_DAILY_USE_OUTPUT)"', target)

    def test_fast_check_and_readme_cover_three_one_shot_runs(self) -> None:
        fast = FAST_CHECK.read_text(encoding="utf-8")
        for module in (
            "test_browser_daily_use_upload",
            "test_megrez_physical_graphics",
            "test_megrez_firefox_daily_use",
            "test_megrez_firefox_daily_use_report",
        ):
            self.assertIn(f"tools.riscv.tests.{module}", fast)
        readme = README.read_text(encoding="utf-8")
        for fragment in (
            "one profile per boot",
            "run-a1",
            "run-a2",
            "run-a3",
            "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0",
            "10.100.19.216:17894",
            "10.100.19.200",
            "target/firefox-daily-use-physical/",
            "megrez_firefox_daily_use_report",
            "qualified",
        ):
            self.assertIn(fragment, readme)


if __name__ == "__main__":
    unittest.main()
