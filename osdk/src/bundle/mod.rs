// SPDX-License-Identifier: MPL-2.0

pub mod bin;
pub mod file;
pub mod vm_image;

use std::{
    collections::HashSet,
    ffi::OsStr,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    process::{self, ExitStatus},
    time::{Duration, SystemTime},
};

use tempfile::NamedTempFile;

use bin::AsterBin;
use file::{BundleFile, Initramfs};
use vm_image::{AsterVmImage, AsterVmImageType};

use crate::{
    arch::Arch,
    config::{
        Config,
        scheme::{Action, ActionChoice, BootMethod},
    },
    error::Errno,
    error_msg,
    util::new_command_checked_exists,
};

/// The osdk bundle artifact that stores as `bundle` directory.
///
/// This `Bundle` struct is used to track a bundle on a filesystem. Every modification to the bundle
/// would result in file system writes. But the bundle will not be removed from the file system when
/// the `Bundle` is dropped.
pub struct Bundle {
    manifest: BundleManifest,
    path: PathBuf,
}

/// The osdk bundle artifact manifest that stores as `bundle.toml`.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct BundleManifest {
    initramfs: Option<Initramfs>,
    aster_bin: Option<AsterBin>,
    #[serde(default)]
    debug_elf: Option<AsterBin>,
    vm_image: Option<AsterVmImage>,
    config: Config,
    action: ActionChoice,
    last_modified: SystemTime,
}

const MANIFEST_FILE_NAME: &str = "bundle.toml";

impl BundleManifest {
    fn artifacts(&self) -> impl Iterator<Item = &dyn BundleFile> {
        [
            self.aster_bin.as_ref().map(|file| file as &dyn BundleFile),
            self.debug_elf.as_ref().map(|file| file as &dyn BundleFile),
            self.initramfs.as_ref().map(|file| file as &dyn BundleFile),
            self.vm_image.as_ref().map(|file| file as &dyn BundleFile),
        ]
        .into_iter()
        .flatten()
    }

    fn trace_bin(&self) -> Option<&AsterBin> {
        self.debug_elf.as_ref().or_else(|| {
            self.aster_bin
                .as_ref()
                .filter(|bin| bin.is_unstripped_elf())
        })
    }
}

fn artifact_path_is_usable(path: &Path) -> bool {
    path.file_name()
        .is_some_and(|name| name != OsStr::new(MANIFEST_FILE_NAME))
}

fn artifact_path_is_unique(manifest: &BundleManifest, artifact: &impl BundleFile) -> bool {
    let artifact_name = artifact.path().file_name();
    artifact_path_is_usable(artifact.path())
        && manifest
            .artifacts()
            .all(|file| file.path().file_name() != artifact_name)
}

fn artifact_paths_are_unique(manifest: &BundleManifest) -> bool {
    let mut names = HashSet::new();
    manifest.artifacts().all(|file| {
        artifact_path_is_usable(file.path())
            && file
                .path()
                .file_name()
                .is_some_and(|name| names.insert(name))
    })
}

fn bundle_file_is_valid(bundle_path: &Path, file: &dyn BundleFile) -> bool {
    let Ok(metadata) = bundle_path.join(file.path()).metadata() else {
        return false;
    };
    metadata.is_file()
        && file.size() == &metadata.len()
        && metadata
            .modified()
            .is_ok_and(|modified| file.modified_time() == &modified)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum QemuExit {
    Success,
    Failed,
    Unknown,
}

pub(crate) fn classify_qemu_exit_status(exit_status: ExitStatus) -> QemuExit {
    if exit_status.success() {
        return QemuExit::Success;
    }

    let Some(qemu_exit_code) = exit_status.code() else {
        return QemuExit::Unknown;
    };

    // For x86 QEMU with `isa-debug-exit`, the guest exit code is encoded as
    // `(code << 1) | 1`. Do not decode QEMU's own failure exit code `1`.
    if qemu_exit_code == 1 {
        return QemuExit::Unknown;
    }

    let kernel_exit_code = qemu_exit_code >> 1;
    match kernel_exit_code {
        // Corresponds to `ostd::QemuExitCode::Success`.
        0x10 => QemuExit::Success,
        // Corresponds to `ostd::QemuExitCode::Failed`.
        0x20 => QemuExit::Failed,
        // Unknown exit code, e.g., a triple fault.
        _ => QemuExit::Unknown,
    }
}

impl Bundle {
    /// Creates a bundle, copies its initramfs if configured, and writes its manifest.
    pub fn new(path: impl AsRef<Path>, config: &Config, action: ActionChoice) -> Self {
        std::fs::create_dir_all(path.as_ref()).unwrap();
        let config_initramfs = match action {
            ActionChoice::Run => config.run.boot.initramfs.as_ref(),
            ActionChoice::Test => config.test.boot.initramfs.as_ref(),
        };
        let initramfs = if let Some(ref initramfs) = config_initramfs {
            if !initramfs.exists() {
                error_msg!("initramfs file not found: {}", initramfs.display());
                process::exit(Errno::BuildCrate as _);
            }
            if !artifact_path_is_usable(initramfs) {
                panic!("initramfs conflicts with the bundle manifest");
            }
            Some(Initramfs::new(initramfs).copy_to(&path))
        } else {
            None
        };
        let mut created = Self {
            manifest: BundleManifest {
                initramfs,
                aster_bin: None,
                debug_elf: None,
                vm_image: None,
                config: config.clone(),
                action,
                last_modified: SystemTime::now(),
            },
            path: path.as_ref().to_path_buf(),
        };
        created.write_manifest_to_fs();
        created
    }

    // Load the bundle from the file system. If the bundle does not exist or have inconsistencies,
    // it will return `None`.
    pub fn load(path: impl AsRef<Path>) -> Option<Self> {
        let manifest_file_path = path.as_ref().join(MANIFEST_FILE_NAME);
        let manifest_file_content = std::fs::read_to_string(manifest_file_path).ok()?;
        let manifest: BundleManifest = toml::from_str(&manifest_file_content).ok()?;
        if !artifact_paths_are_unique(&manifest)
            || manifest
                .artifacts()
                .any(|file| !bundle_file_is_valid(path.as_ref(), file))
        {
            return None;
        }

        if let Some(debug_elf) = &manifest.debug_elf
            && manifest
                .aster_bin
                .as_ref()
                .is_none_or(|aster_bin| !debug_elf.is_valid_debug_elf_for(aster_bin))
        {
            return None;
        }

        Some(Self {
            manifest,
            path: path.as_ref().to_path_buf(),
        })
    }

    pub fn can_run_with_config(&self, config: &Config, action: ActionChoice) -> Result<(), String> {
        // If built for testing, better not to run it. Vice versa.
        if self.manifest.action != action {
            return Err(format!(
                "the bundle is built for {:?}",
                self.manifest.action
            ));
        }
        if self.manifest.config.target_arch != config.target_arch {
            return Err(
                "the kernel architecture is not compatible with the run configuration".to_owned(),
            );
        }

        let self_action = match self.manifest.action {
            ActionChoice::Run => &self.manifest.config.run,
            ActionChoice::Test => &self.manifest.config.test,
        };
        let config_action = match action {
            ActionChoice::Run => &config.run,
            ActionChoice::Test => &config.test,
        };

        // Compare the manifest with the run configuration except the initramfs and the boot method.
        if self_action.grub != config_action.grub
            || self_action.qemu != config_action.qemu
            || self_action.build != config_action.build
            || self_action.boot.kcmdline != config_action.boot.kcmdline
        {
            return Err("the bundle is not compatible with the run configuration".to_owned());
        }

        match config_action.boot.method {
            BootMethod::QemuDirect => {
                let Some(aster_bin) = self.manifest.aster_bin.as_ref() else {
                    return Err("kernel binary is required for direct QEMU booting".to_owned());
                };

                // Validate the kernel binary type against the configured boot protocol.
                // This prevents reusing an incompatible binary (e.g. ELF vs. `bzImage`) when
                // switching boot methods (for example, from a Grub ISO to `qemu-direct`),
                // which would otherwise cause boot failures.
                if aster_bin.arch() != config.target_arch {
                    return Err(
                        "the kernel architecture is not compatible with the run configuration"
                            .to_owned(),
                    );
                }

                let expected_kind = bin::expected_qemu_direct_bin_kind(
                    config.target_arch,
                    config_action.grub.boot_protocol,
                );
                if aster_bin.typ().qemu_direct_kind() != expected_kind {
                    return Err(
                        "the kernel binary type is not compatible with the run configuration"
                            .to_owned(),
                    );
                }
                if expected_kind == bin::QemuDirectBinKind::RiscvImage
                    && self.manifest.debug_elf.is_none()
                {
                    return Err("a debug ELF is required for a RISC-V Image".to_owned());
                }
            }
            BootMethod::GrubRescueIso => {
                let Some(ref vm_image) = self.manifest.vm_image else {
                    return Err("VM image is required for QEMU booting".to_owned());
                };
                if !matches!(vm_image.typ(), AsterVmImageType::GrubIso(_)) {
                    return Err("VM image in the bundle is not a Grub ISO image".to_owned());
                }
            }
            BootMethod::GrubQcow2 => {
                let Some(ref vm_image) = self.manifest.vm_image else {
                    return Err("VM image is required for QEMU booting".to_owned());
                };
                if !matches!(vm_image.typ(), AsterVmImageType::Qcow2(_)) {
                    return Err("VM image in the bundle is not a Qcow2 image".to_owned());
                }
            }
        }

        // Compare the initramfs.
        let initramfs_err =
            "The initramfs in the bundle is different from the one in the run configuration"
                .to_owned();
        match (&self.manifest.initramfs, &config_action.boot.initramfs) {
            (Some(initramfs), Some(initramfs_path)) => {
                let config_initramfs = Initramfs::new(initramfs_path);
                if initramfs.size() != config_initramfs.size()
                    || initramfs.modified_time() < config_initramfs.modified_time()
                {
                    return Err(initramfs_err);
                }
            }
            (None, None) => {}
            _ => {
                return Err(initramfs_err);
            }
        };

        Ok(())
    }

    pub fn last_modified_time(&self) -> SystemTime {
        self.manifest.last_modified
    }

    pub fn run(&self, config: &Config, action: ActionChoice) {
        let exit_status = self.run_qemu_and_wait(config, action);
        // FIXME: When panicking it sometimes returns success, why?
        match classify_qemu_exit_status(exit_status) {
            QemuExit::Success => {}
            QemuExit::Failed => std::process::exit(1),
            QemuExit::Unknown => std::process::exit(2),
        }
    }

    pub(crate) fn run_qemu_and_wait(&self, config: &Config, action: ActionChoice) -> ExitStatus {
        match self.can_run_with_config(config, action) {
            Ok(()) => {}
            Err(msg) => {
                error_msg!("{}", msg);
                std::process::exit(Errno::RunBundle as _);
            }
        }
        let action = match action {
            ActionChoice::Run => &config.run,
            ActionChoice::Test => &config.test,
        };

        let mut qemu_cmd = new_command_checked_exists(&action.qemu.path);
        qemu_cmd.current_dir(&config.work_dir);

        match action.boot.method {
            BootMethod::QemuDirect => {
                let aster_bin = self.manifest.aster_bin.as_ref().unwrap();
                qemu_cmd
                    .arg("-kernel")
                    .arg(self.path.join(aster_bin.path()));
                if let Some(ref initramfs) = action.boot.initramfs {
                    qemu_cmd.arg("-initrd").arg(initramfs);
                } else {
                    info!("No initramfs specified");
                };
                qemu_cmd.arg("-append").arg(action.boot.kcmdline.join(" "));
            }
            BootMethod::GrubRescueIso => {
                let vm_image = self.manifest.vm_image.as_ref().unwrap();
                assert!(matches!(vm_image.typ(), AsterVmImageType::GrubIso(_)));
                let bootdev_opts = action
                    .qemu
                    .bootdev_append_options
                    .as_deref()
                    .unwrap_or(",index=2,media=cdrom");
                qemu_cmd.arg("-drive").arg(format!(
                    "file={},format=raw{}",
                    self.path.join(vm_image.path()).to_string_lossy(),
                    bootdev_opts,
                ));
            }
            BootMethod::GrubQcow2 => {
                let vm_image = self.manifest.vm_image.as_ref().unwrap();
                assert!(matches!(vm_image.typ(), AsterVmImageType::Qcow2(_)));
                // FIXME: this doesn't work for regular QEMU, but may work for TDX.
                let bootdev_opts = action
                    .qemu
                    .bootdev_append_options
                    .as_deref()
                    .unwrap_or(",if=virtio");
                qemu_cmd.arg("-drive").arg(format!(
                    "file={},format=qcow2{}",
                    self.path.join(vm_image.path()).to_string_lossy(),
                    bootdev_opts,
                ));
            }
        };

        match shlex::split(&action.qemu.args) {
            Some(v) => {
                for arg in v {
                    qemu_cmd.arg(arg);
                }
            }
            None => {
                error_msg!("Failed to parse qemu args: {:#?}", &action.qemu.args);
                process::exit(Errno::ParseMetadata as _);
            }
        }

        let exit_status = if action.qemu.with_monitor
            && let Some(qemu_log_file) = &action.qemu.log_file
        {
            let qemu_log_path = config.work_dir.join(qemu_log_file);
            let qemu_monitor_socket_path = NamedTempFile::new().unwrap().into_temp_path();
            qemu_cmd.arg("-monitor").arg(format!(
                "unix:{},server,nowait",
                qemu_monitor_socket_path.to_string_lossy()
            ));

            info!("Running QEMU: {qemu_cmd:#?}");
            let mut qemu_child = qemu_cmd.spawn().unwrap();
            std::thread::sleep(Duration::from_secs(1)); // Wait for QEMU to start
            let mut qemu_monitor_stream = UnixStream::connect(&qemu_monitor_socket_path).unwrap();
            wait_until_guest_kernel_shutdown(config, &qemu_log_path, &mut qemu_monitor_stream);
            info!("VM is paused (shutdown)");

            self.post_run_action(config, action, Some(&mut qemu_monitor_stream));

            let _ = qemu_monitor_stream.write_all(b"quit\n");
            qemu_child.wait().unwrap()
        } else {
            info!("Running QEMU: {qemu_cmd:#?}");
            let exit_status = qemu_cmd.status().unwrap();
            self.post_run_action(config, action, None);
            exit_status
        };

        fn wait_until_guest_kernel_shutdown(
            config: &Config,
            qemu_log_path: &Path,
            qemu_monitor_stream: &mut UnixStream,
        ) {
            // Check VM status every 0.1 seconds and break the loop if the VM is stopped or hanging.
            while qemu_monitor_stream.write_all(b"info status\n").is_ok() {
                let status = BufReader::new(&mut *qemu_monitor_stream)
                    .lines()
                    .find(|line| line.as_ref().is_ok_and(|s| s.starts_with("VM status:")));
                if status.is_some_and(|msg| msg.unwrap() == "VM status: paused (shutdown)") {
                    break;
                }

                if config.target_arch == Arch::RiscV64
                    && let Ok(log_file) = std::fs::File::open(qemu_log_path)
                {
                    let log = rev_buf_reader::RevBufReader::new(&log_file);
                    if log.lines().next().is_some_and(|line| {
                        line.as_ref().is_ok_and(|s| {
                            s.contains("SBI system_reset cannot shut down the underlying machine")
                        })
                    }) {
                        break;
                    }
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
        exit_status
    }

    /// Moves the VM image into the bundle.
    pub fn consume_vm_image(&mut self, vm_image: AsterVmImage) {
        if self.manifest.vm_image.is_some() {
            panic!("vm_image already exists");
        }
        if !artifact_path_is_unique(&self.manifest, &vm_image) {
            panic!("vm_image conflicts with another bundle artifact");
        }
        self.manifest.vm_image = Some(vm_image.copy_to(&self.path));
        self.write_manifest_to_fs();
    }

    /// Moves the kernel binary into the bundle.
    pub fn consume_aster_bin(&mut self, aster_bin: AsterBin) {
        if self.manifest.aster_bin.is_some() {
            panic!("aster_bin already exists");
        }
        if !artifact_path_is_unique(&self.manifest, &aster_bin) {
            panic!("aster_bin conflicts with another bundle artifact");
        }
        self.manifest.aster_bin = Some(aster_bin.copy_to(&self.path));
        self.write_manifest_to_fs();
    }

    /// Moves the symbol-bearing ELF into the bundle.
    pub fn consume_debug_elf(&mut self, debug_elf: AsterBin) {
        if self.manifest.debug_elf.is_some() {
            panic!("debug_elf already exists");
        }
        let aster_bin = self
            .manifest
            .aster_bin
            .as_ref()
            .expect("the boot artifact must be stored before the debug ELF");
        if !debug_elf.validate() {
            panic!("debug_elf is no longer valid");
        }
        if !debug_elf.is_valid_debug_elf_for(aster_bin) {
            panic!("debug_elf is not compatible with the boot artifact");
        }
        if !artifact_path_is_unique(&self.manifest, &debug_elf) {
            panic!("debug_elf conflicts with another bundle artifact");
        }
        self.manifest.debug_elf = Some(debug_elf.copy_to(&self.path));
        self.write_manifest_to_fs();
    }

    fn write_manifest_to_fs(&mut self) {
        self.manifest.last_modified = SystemTime::now();
        let manifest_file_content = toml::to_string(&self.manifest).unwrap();
        let manifest_file_path = self.path.join(MANIFEST_FILE_NAME);
        std::fs::write(manifest_file_path, manifest_file_content).unwrap();
    }

    fn post_run_action(
        &self,
        config: &Config,
        action: &Action,
        qemu_monitor_stream: Option<&mut UnixStream>,
    ) {
        let Some(qemu_log_file) = &action.qemu.log_file else {
            return;
        };

        // Read the configured QEMU output and check if it failed with a panic.
        // Setting a QEMU log is required for source line stack trace because piping the output
        // is less desirable when running QEMU with serial redirected to standard I/O.
        let qemu_log_path = config.work_dir.join(qemu_log_file);
        let trace_bin = self.manifest.trace_bin();
        if let Ok(file) = std::fs::File::open(&qemu_log_path)
            && let Some(trace_bin) = trace_bin
        {
            crate::util::trace_panic_from_log(file, self.path.join(trace_bin.path()));
        }

        // Find the coverage data information in the QEMU log, and dump it if found.
        if let Some(qemu_monitor_stream) = qemu_monitor_stream
            && let Ok(file) = std::fs::File::open(&qemu_log_path)
        {
            crate::util::dump_coverage_from_qemu(file, qemu_monitor_stream);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs::{self, File, FileTimes},
        path::PathBuf,
        time::SystemTime,
    };

    use super::Bundle;
    use crate::{
        arch::Arch,
        bundle::{
            bin::{AsterBin, AsterBinType, AsterElfMeta},
            file::BundleFile,
            vm_image::{AsterGrubIsoImageMeta, AsterVmImage, AsterVmImageType},
        },
        config::{
            Config,
            scheme::{Action, ActionChoice, BootMethod, Build},
        },
    };

    fn config(arch: Arch, boot_method: BootMethod) -> Config {
        let mut action = Action::default();
        action.boot.method = boot_method;
        Config {
            work_dir: PathBuf::new(),
            target_arch: arch,
            build: Build::default(),
            run: action.clone(),
            test: action,
        }
    }

    fn elf_type() -> AsterBinType {
        AsterBinType::Elf(AsterElfMeta {
            has_linux_header: false,
            has_pvh_header: false,
            has_multiboot_header: true,
            has_multiboot2_header: true,
        })
    }

    fn riscv_image(path: impl AsRef<std::path::Path>) -> AsterBin {
        AsterBin::new(
            path,
            Arch::RiscV64,
            AsterBinType::RiscvImage,
            String::new(),
            false,
        )
    }

    fn debug_elf(path: impl AsRef<std::path::Path>) -> AsterBin {
        AsterBin::new(path, Arch::RiscV64, elf_type(), String::new(), false)
    }

    #[test]
    fn debug_elf_is_preferred_for_panic_tracing() {
        let source_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let image_path = source_dir.path().join("kernel.Image");
        let debug_elf_path = source_dir.path().join("kernel.elf");
        fs::write(&image_path, b"Image").unwrap();
        fs::write(&debug_elf_path, b"\x7fELF").unwrap();
        let stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        bundle.consume_aster_bin(riscv_image(image_path));
        bundle.consume_debug_elf(debug_elf(debug_elf_path));

        assert_eq!(
            bundle.manifest.trace_bin().unwrap().path(),
            bundle.manifest.debug_elf.as_ref().unwrap().path()
        );
    }

    #[test]
    fn cached_bundle_architecture_is_checked_for_grub_boot() {
        let bundle_dir = tempfile::tempdir().unwrap();
        let stored_config = config(Arch::RiscV64, BootMethod::GrubRescueIso);
        let bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        let requested_config = config(Arch::X86_64, BootMethod::GrubRescueIso);

        assert_eq!(
            bundle.can_run_with_config(&requested_config, ActionChoice::Run),
            Err("the kernel architecture is not compatible with the run configuration".to_owned()),
        );
    }

    #[test]
    fn missing_cached_boot_artifact_invalidates_the_bundle() {
        let source_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let image_path = source_dir.path().join("kernel.Image");
        fs::write(&image_path, b"Image").unwrap();
        let stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        bundle.consume_aster_bin(riscv_image(image_path));
        fs::remove_file(
            bundle_dir
                .path()
                .join(bundle.manifest.aster_bin.as_ref().unwrap().path()),
        )
        .unwrap();

        assert!(Bundle::load(bundle_dir.path()).is_none());
    }

    #[test]
    fn replaced_cached_boot_artifact_invalidates_the_bundle() {
        let source_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let image_path = source_dir.path().join("kernel.Image");
        fs::write(&image_path, b"Image").unwrap();
        let stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        bundle.consume_aster_bin(riscv_image(image_path));
        let stored_image_path = bundle_dir
            .path()
            .join(bundle.manifest.aster_bin.as_ref().unwrap().path());
        fs::write(&stored_image_path, b"Other").unwrap();
        File::options()
            .write(true)
            .open(&stored_image_path)
            .unwrap()
            .set_times(FileTimes::new().set_modified(SystemTime::UNIX_EPOCH))
            .unwrap();

        assert!(Bundle::load(bundle_dir.path()).is_none());
    }

    #[test]
    fn cached_debug_artifact_must_be_an_unstripped_elf() {
        let source_dir = tempfile::tempdir().unwrap();
        let cases = [(AsterBinType::RiscvImage, false), (elf_type(), true)];

        for (index, (typ, stripped)) in cases.into_iter().enumerate() {
            let bundle_dir = tempfile::tempdir().unwrap();
            let debug_elf_path = source_dir.path().join(format!("debug-{index}"));
            fs::write(&debug_elf_path, b"\x7fELF").unwrap();
            let stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
            let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
            bundle.manifest.debug_elf = Some(AsterBin::new(
                debug_elf_path,
                Arch::RiscV64,
                typ,
                String::new(),
                stripped,
            ));
            bundle.write_manifest_to_fs();

            assert!(Bundle::load(bundle_dir.path()).is_none());
        }
    }

    #[test]
    fn missing_cached_debug_elf_invalidates_the_bundle() {
        let source_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let debug_elf_path = source_dir.path().join("kernel.elf");
        let image_path = source_dir.path().join("kernel.Image");
        fs::write(&debug_elf_path, b"\x7fELF").unwrap();
        fs::write(&image_path, b"Image").unwrap();
        let stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        bundle.consume_aster_bin(riscv_image(image_path));
        bundle.consume_debug_elf(debug_elf(debug_elf_path));
        fs::remove_file(
            bundle_dir
                .path()
                .join(bundle.manifest.debug_elf.as_ref().unwrap().path()),
        )
        .unwrap();

        assert!(Bundle::load(bundle_dir.path()).is_none());
    }

    #[test]
    fn debug_elf_cannot_overwrite_the_initramfs() {
        let initramfs_dir = tempfile::tempdir().unwrap();
        let debug_elf_dir = tempfile::tempdir().unwrap();
        let image_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let initramfs_path = initramfs_dir.path().join("kernel");
        let debug_elf_path = debug_elf_dir.path().join("kernel");
        let image_path = image_dir.path().join("kernel.Image");
        fs::write(&initramfs_path, b"initramfs").unwrap();
        fs::write(&debug_elf_path, b"\x7fELF").unwrap();
        fs::write(&image_path, b"Image").unwrap();
        let mut stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        stored_config.run.boot.initramfs = Some(initramfs_path.clone());
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        bundle.consume_aster_bin(riscv_image(image_path));

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            bundle.consume_debug_elf(debug_elf(debug_elf_path));
        }));

        assert!(result.is_err());
        assert_eq!(
            fs::read(bundle_dir.path().join("kernel")).unwrap(),
            b"initramfs"
        );
        assert_eq!(fs::read(&initramfs_path).unwrap(), b"initramfs");
    }

    #[test]
    fn boot_artifact_cannot_overwrite_the_initramfs() {
        let initramfs_dir = tempfile::tempdir().unwrap();
        let image_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let initramfs_path = initramfs_dir.path().join("kernel.Image");
        let image_path = image_dir.path().join("kernel.Image");
        fs::write(&initramfs_path, b"initramfs").unwrap();
        fs::write(&image_path, b"Image").unwrap();
        let mut stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        stored_config.run.boot.initramfs = Some(initramfs_path.clone());
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            bundle.consume_aster_bin(riscv_image(image_path));
        }));

        assert!(result.is_err());
        assert_eq!(
            fs::read(bundle_dir.path().join("kernel.Image")).unwrap(),
            b"initramfs"
        );
        assert_eq!(fs::read(&initramfs_path).unwrap(), b"initramfs");
    }

    #[test]
    fn vm_image_cannot_overwrite_the_initramfs() {
        let initramfs_dir = tempfile::tempdir().unwrap();
        let vm_image_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let initramfs_path = initramfs_dir.path().join("kernel.iso");
        let vm_image_path = vm_image_dir.path().join("kernel.iso");
        fs::write(&initramfs_path, b"initramfs").unwrap();
        fs::write(&vm_image_path, b"VM image").unwrap();
        let mut stored_config = config(Arch::X86_64, BootMethod::GrubRescueIso);
        stored_config.run.boot.initramfs = Some(initramfs_path.clone());
        let mut bundle = Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        let vm_image = AsterVmImage::new(
            vm_image_path,
            AsterVmImageType::GrubIso(AsterGrubIsoImageMeta {
                grub_version: String::new(),
            }),
            String::new(),
        );

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            bundle.consume_vm_image(vm_image);
        }));

        assert!(result.is_err());
        assert_eq!(
            fs::read(bundle_dir.path().join("kernel.iso")).unwrap(),
            b"initramfs"
        );
        assert_eq!(fs::read(&initramfs_path).unwrap(), b"initramfs");
    }

    #[test]
    fn manifest_cannot_overwrite_the_initramfs() {
        let initramfs_dir = tempfile::tempdir().unwrap();
        let bundle_dir = tempfile::tempdir().unwrap();
        let initramfs_path = initramfs_dir.path().join("bundle.toml");
        fs::write(&initramfs_path, b"initramfs").unwrap();
        let mut stored_config = config(Arch::RiscV64, BootMethod::QemuDirect);
        stored_config.run.boot.initramfs = Some(initramfs_path.clone());

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            Bundle::new(bundle_dir.path(), &stored_config, ActionChoice::Run);
        }));

        assert!(result.is_err());
        assert_eq!(fs::read(initramfs_path).unwrap(), b"initramfs");
    }
}
