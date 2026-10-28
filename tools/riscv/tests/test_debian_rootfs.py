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
