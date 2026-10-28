#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class PreparationContractTests(unittest.TestCase):
    def test_variant_preparation_packages_only_payload_dtb(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn('variant="${QEMU_UBOOT_VARIANT:-}"', script)
        self.assertIn('qemu_uboot_variants.py" validate', script)
        self.assertIn('qemu_uboot_dtb.py" derive-variant', script)
        self.assertIn('qemu_uboot_dtb.py" audit-existing', script)
        variant_offset = script.index('variant="${QEMU_UBOOT_VARIANT:-}"')
        validation_offset = script.index('qemu_uboot_variants.py" validate')
        mkdir_offset = script.index('mkdir -p "${out_dir}" "${cache_dir}"')
        derive_offset = script.index('qemu_uboot_dtb.py" derive-variant')
        audit_offset = script.index('qemu_uboot_dtb.py" audit-existing')
        stage_copy = (
            'cp "${out_dir}/${dtb_filename}" "${stage_dir}/${dtb_filename}"'
        )

        self.assertLess(variant_offset, validation_offset)
        self.assertLess(validation_offset, mkdir_offset)
        self.assertLess(derive_offset, audit_offset)
        self.assertIn('--source-dtb "${out_dir}/qemu-virt.source.dtb"', script)
        self.assertIn('--payload-dtb "${out_dir}/${dtb_filename}"', script)
        self.assertIn(
            '--audit-output "${out_dir}/qemu-dtb-variant-audit.json"',
            script,
        )
        self.assertIn(stage_copy, script)
        self.assertNotIn('qemu-virt.source.dtb" "${stage_dir}', script)
        self.assertNotIn('qemu-dtb-audit.json" "${stage_dir}', script)
        self.assertNotIn('qemu-dtb-variant-audit.json" "${stage_dir}', script)
        for evidence in (
            '"${out_dir}/qemu-virt.source.dtb"',
            '"${out_dir}/${dtb_filename}"',
            '"${out_dir}/qemu-dtb-variant-audit.json"',
            '"${out_dir}/qemu-dtb-audit.json"',
            '"${out_dir}/boot.ext4"',
        ):
            self.assertIn(evidence, script)

    def test_preparation_rejects_unknown_variant_before_side_effects(self) -> None:
        output_root = REPO_ROOT / "target/qemu-uboot"
        output_root.mkdir(parents=True, exist_ok=True)
        cases = (
            ("unknown variant", "not-registered", None, "unknown QEMU U-Boot variant"),
            (
                "registered variant with default profile",
                FIRST_PROCESS_CONSOLE_LOSS.name,
                None,
                f"requires profile {FIRST_PROCESS_CONSOLE_LOSS.base_profile_name}",
            ),
            (
                "registered variant with explicit generic profile",
                FIRST_PROCESS_CONSOLE_LOSS.name,
                "generic-sv39",
                f"requires profile {FIRST_PROCESS_CONSOLE_LOSS.base_profile_name}",
            ),
        )
        for label, variant, profile, expected_error in cases:
            with (
                self.subTest(label=label),
            ):
                # These cases fail during variant/profile validation, before
                # the builder creates any output.  Do not require write access
                # to a possibly root-owned persistent cache directory merely
                # to exercise the no-side-effect contract.
                root = output_root / f"validation-{os.getpid()}-{label.replace(' ', '-')}"
                out_dir = root / "out"
                cache_dir = root / "cache"
                environment = {
                    "PATH": os.environ["PATH"],
                    "ASTERINAS_RISCV_BOOTI": str(root / "missing-kernel"),
                    "ASTERINAS_INITRAMFS": str(root / "missing-initramfs"),
                    "QEMU_UBOOT_OUT_DIR": str(out_dir),
                    "QEMU_UBOOT_CACHE_DIR": str(cache_dir),
                    "QEMU_UBOOT_VARIANT": variant,
                }
                if profile is not None:
                    environment["QEMU_UBOOT_PROFILE"] = profile
                result = subprocess.run(
                    ["bash", str(PREPARE_SCRIPT), "prepare"],
                    capture_output=True,
                    env=environment,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(out_dir.exists())
                self.assertFalse(cache_dir.exists())

    def test_resolves_relative_managed_paths_from_the_repository_root(self) -> None:
        relative = "target/qemu-uboot/relative-contract"
        expected = REPO_ROOT / relative

        output = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--canonical-output-dir", relative],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        build = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--canonical-build-dir", relative],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )

        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.strip(), str(expected))
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(build.stdout.strip(), str(expected))

    def test_accepts_only_nested_private_ltp_qemu_output_paths(self) -> None:
        accepted = REPO_ROOT / "target/ltp/qemu/smp1"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(accepted),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), accepted)

    def test_rejects_ltp_output_paths_outside_private_qemu_children(self) -> None:
        rejected_paths = (
            REPO_ROOT / "target/ltp",
            REPO_ROOT / "target/ltp/qemu",
            REPO_ROOT / "target/ltp/not-qemu",
            REPO_ROOT / "target/qemu-uboot/../ltp/escape",
        )

        for rejected in rejected_paths:
            with self.subTest(rejected=rejected):
                result = subprocess.run(
                    [
                        "bash",
                        str(PREPARE_SCRIPT),
                        "--canonical-output-dir",
                        str(rejected),
                    ],
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("must resolve below", result.stderr)

    def test_shared_qemu_current_output_path_remains_accepted(self) -> None:
        current = REPO_ROOT / "target/qemu-uboot/current"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(current),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), current)

    def test_rejects_an_output_directory_with_parent_traversal(self) -> None:
        candidate = REPO_ROOT / "target/qemu-uboot/../../../outside"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(candidate),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must resolve below", result.stderr)

    def test_rejects_an_output_directory_through_a_symlink(self) -> None:
        output_root = REPO_ROOT / "target/qemu-uboot"
        output_root.mkdir(parents=True, exist_ok=True)
        if not os.access(output_root, os.W_OK):
            self.skipTest("persistent qemu-uboot output directory is not writable")
        with tempfile.TemporaryDirectory(dir=output_root) as tmp:
            link = Path(tmp) / "outside"
            link.symlink_to(Path(tmp).parent.parent.parent)

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--canonical-output-dir",
                    str(link / "run"),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must resolve below", result.stderr)

    def test_prints_the_pinned_uboot_commit(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--print-uboot-commit"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "ece349ade2973e220f524ce59e59711cc919263f\n",
        )

    def test_cached_uboot_uses_only_command_scoped_safe_directory(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn('git -c "safe.directory=${source_dir}"', script)
        self.assertNotIn("git config --global", script)

    def test_check_tools_reports_every_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as empty_path:
            python = Path(empty_path) / "python3"
            python.write_text("#!/bin/sh\nexit 1\n")
            python.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(PREPARE_SCRIPT), "--check-tools"],
                capture_output=True,
                env={"PATH": empty_path},
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        for tool in (
            "bc",
            "bison",
            "dtc",
            "fdtget",
            "fdtput",
            "flex",
            "git",
            "make",
            "mkfs.ext4",
            "qemu-system-riscv64",
            "riscv64-linux-gnu-gcc",
            "swig",
        ):
            self.assertIn(f"missing tool: {tool}", result.stderr)
        self.assertIn("missing Python module: setuptools", result.stderr)
        self.assertIn("missing Python development headers", result.stderr)
        self.assertIn(
            "missing OpenSSL development headers",
            PREPARE_SCRIPT.read_text(),
        )

    def test_preparation_delegates_profile_matched_dtb_before_artifacts(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn("QEMU_UBOOT_PROFILE:-generic-sv39", script)
        self.assertNotIn(
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            script,
        )
        validate_offset = script.index('qemu_uboot_dtb.py" validate-profile')
        clone_offset = script.index("git clone --filter=blob:none")
        generate_offset = script.index('qemu_uboot_dtb.py" generate')
        copy_offset = script.index(
            'cp "${out_dir}/${dtb_filename}" "${stage_dir}/${dtb_filename}"'
        )
        manifest_offset = script.index('qemu_uboot_booti.py" write-manifest')
        checksum_offset = script.index("\n    sha256sum \\\n")
        self.assertLess(validate_offset, clone_offset)
        self.assertLess(generate_offset, copy_offset)
        self.assertLess(copy_offset, manifest_offset)
        self.assertLess(manifest_offset, checksum_offset)

    def test_payload_manifest_contains_only_the_three_boot_artifacts(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--print-payload-manifest"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["asterinas.booti", "initramfs.cpio.gz", "qemu-virt.dtb"],
        )

    def test_rejects_a_mismatched_uboot_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            (checkout / "README").write_text("not the pinned revision\n")
            subprocess.run(["git", "-C", str(checkout), "add", "README"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--verify-uboot-source",
                    str(checkout),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("U-Boot commit mismatch", result.stderr)

    def test_rejects_a_dirty_uboot_checkout_at_the_expected_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            tracked = checkout / "tracked"
            tracked.write_text("clean\n")
            subprocess.run(["git", "-C", str(checkout), "add", "tracked"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            expected_commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("dirty\n")

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--verify-uboot-source",
                    str(checkout),
                    expected_commit,
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("U-Boot checkout is not clean", result.stderr)

    def test_prepare_requires_explicit_kernel_and_initramfs(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "prepare"],
            capture_output=True,
            env={"PATH": os.environ["PATH"]},
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ASTERINAS_RISCV_BOOTI", result.stderr)
        self.assertIn("ASTERINAS_INITRAMFS", result.stderr)

    def test_preparation_preserves_firmware_and_qemu_provenance(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn("u-boot-commit.txt", script)
        self.assertIn("u-boot.config", script)
        self.assertIn("qemu-version.txt", script)
        self.assertIn('"${out_dir}/u-boot.config"', script)

    def test_preparation_clears_stale_run_evidence_before_fetching_uboot(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        clear_offset = script.index('rm -f "${out_dir}/result.json"')
        clone_offset = script.index("git clone --filter=blob:none")
        self.assertLess(clear_offset, clone_offset)
        self.assertIn('"${out_dir}/serial.log"', script[clear_offset:clone_offset])
        self.assertIn(
            '"${out_dir}/marker-event.txt"', script[clear_offset:clone_offset]
        )
        self.assertIn('"${out_dir}/qemu-virt.dtb"', script[clear_offset:clone_offset])


class RepositoryIntegrationTests(unittest.TestCase):
    def test_marker_init_writes_to_serial_and_remains_alive(self) -> None:
        source = INITRAMFS_SOURCE.read_text()

        self.assertIn("/dev/ttyS0", source)
        self.assertIn("li a7, 56", source)
        self.assertNotIn("li a7, 93", source)

    def test_marker_init_retries_short_writes_in_order(self) -> None:
        source = INITRAMFS_SOURCE.read_text()
        write_loop = source[source.index("1:") : source.index("2:")]

        required_operations = (
            "mv s0, a0",
            "mv a0, s0",
            "li a7, 64",
            "ecall",
            "blez a0, 3b",
            "sub a2, a2, a0",
            "add a1, a1, a0",
            "bnez a2, 3b",
        )
        offsets = [write_loop.index(operation) for operation in required_operations]
        self.assertEqual(offsets, sorted(offsets))

    def test_tracked_marker_initramfs_builder_is_reproducible(self) -> None:
        builder = INITRAMFS_BUILDER.read_text()
        source = INITRAMFS_SOURCE.read_text()

        self.assertIn("gzip.compress", builder)
        self.assertIn("mtime=0", builder)
        self.assertIn("riscv64-linux-gnu-gcc", builder)
        self.assertIn(">>> Hello from RISC-V userspace on Asterinas! <<<", source)
        self.assertIn("li a7, 64", source)

    @unittest.skipUnless(
        shutil.which("riscv64-linux-gnu-gcc"),
        "RISC-V cross compiler is only required in the development container",
    )
    def test_marker_initramfs_builder_is_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.cpio.gz"
            second = Path(tmp) / "second.cpio.gz"
            subprocess.run(
                [sys.executable, str(INITRAMFS_BUILDER), str(first)], check=True
            )
            subprocess.run(
                [sys.executable, str(INITRAMFS_BUILDER), str(second)], check=True
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_controller_is_split_into_single_responsibility_modules(self) -> None:
        module_names = (
            "qemu_uboot_artifacts.py",
            "qemu_uboot_commands.py",
            "qemu_uboot_execution.py",
            "qemu_uboot_session.py",
            "qemu_uboot_audit.py",
        )

        self.assertEqual(
            [name for name in module_names if not (SCRIPT.parent / name).is_file()],
            [],
        )
        # The prepare script grew past 250 lines with the Linux reference and
        # third-board flows; keep a generous bound that still flags a
        # future uncontrolled growth rather than a single added command.
        self.assertLessEqual(len(SCRIPT.read_text().splitlines()), 400)

    def test_make_exposes_unit_and_full_uboot_booti_targets(self) -> None:
        makefile = MAKEFILE.read_text()

        self.assertIn("test_riscv_uboot_booti_unit:", makefile)
        self.assertIn("test_riscv_uboot_booti:", makefile)
        self.assertIn("prepare_qemu_uboot_booti.sh prepare", makefile)
        self.assertIn("qemu_uboot_booti.py run", makefile)
        self.assertIn(
            'QEMU_UBOOT_BUILD_DIR="$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)"',
            makefile,
        )
        generic_target = makefile[
            makefile.index("test_riscv_uboot_booti:") :
            makefile.index(".PHONY: test_riscv_sifive_u")
        ]
        self.assertIn('QEMU_UBOOT_PROFILE="generic-sv39"', generic_target)
        self.assertIn('--profile "generic-sv39"', generic_target)
        self.assertIn("flock", PREPARE_SCRIPT.read_text())

    def test_make_exposes_opt_in_sifive_smoke_and_linux_control(self) -> None:
        makefile = MAKEFILE.read_text()

        self.assertIn("test_riscv_sifive_u:", makefile)
        self.assertIn("test_riscv_sifive_u_linux_reference:", makefile)
        self.assertIn("sifive-u-asterinas-smoke", makefile)
        self.assertIn("sifive-u-linux-reference", makefile)
        self.assertIn("SIFIVE_U_KERNEL_LABEL := ASTERINAS_RISCV_BOOTI", makefile)
        self.assertIn("SIFIVE_U_INITRAMFS_LABEL := ASTERINAS_INITRAMFS", makefile)
        self.assertIn("SIFIVE_U_KERNEL_LABEL := RISCV_LINUX_IMAGE", makefile)
        self.assertIn("SIFIVE_U_INITRAMFS_LABEL := RISCV_LINUX_INITRAMFS", makefile)
        self.assertIn('$(SIFIVE_U_KERNEL_LABEL) is required', makefile)
        self.assertIn('$(SIFIVE_U_INITRAMFS_LABEL) is required', makefile)

        target_text = makefile[
            makefile.index("test_riscv_sifive_u:") :
            makefile.index(".PHONY: check_vdso")
        ]
        for downloader in ("curl ", "wget "):
            self.assertNotIn(downloader, target_text)
        invoked_python = {
            token for token in target_text.split() if token.endswith(".py")
        }
        self.assertEqual(invoked_python, {"tools/riscv/qemu_uboot_booti.py"})

    def test_make_uses_one_nonempty_absolute_path_contract(self) -> None:
        default_out = REPO_ROOT / "target/qemu-uboot/current"
        default_build = REPO_ROOT / "target/qemu-uboot/cache/u-boot-build"
        empty = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "QEMU_UBOOT_OUT_DIR=",
                "QEMU_UBOOT_BUILD_DIR=",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{default_out}"', empty.stdout)
        self.assertIn(f'QEMU_UBOOT_BUILD_DIR="{default_build}"', empty.stdout)
        self.assertIn(f'--uboot "{default_build}/u-boot"', empty.stdout)
        self.assertIn(f'--manifest "{default_out}/artifacts.json"', empty.stdout)

        normalized_out = REPO_ROOT / "target/qemu-uboot/final"
        normalized_build = REPO_ROOT / "target/qemu-uboot/custom-build"
        relative = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "QEMU_UBOOT_OUT_DIR=target/qemu-uboot/missing/../final",
                "QEMU_UBOOT_BUILD_DIR=target/qemu-uboot/cache/../custom-build",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(relative.returncode, 0, relative.stderr)
        self.assertNotIn("missing/..", relative.stdout)
        self.assertNotIn("cache/..", relative.stdout)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{normalized_out}"', relative.stdout)
        self.assertIn(f'QEMU_UBOOT_BUILD_DIR="{normalized_build}"', relative.stdout)
        self.assertIn(f'--uboot "{normalized_build}/u-boot"', relative.stdout)
        self.assertIn(f'--manifest "{normalized_out}/artifacts.json"', relative.stdout)

        sifive = subprocess.run(
            [
                "make", "-n", "test_riscv_sifive_u",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "RISCV_SIFIVE_U_OUT_DIR=",
                "RISCV_SIFIVE_U_BUILD_DIR=",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        expected_sifive_out = REPO_ROOT / "target/qemu-uboot/sifive-u"
        expected_sifive_build = (
            REPO_ROOT / "target/qemu-uboot/cache/sifive-u-uboot-build"
        )
        self.assertEqual(sifive.returncode, 0, sifive.stderr)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{expected_sifive_out}"', sifive.stdout)
        self.assertIn(f'--uboot "{expected_sifive_build}/u-boot.bin"', sifive.stdout)

    def test_make_never_sources_generated_path_metadata(self) -> None:
        hostile_out_dir = REPO_ROOT / "target/qemu-uboot/x;touch injected;#"
        result = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                f"QEMU_UBOOT_OUT_DIR={hostile_out_dir}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("prepared.env", result.stdout)
        self.assertNotIn("prepared.env", PREPARE_SCRIPT.read_text())

    def test_readme_documents_entry_points_and_qemu_limit(self) -> None:
        readme = README.read_text()

        self.assertIn("make test_riscv_uboot_booti_unit", readme)
        self.assertIn("make test_riscv_uboot_booti", readme)
        self.assertIn("make test_riscv_sifive_u", readme)
        self.assertIn("make test_riscv_sifive_u_linux_reference", readme)
        self.assertIn("evidence, not hardware emulation", readme)
        self.assertIn("userspace marker", readme)
        self.assertIn("RISCV_LINUX_IMAGE", readme)
        self.assertIn("RISCV_LINUX_INITRAMFS", readme)


if __name__ == "__main__":
    unittest.main()
