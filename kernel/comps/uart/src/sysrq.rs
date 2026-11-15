// SPDX-License-Identifier: MPL-2.0

//! Serial-line SysRq: a break condition followed by a command character.
//!
//! When userspace is wedged the console has no reader, so nothing in the
//! system can be asked to reboot -- the 2026-11-11 state. The UART still sees
//! a break, though, and a break plus one key is the one command that reaches
//! the kernel without any process: the same path Linux calls SysRq.
//!
//! The window is deliberately short and the key set deliberately small. A
//! stray break (adapter re-enumeration, line noise) must not turn the next
//! byte of ordinary console traffic into a reboot, and the command character
//! is consumed rather than delivered so the shell never sees it.

use core::sync::atomic::{AtomicU64, Ordering};

/// How long after a break its command character is accepted.
pub(crate) const WINDOW_MS: u64 = 2_000;

/// What a break-and-key pair asks the kernel to do.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Action {
    Restart,
    PowerOff,
}

/// Monotonic milliseconds from the architecture timer.
pub(crate) fn now_ms() -> u64 {
    let frequency = ostd::arch::tsc_freq();
    if frequency == 0 {
        return 0;
    }
    ostd::arch::read_tsc() / (frequency / 1000).max(1)
}

/// The armed-until timestamp, or zero when disarmed.
static ARMED_UNTIL_MS: AtomicU64 = AtomicU64::new(0);

/// Arms the command window after a break condition was observed.
pub(crate) fn note_break(now_ms: u64) {
    // A break can arrive without a byte (RLSI only), so arming must not depend
    // on the receive batch being non-empty.
    ARMED_UNTIL_MS.store(now_ms.saturating_add(WINDOW_MS).max(1), Ordering::Release);
}

/// Maps the key after a break to its action; keys outside the set are ordinary.
fn action_for(key: u8) -> Option<Action> {
    match key {
        b'b' => Some(Action::Restart),
        b'o' | b'h' => Some(Action::PowerOff),
        _ => None,
    }
}

/// Filters one received batch against a pending break.
///
/// Returns how many leading bytes of `buf` the caller may deliver, and the
/// action to take (if any). A recognized command is consumed: the caller must
/// not deliver it. An unrecognized byte cancels the window and is delivered
/// exactly as typed.
pub(crate) fn filter(buf: &mut [u8], len: usize, now_ms: u64) -> (usize, Option<Action>) {
    let armed_until = ARMED_UNTIL_MS.load(Ordering::Acquire);
    if armed_until == 0 {
        return (len, None);
    }
    if now_ms > armed_until {
        ARMED_UNTIL_MS.store(0, Ordering::Release);
        return (len, None);
    }
    let Some(&first) = buf.first() else {
        // The break arrived alone; keep waiting for its key.
        return (0, None);
    };

    // One key per break: whatever follows is normal terminal input.
    ARMED_UNTIL_MS.store(0, Ordering::Release);
    match action_for(first) {
        Some(action) => (0, Some(action)),
        None => (len, None),
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn reset() {
        ARMED_UNTIL_MS.store(0, Ordering::Release);
    }

    #[ktest]
    fn a_break_without_a_key_keeps_the_window_open() {
        reset();
        note_break(1_000);
        let mut empty: [u8; 1] = [0];
        assert_eq!(filter(&mut empty[..0], 0, 1_100), (0, None));

        let mut key = [b'b'];
        assert_eq!(filter(&mut key, 1, 1_200), (0, Some(Action::Restart)));
    }

    #[ktest]
    fn b_restarts_and_o_or_h_powers_off() {
        for (key, action) in [
            (b'b', Action::Restart),
            (b'o', Action::PowerOff),
            (b'h', Action::PowerOff),
        ] {
            reset();
            note_break(0);
            let mut buf = [key];
            assert_eq!(
                filter(&mut buf, 1, 500),
                (0, Some(action)),
                "key {}",
                key as char
            );
        }
    }

    #[ktest]
    fn an_ordinary_byte_cancels_the_window_and_is_delivered() {
        reset();
        note_break(1_000);
        let mut buf = [b'x'];
        assert_eq!(filter(&mut buf, 1, 1_100), (1, None));

        // The window is one-shot: a later 'b' is ordinary input.
        let mut later = [b'b'];
        assert_eq!(filter(&mut later, 1, 1_200), (1, None));
    }

    #[ktest]
    fn a_stale_break_never_consumes_anything() {
        reset();
        note_break(1_000);
        let mut buf = [b'b'];
        assert_eq!(filter(&mut buf, 1, 1_000 + WINDOW_MS + 1), (1, None));
    }

    #[ktest]
    fn without_a_break_every_byte_flows_through() {
        reset();
        let mut buf = [b'b'];
        assert_eq!(filter(&mut buf, 1, 1_000), (1, None));
    }

    #[ktest]
    fn the_window_is_measured_from_the_break() {
        reset();
        note_break(10_000);
        let mut buf = [b'b'];
        // Last millisecond still inside, first one outside.
        assert_eq!(
            filter(&mut buf, 1, 10_000 + WINDOW_MS),
            (0, Some(Action::Restart))
        );
        reset();
        note_break(10_000);
        assert_eq!(filter(&mut buf, 1, 10_001 + WINDOW_MS), (1, None));
    }
}
