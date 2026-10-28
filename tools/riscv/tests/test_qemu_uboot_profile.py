#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class PreparedRunProfileTests(_PreparedRunFixtures, unittest.TestCase):
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
