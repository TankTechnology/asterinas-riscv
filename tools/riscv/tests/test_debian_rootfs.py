from tools.riscv.tests.support.debian_rootfs import *

class DebianRootfsFsOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.source = self.directory / "source"
        (self.source / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS:
            source = self.source / relative_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"new:{relative_path}".encode())

    def make_existing_output(self, name: str = "output") -> Path:
        output = self.directory / name
        (output / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS[1:]:
            destination = output / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"old:{relative_path}".encode())
        return output

    def output_snapshot(self, output: Path) -> dict[str, bytes | None]:
        return {
            relative_path: (
                (output / relative_path).read_bytes()
                if (output / relative_path).exists()
                else None
            )
            for relative_path in PUBLISHED_ARTIFACTS
        }

    def test_cache_admission_reuses_and_rejects_corrupt_entry(self) -> None:
        source = self.directory / "package.deb"
        source.write_bytes(b"package")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = self.directory / "cache"

        fsops_module.admit_cache_entry(cache, source, digest)
        destination = cache / "sha256" / digest[:2] / f"{digest}.deb"
        self.assertEqual(destination.read_bytes(), b"package")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o444)
        fsops_module.admit_cache_entry(cache, source, digest)

        destination.chmod(0o644)
        destination.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            fsops_module.admit_cache_entry(cache, source, digest)

    def test_publish_set_rolls_back_second_replace_failure(self) -> None:
        output = self.make_existing_output()
        original = self.output_snapshot(output)
        real_replace = os.replace
        replace_count = 0

        def fail_second_replace(*args, **kwargs):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 2:
                raise OSError("injected second replace failure")
            return real_replace(*args, **kwargs)

        with (
            mock.patch.object(fsops_module.os, "replace", new=fail_second_replace),
            self.assertRaisesRegex(OSError, "injected second replace failure"),
        ):
            fsops_module.publish_set(output, self.source)

        self.assertEqual(replace_count, 1 + len(PUBLISHED_ARTIFACTS))
        self.assertEqual(self.output_snapshot(output), original)

    def test_publish_set_rolls_back_real_sigterm_on_second_replace(self) -> None:
        output = self.make_existing_output()
        original = self.output_snapshot(output)
        process_id = os.fork()
        if process_id == 0:
            real_replace = os.replace
            replace_count = 0

            def signal_second_replace(*args, **kwargs):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    os.kill(os.getpid(), signal.SIGTERM)
                return real_replace(*args, **kwargs)

            try:
                with mock.patch.object(
                    fsops_module.os,
                    "replace",
                    new=signal_second_replace,
                ):
                    fsops_module.publish_set(output, self.source)
            except BaseException as error:
                interrupted = getattr(fsops_module, "PublishInterrupted", ())
                if isinstance(error, interrupted):
                    os._exit(128 + error.signum)
                os._exit(99)
            os._exit(0)

        _, wait_status = os.waitpid(process_id, 0)
        self.assertTrue(os.WIFEXITED(wait_status))
        self.assertEqual(os.WEXITSTATUS(wait_status), 143)
        self.assertEqual(self.output_snapshot(output), original)

    def test_publish_set_defers_sigterm_until_after_commit_cleanup(self) -> None:
        output = self.make_existing_output()
        previous_handler = signal.getsignal(signal.SIGTERM)
        real_cleanup = fsops_module._cleanup_publication_files

        def signal_during_cleanup(entries):
            os.kill(os.getpid(), signal.SIGTERM)
            real_cleanup(entries)

        with (
            mock.patch.object(
                fsops_module,
                "_cleanup_publication_files",
                new=signal_during_cleanup,
            ),
            self.assertRaises(fsops_module.PublishInterrupted) as caught,
        ):
            fsops_module.publish_set(output, self.source)

        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertEqual(
            self.output_snapshot(output),
            self.output_snapshot(self.source),
        )
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_publish_cli_returns_sigterm_status_after_commit_cleanup(self) -> None:
        output = self.make_existing_output()
        real_cleanup = fsops_module._cleanup_publication_files

        def signal_during_cleanup(entries):
            os.kill(os.getpid(), signal.SIGTERM)
            real_cleanup(entries)

        with mock.patch.object(
            fsops_module,
            "_cleanup_publication_files",
            new=signal_during_cleanup,
        ):
            result = fsops_module.main(
                [
                    "publish-set",
                    "--output-dir",
                    str(output),
                    "--source-root",
                    str(self.source),
                ]
            )

        self.assertEqual(result, 143)
        self.assertEqual(
            self.output_snapshot(output),
            self.output_snapshot(self.source),
        )

    def test_publish_set_rejects_symlinked_or_swapped_output(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        symlink_output = self.directory / "symlink-output"
        symlink_output.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            fsops_module.publish_set(symlink_output, self.source)
        self.assertEqual(list(outside.iterdir()), [])

        output = self.make_existing_output("swap-output")
        moved_output = self.directory / "moved-output"
        original = self.output_snapshot(output)
        real_replace = os.replace
        replace_count = 0

        def swap_before_first_replace(*args, **kwargs):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 1:
                output.rename(moved_output)
                output.symlink_to(outside, target_is_directory=True)
            return real_replace(*args, **kwargs)

        with (
            mock.patch.object(
                fsops_module.os,
                "replace",
                new=swap_before_first_replace,
            ),
            self.assertRaisesRegex(ValueError, "changed during publication"),
        ):
            fsops_module.publish_set(output, self.source)
        self.assertEqual(self.output_snapshot(moved_output), original)
        self.assertEqual(list(outside.iterdir()), [])


class DebianRootfsManifestWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.directory = Path(cls.temporary_directory.name)
        cls.image = cls.directory / "writer-root.ext2"
        with cls.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)

    def setUp(self) -> None:
        self.packages_lock = self.directory / "writer-packages.lock"
        self.inrelease = self.directory / "writer-InRelease"
        self.package_checksums = self.directory / "writer-package-checksums"
        self.output = self.directory / "writer-manifest.json"
        self.reset_inputs()
        self.output.unlink(missing_ok=True)

    def reset_inputs(self) -> None:
        with self.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)
        self.packages_lock.write_text(_lock_text(), encoding="utf-8")
        self.inrelease.write_bytes(b"InRelease")
        self.package_checksums.write_text(
            _package_checksums_text(),
            encoding="utf-8",
        )

    def writer_arguments(self) -> list[str]:
        return [
            "write-manifest",
            "--output",
            str(self.output),
            "--image",
            str(self.image),
            "--packages-lock",
            str(self.packages_lock),
            "--inrelease",
            str(self.inrelease),
            "--package-checksums",
            str(self.package_checksums),
            "--mirror",
            "https://deb.debian.org/debian",
            "--suite",
            "trixie",
            "--debian-release",
            "13.6",
            "--build-timestamp",
            "2026-08-24T00:00:00Z",
            "--tool-version",
            "debootstrap=1.0.141",
            "--tool-version",
            "mke2fs=1.47.2",
            "--tool-version",
            "qemu-riscv64-static=10.0.2",
        ]

    def run_writer(
        self,
        arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                CONTRACT_MODULE,
                *(self.writer_arguments() if arguments is None else arguments),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_verifier(
        self,
        arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        exact_arguments = [
            "verify",
            "--image",
            str(self.image),
            "--manifest",
            str(self.output),
            "--packages-lock",
            str(self.packages_lock),
        ]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                CONTRACT_MODULE,
                *(exact_arguments if arguments is None else arguments),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def write_verifier_fixture(self) -> None:
        self.output.write_text(
            json.dumps(_manifest_payload(_sha256_text(_lock_text()))),
            encoding="utf-8",
        )

    def test_writes_canonical_exact_manifest_consumable_by_task1_contract(
        self,
    ) -> None:
        result = self.run_writer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        payload = _manifest_payload(_sha256_text(_lock_text()))
        payload["signed_metadata"]["sha256"] = hashlib.sha256(b"InRelease").hexdigest()
        payload["root_image_sha256"] = ZERO_FILLED_ROOT_SHA256
        expected = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.assertEqual(self.output.read_text(encoding="utf-8"), expected)
        self.assertEqual(set(json.loads(expected)), set(_manifest_payload("0" * 64)))

        manifest = load_manifest(self.output)
        validated = validate_frozen_root(self.image, manifest, self.packages_lock)
        self.assertEqual(validated.debian_release, "13.6")
        self.assertEqual(validated.downloaded_packages[0][0], "base-files")

    def test_verify_cli_accepts_exact_plan_command_quietly(self) -> None:
        self.write_verifier_fixture()

        result = self.run_verifier()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_verify_cli_rejects_tampered_artifacts(self) -> None:
        cases = ("image", "manifest", "packages-lock")
        for case in cases:
            with self.subTest(case=case):
                self.reset_inputs()
                self.write_verifier_fixture()
                if case == "image":
                    with self.image.open("r+b") as image_file:
                        image_file.write(b"X")
                elif case == "manifest":
                    payload = _manifest_payload(_sha256_text(_lock_text()))
                    payload["suite"] = "bookworm"
                    self.output.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    self.packages_lock.write_text(
                        _lock_text().replace(
                            "bash\triscv64\t5.2.37-2+b5",
                            "bash\triscv64\t5.2.37-2+b6",
                        ),
                        encoding="utf-8",
                    )

                result = self.run_verifier()

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("contract: error:", result.stderr)

    def test_verify_cli_rejects_missing_and_unknown_arguments(self) -> None:
        self.write_verifier_fixture()
        exact = [
            "verify",
            "--image",
            str(self.image),
            "--manifest",
            str(self.output),
            "--packages-lock",
            str(self.packages_lock),
        ]
        cases = (
            exact[:-2],
            [*exact, "--unknown", "value"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_verifier(arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)

    def test_rejects_missing_duplicate_unknown_and_invalid_cli_inputs(self) -> None:
        valid = self.writer_arguments()
        cases = (
            ("missing", [valid[0], *valid[3:]]),
            ("duplicate", [*valid, "--suite", "trixie"]),
            ("unknown", [*valid, "--unknown", "value"]),
            (
                "invalid",
                [
                    (
                        "http://deb.debian.org/debian"
                        if value == "https://deb.debian.org/debian"
                        else value
                    )
                    for value in valid
                ],
            ),
        )

        for name, arguments in cases:
            with self.subTest(name=name):
                result = self.run_writer(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_rejects_invalid_package_checksum_rows(self) -> None:
        valid_row = _package_checksums_text().splitlines()[0]
        cases = (
            f"{valid_row}\n{valid_row}\n",
            "bash\triscv64\t5.2.37-2+b5\tnot-a-hash\n",
            "bash\triscv64\t5.2.37-2+b5\n",
        )

        for contents in cases:
            with self.subTest(contents=contents):
                self.package_checksums.write_text(contents, encoding="utf-8")
                result = self.run_writer()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_refuses_symlink_output_without_changing_target(self) -> None:
        self.output.symlink_to(self.packages_lock)

        result = self.run_writer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(self.packages_lock.read_text(encoding="utf-8"), _lock_text())

    def test_writes_schema9_ext4_journal_profile_manifest(self) -> None:
        lock_text = _lock_text(SYSTEMD_M2_PACKAGE_ROWS)
        self.packages_lock.write_text(lock_text, encoding="utf-8")
        self.package_checksums.write_text(
            _package_checksums_text(SYSTEMD_M2_PACKAGE_ROWS),
            encoding="utf-8",
        )
        arguments = self.writer_arguments()
        arguments.extend(("--profile", "systemd-ext4"))

        result = self.run_writer(arguments)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(self.output.read_text())
        self.assertEqual(payload["schema_version"], 9)
        self.assertEqual(payload["profile"], "systemd-ext4")
        self.assertEqual(payload["filesystem"]["type"], "ext4")
        manifest = load_manifest(self.output)
        validated = validate_frozen_root(self.image, manifest, self.packages_lock)
        self.assertEqual(validated.profile, "systemd-ext4")

    def test_refuses_output_equal_to_any_input(self) -> None:
        input_paths = (
            self.image,
            self.packages_lock,
            self.inrelease,
            self.package_checksums,
        )

        for input_path in input_paths:
            with self.subTest(input_path=input_path):
                self.reset_inputs()
                arguments = self.writer_arguments()
                arguments[arguments.index("--output") + 1] = str(input_path)
                result = self.run_writer(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("alias", result.stderr)

    def test_refuses_hardlink_output_alias(self) -> None:
        self.output.hardlink_to(self.packages_lock)

        result = self.run_writer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias", result.stderr)
        self.assertEqual(self.packages_lock.read_text(encoding="utf-8"), _lock_text())



class DebianRootfsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.directory = Path(cls.temporary_directory.name)
        cls.image = cls.directory / "debian-root.ext2"
        with cls.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)

    def setUp(self) -> None:
        self.packages_lock = self.directory / "packages.lock"
        self.manifest_path = self.directory / "rootfs-manifest.json"
        self.packages_lock.write_text(_lock_text(), encoding="utf-8")
        self.payload = _manifest_payload(_sha256_text(_lock_text()))

    def write_manifest(self, payload: dict[str, object] | None = None) -> None:
        self.manifest_path.write_text(
            json.dumps(self.payload if payload is None else payload),
            encoding="utf-8",
        )

    def load_and_validate(self):
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        rows = parse_packages_lock(self.packages_lock)
        return validate_frozen_root(self.image, manifest, self.packages_lock), rows

    def test_accepts_frozen_manifest_and_complete_package_lock(self) -> None:
        validated, rows = self.load_and_validate()

        self.assertEqual(
            INSTALL_PACKAGES,
            (
                "bash",
                "ca-certificates",
                "coreutils",
                "procps",
                "util-linux",
            ),
        )
        self.assertEqual(
            GATE_IDENTITY_PACKAGES,
            (
                "base-files",
                "libc6",
                "bash",
                "coreutils",
                "util-linux",
            ),
        )
        self.assertEqual(ROOT_LABEL, "ASTER_DEBIANROOT")
        self.assertEqual(rows, PACKAGE_ROWS)
        self.assertEqual(validated.debian_release, "13.6")
        self.assertEqual(validated.filesystem.size_bytes, ROOT_IMAGE_SIZE_BYTES)
        with self.assertRaises(FrozenInstanceError):
            validated.suite = "forky"
        with self.assertRaises(FrozenInstanceError):
            validated.filesystem.label = "mutable"

    def test_root_label_fits_ext2_limit(self) -> None:
        encoded_label = ROOT_LABEL.encode("ascii")

        self.assertLessEqual(len(encoded_label), 16)

    def test_accepts_signed_debian_13_point_release_versions(self) -> None:
        for release in ("13.0", "13.6", "13.10"):
            with self.subTest(release=release):
                self.payload["debian_release"] = release
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_and_unknown_json_keys(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []

        missing_top_level = copy.deepcopy(self.payload)
        del missing_top_level["architecture"]
        cases.append(("missing manifest fields", missing_top_level))

        unknown_top_level = copy.deepcopy(self.payload)
        unknown_top_level["extra"] = "not allowed"
        cases.append(("unknown manifest fields", unknown_top_level))

        missing_filesystem = copy.deepcopy(self.payload)
        del missing_filesystem["filesystem"]["uuid"]
        cases.append(("missing filesystem fields", missing_filesystem))

        unknown_signed_metadata = copy.deepcopy(self.payload)
        unknown_signed_metadata["signed_metadata"]["signature"] = "detached"
        cases.append(("unknown signed_metadata fields", unknown_signed_metadata))

        for expected_error, payload in cases:
            with self.subTest(expected_error=expected_error):
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, expected_error):
                    load_manifest(self.manifest_path)

    def test_rejects_duplicate_json_keys_at_every_depth(self) -> None:
        serialized = json.dumps(self.payload)
        documents = (
            serialized.replace(
                '"suite": "trixie"',
                '"suite": "trixie", "suite": "bookworm"',
                1,
            ),
            serialized.replace(
                f'"label": "{ROOT_LABEL}"',
                f'"label": "{ROOT_LABEL}", "label": "shadow"',
                1,
            ),
        )

        for document in documents:
            with self.subTest(document=document):
                self.manifest_path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
                    load_manifest(self.manifest_path)

    def test_wraps_malformed_json_as_contract_error(self) -> None:
        self.manifest_path.write_text('{"schema_version":', encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "invalid manifest JSON"):
            load_manifest(self.manifest_path)

    def test_wraps_invalid_manifest_utf8_as_contract_error(self) -> None:
        self.manifest_path.write_bytes(b'{"suite":"trixie","bad":"\xff"}')

        with self.assertRaisesRegex(ContractError, "manifest must be UTF-8"):
            load_manifest(self.manifest_path)

    def test_rejects_booleans_where_integers_are_required(self) -> None:
        cases = (
            (("schema_version",), True),
            (("filesystem", "size_bytes"), True),
            (("filesystem", "block_size_bytes"), False),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    load_manifest(self.manifest_path)

        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            validate_frozen_root(
                self.image,
                replace(manifest, schema_version=True),
                self.packages_lock,
            )

    def test_rejects_non_https_provenance_urls(self) -> None:
        cases = (
            (("mirror_url",), "http://deb.debian.org/debian"),
            (
                ("signed_metadata", "url"),
                "file:///var/cache/debian/dists/trixie/InRelease",
            ),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_wraps_malformed_provenance_urls_as_contract_error(self) -> None:
        cases = (
            (("mirror_url",), "mirror_url"),
            (("signed_metadata", "url"), "signed_metadata.url"),
        )

        for path, expected_field in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "https://[invalid-authority"
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(
                    ContractError,
                    rf"{expected_field}.*HTTPS URL",
                ):
                    validate_frozen_root(
                        self.image,
                        manifest,
                        self.packages_lock,
                    )

    def test_filesystem_errors_are_not_wrapped_as_contract_errors(self) -> None:
        missing_manifest = self.directory / "missing-manifest.json"
        with self.assertRaises(FileNotFoundError):
            load_manifest(missing_manifest)

        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        missing_image = self.directory / "missing-root.ext2"
        with self.assertRaises(FileNotFoundError):
            validate_frozen_root(
                missing_image,
                manifest,
                self.packages_lock,
            )

    def test_rejects_wrong_debian_and_filesystem_identity(self) -> None:
        cases = (
            (("suite",), "bookworm"),
            (("architecture",), "amd64"),
            (("filesystem", "type"), "ext4"),
            (("filesystem", "label"), "DEBIAN_ROOT"),
            (("filesystem", "size_bytes"), ROOT_IMAGE_SIZE_BYTES // 2),
            (("filesystem", "block_size_bytes"), 1024),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_noncanonical_debian_release_versions(self) -> None:
        for release in (
            "",
            "12",
            "13",
            "13.",
            "13..6",
            "13.06",
            "13.6.1",
            "13.6a",
            " 13.6",
            "13.6 ",
        ):
            with self.subTest(release=release):
                self.payload["debian_release"] = release
                self.write_manifest()
                with self.assertRaises(ValueError):
                    manifest = load_manifest(self.manifest_path)
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_malformed_sha256_values(self) -> None:
        paths = (
            ("signed_metadata", "sha256"),
            ("packages_lock_sha256",),
            ("downloaded_packages", 0, "sha256"),
            ("root_image_sha256",),
        )

        for path in paths:
            with self.subTest(path=".".join(str(part) for part in path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "A" * 64
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    load_manifest(self.manifest_path)

    def test_rejects_duplicate_and_unsorted_package_entries(self) -> None:
        cases = (
            PACKAGE_ROWS + (PACKAGE_ROWS[-1],),
            tuple(reversed(PACKAGE_ROWS)),
        )

        for rows in cases:
            with self.subTest(rows=rows):
                lock_text = _lock_text(rows)
                self.packages_lock.write_text(lock_text, encoding="utf-8")
                self.payload["packages_lock_sha256"] = _sha256_text(lock_text)
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "sorted and unique"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_two_versions_for_one_package_architecture(self) -> None:
        rows = list(PACKAGE_ROWS)
        procps_index = rows.index(("procps", "riscv64", "2:4.0.4-9"))
        rows.insert(procps_index, ("procps", "riscv64", "2:4.0.4-8"))
        lock_text = _lock_text(tuple(rows))
        self.packages_lock.write_text(lock_text, encoding="utf-8")
        self.payload["packages_lock_sha256"] = _sha256_text(lock_text)
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "package identities must be unique"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_unsorted_and_duplicate_downloaded_packages(self) -> None:
        downloaded_packages = self.payload["downloaded_packages"]
        cases = (
            (
                "sorted",
                list(reversed(downloaded_packages)),
            ),
            (
                "unique",
                downloaded_packages[:1] + downloaded_packages,
            ),
        )

        for expected_error, identities in cases:
            with self.subTest(expected_error=expected_error):
                self.payload["downloaded_packages"] = identities
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, expected_error):
                    validate_frozen_root(
                        self.image,
                        manifest,
                        self.packages_lock,
                    )

    def test_rejects_downloaded_package_absent_from_lock(self) -> None:
        self.payload["downloaded_packages"][0]["version"] = "0.not-locked"
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "does not match packages.lock"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_explicit_install_download(self) -> None:
        self.payload["downloaded_packages"] = [
            identity
            for identity in self.payload["downloaded_packages"]
            if identity["name"] != "procps"
        ]
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "missing explicit install packages"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_non_explicit_locked_package_download(self) -> None:
        self.payload["downloaded_packages"] = [
            {
                "name": name,
                "architecture": architecture,
                "version": version,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name, architecture, version in PACKAGE_ROWS
            if name != "base-files"
        ]
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "packages.lock set"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_manifest_package_lock_version_mismatch(self) -> None:
        self.payload["gate_packages"]["bash"] = "0.invalid"
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "gate package bash version"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_base_image_size_and_hash_mismatch(self) -> None:
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        short_image = self.directory / "short.ext2"
        short_image.write_bytes(b"not one GiB")

        with self.assertRaisesRegex(ValueError, "image size"):
            validate_frozen_root(short_image, manifest, self.packages_lock)

        payload = copy.deepcopy(self.payload)
        payload["root_image_sha256"] = "0" * 64
        self.write_manifest(payload)
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "image SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_package_lock_hash_mismatch(self) -> None:
        self.payload["packages_lock_sha256"] = "0" * 64
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "package-lock SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_package_lock_validation_uses_one_open_file(self) -> None:
        original_lock = self.directory / "swap-packages.lock"
        replacement_lock = self.directory / "replacement-packages.lock"
        original_lock.write_text(_lock_text(), encoding="utf-8")
        replacement_text = "substituted\triscv64\t0.invalid\n"
        replacement_lock.write_text(replacement_text, encoding="utf-8")
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        real_open = Path.open
        callback_count = 0

        def replace_after_open(path: Path, *args, **kwargs):
            nonlocal callback_count
            opened_file = real_open(path, *args, **kwargs)
            if path == original_lock:
                callback_count += 1
                if callback_count == 1:
                    replacement_lock.replace(path)
            return opened_file

        with mock.patch.object(Path, "open", new=replace_after_open):
            validated = validate_frozen_root(self.image, manifest, original_lock)

        self.assertEqual(callback_count, 1)
        self.assertEqual(validated.packages_lock_sha256, _sha256_text(_lock_text()))
        self.assertEqual(original_lock.read_text(encoding="utf-8"), replacement_text)

    def test_image_validation_uses_one_open_file(self) -> None:
        image = self.directory / "swap-root.ext2"
        replacement_image = self.directory / "replacement-root.ext2"
        with image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)
        replacement_bytes = b"short replacement image"
        replacement_image.write_bytes(replacement_bytes)
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        real_open = Path.open
        callback_count = 0

        def replace_after_open(path: Path, *args, **kwargs):
            nonlocal callback_count
            opened_file = real_open(path, *args, **kwargs)
            if path == image:
                callback_count += 1
                if callback_count == 1:
                    replacement_image.replace(path)
            return opened_file

        with mock.patch.object(Path, "open", new=replace_after_open):
            validate_frozen_root(image, manifest, self.packages_lock)

        self.assertEqual(callback_count, 1)
        self.assertEqual(image.read_bytes(), replacement_bytes)


class DebianRootfsGateProtocolTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4
    EXPECTED_PACKAGES = tuple(
        (name, version)
        for name, architecture, version in PACKAGE_ROWS
        if name in GATE_IDENTITY_PACKAGES and architecture == "riscv64"
    )
    MANIFEST_PACKAGES = tuple(
        next(
            (package_name, version)
            for package_name, architecture, version in PACKAGE_ROWS
            if package_name == name and architecture == "riscv64"
        )
        for name in GATE_IDENTITY_PACKAGES
    )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.uboot = self._regular_file("u-boot", b"u-boot")
        self.boot_disk = self._regular_file("boot.ext4", b"ext4")
        self.root_disk = self._regular_file("debian-root.ext2", b"ext2")
        self.monitor_socket = self.directory / "monitor.sock"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _regular_file(self, name: str, contents: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(contents)
        return path

    def _qemu_argv(self, **overrides: object) -> tuple[str, ...]:
        arguments: dict[str, object] = {
            "uboot": self.uboot,
            "boot_disk": self.boot_disk,
            "root_disk": self.root_disk,
            "monitor_socket": self.monitor_socket,
            "smp": 4,
            "dtb_enabled_cpu_count": 4,
        }
        arguments.update(overrides)
        return qemu_argv(**arguments)

    @staticmethod
    def _command_output(command: ShellCommand, *, boot_number: int) -> str:
        outputs = {
            "architecture": "riscv64",
            "debian-release": "13.6",
            "bash-version": "5.2.37(1)-release",
            "packages": "\r\r\n".join(
                f"{name}\t{version}"
                for name, version in DebianRootfsGateProtocolTests.EXPECTED_PACKAGES
            ),
            "root-filesystem": "ext2/ext3",
            "persistence": DebianRootfsGateProtocolTests.NONCE,
            "second-probe": "boot2-probe-created",
        }
        if command.name == "second-probe" and boot_number != 2:
            raise AssertionError("boot one must not create the second probe")
        return outputs[command.name]

    def _successful_transcript(
        self,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
        echoed: bool = False,
    ) -> str:
        lines = ["Asterinas boot noise"]
        for command in commands:
            if echoed:
                lines.append(f"root@asterinas:/# {command.payload}")
            lines.extend(
                (
                    command.begin_marker,
                    self._command_output(command, boot_number=boot_number),
                    f"{command.status_prefix}0",
                    command.end_marker,
                )
            )
        return "\r\n".join(lines) + "\r\n"

    def _classify(
        self,
        transcript: str | bytes,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
    ) -> GateResult:
        return classify_boot(
            transcript,
            commands,
            boot_number=boot_number,
            expected_debian_release="13.6",
            expected_packages=self.MANIFEST_PACKAGES,
            expected_nonce=self.NONCE,
        )

    def test_qemu_argv_is_the_frozen_headless_two_disk_contract(self) -> None:
        argv = self._qemu_argv()

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(
            argv[argv.index("-cpu") + 1],
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        )
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertIn("-no-reboot", argv)

        drives = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-drive"
        ]
        devices = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-device"
        ]
        self.assertEqual(
            drives,
            [
                f"if=none,format=raw,file={self.boot_disk},id=bootdisk,readonly=on",
                (
                    f"if=none,format=raw,file={self.root_disk},id=rootdisk,"
                    "cache=directsync"
                ),
            ],
        )
        self.assertEqual(
            devices,
            [
                "virtio-blk-device,drive=bootdisk",
                "virtio-blk-device,drive=rootdisk",
            ],
        )
        forbidden = ("xhci", "usb", "keyboard", "tablet", "mouse", "netdev")
        self.assertFalse(
            any(token in argument.lower() for argument in argv for token in forbidden)
        )

    def test_qemu_argv_rejects_non_four_hart_contracts(self) -> None:
        for override in ({"smp": 1}, {"smp": True}, {"dtb_enabled_cpu_count": 3}):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ValueError, "exactly 4"),
            ):
                self._qemu_argv(**override)

    def test_qemu_argv_rejects_unsafe_or_non_regular_inputs(self) -> None:
        comma_path = self._regular_file("boot,unsafe.ext4", b"ext4")
        missing_path = self.directory / "missing.ext2"
        symlink_path = self.directory / "root-link.ext2"
        symlink_path.symlink_to(self.root_disk)
        cases = (
            ("boot_disk", comma_path, "comma"),
            ("root_disk", missing_path, "regular file"),
            ("root_disk", symlink_path, "symbolic link"),
            ("uboot", self.directory, "regular file"),
        )

        for argument, path, message in cases:
            with (
                self.subTest(argument=argument, path=path),
                self.assertRaisesRegex(ValueError, message),
            ):
                self._qemu_argv(**{argument: path})

    def test_protocol_types_are_frozen(self) -> None:
        command = ShellCommand("name", "true", "begin", "end", "status=")
        evidence = BootEvidence(
            boot_number=1,
            architecture="riscv64",
            debian_release="13.6",
            bash_version="5.2",
            packages=self.EXPECTED_PACKAGES,
            root_filesystem="ext2/ext3",
            persistence_nonce=self.NONCE,
            second_probe=None,
        )
        result = GateResult(True, "pass", evidence)

        for instance in (command, evidence, result):
            with (
                self.subTest(instance=instance),
                self.assertRaises(FrozenInstanceError),
            ):
                instance.__setattr__(next(iter(instance.__dataclass_fields__)), None)

    def test_shell_commands_cover_boot_one_identity_write_and_sync(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)

        self.assertEqual(
            tuple(command.name for command in commands),
            (
                "architecture",
                "debian-release",
                "bash-version",
                "packages",
                "root-filesystem",
                "persistence",
            ),
        )
        payload = "\n".join(command.payload for command in commands)
        for required in (
            "uname -m",
            "/etc/debian_version",
            "BASH_VERSION",
            "base-files libc6 bash coreutils util-linux",
            "stat -f",
            "/var/lib/asterinas-debian-m1",
            self.NONCE,
            "sync",
        ):
            self.assertIn(required, payload)
        self.assertIn("> /var/lib/asterinas-debian-m1/persist", payload)

    def test_shell_commands_cover_boot_two_read_probe_and_sync(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        payloads = {command.name: command.payload for command in commands}

        self.assertIn(
            "cat /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertNotIn(
            "> /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertIn("second-probe", payloads["second-probe"])
        self.assertIn("sync", payloads["second-probe"])
        self.assertEqual(
            len({command.begin_marker for command in commands}), len(commands)
        )
        self.assertTrue(
            all(
                len(command.payload.encode()) <= MAX_COMMAND_PAYLOAD_BYTES
                for command in commands
            )
        )

    def test_shell_commands_reject_bad_boot_or_nonce(self) -> None:
        for arguments in (
            {"boot_number": 3, "nonce": self.NONCE},
            {"boot_number": 1, "nonce": "not-shell-safe"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                shell_commands(**arguments)

    def test_classify_boot_accepts_echoed_commands_and_exact_identity(self) -> None:
        for boot_number in (1, 2):
            with self.subTest(boot_number=boot_number):
                commands = shell_commands(boot_number=boot_number, nonce=self.NONCE)
                transcript = self._successful_transcript(
                    commands, boot_number=boot_number, echoed=True
                )

                result = self._classify(transcript, commands, boot_number=boot_number)

                self.assertTrue(result.passed)
                self.assertEqual(result.reason, "pass")
                self.assertEqual(result.evidence.architecture, "riscv64")
                self.assertEqual(result.evidence.packages, self.EXPECTED_PACKAGES)
                self.assertEqual(result.evidence.persistence_nonce, self.NONCE)
                self.assertEqual(
                    result.evidence.second_probe,
                    "boot2-probe-created" if boot_number == 2 else None,
                )

    def test_classify_boot_rejects_stale_duplicate_and_reordered_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        blocks = good.splitlines()[1:]
        first_block = blocks[:4]
        second_block = blocks[4:8]
        cases = (
            (commands[0].begin_marker + "\n" + good, "duplicate"),
            ("\n".join(second_block + first_block + blocks[8:]), "reordered"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_nonzero_and_wrong_identity(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        cases = (
            (
                good.replace(
                    f"{commands[0].status_prefix}0", f"{commands[0].status_prefix}7"
                ),
                "status 7",
            ),
            (good.replace("13.6", "12.0", 1), "Debian release"),
            (good.replace("5.2.37-2+b5", "0.invalid", 1), "package versions"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_scans_complete_transcript_for_fatal_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        fatal_markers = (
            "Kernel panic - not syncing",
            "reboot: Restarting system",
            "EXT2-fs error (device vdb)",
            "Buffer I/O error on dev vdb",
            "blk_update_request: I/O error",
            "DEBIAN_ROOTFS_FAIL reason=root_mount",
        )

        for marker in fatal_markers:
            with self.subTest(marker=marker):
                result = self._classify(good + marker + "\n", commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn("fatal transcript marker", result.reason)

    def test_classify_boot_rejects_oversized_transcript_and_payload(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        oversized_command = replace(
            commands[0], payload="x" * (MAX_COMMAND_PAYLOAD_BYTES + 1)
        )
        cases = (
            (b"x" * (MAX_TRANSCRIPT_BYTES + 1), commands, "transcript exceeds"),
            (b"", (oversized_command, *commands[1:]), "payload exceeds"),
        )

        for transcript, candidate_commands, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, candidate_commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_missing_or_mismatched_persistence(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=2)
        cases = (
            (good.replace(self.NONCE, "", 1), "persistence nonce"),
            (good.replace(self.NONCE, "f" * 64, 1), "persistence nonce"),
            (good.replace("boot2-probe-created", "", 1), "second probe"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=2)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)


class DebianRootfsGateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_serial_console_wait_can_start_after_a_transcript_checkpoint(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(reader, max_bytes=1024)
        os.write(writer, b"repeat-marker\n")
        console.wait_for(b"repeat-marker", time.monotonic() + 1.0)
        checkpoint = console.checkpoint()

        delayed = threading.Thread(
            target=lambda: (time.sleep(0.02), os.write(writer, b"repeat-marker\n"))
        )
        delayed.start()
        self.addCleanup(delayed.join)
        transcript = console.wait_for(
            b"repeat-marker", time.monotonic() + 1.0, start=checkpoint
        )

        self.assertEqual(transcript.count(b"repeat-marker"), 2)

    def test_serial_console_observes_admitted_chunks(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        observed: list[bytes] = []
        console = SerialConsole(reader, max_bytes=8, observer=observed.append)
        os.write(writer, b"ready\n")

        console.wait_for(b"ready", time.monotonic() + 1.0)

        self.assertEqual(observed, [b"ready\n"])
        os.write(writer, b"overflow")
        with self.assertRaisesRegex(BufferError, "serial transcript"):
            console.wait_for(b"missing", time.monotonic() + 1.0)
        self.assertEqual(observed, [b"ready\n"])

    def test_serial_console_wait_for_any_returns_the_first_observed_marker(
        self,
    ) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(reader, max_bytes=1024)
        os.write(writer, b"boot noise\nDEBIAN_DESKTOP_M3_FAIL reason=xorg\n")

        marker = console.wait_for_any(
            (b"DEBIAN_DESKTOP_M3_READY", b"DEBIAN_DESKTOP_M3_FAIL"),
            time.monotonic() + 1.0,
        )

        self.assertEqual(marker, b"DEBIAN_DESKTOP_M3_FAIL")

    @staticmethod
    def _deadline(seconds: float = 1.0) -> float:
        return time.monotonic() + seconds

    def _hmp_server(
        self,
        chunks: tuple[bytes, ...],
        *,
        receive_command: bool = False,
        keep_open: float = 0.0,
    ) -> tuple[Path, threading.Thread, list[bytes]]:
        path = self.directory / f"hmp-{time.monotonic_ns()}.sock"
        ready = threading.Event()
        received: list[bytes] = []

        def serve() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    if receive_command:
                        connection.sendall(b"QEMU 10.0\r\n(qemu) ")
                        received.append(connection.recv(1024))
                    for chunk in chunks:
                        connection.sendall(chunk)
                        time.sleep(0.005)
                    if keep_open:
                        time.sleep(keep_open)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1.0))
        return path, thread, received

    def test_serial_console_matches_split_markers_with_one_total_deadline(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        console = SerialConsole(master, max_bytes=128)

        def write_split_marker() -> None:
            for chunk in (b"boot noi", b"se\r\nROOT_", b"READY\r\n"):
                os.write(slave, chunk)
                time.sleep(0.01)

        writer = threading.Thread(target=write_split_marker)
        writer.start()
        matched = console.wait_for(b"ROOT_READY", self._deadline())
        writer.join()
        console.send(b"echo ready\n", self._deadline())

        self.assertIn(b"ROOT_READY", matched)
        self.assertIn(b"boot noise", console.transcript)
        self.assertEqual(os.read(slave, 128), b"echo ready\n")

    def test_serial_console_can_pace_physical_uart_transmission(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(writer, max_bytes=128, tx_delay=0.005)

        with mock.patch("tools.riscv.debian.rootfs.gate_runtime.time.sleep") as sleep:
            console.send(b"abc", self._deadline(3.0))

        self.assertEqual(os.read(reader, 128), b"abc")
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_has_calls([mock.call(0.005), mock.call(0.005)])

    def test_paced_transmission_drains_concurrent_serial_output(self) -> None:
        local, remote = socket.socketpair()
        descriptor = local.detach()
        self.addCleanup(os.close, descriptor)
        self.addCleanup(remote.close)
        console = SerialConsole(descriptor, max_bytes=128, tx_delay=0.005)
        payload = b"abcdefgh"

        def echo_during_send() -> None:
            remote.recv(1)
            remote.sendall(b"readline-redraw\n")
            remaining = len(payload) - 1
            while remaining:
                remaining -= len(remote.recv(remaining))

        echo = threading.Thread(target=echo_during_send, daemon=True)
        echo.start()
        self.addCleanup(echo.join, 1.0)

        console.send(payload, self._deadline(3.0))
        echo.join(1.0)

        self.assertFalse(echo.is_alive())
        self.assertIn(b"readline-redraw\n", console.transcript)

    def test_serial_console_rejects_paced_command_before_partial_transmission(
        self,
    ) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(writer, max_bytes=128, tx_delay=0.005)

        with (
            mock.patch(
                "tools.riscv.debian.rootfs.gate_runtime.time.monotonic",
                return_value=10.0,
            ),
            self.assertRaisesRegex(TimeoutError, "before transmission"),
        ):
            console.send(b"abcdef", 10.01)

        readable, _, _ = select.select([reader], [], [], 0)
        self.assertEqual(readable, [])

    def test_serial_console_bounds_prompt_boot_and_drain_deadlines(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        console = SerialConsole(master, max_bytes=128)

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "serial marker"):
            console.wait_for(b"never", started + 0.04)
        self.assertLess(time.monotonic() - started, 0.25)

        started = time.monotonic()
        drained = console.drain(started + 0.04)
        self.assertEqual(drained, b"")
        self.assertLess(time.monotonic() - started, 0.25)
        os.close(slave)

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        console = SerialConsole(write_fd, max_bytes=128)
        with self.assertRaisesRegex(TimeoutError, "serial command"):
            console.send(b"x" * (1024 * 1024), self._deadline(0.04))
        os.close(write_fd)

    def test_serial_console_caps_transcript_and_reports_early_process_exit(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        process = launch_process(("/bin/sh", "-c", "exit 23"))
        console = SerialConsole(read_fd, process=process, max_bytes=8)
        os.write(write_fd, b"123456789")
        os.close(write_fd)
        with self.assertRaisesRegex(BufferError, "serial transcript"):
            console.wait_for(b"missing", self._deadline())
        process.wait(self._deadline())

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.close(write_fd)
        console = SerialConsole(read_fd, process=process, max_bytes=128)
        with self.assertRaises(EarlyProcessExit) as caught:
            console.wait_for(b"missing", self._deadline())
        self.assertEqual(caught.exception.returncode, 23)

    def test_hmp_connect_and_prompt_deadlines_are_bounded(self) -> None:
        missing = self.directory / "missing.sock"
        started = time.monotonic()
        with self.assertRaises(MonitorError):
            HmpMonitor.connect(missing, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)

        path, thread, _ = self._hmp_server((b"not-a-prompt",), keep_open=0.15)
        started = time.monotonic()
        with self.assertRaisesRegex(MonitorError, "prompt"):
            HmpMonitor.connect(path, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)
        thread.join(1.0)

    def test_hmp_matches_split_prompt_and_caps_each_response(self) -> None:
        path, thread, received = self._hmp_server(
            (b"info sta", b"tus\r\nrunning\r\n(qe", b"mu) "),
            receive_command=True,
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=128)
        self.addCleanup(monitor.close)
        response = monitor.command("info status", self._deadline())
        thread.join(1.0)
        self.assertIn(b"running", response)
        self.assertEqual(received, [b"info status\n"])

        path, thread, _ = self._hmp_server(
            (b"x" * 65,), receive_command=True, keep_open=0.05
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=64)
        with self.assertRaisesRegex(MonitorError, "exceeds"):
            monitor.command("query", self._deadline())
        monitor.close()
        thread.join(1.0)

    def test_pinned_output_rejects_symlink_and_survives_parent_swap(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        symlink = self.directory / "output-link"
        symlink.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            PinnedOutputDirectory(symlink)

        output = self.directory / "output"
        output.mkdir()
        moved = self.directory / "moved-output"
        with PinnedOutputDirectory(output) as pinned:
            output.rename(moved)
            output.symlink_to(outside, target_is_directory=True)
            pinned.atomic_write("result.json", b"safe")
        self.assertEqual((moved / "result.json").read_bytes(), b"safe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_pinned_output_invalidates_stale_result_before_validation(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        stale = output / "result.json"
        stale.write_bytes(b"stale pass")

        with PinnedOutputDirectory(output) as pinned:
            pinned.invalidate("result.json")
            with self.assertRaisesRegex(ValueError, "bad input"):
                raise ValueError("bad input")

        self.assertFalse(stale.exists())

    def test_pinned_output_atomic_write_copy_hash_and_directory_fsync(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        source = self.directory / "source"
        source.write_bytes(b"copy contents")
        fsync_modes: list[int] = []
        real_fsync = gate_runtime_module.os.fsync

        def record_fsync(fd: int) -> None:
            fsync_modes.append(os.fstat(fd).st_mode)
            real_fsync(fd)

        with (
            mock.patch.object(gate_runtime_module.os, "fsync", new=record_fsync),
            PinnedOutputDirectory(output) as pinned,
        ):
            pinned.atomic_write("result.json", b"new result")
            pinned.atomic_copy("run-root.ext2", source)
            self.assertEqual(
                pinned.sha256("run-root.ext2"),
                hashlib.sha256(b"copy contents").hexdigest(),
            )

        self.assertEqual((output / "result.json").read_bytes(), b"new result")
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in fsync_modes))
        self.assertTrue(any(stat.S_ISREG(mode) for mode in fsync_modes))
        self.assertFalse(
            any(path.name.startswith(".gate-") for path in output.iterdir())
        )

    def test_launch_starts_new_session_with_unblocked_termination_masks(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        os.set_inheritable(slave, True)
        passed_path = self.directory / "passed-fd"
        passed_path.write_bytes(b"fd contents")
        passed_fd = os.open(passed_path, os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(os.close, passed_fd)
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGTERM}
        )
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, previous_mask)
        script = (
            "import os,signal,sys; "
            "m=signal.pthread_sigmask(signal.SIG_BLOCK, []); "
            "stdio_inherited=1; passed_inherited=1; "
            "\ntry: os.fstat(int(sys.argv[1]))\n"
            "except OSError: stdio_inherited=0\n"
            "try: os.fstat(int(sys.argv[2]))\n"
            "except OSError: passed_inherited=0\n"
            "print(os.getpid(),os.getsid(0),int(signal.SIGHUP in m),"
            "int(signal.SIGTERM in m),stdio_inherited,passed_inherited,flush=True)"
        )
        process = launch_process(
            (sys.executable, "-c", script, str(slave), str(passed_fd)),
            stdio_fd=slave,
            pass_fds=(passed_fd,),
        )
        os.close(slave)
        output = SerialConsole(master, process=process, max_bytes=256).drain(
            self._deadline()
        )
        self.assertEqual(process.wait(self._deadline()), 0)
        pid, sid, hup_blocked, term_blocked, stdio_inherited, passed_inherited = (
            output.decode().split()
        )
        self.assertEqual(pid, sid)
        self.assertEqual((hup_blocked, term_blocked), ("0", "0"))
        self.assertEqual((stdio_inherited, passed_inherited), ("0", "1"))

    def test_cleanup_kills_group_after_leader_exit(self) -> None:
        child_pid_file = self.directory / "child.pid"
        script = (
            "import ctypes,os,time\n"
            "p=os.fork()\n"
            "if p:\n"
            f" open({str(child_pid_file)!r},'w').write(str(p))\n"
            " os._exit(0)\n"
            "ctypes.CDLL(None).prctl(15,b'gate child name',0,0,0)\n"
            "time.sleep(30)\n"
        )
        process = launch_process((sys.executable, "-c", script))
        self.assertEqual(process.wait(self._deadline()), 0)
        child_pid = int(child_pid_file.read_text())

        process.terminate_group(self._deadline(0.2), self._deadline(0.5))

        child_stat = Path(f"/proc/{child_pid}/stat")
        if child_stat.exists():
            _, _, remainder = child_stat.read_text(encoding="ascii").rpartition(") ")
            self.assertEqual(remainder.split()[0], "Z")

    def test_cleanup_escalates_term_to_kill_for_stubborn_group(self) -> None:
        script = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(30)"
        )
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        process = launch_process((sys.executable, "-c", script), stdio_fd=slave)
        os.close(slave)
        SerialConsole(master, process=process, max_bytes=128).wait_for(
            b"ready", self._deadline()
        )

        process.terminate_group(self._deadline(0.04), self._deadline(0.5))

        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_teardown_orders_monitor_group_cleanup_and_serial_drain(self) -> None:
        events: list[str] = []

        class RecordingMonitor:
            def close(self) -> None:
                events.append("monitor")

        class RecordingProcess:
            def terminate_group(
                self, term_deadline: float, kill_deadline: float
            ) -> None:
                events.append("process")

        class RecordingSerial:
            def drain(self, deadline: float) -> bytes:
                events.append("serial")
                return b"tail"

        tail = teardown_gate(
            RecordingMonitor(),
            RecordingProcess(),
            RecordingSerial(),
            term_deadline=self._deadline(),
            kill_deadline=self._deadline(),
            drain_deadline=self._deadline(),
        )
        self.assertEqual(events, ["monitor", "process", "serial"])
        self.assertEqual(tail, b"tail")

        events.clear()

        class FailingMonitor:
            def close(self) -> None:
                events.append("monitor")
                raise MonitorError("close failed")

        with self.assertRaisesRegex(MonitorError, "close failed"):
            teardown_gate(
                FailingMonitor(),
                RecordingProcess(),
                RecordingSerial(),
                term_deadline=self._deadline(),
                kill_deadline=self._deadline(),
                drain_deadline=self._deadline(),
            )
        self.assertEqual(events, ["monitor", "process", "serial"])

    def test_first_hup_or_term_is_deferred_through_publication_and_restored(
        self,
    ) -> None:
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                previous = signal.getsignal(signum)
                result = self.directory / f"result-{signum}.json"
                with self.assertRaises(GateTermination) as caught:
                    with TerminationSignalState():
                        os.kill(os.getpid(), signum)
                        result.write_text("published", encoding="utf-8")
                self.assertEqual(caught.exception.signum, signum)
                self.assertEqual(result.read_text(encoding="utf-8"), "published")
                self.assertIs(signal.getsignal(signum), previous)

    def test_second_scoped_termination_signal_hard_exits(self) -> None:
        pid = os.fork()
        if pid == 0:
            with TerminationSignalState():
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 128 + signal.SIGTERM)


class DebianRootfsGateOrchestrationTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4

    class Operations:
        def __init__(self, failure: str | None = None) -> None:
            self.failure = failure
            self.events: list[str] = []
            self.publications: list[dict[str, object]] = []

        def _event(self, name: str) -> None:
            self.events.append(name)
            if self.failure == name:
                raise GateFailure(name)

        def invalidate(self, config: GateConfig) -> None:
            del config
            self._event("invalidate")

        def snapshot_inputs(self, config: GateConfig) -> dict[str, str]:
            del config
            self._event("snapshot")
            return {"kernel": "a" * 64, "root_image": "b" * 64}

        def validate_inputs(
            self, config: GateConfig, snapshots: dict[str, str]
        ) -> dict[str, object]:
            del config, snapshots
            self._event("validate")
            return {
                "debian_release": "13.6",
                "packages": (("bash", "5.2"),),
            }

        def prepare(
            self,
            config: GateConfig,
            snapshots: dict[str, str],
            identity: dict[str, object],
        ) -> dict[str, str]:
            del config, snapshots, identity
            self._event("prepare")
            return {"boot_disk": "boot.ext4", "root_disk": "root.ext2"}

        def launch(
            self, config: GateConfig, prepared: dict[str, str], boot_number: int
        ) -> dict[str, object]:
            del config, prepared
            self._event(f"launch{boot_number}")
            return {
                "boot_number": boot_number,
                "argv": ("qemu-system-riscv64", f"boot={boot_number}"),
            }

        def drive_uboot(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"uboot{session['boot_number']}")

        def enter_debian(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"shell{session['boot_number']}")

        def execute_checks(
            self,
            session: dict[str, object],
            config: GateConfig,
            identity: dict[str, object],
            nonce: str,
        ) -> None:
            del config, identity, nonce
            self._event(f"commands{session['boot_number']}")

        def request_quit(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"quit{session['boot_number']}")

        def close_monitor(self, session: dict[str, object]) -> None:
            self._event(f"close{session['boot_number']}")

        def cleanup_process(
            self, session: dict[str, object], config: GateConfig
        ) -> None:
            del config
            self._event(f"cleanup{session['boot_number']}")

        def drain_serial(self, session: dict[str, object], config: GateConfig) -> bytes:
            del config
            boot_number = session["boot_number"]
            self._event(f"drain{boot_number}")
            return f"complete boot {boot_number}\n".encode()

        def hash_final_root(self, config: GateConfig, prepared: dict[str, str]) -> str:
            del config, prepared
            self._event("hash-final-root")
            return "c" * 64

        def publish(
            self,
            config: GateConfig,
            prepared: dict[str, str] | None,
            transcripts: tuple[bytes, bytes],
            result: dict[str, object],
        ) -> None:
            del config, prepared, transcripts
            self._event("publish")
            self.publications.append(copy.deepcopy(result))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        inputs = []
        for name in (
            "kernel",
            "u-boot",
            "qemu-virt.dtb",
            "stage1-initramfs.cpio",
            "debian-root.ext2",
            "rootfs-manifest.json",
            "packages.lock",
            "package-checksums",
        ):
            path = directory / name
            path.write_bytes(name.encode())
            inputs.append(path)
        self.config = GateConfig(*inputs, directory / "output")

    def test_configuration_is_frozen_and_has_exact_four_hart_timeouts(self) -> None:
        self.assertEqual(self.config.smp, 4)
        self.assertGreater(self.config.boot_timeout, 0)
        self.assertGreater(self.config.command_timeout, 0)
        self.assertGreater(self.config.cleanup_timeout, 0)
        with self.assertRaises(FrozenInstanceError):
            self.config.smp = 1

    def test_success_uses_exact_two_boot_lifecycle_and_same_prepared_root(self) -> None:
        operations = self.Operations()

        result = orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(
            operations.events,
            [
                "invalidate",
                "snapshot",
                "validate",
                "prepare",
                "launch1",
                "uboot1",
                "shell1",
                "commands1",
                "quit1",
                "close1",
                "cleanup1",
                "drain1",
                "launch2",
                "uboot2",
                "shell2",
                "commands2",
                "quit2",
                "close2",
                "cleanup2",
                "drain2",
                "hash-final-root",
                "publish",
            ],
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "pass")
        self.assertEqual(
            result["nonce_sha256"], hashlib.sha256(self.NONCE.encode()).hexdigest()
        )
        self.assertNotIn(self.NONCE, json.dumps(result))
        self.assertEqual(len(result["qemu_argv"]), 2)
        self.assertEqual(
            result["manifest_identity"],
            {"debian_release": "13.6"},
        )
        self.assertEqual(result["package_identity"], (("bash", "5.2"),))
        self.assertEqual(
            set(result["phase_durations_seconds"]),
            {"snapshot", "validate", "prepare", "boot1", "boot2", "hash-final-root"},
        )
        self.assertTrue(
            all(
                isinstance(duration, float) and duration >= 0
                for duration in result["phase_durations_seconds"].values()
            )
        )

    def test_failures_never_publish_a_passing_result_and_always_drain_launched_boot(
        self,
    ) -> None:
        cases = (
            ("prepare", []),
            ("launch1", []),
            ("uboot1", ["close1", "cleanup1", "drain1"]),
            ("shell1", ["close1", "cleanup1", "drain1"]),
            ("commands1", ["close1", "cleanup1", "drain1"]),
            ("drain1", ["close1", "cleanup1", "drain1"]),
            ("cleanup1", ["close1", "cleanup1", "drain1"]),
            ("hash-final-root", ["close2", "cleanup2", "drain2"]),
        )

        for failure, required_tail in cases:
            with self.subTest(failure=failure):
                operations = self.Operations(failure)
                result = orchestrate_gate(self.config, operations, nonce=self.NONCE)
                self.assertFalse(result["passed"])
                self.assertEqual(result["reason"], failure)
                for event in required_tail:
                    self.assertIn(event, operations.events)
                self.assertFalse(
                    any(
                        publication["passed"] for publication in operations.publications
                    )
                )

    def test_interrupted_publication_cannot_leave_passing_evidence(self) -> None:
        operations = self.Operations("publish")

        with self.assertRaisesRegex(GateFailure, "publish"):
            orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(operations.events[0], "invalidate")
        self.assertEqual(operations.events[-1], "publish")
        self.assertEqual(operations.publications, [])


class DebianRootfsGateArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_real_boot_image_is_64_mib_and_contains_exactly_three_files(self) -> None:
        kernel = self.directory / "kernel"
        dtb = self.directory / "dtb"
        initramfs = self.directory / "initramfs"
        kernel.write_bytes(b"kernel")
        dtb.write_bytes(b"dtb")
        initramfs.write_bytes(b"initramfs")
        image = self.directory / "boot.ext4"

        build_boot_image(kernel, dtb, initramfs, image)

        self.assertEqual(image.stat().st_size, 64 * 1024 * 1024)
        listing = subprocess.run(
            ["debugfs", "-R", "ls -p /", str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        names = {
            fields[5]
            for line in listing.splitlines()
            if len(fields := line.split("/")) >= 6 and fields[5] not in (".", "..", "")
        }
        self.assertEqual(
            names,
            {"asterinas.booti", "qemu-virt.dtb", "stage1-initramfs.cpio"},
        )
        self.assertNotIn("lost+found", listing)

    def test_sparse_root_copy_uses_descriptor_and_preserves_source_identity(
        self,
    ) -> None:
        source = self.directory / "base.ext2"
        with source.open("wb") as stream:
            stream.write(b"root")
            stream.seek(16 * 1024 * 1024 - 1)
            stream.write(b"\0")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, descriptor)
        destination = self.directory / "run.ext2"

        before, after, copied = copy_sparse_root(descriptor, destination)

        self.assertEqual(before, after)
        self.assertEqual(copied, before)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertLess(destination.stat().st_blocks * 512, destination.stat().st_size)

        directory_fd = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        self.addCleanup(os.close, directory_fd)
        pinned_destination = Path(f"/proc/self/fd/{directory_fd}/pinned-run.ext2")
        pinned_before, pinned_after, pinned_copied = copy_sparse_root(
            descriptor,
            pinned_destination,
            destination_directory_fd=directory_fd,
        )
        self.assertEqual(
            (pinned_before, pinned_after, pinned_copied),
            (before, before, before),
        )
        self.assertTrue((self.directory / "pinned-run.ext2").is_file())

    def test_fdtget_accepts_exactly_four_enabled_cpu_nodes(self) -> None:
        source = self.directory / "four.dts"
        dtb = self.directory / "four.dtb"
        cpus = "\n".join(
            f'cpu@{index} {{ device_type = "cpu"; reg = <{index}>; status = "okay"; }};'
            for index in range(4)
        )
        source.write_text(
            "/dts-v1/; / { #address-cells = <1>; #size-cells = <1>; "
            f"cpus {{ #address-cells = <1>; #size-cells = <0>; {cpus} }}; }};",
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)

        self.assertEqual(verify_four_hart_dtb(dtb), 4)

        source.write_text(
            source.read_text().replace('status = "okay";', 'status = "disabled";', 1),
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)
        with self.assertRaisesRegex(GateFailure, "exactly 4"):
            verify_four_hart_dtb(dtb)

    def test_cli_requires_all_eight_inputs_and_output_without_build_mode(self) -> None:
        required = (
            "--kernel",
            "--uboot",
            "--dtb",
            "--stage1-initramfs",
            "--root-image",
            "--root-manifest",
            "--packages-lock",
            "--package-checksums",
            "--output-directory",
        )
        arguments = [
            value for option in required for value in (option, f"/{option[2:]}")
        ]

        config = parse_gate_args(arguments)

        self.assertEqual(config.smp, 4)
        self.assertEqual(config.root_fs, "ext2")
        self.assertEqual(
            parse_gate_args([*arguments, "--root-fs", "ext4"]).root_fs,
            "ext4",
        )
        with self.assertRaises(SystemExit):
            parse_gate_args(arguments[:-2])
        for forbidden in ("download", "rebuild", "mirror"):
            self.assertNotIn(forbidden, " ".join(arguments))
        with mock.patch.object(
            gate_backend_module, "main", return_value=7
        ) as backend_main:
            self.assertEqual(rootfs_gate_module.main(arguments), 7)
        backend_main.assert_called_once_with(arguments)

        help_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.riscv.debian.rootfs.rootfs_gate",
                "--help",
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--root-image", help_result.stdout)


class DebianRootfsGateBackendSessionTests(unittest.TestCase):
    def test_validation_uses_manifest_schema_for_package_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            inputs = []
            for index in range(8):
                path = directory / f"input-{index}"
                path.write_bytes(str(index).encode())
                inputs.append(path)
            output = directory / "output"
            output.mkdir()
            config = GateConfig(*inputs, output)
            downloaded_packages = (
                ("firefox-esr", "riscv64", "140.14.0esr-1", "0" * 64, "security"),
            )
            manifest = mock.Mock(
                schema_version=6,
                downloaded_packages=downloaded_packages,
                suite="trixie",
                architecture="riscv64",
                debian_release="13.6",
                root_image_sha256="1" * 64,
                packages_lock_sha256="2" * 64,
                gate_packages=(),
            )
            with (
                mock.patch.object(
                    gate_backend_module, "load_manifest", return_value=manifest
                ),
                mock.patch.object(gate_backend_module, "validate_frozen_root"),
                mock.patch.object(
                    gate_backend_module,
                    "load_package_checksums",
                    return_value=downloaded_packages,
                ) as load_checksums,
                mock.patch.object(
                    gate_backend_module, "verify_four_hart_dtb", return_value=4
                ),
                gate_backend_module.ConcreteOperations(config) as operations,
            ):
                operations.validate_inputs(config, operations.snapshot_inputs(config))

            load_checksums.assert_called_once_with(
                mock.ANY, schema_version=manifest.schema_version
            )

    def test_session_uses_hardlinked_same_root_and_removes_directory_after_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            inputs = []
            for index in range(8):
                path = directory / f"input-{index}"
                path.write_bytes(str(index).encode())
                inputs.append(path)
            output = directory / ("repository-output-" + "x" * 40) / ("y" * 40)
            output.mkdir(parents=True)
            boot = output / "boot.ext4"
            root = output / "debian-root.run.ext2"
            boot.write_bytes(b"boot")
            root.write_bytes(b"root")
            config = GateConfig(*inputs, output)
            process = mock.Mock()
            monitor = mock.Mock()
            serial = mock.Mock(transcript=b"")
            serial.drain.return_value = b""
            with (
                mock.patch.object(
                    gate_backend_module, "launch_process", return_value=process
                ) as launch,
                mock.patch.object(
                    gate_backend_module.HmpMonitor,
                    "connect",
                    side_effect=lambda path, *_args, **_kwargs: (
                        monitor
                        if len(os.fsencode(path)) < 108
                        else (_ for _ in ()).throw(OSError("AF_UNIX path too long"))
                    ),
                ),
                mock.patch.object(
                    gate_backend_module, "SerialConsole", return_value=serial
                ),
                gate_backend_module.ConcreteOperations(config) as operations,
            ):
                session = operations.launch(
                    config, {"boot_disk": boot, "root_disk": root}, 1
                )
                session_directory = session["directory"]
                output_fd = operations._require_output()._operation_fd
                self.assertEqual(session_directory.parent, Path("/tmp"))
                self.assertLess(
                    len(os.fsencode(session_directory / "monitor.sock")), 108
                )
                self.assertFalse((session_directory / root.name).exists())
                launched_argv = launch.call_args.args[0]
                self.assertIn(
                    f"file=/proc/self/fd/{output_fd}/{root.name}",
                    " ".join(launched_argv),
                )
                self.assertEqual(launch.call_args.kwargs["pass_fds"], (output_fd,))
                operations.close_monitor(session)
                operations.cleanup_process(session, config)
                self.assertEqual(operations.drain_serial(session, config), b"")
                self.assertFalse(session_directory.exists())

    def test_shell_wait_marker_is_not_present_in_echoed_command_payload(self) -> None:
        class EchoingSerial:
            def __init__(self) -> None:
                self.payload = b""

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.payload = payload

            def wait_for(self, marker: bytes, deadline: float) -> bytes:
                del deadline
                if marker in self.payload:
                    raise AssertionError("echoed payload matched completion marker")
                return self.payload + marker + b"\r\n"

        operations = object.__new__(gate_backend_module.ConcreteOperations)
        session = {"boot_number": 1, "serial": EchoingSerial()}
        operations.execute_checks(
            session,
            mock.Mock(command_timeout=1.0),
            {"debian_release": "13.6", "packages": ()},
            "0123456789abcdef" * 4,
        )
        self.assertIn("commands", session)


class DebianDesktopM3GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.uboot = self.directory / "u-boot"
        self.boot = self.directory / "boot.ext4"
        self.root = self.directory / "root.ext2"
        for path in (self.uboot, self.boot, self.root):
            path.write_bytes(path.name.encode())

    def test_graphical_qemu_contract_adds_only_display_and_input_devices(self) -> None:
        arguments = desktop_m3_qemu_argv(
            uboot=self.uboot,
            boot_disk=self.boot,
            root_disk=self.root,
            monitor_socket=self.directory / "monitor.sock",
        )

        self.assertIn("bochs-display,xres=1280,yres=1024", arguments)
        self.assertIn("virtio-keyboard-device", arguments)
        self.assertIn("virtio-tablet-device", arguments)
        self.assertEqual(arguments[arguments.index("-smp") + 1], "4")
        self.assertEqual(arguments[arguments.index("-m") + 1], "2G")
        self.assertEqual(arguments[arguments.index("-display") + 1], "none")
        self.assertEqual(arguments[arguments.index("-nic") + 1], "none")
        self.assertNotIn("-netdev", arguments)

    def test_classifier_requires_ordered_complete_desktop_evidence(self) -> None:
        transcript = ("boot noise\n" + "\n".join(DESKTOP_M3_MILESTONES) + "\n").encode()

        result = classify_desktop_m3(transcript, expected_debian_release="13.6")

        self.assertTrue(result.passed, result.reason)
        reversed_result = classify_desktop_m3(
            ("\n".join(reversed(DESKTOP_M3_MILESTONES)) + "\n").encode(),
            expected_debian_release="13.6",
        )
        self.assertFalse(reversed_result.passed)
        self.assertEqual(reversed_result.reason, "desktop milestones out of order")

        fatal = classify_desktop_m3(
            transcript + b"(EE) no screens found\n",
            expected_debian_release="13.6",
        )
        self.assertFalse(fatal.passed)
        self.assertEqual(fatal.reason, "Xorg has no screens")

    def test_ppm_inspection_rejects_blank_and_accepts_rendered_pixels(self) -> None:
        blank = b"P6\n4 2\n255\n" + b"\xff\xff\xff" * 8
        cursor_only = b"P6\n100 100\n255\n" + (
            b"\x00\x00\x00" * 9_999 + b"\xff\xff\xff"
        )
        rendered = b"P6\n# QEMU\n4 2\n255\n" + (
            b"\xff\xff\xff" * 4 + b"\x20\x20\x28" * 4
        )

        with self.assertRaisesRegex(GateFailure, "single color"):
            inspect_ppm(blank, expected_width=4, expected_height=2)
        with self.assertRaisesRegex(GateFailure, "insufficient rendered content"):
            inspect_ppm(cursor_only, expected_width=100, expected_height=100)
        metadata = inspect_ppm(rendered, expected_width=4, expected_height=2)

        self.assertEqual(metadata["width"], 4)
        self.assertEqual(metadata["height"], 2)
        self.assertEqual(metadata["pixel_count"], 8)
        self.assertGreaterEqual(metadata["distinct_sampled_colors"], 2)
        self.assertEqual(metadata["non_background_pixels"], 4)

    def test_screenshot_waits_for_first_rendered_frame(self) -> None:
        screenshot = self.directory / "desktop.ppm"
        blank = b"P6\n4 2\n255\n" + b"\xff\xff\xff" * 8
        rendered = b"P6\n4 2\n255\n" + (b"\xff\xff\xff" * 4 + b"\x20\x20\x28" * 4)

        class Monitor:
            calls = 0

            def command(self, command: str, deadline: float) -> bytes:
                del command, deadline
                screenshot.write_bytes((blank, rendered)[self.calls])
                self.calls += 1
                return b""

        monitor = Monitor()
        contents, metadata = capture_rendered_ppm(
            monitor,
            screenshot,
            time.monotonic() + 1.0,
            expected_width=4,
            expected_height=2,
            retry_interval=0.0,
        )

        self.assertEqual(monitor.calls, 2)
        self.assertEqual(contents, rendered)
        self.assertGreaterEqual(metadata["distinct_sampled_colors"], 2)

    def test_desktop_adapter_binds_schema_three_profile(self) -> None:
        self.assertTrue(
            issubclass(DesktopM3Operations, gate_backend_module.ConcreteOperations)
        )


class DebianDesktopM4GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.uboot = self.directory / "u-boot"
        self.boot = self.directory / "boot.ext4"
        self.root = self.directory / "root.ext2"
        for path in (self.uboot, self.boot, self.root):
            path.write_bytes(path.name.encode())

    def test_graphical_contract_reuses_m3_devices_for_m4(self) -> None:
        arguments = desktop_m4_qemu_argv(
            uboot=self.uboot,
            boot_disk=self.boot,
            root_disk=self.root,
            monitor_socket=self.directory / "monitor.sock",
        )

        self.assertIn("bochs-display,xres=1280,yres=1024", arguments)
        self.assertIn("virtio-keyboard-device", arguments)
        self.assertIn("virtio-tablet-device", arguments)
        self.assertEqual(arguments[arguments.index("-smp") + 1], "4")
        self.assertEqual(arguments[arguments.index("-nic") + 1], "none")

    def test_ext4_root_mode_adds_stage1_filesystem_selector(self) -> None:
        operations = object.__new__(DesktopM4Operations)
        ext2 = mock.Mock(root_fs="ext2")
        ext4 = mock.Mock(root_fs="ext4")

        qemu_bootargs = (
            "console=ttyS0 loglevel=4 "
            "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
            "init=/init -- --root-init=systemd"
        )
        self.assertEqual(operations._bootargs(ext2), qemu_bootargs)
        self.assertEqual(
            operations._bootargs(ext4),
            "console=ttyS0 loglevel=4 "
            "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
            "init=/init -- --root-fs=ext4 --root-init=systemd",
        )

    def test_boot_commands_reserve_scanout_before_booti(self) -> None:
        operations = object.__new__(DesktopM4Operations)
        operations.config = mock.Mock(root_fs="ext2")
        commands = operations._boot_commands(0x40000000)

        self.assertIn("fdt mknode / reserved-memory", commands)
        self.assertIn(
            "fdt set /reserved-memory/framebuffer@40000000 "
            "reg <0x0 0x40000000 0x0 0x1000000>",
            commands,
        )
        self.assertIn(
            "fdt set /reserved-memory/framebuffer@40000000 no-map", commands
        )
        self.assertIn("fdt print /framebuffer@40000000", commands)
        bootargs_index = next(
            index
            for index, command in enumerate(commands)
            if command.startswith('setenv bootargs "')
        )
        self.assertLess(
            commands.index("fdt set /reserved-memory/framebuffer@40000000 no-map"),
            bootargs_index,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered",
            commands[bootargs_index],
        )

    def test_classifier_requires_ordered_application_evidence(self) -> None:
        framebuffer = (
            "DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved\n"
            "DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=" + "a" * 64 + "\n"
            "DEBIAN_DESKTOP_M4_FB_COLORS distinct=10 non_background=1024\n"
        )
        transcript = (
            "boot noise\n" + "\n".join(DESKTOP_M4_MILESTONES) + "\n" + framebuffer
        ).encode()

        result = classify_desktop_m4(transcript, expected_debian_release="13.6")

        self.assertTrue(result.passed, result.reason)
        reversed_result = classify_desktop_m4(
            ("\n".join(reversed(DESKTOP_M4_MILESTONES)) + "\n").encode(),
            expected_debian_release="13.6",
        )
        self.assertFalse(reversed_result.passed)
        self.assertEqual(reversed_result.reason, "desktop milestones out of order")
        failed = classify_desktop_m4(
            transcript + b"DEBIAN_DESKTOP_M4_FAIL reason=netsurf\n",
            expected_debian_release="13.6",
        )
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "desktop guest failure")

    def test_adapter_binds_only_schema_four_profile_and_m4_outputs(self) -> None:
        operations = object.__new__(DesktopM4Operations)
        config = mock.Mock()

        with (
            mock.patch.object(
                DesktopM4Operations,
                "input_paths",
                new_callable=mock.PropertyMock,
                return_value={"manifest": self.directory / "manifest.json"},
            ),
            mock.patch.object(
                gate_backend_module.ConcreteOperations,
                "validate_inputs",
                return_value={"debian_release": "13.6"},
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.desktop_m3_gate.load_manifest",
                return_value=mock.Mock(schema_version=4, profile="desktop-m4"),
            ),
        ):
            identity = operations.validate_inputs(config, {})

        self.assertEqual(identity["profile"], "desktop-m4")
        self.assertEqual(operations.ARTIFACT_PREFIX, "desktop-m4")

        for schema, profile in ((3, "desktop-m3"), (4, "desktop-m3")):
            with (
                self.subTest(schema=schema, profile=profile),
                mock.patch.object(
                    DesktopM4Operations,
                    "input_paths",
                    new_callable=mock.PropertyMock,
                    return_value={"manifest": self.directory / "manifest.json"},
                ),
                mock.patch.object(
                    gate_backend_module.ConcreteOperations,
                    "validate_inputs",
                    return_value={},
                ),
                mock.patch(
                    "tools.riscv.debian.rootfs.desktop_m3_gate.load_manifest",
                    return_value=mock.Mock(schema_version=schema, profile=profile),
                ),
                self.assertRaisesRegex(GateFailure, "desktop-m4 profile"),
            ):
                operations.validate_inputs(config, {})



if __name__ == "__main__":
    unittest.main()
