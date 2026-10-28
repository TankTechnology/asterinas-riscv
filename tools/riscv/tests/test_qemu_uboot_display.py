#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class CommandLineTests(_PreparedRunFixtures, unittest.TestCase):
    def test_cli_validates_display_outputs_and_device_set(self) -> None:
        required = [
            "run", "--uboot", "u", "--boot-disk", "b", "--manifest", "m",
            "--serial-log", "s", "--marker-event", "e", "--result", "r",
        ]
        valid = [*required, "--device-set", "megrez-basic", "--screenshot", "shot", "--display-audit", "audit"]
        parsed = qemu_uboot_booti._parse_args(valid)
        self.assertIs(parsed.device_set, MEGREZ_BASIC)
        for arguments in (
            [*required, "--device-set", "megrez-basic", "--screenshot", "shot"],
            [*required, "--screenshot", "shot", "--display-audit", "audit"],
            [*valid, "--scenario", "stale-bootargs"],
            [*required, "--device-set", "unknown"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(arguments)

    def test_programmatic_display_validation_precedes_input_access(self) -> None:
        replaced = QemuDeviceSet("headless", ())
        with mock.patch.object(qemu_uboot_booti, "load_artifact_manifest") as manifest:
            for kwargs, message in (
                ({"device_set": replaced}, "not a registered"),
                ({"device_set": MEGREZ_BASIC}, "requires positive display outputs"),
                ({"screenshot": Path("shot")}, "provided together"),
            ):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                    qemu_uboot_booti.run_prepared(
                        uboot=Path("/missing/u"), boot_disk=Path("/missing/b"), manifest=Path("/missing/m"),
                        serial_log=Path("/missing/s"), marker_event=Path("/missing/e"), result_path=Path("/missing/r"),
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                        **kwargs,
                    )
            manifest.assert_not_called()


class DisplayEvidenceTests(_PreparedRunFixtures, unittest.TestCase):
    def test_workspace_rejects_evidence_mutation_during_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result"}
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                def forge(evidence: object) -> None:
                    if evidence.label == "serial_log":
                        with outputs["serial_log"].open("r+b") as output:
                            output.write(b"FORGED")
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=forge):
                    with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                        workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)

    def test_workspace_rejects_output_directory_swap_during_evidence_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            output_directory = directory / "outputs"
            output_directory.mkdir()
            outputs = {name: output_directory / name for name in ("serial_log", "marker_event", "result_path")}
            detached = directory / "detached"
            real_verify = EXECUTION_IO_MODULE.PinnedRegularInput.verify_unchanged
            swapped = False

            def verify(evidence: object) -> None:
                nonlocal swapped
                real_verify(evidence)
                if not swapped and evidence.label == "serial_log":
                    swapped = True
                    output_directory.rename(detached)
                    output_directory.mkdir()
                    for path in outputs.values():
                        path.write_bytes(b"attacker replacement")

            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)
                with workspace.prepare_result(b"result") as prepared:
                    with prepared.retain() as result:
                        workspace.publish_result(prepared, b"result")
                        workspace.sync_result()
                        with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=verify):
                            with self.assertRaisesRegex(RuntimeError, "output directory path changed"):
                                workspace.verify_after_result(serial_identity=serial, marker_identity=marker, result_identity=result.identity)

    def test_workspace_rejects_path_swap_during_evidence_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            real_verify = EXECUTION_IO_MODULE.PinnedRegularInput.verify_unchanged
            swapped = False

            def verify(evidence: object) -> None:
                nonlocal swapped
                real_verify(evidence)
                if not swapped and evidence.label == "serial_log":
                    swapped = True
                    outputs["serial_log"].unlink()
                    outputs["serial_log"].write_bytes(b"replacement")

            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=verify):
                    with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                        workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)

    def test_workspace_rejects_output_mutation_before_evidence_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            real_open = EXECUTION_IO_MODULE.PinnedRegularInput.open

            def open_evidence(path: Path, *, label: str):
                if label == "serial_log":
                    with path.open("r+b") as output:
                        output.truncate()
                        output.write(b"attacker mutation")
                return real_open(path, label=label)

            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None
            ) as workspace:
                with mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedRegularInput,
                    "open",
                    side_effect=open_evidence,
                ):
                    with self.assertRaisesRegex(RuntimeError, "published output changed"):
                        workspace.publish_evidence("serial_log", b"trusted serial")

    def test_workspace_rejects_display_output_pair_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            common = dict(
                **inputs, serial_log=directory / "serial", marker_event=directory / "marker",
                result_path=directory / "result", progress_log=None,
            )
            with self.assertRaisesRegex(ValueError, "provided together"):
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **common, screenshot=directory / "shot", display_audit=None
                ):
                    pass
            with self.assertRaisesRegex(ValueError, "overlap"):
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **common,
                    screenshot=directory / "shot",
                    display_audit=directory / "shot",
                ):
                    pass
            original_inputs = {name: path.read_bytes() for name, path in inputs.items()}
            shot = directory / "shot"
            audit = directory / "audit"
            shot.write_bytes(b"existing")
            os.link(shot, audit)
            cases = [(shot, audit, "overlap")]
            link_target = directory / "link-target"
            link_target.write_bytes(b"target")
            for name in ("screenshot", "display_audit"):
                link = directory / f"{name}-link"
                link.symlink_to(link_target)
                cases.append((link if name == "screenshot" else shot, link if name == "display_audit" else audit, "symbolic link"))
                target = directory / f"{name}-directory"
                target.mkdir()
                cases.append((target if name == "screenshot" else shot, target if name == "display_audit" else audit, "regular file"))
            for input_path in inputs.values():
                cases.append((input_path, audit, "read-only input"))
            linked_input = directory / "linked-input"
            os.link(inputs["uboot"], linked_input)
            cases.append((linked_input, audit, "read-only input"))
            for screenshot, display_audit, message in cases:
                with self.subTest(screenshot=screenshot, display_audit=display_audit):
                    with self.assertRaisesRegex(ValueError, message):
                        with EXECUTION_IO_MODULE.open_execution_workspace(
                            **common,
                            screenshot=screenshot,
                            display_audit=display_audit,
                        ):
                            self.fail("rejected workspace entered its body")
                    self.assertEqual(
                        {name: path.read_bytes() for name, path in inputs.items()},
                        original_inputs,
                    )
                    self.assertFalse((directory / "serial").exists())
                    self.assertFalse((directory / "marker").exists())
                    self.assertFalse((directory / "result").exists())

    def test_workspace_display_pair_rolls_back_on_audit_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            original_inputs = {name: path.read_bytes() for name, path in inputs.items()}
            order: list[str] = []
            real_publish = EXECUTION_IO_MODULE.PreparedPublication.publish
            def publish(prepared: object) -> None:
                order.append(prepared._destination_name)
                if prepared._destination_name == "audit":
                    raise OSError("injected audit publish failure")
                real_publish(prepared)
            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None,
            ) as workspace:
                staging = workspace._staging_directory
                with mock.patch.object(EXECUTION_IO_MODULE.PreparedPublication, "publish", autospec=True, side_effect=publish):
                    with self.assertRaisesRegex(OSError, "injected audit"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                self.assertFalse(outputs["result_path"].exists())
                self.assertFalse(outputs["screenshot"].exists())
                self.assertFalse(outputs["display_audit"].exists())
                self.assertEqual(order, ["shot", "audit"])
                self.assertEqual({name: path.read_bytes() for name, path in inputs.items()}, original_inputs)
            self.assertFalse(staging.exists())

    def test_workspace_display_removal_failure_clears_invalidated_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            for path in (
                outputs["result_path"],
                outputs["screenshot"],
                outputs["display_audit"],
            ):
                path.write_bytes(b"old")
            real_remove = EXECUTION_IO_MODULE.ExecutionWorkspace._remove_output
            remove_attempts: list[str] = []
            fail_screenshot_once = True

            def remove_output(workspace: object, name: str) -> None:
                nonlocal fail_screenshot_once
                remove_attempts.append(name)
                if name == "screenshot" and fail_screenshot_once:
                    fail_screenshot_once = False
                    raise OSError("injected screenshot removal failure")
                real_remove(workspace, name)

            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None
            ) as workspace:
                with mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "_remove_output",
                    autospec=True,
                    side_effect=remove_output,
                ):
                    with self.assertRaisesRegex(OSError, "injected screenshot removal"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                self.assertFalse(outputs["result_path"].exists())
                self.assertFalse(outputs["screenshot"].exists())
                self.assertFalse(outputs["display_audit"].exists())
            self.assertEqual(
                remove_attempts,
                [
                    "result_path",
                    "screenshot",
                    "result_path",
                    "screenshot",
                    "display_audit",
                ],
            )

    def test_workspace_rejects_reconstructed_absent_display_output(self) -> None:
        for phase in ("staging", "result"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {
                    "serial_log": directory / "serial",
                    "marker_event": directory / "marker",
                    "result_path": directory / "result",
                    "screenshot": directory / "shot",
                    "display_audit": directory / "audit",
                }
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **inputs, **outputs, progress_log=None
                ) as workspace:
                    serial_identity = workspace.publish_evidence("serial_log", b"serial")
                    marker_identity = workspace.publish_evidence("marker_event", b"marker")
                    self.assertEqual(workspace.publish_display_evidence(None, None), (None, None))
                    if phase == "staging":
                        outputs["screenshot"].write_bytes(b"attacker reconstruction")
                        with self.assertRaisesRegex(RuntimeError, "unexpected output entry"):
                            workspace.verify_and_cleanup_staging(
                                serial_identity=serial_identity,
                                marker_identity=marker_identity,
                            )
                        continue
                    workspace.verify_and_cleanup_staging(
                        serial_identity=serial_identity,
                        marker_identity=marker_identity,
                    )
                    with workspace.prepare_result(b"result") as prepared_result:
                        with prepared_result.retain() as result_publication:
                            workspace.publish_result(prepared_result, b"result")
                            workspace.sync_result()
                            outputs["screenshot"].write_bytes(b"attacker reconstruction")
                            with self.assertRaisesRegex(RuntimeError, "unexpected output entry"):
                                workspace.verify_after_result(
                                    serial_identity=serial_identity,
                                    marker_identity=marker_identity,
                                    result_identity=result_publication.identity,
                                )

    def test_workspace_display_prepare_failure_preserves_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            real_prepare = EXECUTION_IO_MODULE.PinnedOutputDirectory.prepare_atomic_write
            calls = 0
            def fail_second(directory_pin: object, name: str, payload: bytes):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second prepare failed")
                return real_prepare(directory_pin, name, payload)
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedOutputDirectory, "prepare_atomic_write", autospec=True, side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "second prepare"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                    self.assertEqual(path.read_bytes(), b"old")

    def test_workspace_display_retain_failure_preserves_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            real_retain = EXECUTION_IO_MODULE.PreparedPublication.retain
            calls = 0
            def fail_second(prepared: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second retain failed")
                return real_retain(prepared)
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                with mock.patch.object(EXECUTION_IO_MODULE.PreparedPublication, "retain", autospec=True, side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "second retain"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                    self.assertEqual(path.read_bytes(), b"old")

    def test_megrez_basic_publishes_guarded_framebuffer_evidence(self) -> None:
        profile = qemu_uboot_booti.GENERIC_SV39
        scenario = qemu_uboot_booti.BootScenario.POSITIVE
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        ppm_audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, object] = {}

            def qemu_arguments(**kwargs: object) -> list[str]:
                seen.update(kwargs)
                return ["qemu-system-riscv64"]

            def session(_argv: list[str], **kwargs: object) -> object:
                self.assertIsNotNone(kwargs["terminal_action"])
                kwargs["terminal_action"]()
                return self._successful_session()

            def capture(socket: Path, shot: Path, *, capture_root: Path) -> bytes:
                self.assertEqual(capture_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(socket, capture_root / "qmp.sock")
                self.assertEqual(shot, capture_root / "shot.ppm")
                seen["capture_root"] = capture_root
                return payload

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=qemu_arguments),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(profile, scenario)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=capture),
                mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=ppm_audit),
                mock.patch.object(EXECUTION_MODULE, "boot_commands", wraps=qemu_uboot_booti.boot_commands) as commands,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot.ppm", display_audit=directory / "audit.json", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertIs(seen["device_set"], MEGREZ_BASIC)
            self.assertIs(commands.call_args.kwargs["device_set"], MEGREZ_BASIC)
            self.assertEqual((directory / "shot.ppm").read_bytes(), payload)
            expected_audit = json.dumps(qemu_uboot_booti.asdict(ppm_audit), indent=2, sort_keys=True) + "\n"
            self.assertEqual((directory / "audit.json").read_text(), expected_audit)
            self.assertEqual(result.device_set, "megrez-basic")
            self.assertEqual(result.screenshot_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(result.display_audit, qemu_uboot_booti.asdict(ppm_audit))
            self.assertFalse(Path(seen["capture_root"]).exists())

    def test_headless_execution_has_no_capture_side_effects(self) -> None:
        profile = qemu_uboot_booti.GENERIC_SV39
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            (directory / "result").write_bytes(b"stale result")
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]) as argv,
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", return_value=self._successful_session()) as session,
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(profile, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump") as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
                mock.patch.object(EXECUTION_MODULE, "boot_commands", wraps=qemu_uboot_booti.boot_commands) as commands,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertIs(argv.call_args.kwargs["device_set"], HEADLESS)
            self.assertIsNone(argv.call_args.kwargs["device_paths"])
            self.assertIs(commands.call_args.kwargs["device_set"], HEADLESS)
            self.assertIsNone(session.call_args.kwargs["terminal_action"])
            capture.assert_not_called()
            ppm.assert_not_called()
            self.assertEqual((result.device_set, result.screenshot_sha256, result.display_audit), ("headless", None, None))
            self.assertNotEqual((directory / "result").read_bytes(), b"stale result")

    def test_headless_workspace_replaces_a_preexisting_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result"}
            outputs["result_path"].write_bytes(b"stale result")
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)
                with workspace.prepare_result(b"new result") as prepared:
                    with prepared.retain() as result:
                        workspace.publish_result(prepared, b"new result")
                        workspace.sync_result()
                        workspace.verify_after_result(serial_identity=serial, marker_identity=marker, result_identity=result.identity)
            self.assertEqual(outputs["result_path"].read_bytes(), b"new result")

    def test_framebuffer_failed_audit_retains_evidence_as_fail(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\0\0\0" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 0, 1, None, False)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)

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
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=inputs["boot_disk"],
                    manifest=inputs["manifest"],
                    serial_log=directory / "serial",
                    marker_event=directory / "marker",
                    result_path=directory / "result",
                    screenshot=directory / "shot",
                    display_audit=directory / "audit",
                    device_set=MEGREZ_BASIC,
                    startup_timeout=1,
                    command_timeout=1,
                    boot_timeout=1,
                    termination_grace=1,
                )
            self.assertFalse(result.passed)
            self.assertEqual(result.status, "FAIL")
            self.assertEqual((directory / "shot").read_bytes(), payload)
            self.assertEqual(result.screenshot_sha256, hashlib.sha256(payload).hexdigest())

    def test_framebuffer_capture_exception_cleans_staging_without_false_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, Path] = {}

            def qemu_arguments(**kwargs: object) -> list[str]:
                paths = kwargs["device_paths"]
                seen["capture_root"] = paths.capture_root
                return ["qemu-system-riscv64"]

            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=qemu_arguments),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=RuntimeError("capture failed")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as audit_ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                        serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                        screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertFalse(seen["capture_root"].exists())
            audit_ppm.assert_not_called()
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertEqual(result.status, "ERROR")
            self.assertTrue((directory / "result").exists())
            result_payload = json.loads((directory / "result").read_text())
            self.assertIsNone(result_payload["screenshot_sha256"])
            self.assertIsNone(result_payload["display_audit"])
            self.assertIn("capture-error:", (directory / "marker").read_text())

    def test_framebuffer_capture_timeout_replaces_stale_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name, payload in (("result", b"old result"), ("shot", b"old shot"), ("audit", b"old audit")):
                (directory / name).write_bytes(payload)
            seen: dict[str, Path] = {}
            def argv(**kwargs: object) -> list[str]:
                seen["root"] = kwargs["device_paths"].capture_root
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=TimeoutError("qmp timed out")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            payload = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, payload["terminal_classification"]), ("ERROR", "INCOMPLETE"))
            self.assertFalse(payload["passed"])
            self.assertIsNone(payload["screenshot_sha256"])
            self.assertIsNone(payload["display_audit"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(seen["root"].exists())
            ppm.assert_not_called()

    def test_framebuffer_capture_value_error_replaces_stale_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name in ("result", "shot", "audit"):
                (directory / name).write_bytes(b"old")
            roots: list[Path] = []
            def argv(**kwargs: object) -> list[str]:
                roots.append(kwargs["device_paths"].capture_root)
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=ValueError("malformed QMP")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            saved = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, saved["terminal_classification"], saved["screenshot_sha256"], saved["display_audit"]), ("ERROR", "INCOMPLETE", None, None))
            self.assertFalse(saved["passed"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(roots[0].exists())
            ppm.assert_not_called()

    def test_framebuffer_audit_value_error_withholds_capture_pair(self) -> None:
        ppm_payload = b"P6\n1280 1024\n255\n" + b"\1\2\3" * (1280 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name in ("result", "shot", "audit"):
                (directory / name).write_bytes(b"old")
            roots: list[Path] = []
            def argv(**kwargs: object) -> list[str]:
                roots.append(kwargs["device_paths"].capture_root)
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=ppm_payload) as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm", side_effect=ValueError("malformed PPM")) as audit,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            capture.assert_called_once()
            audit.assert_called_once_with(ppm_payload, expected_width=1280, expected_height=1024)
            saved = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, saved["terminal_classification"], saved["screenshot_sha256"], saved["display_audit"]), ("ERROR", "INCOMPLETE", None, None))
            self.assertFalse(saved["passed"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(roots[0].exists())

    def test_framebuffer_boot_timeout_removes_stale_display_evidence(self) -> None:
        timeout = replace(self._successful_session(), marker_seen=False, timed_out=True, failure="boot-timeout")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            (directory / "result").write_bytes(b"old")
            (directory / "shot").write_bytes(b"old")
            (directory / "audit").write_bytes(b"old")
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", return_value=timeout),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump") as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                    serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertEqual((result.status, result.screenshot_sha256, result.display_audit), ("FAIL", None, None))
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(json.loads((directory / "result").read_text())["passed"])
            self.assertEqual(result.terminal_classification, "INCOMPLETE")
            self.assertTrue((directory / "serial").exists())
            self.assertTrue((directory / "marker").exists())
            capture.assert_not_called()
            ppm.assert_not_called()

    def test_in_place_display_evidence_mutation_revokes_result(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            real_verify = EXECUTION_IO_MODULE.ExecutionWorkspace.verify_after_result

            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()

            def verify_after_result(workspace: object, **kwargs: object) -> None:
                for name in ("shot", "audit"):
                    with (directory / name).open("r+b") as evidence:
                        evidence.truncate()
                        evidence.write(b"attacker mutation")
                        evidence.flush()
                        os.fsync(evidence.fileno())
                real_verify(workspace, **kwargs)

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=payload),
                mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=audit),
                mock.patch.object(EXECUTION_IO_MODULE.ExecutionWorkspace, "verify_after_result", autospec=True, side_effect=verify_after_result),
            ):
                with self.assertRaisesRegex(RuntimeError, "screenshot changed during the run"):
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                        serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                        screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                    )
            self.assertFalse(json.loads((directory / "result").read_text())["passed"])

    def test_framebuffer_process_error_withholds_display_evidence(self) -> None:
        session = replace(
            self._successful_session(),
            failure="process-error:booti",
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, Path] = {}
            def argv(**kwargs: object) -> list[str]:
                seen["capture_root"] = kwargs["device_paths"].capture_root
                return ["qemu-system-riscv64"]
            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return session
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=run_session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=TimeoutError("capture timed out")) as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                    serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertEqual((result.status, result.terminal_classification), ("ERROR", "INCOMPLETE"))
            payload = json.loads((directory / "result").read_text())
            self.assertEqual((payload["device_set"], payload["screenshot_sha256"], payload["display_audit"]), ("megrez-basic", None, None))
            self.assertFalse(payload["passed"])
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(seen["capture_root"].exists())
            capture.assert_called_once()
            ppm.assert_not_called()
            self.assertIn("process-error:booti", (directory / "marker").read_text())


if __name__ == "__main__":
    unittest.main()
