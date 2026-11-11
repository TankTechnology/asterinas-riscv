// SPDX-License-Identifier: MPL-2.0

//! RISC-V software reboot recovery.
//!
//! A board that hangs during boot (a wrong initramfs mode, a broken driver, a
//! wedged filesystem) is unreachable from every software channel: the console
//! belongs to userspace, and if userspace never starts there is nothing to
//! type at. Physical access is then the only way out.
//!
//! Recovery is therefore on by default on real hardware: the kernel reboots
//! itself if boot has not reached a healthy userspace within the default
//! deadline (900 s), and reboots instead of halting after a fatal abort. A
//! systemd image disarms the deadline at `basic.target` through
//! `/proc/sys/kernel/asterinas_reboot_watchdog`. The table that decides this
//! lives in the `boot-recovery` crate, where it is model-checked with Kani.
//!
//! `asterinas.reboot_after=<seconds>` overrides the deadline (`0` disables
//! it); a systemd image disarms it at `basic.target`, while an initramfs shell
//! that never disarms keeps it armed and therefore reboots by itself.
//! `asterinas.panic_reboot=0|1` overrides the panic policy. QEMU platforms
//! default both off: a reboot loop there breaks tests that are expected to
//! fail and halt, and QEMU never needs hands on the board.

use alloc::{boxed::Box, sync::Arc};
use core::{
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    time::Duration,
};

use aster_time::read_monotonic_time;
use boot_recovery::{Platform, RecoverySettings, resolve};
use ostd::{
    panic,
    power::{self, ExitCode},
    timer,
};
use spin::Once;

use crate::{
    fs::vfs::path::MountNamespace,
    thread::work_queue::{self, work_item::WorkItem},
};

const GRACEFUL_REBOOT_GRACE: Duration = Duration::from_secs(10);

static REBOOT_AFTER_SECONDS: AtomicU32 = AtomicU32::new(boot_recovery::PARAM_UNSET);
static PANIC_REBOOT: AtomicU32 = AtomicU32::new(boot_recovery::PARAM_UNSET);
static RECOVERY_STATE: RecoveryState = RecoveryState::new();
static GRACEFUL_REBOOT_WORK: Once<Arc<WorkItem>> = Once::new();
static PANIC_RESTART_ENABLED: AtomicBool = AtomicBool::new(false);

aster_cmdline::define_kv_param!("asterinas.reboot_after", REBOOT_AFTER_SECONDS);
aster_cmdline::define_kv_param!("asterinas.panic_reboot", PANIC_REBOOT);

/// The policy installed into the fatal-abort path.
///
/// It must not allocate, lock, log, wait, or panic: it runs in panic context.
/// It covers both recovery paths, so a panic during the boot-deadline window
/// restarts even when `asterinas.panic_reboot` is off.
fn recovery_shall_restart() -> bool {
    PANIC_RESTART_ENABLED.load(Ordering::Relaxed) || is_armed()
}

/// Returns where the kernel runs, as far as recovery is concerned.
///
/// QEMU's `virt` machine identifies itself as `riscv-virtio,qemu`; real
/// boards (the Megrez reports `eswin,eic7700`) do not.
fn platform() -> Platform {
    let Some(fdt) = ostd::arch::boot::DEVICE_TREE.get() else {
        return Platform::Hardware;
    };
    let matches = |name: &str| {
        fdt.root().property(name).is_some_and(|property| {
            core::str::from_utf8(property.value)
                .is_ok_and(|text| text.contains("qemu") || text.contains("riscv-virtio"))
        })
    };
    if matches("model") || matches("compatible") {
        Platform::Virtual
    } else {
        Platform::Hardware
    }
}

pub(super) fn arm_if_requested() {
    let settings: RecoverySettings = resolve(
        REBOOT_AFTER_SECONDS.load(Ordering::Relaxed),
        PANIC_REBOOT.load(Ordering::Relaxed),
        platform(),
    );

    PANIC_RESTART_ENABLED.store(settings.panic_restart, Ordering::Release);
    if settings.arms_anything() {
        // One policy covers both recovery paths. Installing it a second time
        // used to fail and leave the deadline unarmed, so install it once and
        // let it consult the flags at abort time instead.
        let installed = panic::inject_fatal_abort_restart_policy(recovery_shall_restart);
        if settings.panic_restart {
            if installed {
                ostd::early_println!(
                    "ASTERINAS_PANIC_REBOOT_ARMED source={}",
                    source_label(settings.panic_from_default),
                );
            } else {
                ostd::warn!("fatal-abort restart policy was already installed");
            }
        }
    }

    let now = read_monotonic_time();
    let Some(deadline) = deadline_after_seconds(now, settings.reboot_after_seconds) else {
        ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_DISABLED");
        return;
    };

    GRACEFUL_REBOOT_WORK.call_once(|| WorkItem::new(Box::new(graceful_reboot)));
    RECOVERY_STATE.freeze_deadline(deadline);
    timer::register_high_resolution_callback_on_cpu(on_timer_interrupt);
    RECOVERY_STATE.publish_armed();
    timer::request_interrupt_after(deadline.saturating_sub(read_monotonic_time()));

    ostd::early_println!(
        "ASTERINAS_SOFTWARE_REBOOT_ARMED seconds={} source={}",
        settings.reboot_after_seconds,
        source_label(settings.deadline_from_default),
    );
}

fn source_label(from_default: bool) -> &'static str {
    if from_default { "default" } else { "cmdline" }
}

pub(crate) fn is_armed() -> bool {
    RECOVERY_STATE.is_armed()
}

pub(crate) fn disarm() -> bool {
    let was_armed = RECOVERY_STATE.disarm();
    if was_armed {
        ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_DISARMED");
    }
    was_armed
}

fn on_timer_interrupt() {
    let Some(action) = timer_action(&RECOVERY_STATE, read_monotonic_time()) else {
        return;
    };
    match action {
        TimerAction::BeginGraceful => {
            if !RECOVERY_STATE.try_begin_graceful(read_monotonic_time()) {
                return;
            }
            let Some(work) = GRACEFUL_REBOOT_WORK.get() else {
                if RECOVERY_STATE.is_armed() {
                    power::emergency_restart(ExitCode::Failure);
                }
                return;
            };
            if !work_queue::try_submit_high_priority_work_item(work.clone()) {
                if RECOVERY_STATE.is_armed() {
                    power::emergency_restart(ExitCode::Failure);
                }
                return;
            }
            timer::request_interrupt_after(GRACEFUL_REBOOT_GRACE);
        }
        TimerAction::Emergency => {
            if RECOVERY_STATE.is_armed() {
                power::emergency_restart(ExitCode::Failure);
            }
        }
        TimerAction::Rearm(remaining) => {
            // Another one-shot timer may expire before the recovery deadline.
            // Re-arm the shared hardware deadline for the remaining interval.
            timer::request_interrupt_after(remaining);
        }
    }
}

fn graceful_reboot() {
    if !RECOVERY_STATE.is_armed() {
        return;
    }
    ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_SYNC_START");
    if let Err(error) = MountNamespace::get_init_singleton().sync() {
        ostd::error!("software reboot filesystem sync failed: {:?}", error);
        return;
    }
    if RECOVERY_STATE.is_armed() {
        ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_SYNC_COMPLETE");
        power::restart(ExitCode::Failure);
    }
}

#[derive(Debug, PartialEq)]
enum TimerAction {
    BeginGraceful,
    Emergency,
    Rearm(Duration),
}

fn timer_action(state: &RecoveryState, now: Duration) -> Option<TimerAction> {
    let deadline = state.armed_deadline()?;
    let next_deadline = if state.graceful_started() {
        state
            .graceful_fallback_deadline()
            .unwrap_or(deadline.saturating_add(GRACEFUL_REBOOT_GRACE))
    } else {
        deadline
    };
    let remaining = remaining_before_deadline(now, Some(next_deadline))?;
    if remaining.is_zero() {
        if state.graceful_started() {
            Some(TimerAction::Emergency)
        } else {
            Some(TimerAction::BeginGraceful)
        }
    } else {
        Some(TimerAction::Rearm(remaining))
    }
}

fn deadline_after_seconds(now: Duration, seconds: u32) -> Option<Duration> {
    if seconds == 0 {
        return None;
    }

    Some(now.saturating_add(Duration::from_secs(u64::from(seconds))))
}

fn remaining_before_deadline(now: Duration, deadline: Option<Duration>) -> Option<Duration> {
    deadline.map(|deadline| deadline.saturating_sub(now))
}

struct RecoveryState {
    deadline: Once<Duration>,
    fallback_deadline: Once<Duration>,
    is_armed: AtomicBool,
    graceful_started: AtomicBool,
}

impl RecoveryState {
    const fn new() -> Self {
        Self {
            deadline: Once::new(),
            fallback_deadline: Once::new(),
            is_armed: AtomicBool::new(false),
            graceful_started: AtomicBool::new(false),
        }
    }

    fn freeze_deadline(&self, deadline: Duration) {
        self.deadline.call_once(|| deadline);
    }

    fn publish_armed(&self) {
        debug_assert!(self.deadline.get().is_some());
        self.is_armed.store(true, Ordering::Release);
    }

    fn is_armed(&self) -> bool {
        self.is_armed.load(Ordering::Acquire)
    }

    fn disarm(&self) -> bool {
        self.is_armed.swap(false, Ordering::AcqRel)
    }

    fn graceful_started(&self) -> bool {
        self.graceful_started.load(Ordering::Acquire)
    }

    fn try_begin_graceful(&self, now: Duration) -> bool {
        self.fallback_deadline
            .call_once(|| now.saturating_add(GRACEFUL_REBOOT_GRACE));
        self.graceful_started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    fn graceful_fallback_deadline(&self) -> Option<Duration> {
        self.fallback_deadline.get().copied()
    }

    fn armed_deadline(&self) -> Option<Duration> {
        if !self.is_armed() {
            return None;
        }

        self.deadline.get().copied()
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn zero_seconds_leaves_recovery_disabled() {
        assert!(deadline_after_seconds(Duration::from_secs(10), 0).is_none());
    }

    #[ktest]
    fn nonzero_seconds_creates_future_deadline() {
        let deadline = deadline_after_seconds(Duration::from_secs(10), 2).unwrap();
        assert_eq!(deadline, Duration::from_secs(12));
    }

    #[ktest]
    fn deadline_calculation_saturates() {
        let now = Duration::MAX - Duration::from_millis(500);
        let deadline = deadline_after_seconds(now, 1).unwrap();
        assert_eq!(deadline, Duration::MAX);
    }

    #[ktest]
    fn time_before_deadline_rearms_the_remaining_duration() {
        let deadline = Duration::from_secs(100);
        assert_eq!(
            remaining_before_deadline(Duration::from_secs(99), Some(deadline)),
            Some(Duration::from_secs(1))
        );
    }

    #[ktest]
    fn time_at_deadline_starts_orderly_reboot() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert_eq!(
            timer_action(&state, Duration::from_secs(100)),
            Some(TimerAction::BeginGraceful)
        );
    }

    #[ktest]
    fn orderly_reboot_retains_an_emergency_deadline() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();
        assert!(state.try_begin_graceful(Duration::from_secs(140)));

        assert_eq!(
            timer_action(&state, Duration::from_secs(149)),
            Some(TimerAction::Rearm(Duration::from_secs(1)))
        );
        assert_eq!(
            timer_action(&state, Duration::from_secs(150)),
            Some(TimerAction::Emergency)
        );
    }

    #[ktest]
    fn fatal_restart_is_selected_only_after_recovery_is_armed() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());

        state.publish_armed();
        assert!(state.is_armed());
        assert_eq!(state.armed_deadline(), Some(Duration::from_secs(100)));
    }

    #[ktest]
    fn first_disarm_clears_an_armed_recovery() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert!(state.disarm());
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());
    }

    #[ktest]
    fn repeated_disarm_is_a_one_way_noop() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert!(state.disarm());
        assert!(!state.disarm());
        assert_eq!(state.deadline.get(), Some(&Duration::from_secs(100)));
    }

    #[ktest]
    fn disarm_of_disabled_recovery_is_a_noop() {
        let state = RecoveryState::new();

        assert!(!state.disarm());
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());
    }

    #[ktest]
    fn stale_timer_action_is_ignored_after_disarm() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();
        assert!(state.try_begin_graceful(Duration::from_secs(100)));

        assert!(state.disarm());
        assert_eq!(timer_action(&state, Duration::from_secs(110)), None);
    }
}
