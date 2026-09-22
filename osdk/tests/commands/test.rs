// SPDX-License-Identifier: MPL-2.0

use std::{fs, process::Command};

use tempfile::tempdir;

use crate::util::{cargo_osdk, edit_config_files};

#[test]
#[ignore = "requires RISC-V QEMU, its cross-compilation toolchain, and GNU timeout"]
fn riscv_guest_test_result_reaches_host() {
    let work_dir = tempdir().unwrap();
    cargo_osdk(["new", "exit_status_probe"])
        .current_dir(work_dir.path())
        .ok()
        .unwrap();
    let project = work_dir.path().join("exit_status_probe");
    edit_config_files(&project);
    fs::write(
        project.join("src/lib.rs"),
        r#"#![no_std]

#[ostd::prelude::ktest]
fn guest_failure() {
    panic!("intentional exit-status regression failure");
}

#[ostd::prelude::ktest]
fn guest_success() {}
"#,
    )
    .unwrap();
    fs::write(
        project.join("OSDK.toml"),
        r#"[boot]
method = "qemu-direct"
[qemu]
args = "-machine virt -cpu rv64,sv48=false -m 256M -smp 1 -display none -serial stdio -monitor none -no-reboot -nic none"
"#,
    )
    .unwrap();

    // Check failure first: SBI shutdown used to make both cases return zero.
    for (test_name, expected_status, summary) in [
        ("guest_failure", 1, "0 passed; 1 failed; 1 filtered out"),
        ("guest_success", 0, "1 passed; 0 failed; 1 filtered out"),
    ] {
        // Bound the entire process group, including QEMU and inherited output
        // pipes. Killing only cargo-osdk can leave a hung guest holding them open.
        let output = Command::new("timeout")
            .args(["--kill-after=5s", "120s"])
            .arg(env!("CARGO_BIN_EXE_cargo-osdk"))
            .args([
                "osdk",
                "test",
                test_name,
                "--release",
                "--target-arch",
                "riscv64",
                "--features",
                "ostd/riscv_sv39_mode",
            ])
            .current_dir(&project)
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stdout.contains(summary), "{stdout}\n{stderr}");
        assert_eq!(
            output.status.code(),
            Some(expected_status),
            "{test_name}: {stdout}\n{stderr}"
        );
    }
}
