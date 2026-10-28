#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.megrez_physical_graphics import *


class HumanCodeTests(unittest.TestCase):
    def test_host_accepts_exactly_four_decimal_code_digits(self) -> None:
        gate = load_gate(self)
        self.assertEqual(len(gate._validated_nonce_hashes(("0427",))), 1)
        for wrong in ("042", "04270", "abcd"):
            with self.subTest(wrong=wrong), self.assertRaises(gate.HostGateError):
                gate._validated_nonce_hashes((wrong,))

    def test_host_generates_distinct_zero_padded_codes(self) -> None:
        gate = load_gate(self)
        with mock.patch.object(gate.secrets, "randbelow", side_effect=(42, 42, 73, 99)):
            self.assertEqual(gate._fresh_codes(3), ("0042", "0073", "0099"))

    def test_start_reader_accepts_only_exact_code_line(self) -> None:
        gate = load_gate(self)
        gate._read_operator_start(
            "0427",
            1.0,
            stream=io.StringIO("0427\n"),
            wait_readable=lambda _stream, _timeout: True,
        )
        for reply in ("042\n", "9876\n", "0427", "0427\nextra\n"):
            with self.subTest(reply=reply), self.assertRaises(
                (gate.HostGateError, TimeoutError)
            ):
                gate._read_operator_start(
                    "0427",
                    1.0,
                    stream=io.StringIO(reply),
                    wait_readable=lambda _stream, _timeout: True,
                )


class PhysicalMarkerTests(unittest.TestCase):


    @classmethod
    def _cycle_lines(cls, cycle: int) -> list[str]:
        nonce = NONCES[cycle - 1]
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        event_hash = hashlib.sha256(f"events-{cycle}".encode()).hexdigest()
        screenshot_hash = hashlib.sha256(f"screen-{cycle}".encode()).hexdigest()
        return [
            f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} nonce_sha256={nonce_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} key_downs=4 relative_events=2 absolute_events=0 left_down=1 left_up=1 digest={event_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
            f"ASTERINAS_PHYSICAL_GRAPHICS_LATENCY cycle={cycle} count=4 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
            f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} sha256={screenshot_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}",
        ]

    @classmethod
    def _passing(cls) -> str:
        lines = [line for cycle in range(1, 4) for line in cls._cycle_lines(cycle)]
        lines.append("ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3")
        return "boot noise\n" + "\n".join(lines) + "\nrecovery noise\n"

    def test_accepts_three_exact_nonce_bound_cycles(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        cycles = gate.classify_interaction_transcript(self._passing(), NONCES)
        self.assertEqual([cycle.cycle for cycle in cycles], [1, 2, 3])
        self.assertEqual([cycle.key_downs for cycle in cycles], [4, 4, 4])
        self.assertEqual([cycle.input_latency_p95_ms for cycle in cycles], [4.0] * 3)

    def test_accepts_one_exact_nonce_bound_cycle(self) -> None:
        gate = load_gate(self)
        transcript = "\n".join(
            [*self._cycle_lines(1), "ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=1"]
        )

        cycles = gate.classify_interaction_transcript(transcript, NONCES[:1])

        self.assertEqual([cycle.cycle for cycle in cycles], [1])
        self.assertEqual(
            cycles[0].nonce_sha256, hashlib.sha256(NONCES[0].encode()).hexdigest()
        )

    def test_rejects_two_cycle_nonce_and_hash_sequences(self) -> None:
        gate = load_gate(self)
        nonce_hashes = tuple(
            hashlib.sha256(nonce.encode()).hexdigest() for nonce in NONCES[:2]
        )
        transcript = "\n".join(
            [
                *(line for cycle in range(1, 3) for line in self._cycle_lines(cycle)),
                "ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=2",
            ]
        )

        with self.assertRaisesRegex(gate.HostGateError, "one or three"):
            gate.classify_interaction_transcript(transcript, NONCES[:2])
        with self.assertRaisesRegex(gate.HostGateError, "one or three"):
            gate.classify_interaction_hash_transcript(transcript, nonce_hashes)

    def test_accepts_three_exact_nonce_hash_bound_cycles(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_hash_transcript"),
            "interaction hash classifier is missing",
        )
        nonce_hashes = tuple(
            hashlib.sha256(nonce.encode()).hexdigest() for nonce in NONCES
        )

        cycles = gate.classify_interaction_hash_transcript(
            self._passing(), nonce_hashes
        )

        self.assertEqual(tuple(cycle.nonce_sha256 for cycle in cycles), nonce_hashes)
        with self.assertRaises(gate.HostGateError):
            gate.classify_interaction_hash_transcript(
                self._passing(), (nonce_hashes[0],) * 3
            )

    def test_rejects_missing_duplicate_reordered_and_wrong_nonce_markers(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        lines = self._passing().splitlines()
        ready = self._cycle_lines(1)[0]
        wrong_nonce = ready.rsplit("=", 1)[0] + "=" + "0" * 64
        variants = (
            "\n".join(line for line in lines if line != self._cycle_lines(2)[2]),
            "\n".join(lines[:3] + [ready] + lines[3:]),
            "\n".join(lines[:2] + [lines[3], lines[2]] + lines[4:]),
            "\n".join(wrong_nonce if line == ready else line for line in lines),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[:80]),
                self.assertRaises(gate.HostGateError),
            ):
                gate.classify_interaction_transcript(transcript, NONCES)

    def test_rejects_weak_input_and_late_fatal_marker(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        weak = self._passing().replace("key_downs=4", "key_downs=3", 1)
        fatal = self._passing() + "Kernel panic - not syncing\n"
        xhci = self._passing() + "USB HID transfer stopped: timeout\n"
        framebuffer = (
            self._passing()
            + "Firmware framebuffer is not synchronizable; leaving it unregistered\n"
        )
        for transcript in (weak, fatal, xhci, framebuffer):
            with self.assertRaises(gate.HostGateError):
                gate.classify_interaction_transcript(transcript, NONCES)

    def test_rejects_invalid_latency_summary(self) -> None:
        gate = load_gate(self)
        for invalid in (
            "count=0 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
            "count=4 min_ms=5.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
            "count=4 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=60001.000",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(gate.HostGateError):
                gate.classify_interaction_transcript(
                    self._passing().replace(
                        "count=4 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
                        invalid,
                        1,
                    ),
                    NONCES,
                )

    def test_physical_mode_rejects_absolute_only_tablet_motion(self) -> None:
        gate = load_gate(self)
        tablet_only = self._passing().replace(
            "relative_events=2 absolute_events=0",
            "relative_events=0 absolute_events=2",
        )
        with self.assertRaisesRegex(gate.HostGateError, "physical input"):
            gate.classify_interaction_transcript(tablet_only, NONCES)

        cycles = gate.classify_interaction_transcript(
            tablet_only,
            NONCES,
            pointer_mode=gate.PointerEvidenceMode.QEMU_TABLET,
        )
        self.assertTrue(all(cycle.absolute_events == 2 for cycle in cycles))

    def test_rejects_reused_screenshot_digest_across_distinct_cycles(self) -> None:
        gate = load_gate(self)
        first = self._cycle_lines(1)[6].rsplit("=", 1)[1]
        stale = self._passing()
        for cycle in (2, 3):
            current = self._cycle_lines(cycle)[6].rsplit("=", 1)[1]
            stale = stale.replace(current, first)
        with self.assertRaisesRegex(gate.HostGateError, "screenshot digest"):
            gate.classify_interaction_transcript(stale, NONCES)


class HdmiEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_operations_compare_retained_hdmi_with_final_firefox_pixels(self) -> None:
        from tools.riscv.tests.test_hdmi_pixel_oracle import pattern_png

        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = (
                repository / "target" / "current-main-physical-graphics"
                / "physical" / "evidence"
            )
            operations = gate.RealPhysicalGraphicsOperations(
                SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
                "/dev/null", output, repository / "capture.png", repository=repository,
            )
            operations.invalidate()
            screenshot = pattern_png(width=1920, height=1080)
            path = output / "hdmi-evidence.png"
            operations._output.atomic_write(path.name, screenshot, mode=0o600)
            hdmi = gate.FileEvidence(
                path=path, size=len(screenshot),
                sha256=hashlib.sha256(screenshot).hexdigest(), format="png",
            )

            operations.verify_hdmi_pixels(screenshot, hdmi)
            self.assertEqual(operations._pixel_comparison.matched_fraction, 1.0)
            wrong = pattern_png(width=1920, height=1080, wrong=True)
            operations._output.atomic_write("wrong-hdmi.png", wrong, mode=0o600)
            wrong_hdmi = gate.FileEvidence(
                path=output / "wrong-hdmi.png", size=len(wrong),
                sha256=hashlib.sha256(wrong).hexdigest(), format="png",
            )
            with self.assertRaisesRegex(gate.HostGateError, "HDMI pixel comparison failed"):
                operations.verify_hdmi_pixels(screenshot, wrong_hdmi)
            self.assertIsNone(operations._pixel_comparison)
            stale_metrics = output / "hdmi-pixel-check.json"
            stale_metrics.write_text('{"matched_fraction":1.0}')
            operations.invalidate()
            self.assertFalse(stale_metrics.exists())

    def test_ingests_png_and_jpeg_with_digest_and_private_mode(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "ingest_hdmi"), "HDMI ingestion is missing")
        payloads = (
            ("png", png_payload(value=0x51)),
            ("jpg", b"\xff\xd8physical-display\xff\xd9"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix, payload in payloads:
                with self.subTest(suffix=suffix):
                    source = root / f"source.{suffix}"
                    destination = root / f"retained.{suffix}"
                    source.write_bytes(payload)
                    evidence = gate.ingest_hdmi(source, destination)
                    self.assertEqual(destination.read_bytes(), payload)
                    self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        evidence.sha256, hashlib.sha256(payload).hexdigest()
                    )
                    self.assertEqual(evidence.size, len(payload))

    def test_rejects_symlink_empty_oversized_and_output_alias(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "ingest_hdmi"), "HDMI ingestion is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.png"
            valid.write_bytes(png_payload(value=0x51))
            symlink = root / "symlink.png"
            symlink.symlink_to(valid)
            empty = root / "empty.png"
            empty.touch()
            incomplete = root / "incomplete.png"
            incomplete.write_bytes(b"\x89PNG\r\n\x1a\npartial")
            oversized = root / "oversized.png"
            with oversized.open("wb") as stream:
                stream.truncate(gate.MAX_HDMI_BYTES + 1)
            for source, destination in (
                (symlink, root / "from-symlink.png"),
                (empty, root / "from-empty.png"),
                (incomplete, root / "from-incomplete.png"),
                (oversized, root / "from-oversized.png"),
                (valid, valid),
            ):
                with (
                    self.subTest(source=source.name),
                    self.assertRaises(gate.HostGateError),
                ):
                    gate.ingest_hdmi(source, destination)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_rejects_fifo_sources_without_blocking(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "capture.fifo"
            os.mkfifo(fifo)
            before = time.monotonic()
            with self.assertRaises(gate.HostGateError):
                gate.ingest_hdmi(fifo, root / "retained.png")
            self.assertLess(time.monotonic() - before, 1.0)

    def test_real_attempt_requires_a_capture_created_or_changed_after_start(
        self,
    ) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            capture = repository / "capture.png"
            stale_payload = png_payload(value=0x51)
            current_payload = png_payload(value=0x52)
            capture.write_bytes(stale_payload)
            operations = gate.RealPhysicalGraphicsOperations(
                SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
                "/dev/null",
                output,
                capture,
                repository=repository,
            )
            try:
                operations.invalidate()
                operations._guest_deadline = time.monotonic() + 900
                with self.assertRaises(TimeoutError):
                    operations.retain_hdmi(0.001)
                update = threading.Timer(
                    0.02,
                    capture.write_bytes,
                    args=(current_payload,),
                )
                update.start()
                try:
                    evidence = operations.retain_hdmi(1.0)
                finally:
                    update.join()
                self.assertEqual(evidence.path.name, "hdmi-evidence.png")
                self.assertEqual(
                    evidence.sha256, hashlib.sha256(capture.read_bytes()).hexdigest()
                )
            finally:
                operations.close()

    def test_real_attempt_waits_for_a_phased_capture_to_become_stable(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            capture = repository / "capture.png"
            payload = png_payload(value=0x53)
            operations = gate.RealPhysicalGraphicsOperations(
                SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
                "/dev/null",
                output,
                capture,
                repository=repository,
            )

            def produce() -> None:
                time.sleep(0.02)
                with capture.open("wb") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                    time.sleep(0.05)
                    stream.write(payload[:32])
                    stream.flush()
                    os.fsync(stream.fileno())
                    time.sleep(0.05)
                    stream.write(payload[32:])
                    stream.flush()
                    os.fsync(stream.fileno())

            operations.invalidate()
            operations._guest_deadline = time.monotonic() + 900
            producer = threading.Thread(target=produce)
            producer.start()
            try:
                evidence = operations.retain_hdmi(2.0)
            finally:
                producer.join()
                operations.close()
            self.assertEqual(evidence.sha256, hashlib.sha256(payload).hexdigest())


class OperatorDisplayEvidenceTests(unittest.TestCase):
    def test_accepts_only_nonce_bound_confirmed_final_cycle_state(self) -> None:
        gate = load_gate(self)
        nonce_sha256 = hashlib.sha256(b"0123").hexdigest()

        evidence = gate.OperatorDisplayEvidence(
            kind="operator-attested",
            nonce_sha256=nonce_sha256,
            state="cyan-final-cycle-pass",
            confirmed=True,
        )

        self.assertEqual(evidence.nonce_sha256, nonce_sha256)
        for mutation in (
            {"kind": "hdmi"},
            {"nonce_sha256": "0"},
            {"state": "cyan-cycle-3-pass"},
            {"confirmed": False},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(gate.HostGateError):
                gate.OperatorDisplayEvidence(
                    kind=mutation.get("kind", "operator-attested"),
                    nonce_sha256=mutation.get("nonce_sha256", nonce_sha256),
                    state=mutation.get("state", "cyan-final-cycle-pass"),
                    confirmed=mutation.get("confirmed", True),
                )

    def test_configuration_accepts_only_one_or_three_cycles_and_typed_mode(
        self,
    ) -> None:
        gate = load_gate(self)
        one = gate.PhysicalGraphicsConfig(
            cycles_requested=1,
            display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
        )
        self.assertEqual(one.cycles_requested, 1)
        self.assertIs(one.display_mode, gate.DisplayEvidenceMode.OPERATOR_ATTESTED)
        with self.assertRaises(ValueError):
            gate.PhysicalGraphicsConfig(cycles_requested=2)
        with self.assertRaises(ValueError):
            gate.PhysicalGraphicsConfig(display_mode="operator-attested")

    def test_confirmation_reader_accepts_only_one_exact_nonce_bound_line(self) -> None:
        gate = load_gate(self)
        expected = "confirm-cyan-pass 0123"

        def read(payload: str) -> None:
            stream = io.StringIO(payload)
            gate._read_operator_confirmation(
                expected,
                1.0,
                stream=stream,
                wait_readable=lambda candidate, _timeout: candidate.tell()
                < len(payload),
            )

        read(expected + "\n")
        piped = io.StringIO(expected + "\n")
        gate._read_operator_confirmation(
            expected,
            1.0,
            stream=piped,
            wait_readable=lambda _stream, _timeout: True,
        )
        for payload in (
            "",
            "confirm-cyan-pass 9876\n",
            expected,
            expected + "\n" + expected + "\n",
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises((gate.HostGateError, TimeoutError)),
            ):
                read(payload)

        with self.assertRaisesRegex(TimeoutError, "timed out"):
            gate._read_operator_confirmation(
                expected,
                0.01,
                stream=io.StringIO(expected + "\n"),
                wait_readable=lambda _stream, _timeout: False,
            )
        with self.assertRaisesRegex(gate.HostGateError, "EOF"):
            gate._read_operator_confirmation(
                expected,
                1.0,
                stream=io.StringIO(""),
                wait_readable=lambda _stream, _timeout: True,
            )

    def test_real_adapter_binds_confirmation_to_final_nonce_suffix(self) -> None:
        gate = load_gate(self)
        reader = mock.Mock()
        operations = gate.RealPhysicalGraphicsOperations(
            SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
            "/dev/null",
            Path("/unused"),
            None,
            display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
            cycles_requested=1,
            confirmation_reader=reader,
        )
        operations._guest_deadline = time.monotonic() + 60

        evidence = operations.retain_operator_display("0123", 30)

        self.assertEqual(evidence.kind, "operator-attested")
        self.assertEqual(
            evidence.nonce_sha256,
            hashlib.sha256(b"0123").hexdigest(),
        )
        self.assertEqual(reader.call_args.args[0], "confirm-cyan-pass 0123")
        self.assertGreater(reader.call_args.args[1], 0)
        self.assertLessEqual(reader.call_args.args[1], 30)

    def test_real_adapter_interrupts_uboot_without_repeating_command_history(
        self,
    ) -> None:
        gate = load_gate(self)
        session = mock.Mock()
        close_device = mock.Mock()
        operations = gate.RealPhysicalGraphicsOperations(
            SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
            "/dev/serial/by-id/test",
            Path("/unused"),
            None,
            display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
            cycles_requested=1,
            open_device=mock.Mock(return_value=57),
            lock_device=mock.Mock(),
            close_device=close_device,
            session_factory=mock.Mock(return_value=session),
        )

        with mock.patch("os.write") as write:
            operations.open(30)
            operations.close()

        write.assert_called_once_with(57, b"\x03")
        session.send.assert_not_called()
        session.wait_for_uboot_prompt.assert_called_once_with(30)
        close_device.assert_called_once_with(57)


class PhysicalCliTests(unittest.TestCase):
    BASE_ARGUMENTS = (
        "/dev/serial/by-id/test",
        "--plan",
        "/tmp/plan.json",
        "--output-directory",
        "/tmp/evidence",
        "--hdmi-capture",
        "/tmp/capture.png",
    )

    def test_cli_selects_exactly_one_display_mode_and_one_or_three_cycles(
        self,
    ) -> None:
        gate = load_gate(self)
        common = (
            "/dev/serial/by-id/test",
            "--plan",
            "/tmp/plan.json",
            "--output-directory",
            "/tmp/evidence",
        )
        external = gate.parse_args(common + ("--hdmi-capture", "/tmp/capture.png"))
        operator = gate.parse_args(
            common + ("--operator-display-attestation", "--cycles", "1")
        )
        self.assertEqual(external.cycles, 3)
        self.assertIsNotNone(external.hdmi_capture)
        self.assertFalse(external.operator_display_attestation)
        self.assertFalse(external.operator_start)
        self.assertEqual(operator.cycles, 1)
        self.assertTrue(
            gate.parse_args(common + ("--operator-display-attestation", "--operator-start")).operator_start
        )
        self.assertEqual(operator.recovery_timeout, 1170.0)
        self.assertIsNone(operator.hdmi_capture)
        self.assertTrue(operator.operator_display_attestation)

        for variant in (
            (),
            (
                "--hdmi-capture",
                "/tmp/capture.png",
                "--operator-display-attestation",
            ),
            ("--operator-display-attestation", "--cycles", "2"),
        ):
            with self.subTest(variant=variant), self.assertRaises(SystemExit):
                gate.parse_args(common + variant)

    def test_cli_accepts_one_complete_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        values = gate.parse_args(
            self.BASE_ARGUMENTS
            + (
                "--mmc-kernel",
                "asterinas-a1b2c3d4-34bc1cc0.Image",
                "--mmc-initramfs",
                "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
                "--mmc-dtb",
                "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
            )
        )
        self.assertEqual(values.mmc_kernel, "asterinas-a1b2c3d4-34bc1cc0.Image")
        self.assertEqual(
            values.mmc_initramfs,
            "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
        )
        self.assertEqual(
            values.mmc_dtb,
            "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
        )

    def test_cli_rejects_partial_or_unsafe_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        variants = (
            ("--mmc-kernel", "kernel.Image"),
            (
                "--mmc-kernel",
                "kernel.Image",
                "--mmc-initramfs",
                "stage1.cpio",
            ),
            (
                "--mmc-kernel",
                "kernel;reset",
                "--mmc-initramfs",
                "stage1.cpio",
                "--mmc-dtb",
                "board.dtb",
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(SystemExit):
                gate.parse_args(self.BASE_ARGUMENTS + variant)


class PhysicalMmcArtifactTests(unittest.TestCase):
    def test_physical_artifacts_require_a_publication_prepared_dtb(self) -> None:
        gate = load_gate(self)
        plan = SimpleNamespace(
            artifacts=(SimpleNamespace(name="megrez_dtb", path="/tmp/board.dtb"),)
        )
        with (
            mock.patch.object(gate, "_validate_current_artifacts", return_value={}),
            mock.patch.object(gate, "validate_prepared_dtb") as validate_dtb,
        ):
            gate._validate_physical_artifacts(plan)
        validate_dtb.assert_called_once_with(Path("/tmp/board.dtb"))

    def test_real_boot_uses_conservative_linux_shell_uart_pacing(self) -> None:
        gate = load_gate(self)
        artifacts = tuple(
            SimpleNamespace(name=name, load_address=address, size=size)
            for name, address, size in (
                ("kernel", 0x80200000, 101),
                ("initramfs", 0x83000000, 202),
                ("megrez_dtb", 0xF0000000, 303),
            )
        )
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._session = mock.Mock()
        operations._fd = 41
        operations._guest_deadline = None
        operations._guest_started = False

        with mock.patch.object(gate, "SerialConsole") as serial_console:
            operations.boot(SimpleNamespace(artifacts=artifacts), "console=tty0", 30)

        commands = [call.args[0] for call in operations._session.command.call_args_list]
        self.assertEqual(commands.count("fdt resize 0x1000"), 1)
        self.assertFalse(any("framebuffer@fd800000" in command for command in commands))
        self.assertFalse(any("asterinas,usb-host" in command for command in commands))
        self.assertEqual(commands[-1], "booti 0x80200000 0x83000000:0xca 0xf0000000")
        self.assertEqual(
            operations._session.command.call_args.kwargs["expect"], "Enter riscv_boot"
        )
        operations._session.send.assert_not_called()
        serial_console.assert_called_once_with(
            41,
            max_bytes=gate.MAX_TRANSCRIPT_BYTES,
            tx_delay=0.01,
        )

    def test_real_operations_load_complete_mmc_mapping_without_board_transport(
        self,
    ) -> None:
        gate = load_gate(self)
        artifacts = tuple(
            SimpleNamespace(
                name=name,
                load_address=address,
                size=size,
                crc32=crc32,
            )
            for name, address, size, crc32 in (
                ("kernel", 0x80200000, 101, "11111111"),
                ("initramfs", 0x83000000, 202, "22222222"),
                ("megrez_dtb", 0xF0000000, 303, "33333333"),
            )
        )
        names = {
            "kernel": "asterinas-a1b2c3d4-34bc1cc0.Image",
            "initramfs": "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
            "megrez_dtb": (
                "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb"
            ),
        }
        session = mock.Mock()
        session.load_artifact.side_effect = (101, 202, 303)
        operations = gate.RealPhysicalGraphicsOperations(
            SimpleNamespace(artifacts=artifacts),
            "/dev/null",
            Path("/unused"),
            Path("/unused-capture"),
            mmc_artifacts=names,
        )
        operations._session = session
        operations._fd = 41

        with mock.patch.object(
            gate,
            "BoardTransport",
            side_effect=AssertionError("YMODEM path must not be constructed"),
        ):
            outcomes = operations.ensure_artifacts(
                SimpleNamespace(artifacts=artifacts), 300
            )

        self.assertEqual(outcomes, ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"))
        self.assertEqual(
            session.load_artifact.call_args_list,
            [
                mock.call("kernel", names["kernel"], 0x80200000, "11111111"),
                mock.call("initramfs", names["initramfs"], 0x83000000, "22222222"),
                mock.call("megrez_dtb", names["megrez_dtb"], 0xF0000000, "33333333"),
            ],
        )

    def test_real_operations_reject_mmc_size_mismatch(self) -> None:
        gate = load_gate(self)
        artifacts = (
            SimpleNamespace(
                name="kernel",
                load_address=0x80200000,
                size=101,
                crc32="11111111",
            ),
        )
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._session = mock.Mock()
        operations._session.load_artifact.return_value = 100
        operations._fd = 41
        operations._mmc_artifacts = {"kernel": "kernel.Image"}
        with self.assertRaisesRegex(gate.HostGateError, "size mismatch"):
            operations.ensure_artifacts(SimpleNamespace(artifacts=artifacts), 300)


class PlanInputTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_rejects_fifo_plan_without_blocking(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "plan.fifo"
            os.mkfifo(fifo)
            before = time.monotonic()
            with self.assertRaises(gate.HostGateError):
                gate._read_plan(fifo)
            self.assertLess(time.monotonic() - before, 1.0)


class OutputDirectoryTests(unittest.TestCase):
    def test_accepts_only_the_current_main_physical_evidence_tree(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            expected = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            self.assertEqual(
                gate._safe_output_directory(expected, repository), expected
            )
            for rejected in (
                repository / "target" / "megrez-physical-graphics" / "attempt",
                repository / "target" / "unrelated",
            ):
                with self.assertRaises(gate.HostGateError):
                    gate._safe_output_directory(rejected, repository)


class DocumentationTests(unittest.TestCase):
    def test_prepare_and_operator_guide_cover_one_cycle_attested_mmc_run(self) -> None:
        makefile = MAKEFILE_PATH.read_text()
        recipe = makefile.split("prepare_riscv_megrez_physical_graphics:", 1)[1].split(
            ".PHONY:", 1
        )[0]
        readme = README_PATH.read_text()
        section = readme.split("## Current-main Megrez physical graphics", 1)[1]
        for fragment in (
            "MEGREZ_PHYSICAL_GRAPHICS_DISPLAY",
            "MEGREZ_PHYSICAL_GRAPHICS_CYCLES",
            "MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL",
            "MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS",
            "MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB",
            "--operator-display-attestation",
            "--cycles",
            "--mmc-kernel",
            "--mmc-initramfs",
            "--mmc-dtb",
        ):
            self.assertIn(fragment, makefile + recipe)
        for phrase in (
            "operator-attested",
            "confirm-cyan-pass",
            "one complete interaction path",
            "does not prove three-cycle repeatability",
            "operator-display-attestation.json",
        ):
            self.assertIn(phrase, section)

    def test_makefile_exposes_unit_qemu_and_non_mutating_prepare_targets(self) -> None:
        makefile = MAKEFILE_PATH.read_text()
        for target in (
            "test_riscv_physical_graphics_unit",
            "test_riscv_physical_graphics_qemu_gate",
            "prepare_riscv_megrez_physical_graphics",
        ):
            self.assertIn(f".PHONY: {target}", makefile)
            self.assertIn(f"{target}:", makefile)

        unit_recipe = makefile.split("test_riscv_physical_graphics_unit:", 1)[1].split(
            ".PHONY:", 1
        )[0]
        for module in (
            "tools.riscv.tests.test_physical_graphics_gate",
            "tools.riscv.tests.test_hdmi_pixel_oracle",
            "tools.riscv.tests.test_megrez_physical_graphics",
            "tools.riscv.tests.test_physical_graphics_qemu_gate",
        ):
            self.assertIn(module, unit_recipe)

        qemu_recipe = makefile.split("test_riscv_physical_graphics_qemu_gate:", 1)[
            1
        ].split(".PHONY:", 1)[0]
        for variable in (
            "DEBIAN_KERNEL",
            "DEBIAN_UBOOT",
            "DEBIAN_DTB",
            "DEBIAN_STAGE1_INITRAMFS",
            "DEBIAN_ROOT_IMAGE",
            "DEBIAN_ROOT_MANIFEST",
            "DEBIAN_PACKAGES_LOCK",
            "DEBIAN_PACKAGE_CHECKSUMS",
            "RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT",
        ):
            self.assertIn(f"$({variable})", qemu_recipe)
        self.assertIn("tools.riscv.physical_graphics_qemu_gate", qemu_recipe)
        self.assertIn("--command-timeout 300", qemu_recipe)

        prepare_recipe = makefile.split("prepare_riscv_megrez_physical_graphics:", 1)[
            1
        ].split(".PHONY:", 1)[0]
        self.assertIn("command -v ffmpeg", prepare_recipe)
        self.assertIn("command -v ffprobe", prepare_recipe)
        self.assertIn(
            "MEGREZ_PHYSICAL_GRAPHICS_OUTPUT ?= "
            "$(CURDIR)/target/current-main-physical-graphics/physical/evidence",
            makefile,
        )
        self.assertIn("_validate_current_artifacts", prepare_recipe)
        self.assertIn("_read_plan", prepare_recipe)
        self.assertIn("--hdmi-capture", prepare_recipe)
        for option in (
            "--open-timeout 60",
            "--artifact-timeout 300",
            "--boot-timeout 300",
            "--cycle-timeout 180",
            "--hdmi-timeout 60",
            "--recovery-timeout 930",
        ):
            self.assertIn(option, prepare_recipe)
        self.assertIn(
            '--output-directory "$(MEGREZ_PHYSICAL_GRAPHICS_OUTPUT)"',
            prepare_recipe,
        )
        self.assertNotIn("tools.riscv.megrez_debug board", prepare_recipe)
        self.assertNotIn("run_physical_graphics", prepare_recipe)

    def test_operator_guide_freezes_source_inputs_and_physical_boundaries(self) -> None:
        readme = README_PATH.read_text()
        section = readme.split("## Current-main Megrez physical graphics", 1)[1]
        prose = " ".join(section.split())
        self.assertIn("69a7b6e41ca74932f79d917f3638199da573b1e9", section)
        for option in (
            "--kernel",
            "--uboot",
            "--dtb",
            "--stage1-initramfs",
            "--root-image",
            "--root-manifest",
            "--packages-lock",
            "--package-checksums",
            "--hdmi-capture",
        ):
            self.assertIn(option, section)
        self.assertIn("seven immutable supporting inputs", prose)
        self.assertIn("three 180-second", prose)
        self.assertIn("asterinas.reboot_after=1140", section)
        self.assertIn("QEMU cannot satisfy the physical result", prose)
        self.assertIn("set -euo pipefail", section)
        self.assertIn("source-identity.txt", section)
        self.assertIn("prepare_riscv_megrez_physical_graphics", section)
        self.assertIn("test_riscv_physical_graphics_qemu_gate", section)
        self.assertIn("physical/operator-hdmi.png", section)
        self.assertIn("physical/evidence", section)


if __name__ == "__main__":
    unittest.main()
