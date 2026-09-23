#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest
from pathlib import Path

from tools.riscv.validate_run_kernel_log import ValidationError, validate_transcript


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DUAL_STACK_FACTS = (
    "ASTERINAS_IPV6_DUAL_STACK_TCP_OK peer=::ffff:127.0.0.1",
    "ipv6_udp: PASS",
    "ASTERINAS_IPV6_DUAL_STACK_UDP_OK peer=::ffff:127.0.0.1",
)


class ValidateRunKernelLogTests(unittest.TestCase):
    def test_ifconf_gate_accepts_only_one_clean_completion(self) -> None:
        marker = "SIOCGIFCONF regression passed."
        validate_transcript(marker + "\n", mode="ifconf")
        for transcript in (
            "boot output\n",
            f"{marker}\n{marker}\n",
            f"{marker}\nKernel panic - not syncing\n",
        ):
            with self.subTest(transcript=transcript):
                with self.assertRaises(ValidationError):
                    validate_transcript(transcript, mode="ifconf")

    def test_ifconf_gate_is_wired_into_make_and_guest(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        runner_path = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/run_ifconf_test.sh"
        )
        general_runner = (
            REPOSITORY_ROOT / "test/initramfs/src/regression/network/run_test.sh"
        ).read_text()

        self.assertIn("else ifeq ($(AUTO_TEST), ifconf)", makefile)
        self.assertIn('/test/run_ifconf_test.sh', makefile)
        self.assertIn('--mode "ifconf"', makefile)
        self.assertEqual(
            tuple(
                line.strip()
                for line in runner_path.read_text().splitlines()
                if line.strip().startswith("/test/")
            ),
            ("/test/network/ifconf",),
        )
        self.assertEqual(general_runner.count("./ifconf"), 1)

    def test_accepts_ipv6_dual_stack_udp_transcript(self) -> None:
        try:
            validate_transcript(
                "ASTERINAS_IPV6_DUAL_STACK_UDP_OK "
                "peer=::ffff:127.0.0.1 dual-port-reserved=1 v6only-isolated=1\n",
                mode="ipv6-dual-stack-udp",
            )
        except KeyError:
            self.fail("ipv6-dual-stack-udp validation mode is not implemented")

    def test_rejects_invalid_ipv6_dual_stack_udp_transcript(self) -> None:
        marker = "ASTERINAS_IPV6_DUAL_STACK_UDP_OK peer=::ffff:127.0.0.1"
        for transcript in (
            "boot output\n",
            f"{marker}\n{marker}\n",
            f"{marker}\nKernel panic - not syncing\n",
        ):
            with self.subTest(transcript=transcript):
                try:
                    validate_transcript(transcript, mode="ipv6-dual-stack-udp")
                except ValidationError:
                    continue
                except KeyError:
                    self.fail("ipv6-dual-stack-udp validation mode is not implemented")
                self.fail("invalid IPv6 dual-stack UDP transcript was accepted")

    def test_ipv6_dual_stack_udp_gate_is_wired_into_make_and_guest(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        runner_path = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/"
            "run_ipv6_dual_stack_udp_test.sh"
        )
        general_runner = (
            REPOSITORY_ROOT / "test/initramfs/src/regression/network/run_test.sh"
        ).read_text()

        self.assertIn("else ifeq ($(AUTO_TEST), ipv6_dual_stack_udp)", makefile)
        self.assertIn('/test/run_ipv6_dual_stack_udp_test.sh', makefile)
        self.assertIn('--mode "ipv6-dual-stack-udp"', makefile)
        self.assertEqual(
            tuple(
                line.strip()
                for line in runner_path.read_text().splitlines()
                if line.strip().startswith("/test/")
            ),
            ("/test/network/ipv6_dual_stack_udp",),
        )
        self.assertEqual(general_runner.count("./ipv6_dual_stack_udp"), 1)

    def test_accepts_udp_user_buffer_prefault_transcript(self) -> None:
        try:
            validate_transcript(
                "UDP user buffer prefault regression passed.\n",
                mode="udp-user-buffer-prefault",
            )
        except KeyError:
            self.fail("udp-user-buffer-prefault validation mode is not implemented")

    def test_rejects_invalid_udp_user_buffer_prefault_transcript(self) -> None:
        marker = "UDP user buffer prefault regression passed."
        for transcript in (
            "boot output\n",
            f"{marker}\n{marker}\n",
            f"{marker}\nKernel panic - not syncing\n",
        ):
            with self.subTest(transcript=transcript):
                try:
                    validate_transcript(
                        transcript,
                        mode="udp-user-buffer-prefault",
                    )
                except ValidationError:
                    continue
                except KeyError:
                    self.fail(
                        "udp-user-buffer-prefault validation mode is not implemented"
                    )
                self.fail("invalid UDP prefault transcript was accepted")

    def test_udp_user_buffer_prefault_gate_is_wired_into_make_and_guest(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        runner_path = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/"
            "run_udp_user_buffer_prefault_test.sh"
        )
        general_runner = (
            REPOSITORY_ROOT / "test/initramfs/src/regression/network/run_test.sh"
        ).read_text()

        self.assertIn("else ifeq ($(AUTO_TEST), udp_user_buffer_prefault)", makefile)
        self.assertIn('/test/run_udp_user_buffer_prefault_test.sh', makefile)
        self.assertIn('--mode "udp-user-buffer-prefault"', makefile)
        self.assertEqual(
            tuple(
                line.strip()
                for line in runner_path.read_text().splitlines()
                if line.strip().startswith("/test/")
            ),
            ("/test/network/udp_user_buffer_prefault",),
        )
        self.assertEqual(general_runner.count("./udp_user_buffer_prefault"), 1)

    def test_accepts_native_ipv6_udp_transcript(self) -> None:
        try:
            validate_transcript("ipv6_udp: PASS\n", mode="ipv6-udp")
        except KeyError:
            self.fail("ipv6-udp validation mode is not implemented")

    def test_rejects_invalid_native_ipv6_udp_transcript(self) -> None:
        for transcript in (
            "boot output\n",
            "ipv6_udp: PASS\nipv6_udp: PASS\n",
            "ipv6_udp: PASS\nKernel panic - not syncing\n",
        ):
            with self.subTest(transcript=transcript):
                try:
                    validate_transcript(transcript, mode="ipv6-udp")
                except ValidationError:
                    continue
                except KeyError:
                    self.fail("ipv6-udp validation mode is not implemented")
                self.fail("invalid native IPv6 UDP transcript was accepted")

    def test_native_ipv6_udp_gate_is_wired_into_make_and_guest(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        runner_path = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/run_ipv6_udp_test.sh"
        )

        self.assertIn("else ifeq ($(AUTO_TEST), ipv6_udp)", makefile)
        self.assertIn('/test/run_ipv6_udp_test.sh', makefile)
        self.assertIn('--mode "ipv6-udp"', makefile)
        self.assertEqual(
            tuple(
                line.strip()
                for line in runner_path.read_text().splitlines()
                if line.strip().startswith("/test/")
            ),
            ("/test/network/ipv6_udp",),
        )

    def test_focused_network_gates_build_only_network_regressions(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        initramfs_makefile = (
            REPOSITORY_ROOT / "test/initramfs/Makefile"
        ).read_text()
        nix_default = (
            REPOSITORY_ROOT / "test/initramfs/nix/default.nix"
        ).read_text()
        nix_regression = (
            REPOSITORY_ROOT / "test/initramfs/nix/regression/default.nix"
        ).read_text()

        self.assertTrue(
            "FOCUSED_NETWORK_AUTO_TESTS" in makefile,
            "top-level focused network test set is missing",
        )
        self.assertTrue(
            'REGRESSION_TEST_DIRS := [ "network" ]' in makefile,
            "focused gates do not select only the network regression package",
        )
        self.assertTrue(
            "--arg regressionTestDirs" in initramfs_makefile,
            "initramfs Makefile does not forward the package selection",
        )
        self.assertTrue(
            "regressionTestDirs ? null" in nix_default,
            "initramfs Nix entry point does not accept the package selection",
        )
        self.assertTrue(
            "testDirs = regressionTestDirs" in nix_default,
            "initramfs Nix entry point does not forward the package selection",
        )
        self.assertTrue(
            "selectedNames" in nix_regression,
            "regression package does not select named subpackages",
        )

    def test_accepts_complete_ipv6_dual_stack_transcript(self) -> None:
        try:
            validate_transcript(
                "\n".join(("boot output", *DUAL_STACK_FACTS)),
                mode="ipv6-dual-stack",
            )
        except KeyError:
            self.fail("ipv6-dual-stack validation mode is not implemented")

    def test_rejects_missing_or_duplicate_ipv6_dual_stack_fact(self) -> None:
        cases = []
        for index in range(len(DUAL_STACK_FACTS)):
            cases.append(DUAL_STACK_FACTS[:index] + DUAL_STACK_FACTS[index + 1 :])
            cases.append(
                DUAL_STACK_FACTS[:index]
                + (DUAL_STACK_FACTS[index], DUAL_STACK_FACTS[index])
                + DUAL_STACK_FACTS[index + 1 :]
            )

        for facts in cases:
            with self.subTest(facts=facts):
                try:
                    validate_transcript("\n".join(facts), mode="ipv6-dual-stack")
                except ValidationError:
                    continue
                except KeyError:
                    self.fail("ipv6-dual-stack validation mode is not implemented")
                self.fail("invalid dual-stack transcript was accepted")

    def test_rejects_fatal_after_ipv6_dual_stack_facts(self) -> None:
        transcript = "\n".join((*DUAL_STACK_FACTS, "Kernel panic - not syncing"))
        try:
            validate_transcript(transcript, mode="ipv6-dual-stack")
        except ValidationError:
            return
        except KeyError:
            self.fail("ipv6-dual-stack validation mode is not implemented")
        self.fail("fatal dual-stack transcript was accepted")

    def test_ipv6_dual_stack_gate_is_wired_into_make_and_guest(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        runner_path = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/network/run_dual_stack_test.sh"
        )

        self.assertIn("else ifeq ($(AUTO_TEST), ipv6_dual_stack)", makefile)
        self.assertIn('/test/network/run_dual_stack_test.sh', makefile)
        self.assertIn('--mode "ipv6-dual-stack"', makefile)
        self.assertTrue(runner_path.is_file(), "dual-stack guest runner is missing")
        runner = runner_path.read_text()
        commands = tuple(
            line.strip()
            for line in runner.splitlines()
            if line.strip().startswith("./")
        )
        self.assertEqual(
            commands,
            ("./ipv6_dual_stack", "./ipv6_udp", "./ipv6_dual_stack_udp"),
        )

    def test_accepts_complete_smp4_icache_regression(self) -> None:
        validate_transcript(
            "\n".join(
                (
                    "boot output",
                    "riscv_flush_icache cross-hart passed: "
                    "cpus=4 local=0 remotes=1,2,3 generations=1024",
                    "All regression tests passed.",
                )
            ),
            mode="regression",
            require_riscv_icache_smp4=True,
        )

    def test_rejects_skip_duplicate_cpus_or_wrong_topology(self) -> None:
        cases = (
            "riscv_flush_icache cross-hart skipped: fewer than two CPUs",
            "riscv_flush_icache cross-hart passed: "
            "cpus=4 local=0 remotes=1,1,3 generations=1024",
            "riscv_flush_icache cross-hart passed: "
            "cpus=2 local=0 remotes=1 generations=1024",
        )
        for evidence in cases:
            with self.subTest(evidence=evidence), self.assertRaises(ValidationError):
                validate_transcript(
                    f"{evidence}\nAll regression tests passed.\n",
                    mode="regression",
                    require_riscv_icache_smp4=True,
                )

    def test_rejects_missing_or_duplicate_terminal_marker(self) -> None:
        for transcript in (
            "boot output\n",
            "All regression tests passed.\nAll regression tests passed.\n",
        ):
            with self.subTest(transcript=transcript), self.assertRaises(ValidationError):
                validate_transcript(transcript, mode="regression")

    def test_rejects_fatal_before_or_after_success(self) -> None:
        for transcript in (
            "Kernel panic - not syncing\nAll regression tests passed.\n",
            "All regression tests passed.\nSBI remote fence.i to hart 3 failed\n",
        ):
            with self.subTest(transcript=transcript), self.assertRaises(ValidationError):
                validate_transcript(transcript, mode="regression")

    def test_icache_contract_is_regression_only(self) -> None:
        with self.assertRaises(ValidationError):
            validate_transcript(
                "Successfully booted.\n",
                mode="boot",
                require_riscv_icache_smp4=True,
            )

    def test_formal_smp4_contract_is_wired_into_ci_and_guest(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/test_riscv.yml").read_text()
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        guest = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/process/riscv_flush_icache/"
            "riscv_flush_icache.c"
        ).read_text()
        runner = (
            REPOSITORY_ROOT / "test/initramfs/src/regression/process/run_test.sh"
        ).read_text()
        top_level_runner = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/run_regression_test.sh"
        ).read_text()

        self.assertIn("test_id: 'regression-debug-smp4'", workflow)
        self.assertIn("riscv_icache_require_smp4: '1'", workflow)
        self.assertIn('RISCV_ICACHE_REQUIRE_SMP4 ?= 0', makefile)
        self.assertIn("--require-riscv-icache-smp4", makefile)
        self.assertIn('strcmp(argv[1], "--require-smp4")', guest)
        self.assertIn("cpu_count != 4", guest)
        self.assertIn("remote_index < cpu_count", guest)
        self.assertIn("select_current_cpu_as_local(cpus, cpu_count)", guest)
        self.assertIn("wait_for_cpu(context->cpu)", guest)
        self.assertIn("wait_for_cpu(cpus[0])", guest)
        self.assertIn("RISCV_ICACHE_REQUIRE_SMP4=1", runner)
        self.assertIn("formal SMP4 cross-hart I-cache regression", top_level_runner)
        self.assertIn(
            '"${SCRIPT_DIR}/process/riscv_flush_icache/riscv_flush_icache" --require-smp4',
            top_level_runner,
        )
        self.assertLess(
            top_level_runner.index("RISCV_ICACHE_REQUIRE_SMP4=1"),
            top_level_runner.index("for dir in"),
        )


if __name__ == "__main__":
    unittest.main()
