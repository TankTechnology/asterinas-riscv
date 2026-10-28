#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class PreparedRunTests(_PreparedRunFixtures, unittest.TestCase):
    def test_registered_recovery_profile_binds_guest_reboot_and_session_wait(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name(
            "generic-sv39-smp4-software-reboot"
        )
        artifacts = self._run_artifacts()
        session_result = replace(
            self._successful_session(),
            recovery_complete=True,
            recovery_elapsed_seconds=60.0,
            boot_to_recovery_elapsed_seconds=65.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ) as argv,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=(
                        session_result,
                        replace(session_result, recovery_complete=False),
                    ),
                ) as session,
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(
                        profile,
                        qemu_uboot_booti.BootScenario.POSITIVE,
                    ),
                ),
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=inputs["boot_disk"],
                    manifest=inputs["manifest"],
                    serial_log=directory / "serial.log",
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )
                incomplete = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=inputs["boot_disk"],
                    manifest=inputs["manifest"],
                    serial_log=directory / "incomplete-serial.log",
                    marker_event=directory / "incomplete-marker-event.txt",
                    result_path=directory / "incomplete-result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )

        self.assertTrue(argv.call_args_list[0].kwargs["guest_reboot"])
        expectation = session.call_args_list[0].kwargs["reboot_expectation"]
        self.assertEqual(
            expectation.trigger_marker,
            profile.validation.completion_line,
        )
        self.assertEqual(expectation.recovery_timeout, 90.0)
        self.assertTrue(result.session.recovery_complete)
        self.assertTrue(result.passed)
        self.assertFalse(incomplete.passed)

    def test_progress_log_is_visible_while_serial_evidence_is_staged(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            progress = directory / "progress.log"
            serial = directory / "serial.log"

            def run_session(_argv: list[str], **kwargs: object) -> object:
                raw_log = kwargs["raw_log_file"]
                raw_log.write(b"live serial progress\n")
                raw_log.flush()
                self.assertEqual(progress.read_bytes(), b"live serial progress\n")
                self.assertFalse(serial.exists())
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
            ):
                result = qemu_uboot_booti.run_prepared(
                    **inputs,
                    serial_log=serial,
                    progress_log=progress,
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                )

            self.assertTrue(result.passed)
            self.assertEqual(progress.read_bytes(), serial.read_bytes())

    def test_console_loss_commands_bind_exact_ram_and_dtb_bootargs(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        diagnostic_bootargs = (
            "cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.first_process_diag=1"
        )

        commands = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )
        commands_by_name = {command.name: command for command in commands}

        self.assertEqual(
            commands_by_name["bootargs-env"].text,
            f'setenv bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={diagnostic_bootargs}",
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb-proof"].expected_output,
            diagnostic_bootargs,
        )
        texts = [command.text for command in commands]
        self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
        self.assertFalse(any("saveenv" in text for text in texts))

        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "first-process-console-loss",
                "--variant",
                "first-process-console-loss",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cli_lines = cli.stdout.splitlines()
        self.assertIn(f'setenv bootargs "{diagnostic_bootargs}"', cli_lines)
        self.assertIn(
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
            cli_lines,
        )
        self.assertNotIn(f'setenv bootargs "{profile.bootargs}"', cli_lines)

        invalid_variants = (
            None,
            replace(FIRST_PROCESS_CONSOLE_LOSS, bootarg_suffix="debug=1"),
            replace(FIRST_PROCESS_CONSOLE_LOSS),
        )
        for variant in invalid_variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=profile,
                        scenario=(
                            qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                        ),
                        variant=variant,
                    )

    def test_positive_scenario_policy_accepts_a_bootargs_override(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        override = (
            "cpu_no_boost_1_6ghz console=ttyS0 loglevel=info init=/init"
        )

        policy = qemu_uboot_audit._scenario_policy(
            profile,
            qemu_uboot_booti.BootScenario.POSITIVE,
            None,
            bootargs_override=override,
        )

        self.assertEqual(policy.effective_bootargs, override)
        self.assertEqual(policy.environment_bootargs, override)
        with self.assertRaises(ValueError):
            qemu_uboot_audit._scenario_policy(
                profile,
                qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                None,
                bootargs_override=override,
            )

    def test_registered_console_suppression_is_fixed_and_uses_standard_payload(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        diagnostic_bootargs = (
            "cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.first_process_diag=1"
        )

        commands = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
        )
        commands_by_name = {command.name: command for command in commands}

        self.assertEqual(
            commands_by_name["bootargs-env"].text,
            f'setenv bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={diagnostic_bootargs}",
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb-proof"].expected_output,
            diagnostic_bootargs,
        )
        texts = [command.text for command in commands]
        self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
        self.assertFalse(any("saveenv" in text for text in texts))
        self.assertFalse(any(" compatible " in text for text in texts))
        self.assertFalse(any("snps,dw-apb-uart" in text for text in texts))

        positive = qemu_uboot_booti.boot_commands(profile=profile)
        self.assertEqual(
            [command.name for command in commands],
            [command.name for command in positive],
        )
        bootargs_names = {
            "bootargs-env",
            "bootargs-env-proof",
            "bootargs-dtb",
            "bootargs-dtb-proof",
        }
        for command, positive_command in zip(commands, positive):
            if command.name not in bootargs_names:
                self.assertEqual(command, positive_command)

        with self.assertRaises(ValueError):
            qemu_uboot_booti.boot_commands(
                profile=profile,
                scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
                variant=FIRST_PROCESS_CONSOLE_LOSS,
            )
        for wrong_profile_name in ("generic-sv39", "megrez-sv48-svadu-fast"):
            with self.subTest(profile=wrong_profile_name):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=qemu_uboot_booti.profile_by_name(wrong_profile_name),
                        scenario=(
                            qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                        ),
                    )

    def test_positive_and_stale_commands_remain_unchanged(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        positive = qemu_uboot_booti.boot_commands(profile=profile)
        positive_explicit_none = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
            variant=None,
        )
        stale = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )
        stale_explicit_none = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            variant=None,
        )

        self.assertEqual(positive_explicit_none, positive)
        self.assertEqual(stale_explicit_none, stale)
        positive_by_name = {command.name: command for command in positive}
        stale_by_name = {command.name: command for command in stale}
        self.assertEqual(
            positive_by_name["bootargs-env"].text,
            f'setenv bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            positive_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={profile.bootargs}",
        )
        self.assertEqual(
            positive_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            positive_by_name["bootargs-dtb-proof"].expected_output,
            profile.bootargs,
        )
        self.assertEqual(
            stale_by_name["bootargs-env"].text,
            'setenv bootargs "cpu_no_boost_1_6ghz"',
        )
        self.assertEqual(
            stale_by_name["bootargs-env-proof"].expected_output,
            "bootargs=cpu_no_boost_1_6ghz",
        )
        self.assertEqual(
            stale_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            stale_by_name["bootargs-dtb-proof"].expected_output,
            profile.bootargs,
        )
        for commands in (positive, stale):
            command_text = "\n".join(command.text for command in commands)
            expected_text = "\n".join(command.expected_output for command in commands)
            self.assertNotIn("asterinas.first_process_diag", command_text)
            self.assertNotIn("asterinas.first_process_diag", expected_text)
            self.assertEqual(
                sum(command.text.startswith("booti ") for command in commands),
                1,
            )
            self.assertFalse(any("saveenv" in command.text for command in commands))

        for scenario in (
            qemu_uboot_booti.BootScenario.POSITIVE,
            qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        ):
            with self.subTest(scenario=scenario):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

        override = "console=ttyS0 loglevel=debug init=/init"
        overridden = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
            bootargs_override=override,
        )
        overridden_by_name = {command.name: command for command in overridden}
        self.assertEqual(
            overridden_by_name["bootargs-env"].text,
            f'setenv bootargs "{override}"',
        )
        self.assertEqual(
            overridden_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={override}",
        )
        self.assertEqual(
            overridden_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{override}"',
        )
        self.assertEqual(
            overridden_by_name["bootargs-dtb-proof"].expected_output,
            override,
        )

    def test_invalid_scenario_variant_pairs_fail_before_artifact_access(self) -> None:
        class UnreadableArtifacts:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(f"artifact field was accessed: {name}")

        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        wrong_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        drifted_profile = replace(
            profile,
            validation=replace(profile.validation, bootargs="init=/wrong"),
        )
        invalid_pairs = (
            (
                "positive-with-variant",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.POSITIVE,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "stale-with-variant",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "console-loss-without-variant",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": None,
                },
            ),
            (
                "suppression-with-variant",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "console-loss-with-wrong-profile",
                {
                    "profile": wrong_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "suppression-with-wrong-profile",
                {
                    "profile": wrong_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                },
            ),
            (
                "console-loss-with-drifted-profile",
                {
                    "profile": drifted_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "suppression-with-drifted-profile",
                {
                    "profile": drifted_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                },
            ),
            (
                "console-loss-with-override",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "suppression-with-override",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "stale-with-override",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "positive-with-variant-and-override",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.POSITIVE,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                    "bootargs_override": "debug=1",
                },
            ),
        )
        artifacts = UnreadableArtifacts()
        for label, arguments in invalid_pairs:
            with self.subTest(pair=label):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(artifacts, **arguments)

        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "first-process-console-loss",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("requires its fixed variant", cli.stderr)

    def test_console_loss_run_binds_registered_values_and_all_identities(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()
        session = self._successful_session()
        audit = self._passing_audit(profile, scenario)
        payload_sha256 = "b" * 64

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            boot_disk = inputs["boot_disk"]
            source_dtb = inputs["source_dtb"]
            serial_log = directory / "serial.log"
            marker_event = directory / "marker-event.txt"
            result_path = directory / "result.json"
            dtb_audit = inputs["dtb_audit"]
            variant_audit = inputs["variant_audit"]
            serial_log.write_text("captured transcript\n")
            source_sha256 = hashlib.sha256(source_dtb.read_bytes()).hexdigest()
            disk_sha256 = hashlib.sha256(boot_disk.read_bytes()).hexdigest()
            real_qemu_argv = qemu_uboot_booti.qemu_argv

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256=payload_sha256),
                ) as verify_dtb,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=real_qemu_argv,
                ) as build_argv,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    return_value=session,
                ) as run_session,
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=audit,
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=boot_disk,
                    manifest=inputs["manifest"],
                    serial_log=serial_log,
                    marker_event=marker_event,
                    result_path=result_path,
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    dtb_audit=dtb_audit,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                    source_dtb=source_dtb,
                    variant_audit=variant_audit,
                )

            expected_bootargs = effective_bootargs(
                profile,
                FIRST_PROCESS_CONSOLE_LOSS,
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.variant, FIRST_PROCESS_CONSOLE_LOSS.name)
            self.assertEqual(result.effective_bootargs, expected_bootargs)
            self.assertEqual(
                (result.source_dtb_sha256_before, result.source_dtb_sha256_after),
                (source_sha256, source_sha256),
            )
            self.assertEqual(
                (result.payload_dtb_sha256_before, result.payload_dtb_sha256_after),
                (payload_sha256, payload_sha256),
            )
            self.assertEqual(
                (result.boot_disk_sha256_before, result.boot_disk_sha256_after),
                (disk_sha256, disk_sha256),
            )
            self.assertEqual(
                json.loads(result_path.read_text())["variant"], result.variant
            )
            build_call = build_argv.call_args.kwargs
            self.assertNotEqual(build_call["uboot"], inputs["uboot"])
            self.assertNotEqual(build_call["boot_disk"], inputs["boot_disk"])
            self.assertEqual(build_call["uboot"].name, "u-boot")
            self.assertEqual(build_call["boot_disk"].name, "boot.ext4")
            self.assertIs(build_call["profile"], profile)
            self.assertTrue(build_call["snapshot_disk"])
            self.assertEqual(
                run_session.call_args.kwargs["completion_line"],
                FIRST_PROCESS_CONSOLE_LOSS.completion_line,
            )
            commands = run_session.call_args.kwargs["commands"]
            self.assertIn(
                f'setenv bootargs "{expected_bootargs}"',
                [command.text for command in commands],
            )
            self.assertEqual(len(verify_dtb.call_args_list), 2)
            for call in verify_dtb.call_args_list:
                arguments = call.kwargs
                for key, original in (
                    ("boot_disk", boot_disk),
                    ("audit_path", dtb_audit),
                    ("variant_audit_path", variant_audit),
                    ("source_dtb", source_dtb),
                ):
                    self.assertNotEqual(arguments[key], original)
                    self.assertEqual(arguments[key].name, original.name)
                self.assertIs(arguments["profile"], profile)
                self.assertEqual(arguments["expected_size"], artifacts.dtb_size)
                self.assertEqual(arguments["expected_crc32"], artifacts.dtb_crc32)
                self.assertIs(arguments["variant"], FIRST_PROCESS_CONSOLE_LOSS)
            self.assertIs(
                audit_log.call_args.kwargs["variant"],
                FIRST_PROCESS_CONSOLE_LOSS,
            )
            self.assertIs(audit_log.call_args.kwargs["scenario"], scenario)

    def test_run_rejects_result_path_equal_to_source_dtb_before_side_effects(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = {
                "uboot": directory / "u-boot",
                "boot_disk": directory / "boot.ext4",
                "manifest": directory / "manifest.json",
                "dtb_audit": directory / "qemu-dtb-audit.json",
                "source_dtb": directory / "qemu-virt.source.dtb",
                "variant_audit": directory / "qemu-dtb-variant-audit.json",
            }
            for name, path in inputs.items():
                path.write_bytes(f"immutable {name}\n".encode())
            original_source = inputs["source_dtb"].read_bytes()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    side_effect=AssertionError("manifest was accessed"),
                ) as load_manifest,
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=AssertionError("QEMU session was started"),
                ) as run_session,
            ):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=inputs["source_dtb"],
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            load_manifest.assert_not_called()
            run_session.assert_not_called()
            self.assertEqual(inputs["source_dtb"].read_bytes(), original_source)

    def test_registered_milestones_reject_overrides_before_artifact_access(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("sifive-u-asterinas-smoke")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            materialized = self._materialize_run_inputs(directory)
            inputs = {
                name: materialized[name]
                for name in ("uboot", "boot_disk", "manifest", "dtb_audit")
            }
            with mock.patch.object(
                qemu_uboot_booti,
                "load_artifact_manifest",
                side_effect=AssertionError("manifest was accessed"),
            ) as load_manifest:
                with self.assertRaisesRegex(ValueError, "registered-milestone"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker.txt",
                        result_path=directory / "result.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        bootargs_override="console=ttyS0",
                    )
                load_manifest.assert_not_called()

    def test_run_rejects_complete_input_and_output_overlap_matrix(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        output_names = (
            "serial_log",
            "marker_event",
            "result_path",
            "progress_log",
        )

        def invoke(
            inputs: dict[str, Path],
            outputs: dict[str, Path],
        ) -> None:
            qemu_uboot_booti.run_prepared(
                **inputs,
                **outputs,
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=1.0,
                profile=profile,
                scenario=scenario,
                variant=FIRST_PROCESS_CONSOLE_LOSS,
            )

        with mock.patch.object(
            qemu_uboot_booti,
            "load_artifact_manifest",
            side_effect=AssertionError("manifest was accessed"),
        ) as load_manifest:
            for input_name in (
                "uboot",
                "boot_disk",
                "manifest",
                "dtb_audit",
                "source_dtb",
                "variant_audit",
            ):
                for output_name in output_names:
                    for alias_kind in ("path", "symlink", "hardlink"):
                        label = (input_name, output_name, alias_kind)
                        with (
                            self.subTest(input_output=label),
                            tempfile.TemporaryDirectory() as tmp,
                        ):
                            directory = Path(tmp)
                            inputs = self._materialize_run_inputs(directory)
                            originals = {
                                name: path.read_bytes() for name, path in inputs.items()
                            }
                            outputs = {
                                "serial_log": directory / "serial.log",
                                "marker_event": directory / "marker-event.txt",
                                "result_path": directory / "result.json",
                                "progress_log": directory / "progress.log",
                            }
                            if alias_kind == "path":
                                outputs[output_name] = inputs[input_name]
                            elif alias_kind == "symlink":
                                outputs[output_name].symlink_to(inputs[input_name])
                            else:
                                os.link(inputs[input_name], outputs[output_name])

                            with self.assertRaisesRegex(
                                (ValueError, RuntimeError),
                                "overlap|symbolic link",
                            ):
                                invoke(inputs, outputs)
                            self.assertEqual(
                                {
                                    name: path.read_bytes()
                                    for name, path in inputs.items()
                                },
                                originals,
                            )

            for left_index, left_name in enumerate(output_names):
                for right_name in output_names[left_index + 1 :]:
                    for alias_kind in ("path", "symlink", "hardlink"):
                        label = (left_name, right_name, alias_kind)
                        with (
                            self.subTest(output_output=label),
                            tempfile.TemporaryDirectory() as tmp,
                        ):
                            directory = Path(tmp)
                            inputs = self._materialize_run_inputs(directory)
                            originals = {
                                name: path.read_bytes() for name, path in inputs.items()
                            }
                            outputs = {
                                "serial_log": directory / "serial.log",
                                "marker_event": directory / "marker-event.txt",
                                "result_path": directory / "result.json",
                                "progress_log": directory / "progress.log",
                            }
                            if alias_kind == "path":
                                outputs[right_name] = outputs[left_name]
                            else:
                                outputs[left_name].write_bytes(b"stale output\n")
                                if alias_kind == "symlink":
                                    outputs[right_name].symlink_to(outputs[left_name])
                                else:
                                    os.link(outputs[left_name], outputs[right_name])

                            with self.assertRaisesRegex(
                                (ValueError, RuntimeError),
                                "overlap|symbolic link",
                            ):
                                invoke(inputs, outputs)
                            self.assertEqual(
                                {
                                    name: path.read_bytes()
                                    for name, path in inputs.items()
                                },
                                originals,
                            )

        load_manifest.assert_not_called()

    def test_run_stages_every_input_and_launches_only_staged_materials(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            originals = {name: path.read_bytes() for name, path in inputs.items()}
            staged: dict[str, Path] = {}

            def load_manifest(path: Path) -> object:
                self.assertNotEqual(path, inputs["manifest"])
                self.assertEqual(path.read_bytes(), originals["manifest"])
                staged["manifest"] = path
                return artifacts

            def verify_dtb(**kwargs: object) -> object:
                bindings = {
                    "boot_disk": "boot_disk",
                    "audit_path": "dtb_audit",
                    "source_dtb": "source_dtb",
                    "variant_audit_path": "variant_audit",
                }
                for argument, name in bindings.items():
                    path = kwargs[argument]
                    self.assertIsInstance(path, Path)
                    assert isinstance(path, Path)
                    self.assertNotEqual(path, inputs[name])
                    self.assertEqual(path.read_bytes(), originals[name])
                    staged[name] = path
                return mock.Mock(sha256="a" * 64)

            real_qemu_argv = qemu_uboot_booti.qemu_argv

            def build_argv(**kwargs: object) -> list[str]:
                for name in ("uboot", "boot_disk"):
                    path = kwargs[name]
                    self.assertIsInstance(path, Path)
                    assert isinstance(path, Path)
                    self.assertNotEqual(path, inputs[name])
                    self.assertEqual(path.read_bytes(), originals[name])
                    staged[name] = path
                for name, path in inputs.items():
                    held = directory / f".{name}.held"
                    os.replace(path, held)
                    path.write_bytes(f"attacker {name}\n".encode())
                    os.replace(held, path)
                return real_qemu_argv(**kwargs)

            def run_session(argv: list[str], **kwargs: object) -> object:
                kernel = Path(argv[argv.index("-kernel") + 1])
                drive = next(item for item in argv if "file=" in item)
                disk = Path(
                    next(
                        field[5:]
                        for field in drive.split(",")
                        if field.startswith("file=")
                    )
                )
                self.assertEqual(kernel, staged["uboot"])
                self.assertEqual(disk, staged["boot_disk"])
                self.assertEqual(kernel.read_bytes(), originals["uboot"])
                self.assertEqual(disk.read_bytes(), originals["boot_disk"])
                raw_log = kwargs["raw_log_file"]
                raw_log.write(b"retained serial transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    side_effect=load_manifest,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    side_effect=verify_dtb,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=build_argv,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    **inputs,
                    serial_log=directory / "serial.log",
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                )

            self.assertTrue(result.passed)
            self.assertEqual(
                audit_log.call_args.args[0],
                "retained serial transcript\n",
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in inputs.items()},
                originals,
            )

    def test_replaced_serial_or_marker_cannot_change_audit_or_be_deleted(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        transcript = b"retained serial transcript\n"
        replacement = b"attacker replacement\n"

        for target_name in ("serial_log", "marker_event"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                originals = {name: path.read_bytes() for name, path in inputs.items()}
                outputs = {
                    "serial_log": directory / "serial.log",
                    "marker_event": directory / "marker-event.txt",
                    "result_path": directory / "result.json",
                }

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(transcript)
                    return self._successful_session()

                def replace_during_audit(serial_text: str, **kwargs: object) -> object:
                    self.assertEqual(serial_text, transcript.decode())
                    self.assertIn("marker_seen=yes", kwargs["marker_event"])
                    target = outputs[target_name]
                    target.unlink()
                    target.write_bytes(replacement)
                    return self._passing_audit(profile, scenario)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        side_effect=replace_during_audit,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "output entry changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(outputs[target_name].read_bytes(), replacement)
                self.assertFalse(outputs["result_path"].exists())
                self.assertEqual(
                    {name: path.read_bytes() for name, path in inputs.items()},
                    originals,
                )

    def test_each_staged_input_mutation_prevents_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for mutation in (
            "uboot",
            "boot_disk",
            "manifest",
            "dtb_audit",
            "source_dtb",
            "variant_audit",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                originals = {name: path.read_bytes() for name, path in inputs.items()}
                staged: dict[str, Path] = {}
                result_path = directory / "result.json"

                def load_manifest(path: Path) -> object:
                    staged["manifest"] = path
                    return self._run_artifacts()

                def verify_dtb(**kwargs: object) -> object:
                    staged["boot_disk"] = kwargs["boot_disk"]
                    staged["dtb_audit"] = kwargs["audit_path"]
                    staged["source_dtb"] = kwargs["source_dtb"]
                    staged["variant_audit"] = kwargs["variant_audit_path"]
                    return mock.Mock(sha256="a" * 64)

                def build_argv(**kwargs: object) -> list[str]:
                    staged["uboot"] = kwargs["uboot"]
                    return ["qemu-system-riscv64"]

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                def mutate_during_audit(*_args: object, **_kwargs: object) -> object:
                    staged[mutation].write_bytes(b"mutated staged input\n")
                    return self._passing_audit(profile, scenario)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        side_effect=load_manifest,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        side_effect=verify_dtb,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        side_effect=build_argv,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        side_effect=mutate_during_audit,
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            serial_log=directory / "serial.log",
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertFalse(result_path.exists())
                self.assertEqual(
                    {name: path.read_bytes() for name, path in inputs.items()},
                    originals,
                )

    def test_staging_cleanup_failure_precedes_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_cleanup = EXECUTION_IO_MODULE.tempfile.TemporaryDirectory.cleanup
            cleanup_attempts = 0

            def fail_first_cleanup(temporary: object) -> None:
                nonlocal cleanup_attempts
                cleanup_attempts += 1
                if cleanup_attempts == 1:
                    real_cleanup(temporary)
                    raise OSError("injected staging cleanup failure")
                real_cleanup(temporary)

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.tempfile.TemporaryDirectory,
                    "cleanup",
                    autospec=True,
                    side_effect=fail_first_cleanup,
                ),
            ):
                with self.assertRaisesRegex(OSError, "staging cleanup failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertGreaterEqual(cleanup_attempts, 1)
            self.assertFalse(result_path.exists())

    def test_post_replace_result_fsync_failure_revokes_owned_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for should_replace_result in (False, True):
            with (
                self.subTest(should_replace_result=should_replace_result),
                tempfile.TemporaryDirectory() as tmp,
            ):
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                result_path = directory / "result.json"
                replacement = b"attacker-owned result replacement\n"
                parent_metadata = result_path.parent.stat()
                parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
                descriptor_count = len(os.listdir("/proc/self/fd"))
                real_fsync = os.fsync
                fsync_failed = False

                def fail_result_parent_fsync(fd: int) -> None:
                    nonlocal fsync_failed
                    metadata = os.fstat(fd)
                    identity = (metadata.st_dev, metadata.st_ino)
                    if (
                        not fsync_failed
                        and result_path.exists()
                        and identity == parent_identity
                    ):
                        fsync_failed = True
                        if should_replace_result:
                            result_path.unlink()
                            result_path.write_bytes(replacement)
                        raise OSError("injected post-replace result fsync failure")
                    real_fsync(fd)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.os,
                        "fsync",
                        side_effect=fail_result_parent_fsync,
                    ),
                ):
                    with self.assertRaisesRegex(
                        OSError, "post-replace result fsync failure"
                    ):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            serial_log=directory / "serial.log",
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertTrue(fsync_failed)
                self.assertEqual(len(os.listdir("/proc/self/fd")), descriptor_count)
                if should_replace_result:
                    self.assertEqual(result_path.read_bytes(), replacement)
                else:
                    revoked = json.loads(result_path.read_text())
                    self.assertFalse(revoked["passed"])
                    self.assertEqual(revoked["status"], "ERROR")
                    self.assertEqual(
                        revoked["terminal_classification"], "INCOMPLETE"
                    )

    def test_post_replace_failure_revokes_preowned_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for error_type in (KeyboardInterrupt, OSError):
            for should_replace_result in (False, True):
                with (
                    self.subTest(
                        error_type=error_type.__name__,
                        should_replace_result=should_replace_result,
                    ),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    directory = Path(tmp)
                    inputs = self._materialize_run_inputs(directory)
                    result_path = directory / "result.json"
                    replacement = b"attacker-owned result replacement\n"
                    descriptor_count = len(os.listdir("/proc/self/fd"))
                    real_replace = os.replace
                    result_replaced = False

                    def fail_after_result_replace(
                        source: object,
                        destination: object,
                        **kwargs: object,
                    ) -> None:
                        nonlocal result_replaced
                        real_replace(source, destination, **kwargs)
                        if destination == result_path.name:
                            result_replaced = True
                            if should_replace_result:
                                result_path.unlink()
                                result_path.write_bytes(replacement)
                            raise error_type("injected failure after replacing result")

                    def run_session(_argv: list[str], **kwargs: object) -> object:
                        kwargs["raw_log_file"].write(b"retained transcript\n")
                        return self._successful_session()

                    with (
                        mock.patch.object(
                            qemu_uboot_booti,
                            "load_artifact_manifest",
                            return_value=self._run_artifacts(),
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "verify_prepared_dtb",
                            return_value=mock.Mock(sha256="a" * 64),
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "qemu_argv",
                            return_value=["qemu-system-riscv64"],
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "qemu_version",
                            return_value="QEMU emulator version test",
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "run_serial_session",
                            side_effect=run_session,
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "audit_serial_log",
                            return_value=self._passing_audit(profile, scenario),
                        ),
                        mock.patch.object(
                            EXECUTION_IO_MODULE.os,
                            "replace",
                            side_effect=fail_after_result_replace,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            error_type, "failure after replacing result"
                        ):
                            qemu_uboot_booti.run_prepared(
                                **inputs,
                                serial_log=directory / "serial.log",
                                marker_event=directory / "marker-event.txt",
                                result_path=result_path,
                                startup_timeout=1.0,
                                command_timeout=1.0,
                                boot_timeout=1.0,
                                termination_grace=1.0,
                                profile=profile,
                                scenario=scenario,
                                variant=FIRST_PROCESS_CONSOLE_LOSS,
                            )

                    self.assertTrue(result_replaced)
                    self.assertEqual(len(os.listdir("/proc/self/fd")), descriptor_count)
                    if should_replace_result:
                        self.assertEqual(result_path.read_bytes(), replacement)
                    else:
                        self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_cannot_hide_retained_evidence_mutation(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for target_name in (
            "serial_log",
            "marker_event",
            "uboot",
            "boot_disk",
            "manifest",
            "dtb_audit",
            "source_dtb",
            "variant_audit",
        ):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {
                    "serial_log": directory / "serial.log",
                    "marker_event": directory / "marker-event.txt",
                    "result_path": directory / "result.json",
                }
                replacement = f"attacker {target_name} replacement\n".encode()
                real_publish_result = (
                    EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                )

                def replace_evidence(
                    workspace: object,
                    prepared: object,
                    payload: bytes,
                ) -> None:
                    real_publish_result(workspace, prepared, payload)
                    target = (outputs | inputs)[target_name]
                    if target_name in outputs:
                        target.unlink()
                    target.write_bytes(replacement)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.ExecutionWorkspace,
                        "publish_result",
                        autospec=True,
                        side_effect=replace_evidence,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(
                    (outputs | inputs)[target_name].read_bytes(), replacement
                )
                self.assertFalse(
                    json.loads(outputs["result_path"].read_text())["passed"]
                )

    def test_framebuffer_result_rechecks_retained_display_evidence(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        for target_name in ("screenshot", "display_audit"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
                real_publish = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                def replace(workspace: object, prepared: object, payload: bytes) -> None:
                    real_publish(workspace, prepared, payload)
                    target = outputs[target_name]
                    target.unlink()
                    target.write_bytes(b"attacker replacement\n")
                def session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["terminal_action"]()
                    return self._successful_session()
                with (
                    mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                    mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                    mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                    mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                    mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                    mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                    mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=payload),
                    mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=audit),
                    mock.patch.object(EXECUTION_IO_MODULE.ExecutionWorkspace, "publish_result", autospec=True, side_effect=replace),
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                            **outputs, device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                        )
                self.assertEqual(outputs[target_name].read_bytes(), b"attacker replacement\n")
                self.assertFalse(json.loads(outputs["result_path"].read_text())["passed"])

    def test_post_result_resource_cleanup_failure_revokes_pass_result(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_close = EXECUTION_IO_MODULE.PinnedRegularInput.close
            cleanup_failed = False

            def fail_input_cleanup(input_pin: object) -> None:
                nonlocal cleanup_failed
                real_close(input_pin)
                if input_pin.label == "uboot" and not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("injected retained-input cleanup failure")

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedRegularInput,
                    "close",
                    autospec=True,
                    side_effect=fail_input_cleanup,
                ),
            ):
                with self.assertRaisesRegex(OSError, "retained-input cleanup failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertTrue(cleanup_failed)
            self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_close_failure_revokes_pass_result(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_publish_result = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
            real_close = EXECUTION_IO_MODULE.PinnedPublication.close
            result_identity: tuple[int, int] | None = None
            close_failed = False

            def track_result_publication(
                workspace: object,
                prepared: object,
                payload: bytes,
            ) -> None:
                nonlocal result_identity
                result_identity = prepared.identity
                real_publish_result(workspace, prepared, payload)

            def fail_result_close(publication: object) -> None:
                nonlocal close_failed
                real_close(publication)
                if publication.identity == result_identity and not close_failed:
                    close_failed = True
                    raise OSError("injected result publication close failure")

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "publish_result",
                    autospec=True,
                    side_effect=track_result_publication,
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedPublication,
                    "close",
                    autospec=True,
                    side_effect=fail_result_close,
                ),
            ):
                with self.assertRaisesRegex(OSError, "publication close failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertTrue(close_failed)
            self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_rechecks_every_output_directory(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for target_name in ("serial_log", "marker_event", "result_path"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                input_directory = root / "inputs"
                input_directory.mkdir()
                inputs = self._materialize_run_inputs(input_directory)
                outputs = {
                    "serial_log": root / "serial-output" / "serial.log",
                    "marker_event": root / "marker-output" / "marker-event.txt",
                    "result_path": root / "result-output" / "result.json",
                }
                for output in outputs.values():
                    output.parent.mkdir()
                detached_directory = root / f"detached-{target_name}"
                replacement = f"attacker {target_name} replacement\n".encode()
                real_publish_result = (
                    EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                )

                def replace_output_directory(
                    workspace: object,
                    prepared: object,
                    payload: bytes,
                ) -> None:
                    real_publish_result(workspace, prepared, payload)
                    target = outputs[target_name]
                    target.parent.rename(detached_directory)
                    target.parent.mkdir()
                    target.write_bytes(replacement)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.ExecutionWorkspace,
                        "publish_result",
                        autospec=True,
                        side_effect=replace_output_directory,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "output directory path changed"
                    ):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(outputs[target_name].read_bytes(), replacement)
                owned_result = (
                    detached_directory / outputs["result_path"].name
                    if target_name == "result_path"
                    else outputs["result_path"]
                )
                self.assertFalse(json.loads(owned_result.read_text())["passed"])

    def test_replaced_result_publication_is_rejected_without_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        replacement = b"attacker-owned result replacement\n"

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            originals = {name: path.read_bytes() for name, path in inputs.items()}
            result_path = directory / "result.json"
            real_publish_result = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result

            def replace_result(workspace: object, prepared: object, payload: bytes) -> None:
                real_publish_result(workspace, prepared, payload)
                result_path.unlink()
                result_path.write_bytes(replacement)

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "publish_result",
                    autospec=True,
                    side_effect=replace_result,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "output entry changed"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertEqual(result_path.read_bytes(), replacement)
            self.assertEqual(
                {name: path.read_bytes() for name, path in inputs.items()},
                originals,
            )

    def test_console_loss_run_rejects_source_payload_or_disk_mutation(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for mutation in ("source", "payload", "disk"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                boot_disk = inputs["boot_disk"]
                source_dtb = inputs["source_dtb"]
                serial_log = directory / "serial.log"
                result_path = directory / "result.json"
                serial_log.write_text("captured transcript\n")
                staged: dict[str, Path] = {}
                verify_count = 0

                def mutate_after_session(*_args: object, **_kwargs: object) -> object:
                    if mutation == "source":
                        staged["source"].write_bytes(b"source DTB after")
                    if mutation == "disk":
                        staged["disk"].write_bytes(b"boot disk after")
                    return self._successful_session()

                def verify_dtb(**kwargs: object) -> object:
                    nonlocal verify_count
                    staged["source"] = kwargs["source_dtb"]
                    staged["disk"] = kwargs["boot_disk"]
                    digest = "b" if mutation == "payload" and verify_count else "a"
                    verify_count += 1
                    return mock.Mock(sha256=digest * 64)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        side_effect=verify_dtb,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=mutate_after_session,
                    ),
                    mock.patch.object(qemu_uboot_booti, "audit_serial_log"),
                ):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            uboot=inputs["uboot"],
                            boot_disk=boot_disk,
                            manifest=inputs["manifest"],
                            serial_log=serial_log,
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            dtb_audit=inputs["dtb_audit"],
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                            source_dtb=source_dtb,
                            variant_audit=inputs["variant_audit"],
                        )
                self.assertFalse(result_path.exists())

    def test_console_loss_argv_keeps_firmware_machine_dtb_implicit(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        argv = qemu_uboot_booti.qemu_argv(
            uboot=Path("/tmp/u-boot"),
            boot_disk=Path("/tmp/boot.ext4"),
            profile=profile,
            snapshot_disk=True,
        )

        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertNotIn("-dtb", argv)
        self.assertFalse(any("dtb=" in item or "dumpdtb=" in item for item in argv))

        commands = qemu_uboot_booti.boot_commands(
            self._run_artifacts(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )
        payload_path_commands = [
            command.text for command in commands if "/qemu-virt.dtb" in command.text
        ]
        self.assertEqual(
            payload_path_commands,
            ["ext4load virtio 0:0 0x88000000 /qemu-virt.dtb"],
        )
        booti_commands = [
            command.text for command in commands if command.text.startswith("booti ")
        ]
        self.assertEqual(
            booti_commands,
            ["booti 0x80200000 0x83000000:${initrd_size} 0x88000000"],
        )

    def test_suppression_probe_uses_standard_dtb_and_normal_completion(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
        artifacts = self._run_artifacts()
        payload_sha256 = "c" * 64
        expected_bootargs = effective_bootargs(profile, FIRST_PROCESS_CONSOLE_LOSS)
        serial_interaction = shell_module.interaction_by_name(
            shell_module.RX_INTERACTION_NAME
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            boot_disk = inputs["boot_disk"]
            serial_log = directory / "serial.log"
            serial_log.write_text("captured transcript\n")
            disk_sha256 = hashlib.sha256(boot_disk.read_bytes()).hexdigest()
            real_qemu_argv = qemu_uboot_booti.qemu_argv

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256=payload_sha256),
                ) as verify_dtb,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=real_qemu_argv,
                ) as build_argv,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    return_value=self._successful_session(),
                ) as run_session,
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=boot_disk,
                    manifest=inputs["manifest"],
                    serial_log=serial_log,
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    dtb_audit=inputs["dtb_audit"],
                    serial_interaction=serial_interaction,
                )

            self.assertTrue(result.passed)
            self.assertIsNone(result.variant)
            self.assertEqual(result.effective_bootargs, expected_bootargs)
            self.assertIsNone(result.source_dtb_sha256_before)
            self.assertIsNone(result.source_dtb_sha256_after)
            self.assertEqual(
                (result.payload_dtb_sha256_before, result.payload_dtb_sha256_after),
                (payload_sha256, payload_sha256),
            )
            self.assertEqual(
                (result.boot_disk_sha256_before, result.boot_disk_sha256_after),
                (disk_sha256, disk_sha256),
            )
            build_call = build_argv.call_args.kwargs
            self.assertNotEqual(build_call["uboot"], inputs["uboot"])
            self.assertNotEqual(build_call["boot_disk"], inputs["boot_disk"])
            self.assertEqual(build_call["uboot"].name, "u-boot")
            self.assertEqual(build_call["boot_disk"].name, "boot.ext4")
            self.assertIs(build_call["profile"], profile)
            self.assertFalse(build_call["snapshot_disk"])
            self.assertEqual(
                run_session.call_args.kwargs["completion_line"],
                SESSION_MODULE.USERSPACE_MARKER,
            )
            self.assertIs(
                run_session.call_args.kwargs["serial_interaction"],
                serial_interaction,
            )
            command_text = "\n".join(
                command.text for command in run_session.call_args.kwargs["commands"]
            )
            self.assertIn(f'setenv bootargs "{expected_bootargs}"', command_text)
            self.assertNotIn("snps,dw-apb-uart", command_text)
            for call in verify_dtb.call_args_list:
                self.assertIsNone(call.kwargs["variant"])
                self.assertIsNone(call.kwargs["variant_audit_path"])
                self.assertIsNone(call.kwargs["source_dtb"])
            self.assertIsNone(audit_log.call_args.kwargs["variant"])
            self.assertIs(audit_log.call_args.kwargs["scenario"], scenario)
            self.assertEqual(
                audit_log.call_args.kwargs["userspace_marker"],
                serial_interaction.completion_line.decode(),
            )
            self.assertEqual(
                audit_log.call_args.kwargs["readiness_marker"],
                serial_interaction.ready_line.decode(),
            )

    def test_positive_stale_and_suppression_reject_variant_materials(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenarios = (
            qemu_uboot_booti.BootScenario.POSITIVE,
            qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION,
        )
        paths = {
            "source_dtb": Path("/unreadable/source.dtb"),
            "variant_audit": Path("/unreadable/variant-audit.json"),
        }
        material_sets = (
            {"variant": FIRST_PROCESS_CONSOLE_LOSS},
            {"source_dtb": paths["source_dtb"]},
            {"variant_audit": paths["variant_audit"]},
            {"variant": FIRST_PROCESS_CONSOLE_LOSS, **paths},
        )
        base_run = {
            "uboot": Path("/unreadable/u-boot"),
            "boot_disk": Path("/unreadable/boot.ext4"),
            "manifest": Path("/unreadable/manifest.json"),
            "serial_log": Path("/unreadable/serial.log"),
            "marker_event": Path("/unreadable/marker-event.txt"),
            "result_path": Path("/unreadable/result.json"),
            "startup_timeout": 1.0,
            "command_timeout": 1.0,
            "boot_timeout": 1.0,
            "termination_grace": 1.0,
            "profile": profile,
            "dtb_audit": Path("/unreadable/dtb-audit.json"),
        }
        with mock.patch.object(
            qemu_uboot_booti,
            "load_artifact_manifest",
            side_effect=AssertionError("manifest was accessed"),
        ) as load_manifest:
            for scenario in scenarios:
                for materials in material_sets:
                    with self.subTest(scenario=scenario, materials=materials):
                        with self.assertRaises(ValueError):
                            qemu_uboot_booti.run_prepared(
                                **base_run,
                                scenario=scenario,
                                **materials,
                            )
            load_manifest.assert_not_called()

        cli_base = [
            "run",
            "--uboot",
            "/tmp/u-boot",
            "--boot-disk",
            "/tmp/boot.ext4",
            "--manifest",
            "/tmp/manifest.json",
            "--serial-log",
            "/tmp/serial.log",
            "--marker-event",
            "/tmp/marker-event.txt",
            "--result",
            "/tmp/result.json",
            "--dtb-audit",
            "/tmp/dtb-audit.json",
            "--profile",
            profile.name,
        ]
        material_cli = [
            "--variant",
            FIRST_PROCESS_CONSOLE_LOSS.name,
            "--source-dtb",
            "/tmp/source.dtb",
            "--variant-audit",
            "/tmp/variant-audit.json",
        ]
        accepted = qemu_uboot_booti._parse_args(
            cli_base
            + ["--scenario", qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS]
            + material_cli
        )
        self.assertIs(accepted.variant, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertEqual(accepted.source_dtb, Path("/tmp/source.dtb"))
        self.assertEqual(
            accepted.variant_audit,
            Path("/tmp/variant-audit.json"),
        )
        for scenario in scenarios:
            with self.subTest(cli_scenario=scenario), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(
                    cli_base + ["--scenario", scenario.value] + material_cli
                )
        for missing_option in ("--variant", "--source-dtb", "--variant-audit"):
            missing_material = list(material_cli)
            option_index = missing_material.index(missing_option)
            del missing_material[option_index : option_index + 2]
            with self.subTest(missing=missing_option), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(
                    cli_base
                    + [
                        "--scenario",
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS.value,
                    ]
                    + missing_material
                )

    def test_resolves_only_the_registered_console_loss_variant(self) -> None:
        variant = variant_by_name("first-process-console-loss")

        self.assertIs(variant, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertEqual(variant.name, "first-process-console-loss")
        self.assertEqual(variant.base_profile_name, "megrez-sv48-svade-fast")
        self.assertEqual(
            variant.bootarg_suffix,
            "asterinas.first_process_diag=1",
        )
        self.assertEqual(variant.payload_uart_compatible, "snps,dw-apb-uart")
        self.assertEqual(variant.terminal_stage, "user_first_write_returned")
        self.assertEqual(
            variant.completion_line,
            b"ASTERINAS_FIRST_PROCESS_DIAG "
            b"stage=user_first_write_returned "
            b"fd=1 requested=50 result=50",
        )
        self.assertEqual(variant.expected_userspace_marker_count, 0)
        self.assertEqual(variant.classification, "EXPECTED_CONSOLE_ROUTE_LOSS")
        self.assertEqual(variant.transport, "U-Boot booti only")
        validate_registered_variant(variant)

        for unknown_name in (
            "",
            "console-loss",
            "first-process-console-loss-copy",
            "first-process-console-loss ",
        ):
            with self.subTest(unknown_name=unknown_name):
                with self.assertRaisesRegex(ValueError, "unknown QEMU U-Boot variant"):
                    variant_by_name(unknown_name)

        with self.assertRaises(FrozenInstanceError):
            variant.name = "modified"  # type: ignore[misc]

        base_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        self.assertEqual(effective_bootargs(base_profile, None), base_profile.bootargs)
        self.assertEqual(
            effective_bootargs(base_profile, variant),
            base_profile.bootargs + " asterinas.first_process_diag=1",
        )

        wrong_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        with self.assertRaisesRegex(ValueError, "requires base profile"):
            effective_bootargs(wrong_profile, variant)

    def test_rejects_modified_variant_identity(self) -> None:
        field_variants = (
            replace(FIRST_PROCESS_CONSOLE_LOSS, name="modified"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, base_profile_name="generic-sv39"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, bootarg_suffix="debug=1"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, payload_uart_compatible="ns16550a"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, terminal_stage="user_enter"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, completion_line=b"modified"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, expected_userspace_marker_count=1),
            replace(FIRST_PROCESS_CONSOLE_LOSS, classification="PASS"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, transport="direct kernel boot"),
        )
        for field_variant in field_variants:
            with self.subTest(field_variant=field_variant):
                with self.assertRaisesRegex(ValueError, "registered singleton"):
                    validate_registered_variant(field_variant)

        equal_copy = QemuUbootVariant(
            name="first-process-console-loss",
            base_profile_name="megrez-sv48-svade-fast",
            bootarg_suffix="asterinas.first_process_diag=1",
            payload_uart_compatible="snps,dw-apb-uart",
            terminal_stage="user_first_write_returned",
            completion_line=(
                b"ASTERINAS_FIRST_PROCESS_DIAG "
                b"stage=user_first_write_returned "
                b"fd=1 requested=50 result=50"
            ),
            expected_userspace_marker_count=0,
            classification="EXPECTED_CONSOLE_ROUTE_LOSS",
            transport="U-Boot booti only",
        )
        self.assertEqual(equal_copy, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertIsNot(equal_copy, FIRST_PROCESS_CONSOLE_LOSS)
        with self.assertRaisesRegex(ValueError, "registered singleton"):
            validate_registered_variant(equal_copy)

    def test_resolves_immutable_generic_and_fast_megrez_profiles(self) -> None:
        svade = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        svadu = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")

        self.assertEqual((svade.memory, svade.hart_count), ("2G", 4))
        self.assertIn("sv57=false", svade.cpu)
        self.assertIn("svade=true", svade.cpu)
        self.assertIn("svadu=false", svade.cpu)
        self.assertIn("svadu=true", svadu.cpu)
        self.assertIn("svade=false", svadu.cpu)
        self.assertNotIn("sv48=false", svade.cpu)
        self.assertIn("zkr=false", svade.cpu)
        self.assertIn("svpbmt=false", svade.cpu)
        self.assertEqual(
            svade.bootargs,
            "cpu_no_boost_1_6ghz loglevel=info init=/init",
        )
        self.assertEqual(
            qemu_uboot_booti.profile_by_name("generic-sv39"),
            qemu_uboot_booti.GENERIC_SV39,
        )

    def test_fast_megrez_profiles_match_the_board_contract(self) -> None:
        contract = load_contract()

        for name in (
            "megrez-sv48-svade-fast",
            "megrez-sv48-svadu-fast",
        ):
            with self.subTest(profile=name):
                qemu_uboot_booti.validate_profile_policy(
                    contract,
                    qemu_uboot_booti.profile_by_name(name),
                )

    def test_slow_profile_is_registered_and_matches_the_board_contract(self) -> None:
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")

        self.assertEqual(slow.memory, "16G")
        self.assertEqual(slow.memory_bytes, 0x4_0000_0000)
        self.assertEqual(slow.hart_count, 4)
        self.assertEqual(slow.mmu_type, "riscv,sv48")
        self.assertEqual(slow.ad_extension, "svade")
        self.assertEqual(
            slow.bootargs,
            "cpu_no_boost_1_6ghz loglevel=info init=/init",
        )
        self.assertTrue(slow.requires_resource_gate)
        qemu_uboot_booti.validate_profile_policy(load_contract(), slow)

    def test_slow_qemu_argv_requires_a_valid_resource_permit(self) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")

        invalid_permits = (
            None,
            True,
            object(),
        )
        for permit in invalid_permits:
            with self.subTest(permit=permit):
                kwargs = {} if permit is None else {"slow_permit": permit}
                with self.assertRaisesRegex(
                    ValueError,
                    "resource gate permit",
                ):
                    qemu_uboot_booti.qemu_argv(
                        **paths,
                        profile=slow,
                        **kwargs,
                    )

        permit = _issue_slow_run_permit()
        argv = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=slow,
            slow_permit=permit,
        )
        self.assertEqual(argv[argv.index("-m") + 1], "16G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")

    def test_slow_gate_cannot_be_bypassed_by_profile_drift(self) -> None:
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")
        drifted = replace(
            slow,
            machine=replace(slow.machine, requires_resource_gate=False),
        )

        with self.assertRaisesRegex(ValueError, "registered value"):
            qemu_uboot_booti.qemu_argv(
                uboot=Path("/tmp/u-boot"),
                boot_disk=Path("/tmp/boot.ext4"),
                profile=drifted,
            )

    def test_megrez_qemu_argv_uses_four_harts_without_changing_the_default(
        self,
    ) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }
        implicit = qemu_uboot_booti.qemu_argv(**paths)
        explicit = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=qemu_uboot_booti.GENERIC_SV39,
        )
        svade = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast"),
        )

        self.assertEqual(implicit, explicit)
        self.assertEqual(
            implicit,
            [
                "qemu-system-riscv64",
                "-machine",
                "virt",
                "-cpu",
                "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
                "-m",
                "2G",
                "-smp",
                "1",
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "stdio",
                "-no-reboot",
                "-kernel",
                "/tmp/u-boot",
                "-drive",
                "if=none,format=raw,file=/tmp/boot.ext4,id=bootdisk",
                "-device",
                "virtio-blk-device,drive=bootdisk",
            ],
        )
        self.assertEqual(svade[svade.index("-smp") + 1], "4")

    def test_snapshot_disk_preserves_guest_reboot_policy(self) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }

        snapshot_only = qemu_uboot_booti.qemu_argv(
            **paths,
            snapshot_disk=True,
        )
        guest_reboot = qemu_uboot_booti.qemu_argv(
            **paths,
            guest_reboot=True,
        )

        self.assertIn("-no-reboot", snapshot_only)
        self.assertTrue(any("snapshot=on" in arg for arg in snapshot_only))
        self.assertNotIn("-no-reboot", guest_reboot)
        self.assertTrue(any("snapshot=on" in arg for arg in guest_reboot))

    def test_profile_policy_rejects_duplicate_or_drifting_cpu_selectors(self) -> None:
        contract = load_contract()
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        mutations = (
            replace(
                profile,
                machine=replace(profile.machine, cpu=f"{profile.cpu},svade=true"),
            ),
            replace(
                profile,
                machine=replace(profile.machine, cpu=f"{profile.cpu},svinval=true"),
            ),
            replace(
                profile,
                machine=replace(
                    profile.machine,
                    cpu=profile.cpu.replace("zkr=false", "zkr=true"),
                ),
            ),
            replace(profile, machine=replace(profile.machine, memory="1G")),
            replace(
                profile,
                validation=replace(profile.validation, bootargs="init=/wrong"),
            ),
        )

        for mutation in mutations:
            with self.subTest(profile=mutation):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.validate_profile_policy(contract, mutation)

    def test_megrez_cli_prints_profile_bootargs(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                "megrez-sv48-svade-fast",
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'setenv bootargs "cpu_no_boost_1_6ghz loglevel=info init=/init"',
            result.stdout,
        )
        self.assertNotIn("console=ttyS0", result.stdout)

    def test_megrez_command_scenarios_sanitize_rng_and_keep_stale_env_local(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        positive = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )
        stale = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )

        rng_guard = (
            "if fdt get value aster_rng_seed /chosen rng-seed; "
            "then fdt rm /chosen rng-seed; fi"
        )
        for commands in (positive, stale):
            texts = [command.text for command in commands]
            self.assertIn(rng_guard, texts)
            self.assertLess(texts.index(rng_guard), texts.index("fdt print /chosen"))
            self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
            self.assertNotIn("saveenv", texts)
        positive_texts = [command.text for command in positive]
        stale_texts = [command.text for command in stale]
        self.assertIn(f'setenv bootargs "{MEGREZ_BOOTARGS}"', positive_texts)
        self.assertIn('setenv bootargs "cpu_no_boost_1_6ghz"', stale_texts)
        self.assertNotIn(f'setenv bootargs "{MEGREZ_BOOTARGS}"', stale_texts)
        self.assertIn(
            f'fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"',
            stale_texts,
        )
        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "stale-bootargs",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn('setenv bootargs "cpu_no_boost_1_6ghz"', cli.stdout)
        self.assertIn(f'fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"', cli.stdout)

    def test_stale_run_requires_expected_timeout_and_complete_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=11326096,
            kernel_crc32="57c40418",
            dtb_size=5048,
            dtb_crc32="6e7844b8",
            initrd_size=3411,
            initrd_crc32="153879f1",
        )
        expected = qemu_uboot_booti.SessionResult(
            marker_seen=False,
            booti_sent_count=1,
            timed_out=True,
            killed=False,
            cleanup_complete=True,
            returncode=0,
            failure="boot-timeout",
            termination_action="SIGTERM",
        )
        sessions = (
            expected,
            replace(expected, cleanup_complete=False),
            replace(expected, timed_out=False),
        )
        audit = qemu_uboot_booti.BootAudit(
            booti_command_count=1,
            userspace_marker_count=0,
            effective_bootargs=MEGREZ_BOOTARGS,
            diagnostic=qemu_uboot_audit.audit_diagnostic_markers(
                "",
                variant=None,
            ),
            application_processor_ids=(1, 2, 3),
            random_source="timestamp",
            classification="EXPECTED_INIT_ENOENT",
            passed=True,
            failures=(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            serial_log = directory / "serial.log"
            serial_log.write_text(MEGREZ_STALE_SERIAL_LOG)
            marker_event = directory / "marker.txt"
            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="abc"),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=sessions,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=audit,
                ) as audit_log,
            ):
                results = [
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"],
                        boot_disk=inputs["boot_disk"],
                        manifest=inputs["manifest"],
                        serial_log=serial_log,
                        marker_event=marker_event,
                        result_path=directory / f"result-{index}.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                        dtb_audit=inputs["dtb_audit"],
                    )
                    for index in range(len(sessions))
                ]

        self.assertEqual([result.passed for result in results], [True, False, False])
        self.assertEqual(results[0].scenario, "stale-bootargs")
        self.assertIn(
            "cleanup_complete=no", audit_log.call_args_list[1].kwargs["marker_event"]
        )
        for call in audit_log.call_args_list:
            self.assertEqual(
                call.kwargs["scenario"],
                qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            )

    def test_run_rejects_profile_drift_before_manifest_or_qemu_access(self) -> None:
        base_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        profile = replace(
            base_profile,
            validation=replace(
                base_profile.validation,
                bootargs="init=/wrong",
            ),
        )

        with mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version:
            with self.assertRaisesRegex(ValueError, "registered value"):
                qemu_uboot_booti.run_prepared(
                    uboot=Path("/does/not/exist/u-boot"),
                    boot_disk=Path("/does/not/exist/boot.ext4"),
                    manifest=Path("/does/not/exist/manifest.json"),
                    serial_log=Path("/does/not/exist/serial.log"),
                    marker_event=Path("/does/not/exist/marker.txt"),
                    result_path=Path("/does/not/exist/result.json"),
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )
            qemu_version.assert_not_called()

    def test_megrez_run_requires_a_profile_bound_dtb_audit_before_qemu(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")

        with mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version:
            with self.assertRaisesRegex(ValueError, "generated DTB audit is required"):
                qemu_uboot_booti.run_prepared(
                    uboot=Path("/does/not/exist/u-boot"),
                    boot_disk=Path("/does/not/exist/boot.ext4"),
                    manifest=Path("/does/not/exist/manifest.json"),
                    serial_log=Path("/does/not/exist/serial.log"),
                    marker_event=Path("/does/not/exist/marker.txt"),
                    result_path=Path("/does/not/exist/result.json"),
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )
            qemu_version.assert_not_called()

    def test_megrez_run_verifies_the_boot_disk_dtb_before_qemu(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=1,
            kernel_crc32="11111111",
            dtb_size=2,
            dtb_crc32="22222222",
            initrd_size=3,
            initrd_crc32="33333333",
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    side_effect=ValueError("forged DTB audit"),
                ) as verify_dtb,
                mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version,
            ):
                with self.assertRaisesRegex(ValueError, "forged DTB audit"):
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"],
                        boot_disk=inputs["boot_disk"],
                        manifest=inputs["manifest"],
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker.txt",
                        result_path=directory / "result.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        dtb_audit=inputs["dtb_audit"],
                    )

            arguments = verify_dtb.call_args.kwargs
            self.assertNotEqual(arguments["boot_disk"], inputs["boot_disk"])
            self.assertNotEqual(arguments["audit_path"], inputs["dtb_audit"])
            self.assertEqual(arguments["boot_disk"].name, "boot.ext4")
            self.assertEqual(arguments["audit_path"].name, "qemu-dtb-audit.json")
            self.assertIs(arguments["profile"], profile)
            self.assertEqual(arguments["expected_size"], artifacts.dtb_size)
            self.assertEqual(arguments["expected_crc32"], artifacts.dtb_crc32)
            self.assertIsNone(arguments["variant"])
            self.assertIsNone(arguments["variant_audit_path"])
            self.assertIsNone(arguments["source_dtb"])
            qemu_version.assert_not_called()

    def test_cli_rejects_unknown_profile_before_querying_qemu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            queried = directory / "qemu-queried"
            fake_qemu = directory / "qemu-system-riscv64"
            fake_qemu.write_text(f"#!/bin/sh\ntouch {queried}\n")
            fake_qemu.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--profile",
                    "bad",
                    "--uboot",
                    str(directory / "u-boot"),
                    "--boot-disk",
                    str(directory / "boot.ext4"),
                    "--manifest",
                    str(directory / "manifest.json"),
                    "--serial-log",
                    str(directory / "serial.log"),
                    "--marker-event",
                    str(directory / "marker.txt"),
                    "--result",
                    str(directory / "result.json"),
                ],
                capture_output=True,
                env=env,
                text=True,
            )
            self.assertFalse(queried.exists())

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown QEMU U-Boot profile: bad", result.stderr)

    def test_print_commands_proves_bootargs_before_one_booti(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "print-commands"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        booti_lines = [line for line in lines if line.startswith("booti ")]
        self.assertEqual(
            booti_lines,
            ["booti 0x80200000 0x83000000:${initrd_size} 0x88000000"],
        )
        bootargs_commands = (
            'setenv bootargs "console=ttyS0 loglevel=info init=/init"',
            "printenv bootargs",
            'fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"',
            "fdt print /chosen",
        )
        for command in bootargs_commands:
            self.assertIn(command, lines)
        command_indices = [lines.index(command) for command in bootargs_commands]
        self.assertEqual(command_indices, sorted(command_indices))
        self.assertLess(command_indices[-1], lines.index(booti_lines[0]))
        self.assertNotIn("saveenv", result.stdout)
        self.assertNotIn("ostd.log_level", result.stdout)

    def test_command_plan_uses_runtime_artifact_sizes_and_crcs(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "ArtifactExpectations"))
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x123,
            kernel_crc32="11111111",
            dtb_size=0x234,
            dtb_crc32="22222222",
            initrd_size=0x345,
            initrd_crc32="33333333",
        )

        commands = qemu_uboot_booti.boot_commands(artifacts)

        expected_outputs = {
            command.name: command.expected_output for command in commands
        }
        self.assertEqual(expected_outputs["kernel-load"], "291 bytes read")
        self.assertEqual(expected_outputs["kernel-size"], "123")
        self.assertEqual(expected_outputs["kernel-crc"], "11111111")
        self.assertEqual(expected_outputs["dtb-load"], "564 bytes read")
        self.assertEqual(expected_outputs["dtb-crc"], "22222222")
        self.assertEqual(expected_outputs["initrd-load"], "837 bytes read")
        self.assertEqual(expected_outputs["initrd-crc"], "33333333")

    def test_derives_artifact_expectations_from_file_contents(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "artifact_expectations_from_paths"))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            artifacts = qemu_uboot_booti.artifact_expectations_from_paths(
                kernel=kernel,
                dtb=dtb,
                initrd=initrd,
            )

        self.assertEqual(artifacts.kernel_size, 64)
        self.assertEqual(artifacts.kernel_crc32, "4c567565")
        self.assertEqual(artifacts.dtb_size, 3)
        self.assertEqual(artifacts.dtb_crc32, "58700668")
        self.assertEqual(artifacts.initrd_size, 6)
        self.assertEqual(artifacts.initrd_crc32, "fa3bf02d")

    def test_write_manifest_records_host_derived_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            manifest = directory / "artifacts.json"
            kernel_bytes = valid_linux_image()
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "write-manifest",
                    "--kernel",
                    str(kernel),
                    "--dtb",
                    str(dtb),
                    "--initrd",
                    str(initrd),
                    "--output",
                    str(manifest),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(manifest.read_text()),
                {
                    "dtb_crc32": "58700668",
                    "dtb_sha256": (
                        "e7ce15bb0946707f3145d94c16c6946c"
                        "ef9b92b157527c1a64c2c8903fdbe86f"
                    ),
                    "dtb_size": 3,
                    "initrd_crc32": "fa3bf02d",
                    "initrd_sha256": (
                        "09e6c018d2c8c4903308613dd1b72484"
                        "d57eadf12ec50ddc8f52e5accce470f2"
                    ),
                    "initrd_size": 6,
                    "kernel_crc32": "4c567565",
                    "kernel_sha256": (
                        "948af20de762380321d412ebdb8a2fc9"
                        "e44c21b1602ca8e5b5b3dd47d3746c89"
                    ),
                    "kernel_size": 64,
                },
            )

    def test_rejects_a_linux_image_whose_header_size_mismatches_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            # A declared size smaller than the on-disk file must be
            # rejected; a declared size larger than the file is valid
            # (Linux declares the uncompressed size including BSS).
            struct.pack_into("<Q", kernel_bytes, 0x10, 63)
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            with self.assertRaisesRegex(ValueError, "Linux Image size"):
                qemu_uboot_booti.artifact_expectations_from_paths(
                    kernel=kernel,
                    dtb=dtb,
                    initrd=initrd,
                )

    def test_rejects_a_linux_image_with_an_unsafe_text_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            struct.pack_into("<Q", kernel_bytes, 0x08, 0)
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            with self.assertRaisesRegex(ValueError, "text offset"):
                qemu_uboot_booti.artifact_expectations_from_paths(
                    kernel=kernel,
                    dtb=dtb,
                    initrd=initrd,
                )

    def test_rejects_payload_ranges_that_overlap_before_qemu_starts(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "validate_fixed_payload_layout"))
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x32_00000,
            kernel_crc32="11111111",
            dtb_size=0x1000,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )

        with self.assertRaisesRegex(ValueError, "kernel overlaps initrd"):
            qemu_uboot_booti.validate_fixed_payload_layout(artifacts)

    def test_payload_ranges_reserve_aligned_maximum_dtb_resize_space(self) -> None:
        for dtb_size in (0x1001, 0x2000):
            with self.subTest(dtb_size=dtb_size):
                artifacts = qemu_uboot_booti.ArtifactExpectations(
                    kernel_size=0x1000,
                    kernel_crc32="11111111",
                    dtb_size=dtb_size,
                    dtb_crc32="22222222",
                    initrd_size=0x1000,
                    initrd_crc32="33333333",
                )

                self.assertEqual(
                    payload_ranges(artifacts)["dtb"],
                    range(0x8800_0000, 0x8800_4000),
                )

    def test_bdinfo_rejects_reserved_dtb_maximum_resize_space(self) -> None:
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x1000,
            kernel_crc32="11111111",
            dtb_size=0x1001,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )
        log = """\
memory[0] [0x80000000-0xffffffff]
reserved[0] [0x88003000-0x88003fff]
"""

        with self.assertRaisesRegex(ValueError, "dtb overlaps"):
            validate_bdinfo_memory_layout(log, artifacts)

    def test_rejects_reversed_bdinfo_ranges(self) -> None:
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x1000,
            kernel_crc32="11111111",
            dtb_size=0x1000,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )
        log = """\
memory[0] [0x80000000-0xffffffff]
reserved[0] [0x90000000-0x80000000]
"""

        with self.assertRaisesRegex(ValueError, "reversed reserved range"):
            validate_bdinfo_memory_layout(log, artifacts)

    def test_qemu_version_rejects_empty_output(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch(
            "qemu_uboot_commands.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "empty version output"):
                qemu_uboot_booti.qemu_version()

    def test_initramfs_rejects_the_newc_trailer_name(self) -> None:
        entry = InitramfsEntry("TRAILER!!!", b"hidden", 0o100644)

        with self.assertRaisesRegex(ValueError, "reserved initramfs entry"):
            make_newc_archive(b"elf", extra_entries=(entry,))

    def test_load_manifest_restores_typed_artifact_gates(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "load_artifact_manifest"))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "artifacts.json"
            manifest.write_text(
                json.dumps(
                    {
                        "kernel_size": 1,
                        "kernel_crc32": "11111111",
                        "kernel_sha256": "1" * 64,
                        "dtb_size": 2,
                        "dtb_crc32": "22222222",
                        "dtb_sha256": "2" * 64,
                        "initrd_size": 3,
                        "initrd_crc32": "33333333",
                        "initrd_sha256": "3" * 64,
                    }
                )
            )

            artifacts = qemu_uboot_booti.load_artifact_manifest(manifest)

        self.assertEqual(
            artifacts,
            qemu_uboot_booti.ArtifactExpectations(
                kernel_size=1,
                kernel_crc32="11111111",
                dtb_size=2,
                dtb_crc32="22222222",
                initrd_size=3,
                initrd_crc32="33333333",
                kernel_sha256="1" * 64,
                dtb_sha256="2" * 64,
                initrd_sha256="3" * 64,
            ),
        )

    def test_qemu_command_models_sv39_svade_and_uses_only_virtio(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "qemu_argv"))

        argv = qemu_uboot_booti.qemu_argv(
            uboot=Path("/tmp/u-boot"),
            boot_disk=Path("/tmp/boot.ext4"),
        )

        self.assertIn(
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            argv,
        )
        self.assertIn("/tmp/u-boot", argv)
        self.assertIn(
            "if=none,format=raw,file=/tmp/boot.ext4,id=bootdisk",
            argv,
        )
        self.assertNotIn("/dev/ttyUSB0", " ".join(argv))



if __name__ == "__main__":
    unittest.main()
