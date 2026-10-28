#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class PreparedRunExecutionTests(_PreparedRunFixtures, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
