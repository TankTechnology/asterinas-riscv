// SPDX-License-Identifier: MPL-2.0

//! Which recovery net a RISC-V boot gets.
//!
//! A board that hangs before userspace is unreachable from every software
//! channel: the console belongs to userspace, and if userspace never starts
//! there is nothing to type at. The kernel therefore resolves two independent
//! nets before the first user task runs — a boot deadline
//! (`asterinas.reboot_after`) and a fatal-abort restart policy
//! (`asterinas.panic_reboot`). Both are on by default on real hardware; both
//! are off by default on a virtual machine, where a restart loop breaks tests
//! that are expected to fail and halt.
//!
//! The resolution is one small `unsafe`-free function so that its truth table
//! can be model-checked — see `proofs` (run with `cargo kani`) and `tests`
//! below. `kernel/src/boot_reboot.rs` calls it and only arms timers.

#![no_std]

/// The command-line parser leaves this value in place when a key is absent,
/// so "not given" stays distinguishable from an explicit zero.
pub const PARAM_UNSET: u32 = u32::MAX;

/// Recovery deadline used when the command line does not choose one: long
/// enough that a slow cold boot is never mistaken for a hang, short enough
/// that an unattended board comes back without hands.
pub const DEFAULT_REBOOT_AFTER_SECONDS: u32 = 900;

/// Where the kernel runs, as far as recovery is concerned.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Platform {
    /// A physical board. An unattended hang needs hands, so recover by default.
    Hardware,
    /// A virtual machine. A restart loop breaks tests, so stay opt-in.
    Virtual,
}

/// The recovery behaviour after defaults are applied.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RecoverySettings {
    /// Boot deadline in seconds; zero disables the deadline.
    pub reboot_after_seconds: u32,
    /// Whether a fatal abort restarts instead of halting.
    pub panic_restart: bool,
    /// Whether the deadline came from the built-in default.
    pub deadline_from_default: bool,
    /// Whether the panic policy came from the built-in default.
    pub panic_from_default: bool,
}

impl RecoverySettings {
    /// Whether any recovery path is armed.
    pub fn arms_anything(&self) -> bool {
        self.panic_restart || self.reboot_after_seconds != 0
    }
}

/// Resolves the two switches against the platform defaults.
///
/// `reboot_after` and `panic_reboot` are the parsed command-line values, or
/// [`PARAM_UNSET`] when the key was absent.
pub fn resolve(reboot_after: u32, panic_reboot: u32, platform: Platform) -> RecoverySettings {
    let deadline_from_default = reboot_after == PARAM_UNSET;
    let reboot_after_seconds = if deadline_from_default {
        match platform {
            Platform::Hardware => DEFAULT_REBOOT_AFTER_SECONDS,
            Platform::Virtual => 0,
        }
    } else {
        reboot_after
    };

    let panic_from_default = panic_reboot == PARAM_UNSET;
    let panic_restart = if panic_from_default {
        matches!(platform, Platform::Hardware)
    } else {
        panic_reboot != 0
    };

    RecoverySettings {
        reboot_after_seconds,
        panic_restart,
        deadline_from_default,
        panic_from_default,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hardware_defaults_arm_both_nets() {
        let settings = resolve(PARAM_UNSET, PARAM_UNSET, Platform::Hardware);
        assert_eq!(settings.reboot_after_seconds, DEFAULT_REBOOT_AFTER_SECONDS);
        assert!(settings.deadline_from_default);
        assert!(settings.panic_restart);
        assert!(settings.panic_from_default);
    }

    #[test]
    fn virtual_defaults_stay_opt_in() {
        let settings = resolve(PARAM_UNSET, PARAM_UNSET, Platform::Virtual);
        assert_eq!(settings.reboot_after_seconds, 0);
        assert!(!settings.panic_restart);
        assert!(!settings.arms_anything());
    }

    #[test]
    fn explicit_values_win_on_either_platform() {
        let settings = resolve(60, 0, Platform::Virtual);
        assert_eq!(settings.reboot_after_seconds, 60);
        assert!(!settings.deadline_from_default);
        assert!(!settings.panic_restart);
        assert!(!settings.panic_from_default);

        let settings = resolve(0, 1, Platform::Hardware);
        assert_eq!(settings.reboot_after_seconds, 0);
        assert!(!settings.deadline_from_default);
        assert!(settings.panic_restart);
        assert!(!settings.panic_from_default);
    }

    #[test]
    fn disabling_one_switch_keeps_the_other() {
        let settings = resolve(PARAM_UNSET, 0, Platform::Hardware);
        assert_eq!(settings.reboot_after_seconds, DEFAULT_REBOOT_AFTER_SECONDS);
        assert!(!settings.panic_restart);
        assert!(settings.arms_anything());
    }
}

/// Model checking with Kani (`cargo kani -p boot-recovery`).
///
/// The central property is `no_recovery_is_only_reached_by_explicit_requests`:
/// an unattended board cannot lose both nets unless the command line asked for
/// that. That is exactly the failure behind the 2026-11-11 wedge, where a
/// silent command line left a hung boot with nothing to reboot it.
#[cfg(kani)]
mod proofs {
    use super::*;

    /// Kani has no `Arbitrary` instance for our two-valued enum.
    fn any_platform() -> Platform {
        if kani::any::<bool>() {
            Platform::Hardware
        } else {
            Platform::Virtual
        }
    }

    #[kani::proof]
    fn a_silent_command_line_on_hardware_arms_both_nets() {
        let settings = resolve(PARAM_UNSET, PARAM_UNSET, Platform::Hardware);
        assert_eq!(settings.reboot_after_seconds, DEFAULT_REBOOT_AFTER_SECONDS);
        assert!(settings.panic_restart);
    }

    #[kani::proof]
    fn hardware_never_loses_every_net_silently() {
        let settings = resolve(kani::any(), kani::any(), Platform::Hardware);
        if !settings.arms_anything() {
            assert!(!settings.deadline_from_default);
            assert!(!settings.panic_from_default);
        }
    }

    #[kani::proof]
    fn explicit_switches_are_never_overridden() {
        let reboot_after: u32 = kani::any();
        let panic_reboot: u32 = kani::any();
        kani::assume(reboot_after != PARAM_UNSET);
        kani::assume(panic_reboot != PARAM_UNSET);

        let settings = resolve(reboot_after, panic_reboot, any_platform());
        assert_eq!(settings.reboot_after_seconds, reboot_after);
        assert_eq!(settings.panic_restart, panic_reboot != 0);
        assert!(!settings.deadline_from_default);
        assert!(!settings.panic_from_default);
    }

    #[kani::proof]
    fn a_disabled_deadline_on_hardware_is_always_explicit() {
        let reboot_after: u32 = kani::any();
        let settings = resolve(reboot_after, kani::any(), Platform::Hardware);
        if settings.reboot_after_seconds == 0 {
            assert_eq!(reboot_after, 0);
            assert!(!settings.deadline_from_default);
        }
    }

    #[kani::proof]
    fn virtual_defaults_arm_nothing() {
        let settings = resolve(PARAM_UNSET, PARAM_UNSET, Platform::Virtual);
        assert_eq!(settings.reboot_after_seconds, 0);
        assert!(!settings.panic_restart);
    }
}
