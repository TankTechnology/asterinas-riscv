# SPDX-License-Identifier: MPL-2.0

"""Test persistence and worktree isolation without changing Docker state."""

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "dev_container", Path(__file__).with_name("dev_container.py")
)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


class DevContainerTests(unittest.TestCase):
    def test_different_worktrees_share_downloads_but_not_cargo_binaries(self):
        first, prefix = dev.identity(Path("/one"), "sha256:123456789abc0")
        second, other_prefix = dev.identity(Path("/two"), "sha256:123456789abc0")
        self.assertNotEqual(first, second)
        self.assertEqual(prefix, other_prefix)
        first_mounts = dev.mount_plan(Path("/one"), first, prefix)
        second_mounts = dev.mount_plan(Path("/two"), second, prefix)
        self.assertNotEqual(first_mounts[2], second_mounts[2])
        self.assertEqual(first_mounts[3:], second_mounts[3:])

    def test_image_upgrade_does_not_reuse_old_container_or_caches(self):
        old = dev.identity(Path("/one"), "sha256:123456789abc0")
        new = dev.identity(Path("/one"), "sha256:223456789abc0")
        self.assertNotEqual(old[0], new[0])
        self.assertNotEqual(old[1], new[1])

    def test_mount_rejects_docker_option_injection(self):
        with self.assertRaises(ValueError):
            dev.mount("/tmp/source,readonly", "/root/asterinas", bind=True)
        self.assertIn(
            "source=/tmp/path with spaces,",
            dev.mount("/tmp/path with spaces", "/workspace", bind=True),
        )

    def test_exec_preserves_argument_boundaries_and_exit_semantics(self):
        command = ["printf", "%s", "$(touch /bad); literal space"]
        argv = dev.command_args("builder", command, True, False)
        self.assertEqual(argv[-3:], command)
        self.assertIn("CARGO_NET_OFFLINE=true", argv)
        self.assertIn(f"NIX_CONFIG={dev.OFFLINE_NIX_CONFIG}", argv)
        self.assertIn("unshare", argv)
        self.assertIn("--net", argv)
        self.assertIn(dev.BUILD_LOCK, argv)
        self.assertIn("-i", argv)
        self.assertNotIn("-t", argv)

    def existing(self, running=True):
        return {
            "Config": {
                "Labels": {
                    f"{dev.LABEL}.schema": dev.SCHEMA,
                    f"{dev.LABEL}.workspace": "/one",
                    f"{dev.LABEL}.image": "image-id",
                }
            },
            "State": {"Running": running},
        }

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_running_container_is_reused(self, inspect, docker):
        inspect.return_value = self.existing()
        dev.ensure_container(Path("/one"), "image-id", "builder", "cache")
        docker.assert_not_called()

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_stopped_container_is_started_without_recreation(self, inspect, docker):
        inspect.return_value = self.existing(False)
        dev.ensure_container(Path("/one"), "image-id", "builder", "cache")
        docker.assert_called_once_with("start", "builder", capture=True)

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_wrong_workspace_is_rejected_without_mutating_container(
        self, inspect, docker
    ):
        inspect.return_value = self.existing()
        with self.assertRaises(RuntimeError):
            dev.ensure_container(Path("/two"), "image-id", "builder", "cache")
        docker.assert_not_called()

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container", return_value=None)
    def test_new_container_keeps_state_and_overrides_image_startup(
        self, inspect, docker
    ):
        dev.ensure_container(Path("/one"), "image-id", "builder", "cache")
        create = docker.call_args_list[0].args
        self.assertEqual(create[0], "create")
        self.assertNotIn("--rm", create)
        self.assertEqual(
            create[-4:], ("--entrypoint", "/bin/sleep", "image-id", "infinity")
        )
        self.assertIn("type=volume,source=cache-rustup,target=/root/.rustup", create)
        self.assertIn("type=volume,source=cache-nix,target=/nix", create)

    @patch.object(dev, "docker")
    def test_daemon_failure_is_not_treated_as_missing_container(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess(
                [], 1, "", "Cannot connect to the Docker daemon"
            ),
            subprocess.CalledProcessError(1, ["docker", "info"]),
        ]
        with self.assertRaises(subprocess.CalledProcessError):
            dev.inspect_container("builder")

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_successful_cache_seed_is_not_repeated(self, inspect, docker):
        inspect.return_value = {"State": {"Status": "exited", "ExitCode": 0}}
        dev.seed_cargo_cache(Path("/one"), "image-id", "cache", "old-cargo")
        docker.assert_called_once_with("volume", "inspect", "old-cargo", capture=True)

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_idle_stop_requests_pid_column_and_preserves_container(
        self, inspect, docker
    ):
        inspect.return_value = self.existing()
        docker.return_value.stdout = "PID COMMAND\n101 docker-init\n102 sleep\n"
        dev.stop_container(Path("/one"), "image-id", "builder")
        docker.assert_any_call("top", "builder", "-eo", "pid,comm", capture=True)
        self.assertEqual(docker.call_args.args, ("stop", "builder"))

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_stop_does_not_kill_untracked_commands(self, inspect, docker):
        inspect.return_value = self.existing()
        docker.return_value.stdout = (
            "PID COMMAND\n101 docker-init\n102 sleep\n103 cargo\n"
        )
        with self.assertRaises(RuntimeError):
            dev.stop_container(Path("/one"), "image-id", "builder")
        self.assertFalse(any(call.args[0] == "stop" for call in docker.call_args_list))

    @patch.object(dev, "docker")
    @patch.object(dev, "inspect_container")
    def test_stop_rejects_unrelated_container(self, inspect, docker):
        inspect.return_value = self.existing()
        with self.assertRaises(RuntimeError):
            dev.stop_container(Path("/two"), "image-id", "builder")
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
