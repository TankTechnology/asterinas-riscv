// SPDX-License-Identifier: MPL-2.0

//! Deterministic bounded selection of a linked GMAC port.

use core::{cmp, time::Duration};

use crate::phy::{Deadline, LinkState, MdioBus, MdioError, read_link_state};

pub const LINK_POLL_INTERVAL_NS: u64 = 50_000_000;
pub const MAX_LINK_SELECTION_NS: u64 = 3_000_000_000;

/// A monotonic clock used to bound port discovery.
pub trait MonotonicClock {
    fn now_nanoseconds(&self) -> u64;
    fn wait_until(&mut self, target_nanoseconds: u64);
}

/// One complete port candidate with its independent MDIO bus.
pub struct PortCandidate<'a> {
    alias_index: u8,
    phy_address: u8,
    bus: &'a mut dyn MdioBus,
}

impl<'a> PortCandidate<'a> {
    /// Creates a linked-port candidate.
    pub fn new(alias_index: u8, phy_address: u8, bus: &'a mut dyn MdioBus) -> Self {
        Self {
            alias_index,
            phy_address,
            bus,
        }
    }
}

/// The deterministic result of dual-port link discovery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SelectedPort {
    alias_index: u8,
    phy_address: u8,
    link_state: LinkState,
}

impl SelectedPort {
    /// Returns the selected device-tree alias index.
    pub const fn alias_index(self) -> u8 {
        self.alias_index
    }

    /// Returns the selected PHY address.
    pub const fn phy_address(self) -> u8 {
        self.phy_address
    }

    /// Returns the link state observed during selection.
    pub const fn link_state(self) -> LinkState {
        self.link_state
    }
}

/// A bounded dual-port selection failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SelectError {
    DeadlineOverflow,
    InvalidCandidates,
    InvalidTimeout,
    Mdio { alias_index: u8, error: MdioError },
    NoLink,
}

/// Selects the lowest-alias linked port before the supplied timeout expires.
pub fn select_linked_port(
    candidates: &mut [PortCandidate<'_>],
    clock: &mut dyn MonotonicClock,
    timeout: Duration,
) -> Result<SelectedPort, SelectError> {
    if candidates.len() != 2 || candidates[0].alias_index == candidates[1].alias_index {
        return Err(SelectError::InvalidCandidates);
    }
    let timeout_ns =
        u64::try_from(timeout.as_nanos()).map_err(|_| SelectError::DeadlineOverflow)?;
    if timeout_ns > MAX_LINK_SELECTION_NS {
        return Err(SelectError::InvalidTimeout);
    }
    let started_at = clock.now_nanoseconds();
    let deadline_ns = started_at
        .checked_add(timeout_ns)
        .ok_or(SelectError::DeadlineOverflow)?;
    let maximum_polls = timeout_ns.div_ceil(LINK_POLL_INTERVAL_NS).saturating_add(1);

    for _ in 0..maximum_polls {
        let now_ns = clock.now_nanoseconds();
        if now_ns >= deadline_ns {
            return Err(SelectError::NoLink);
        }

        let mut selected: Option<SelectedPort> = None;
        for candidate in candidates.iter_mut() {
            let link_state = read_link_state(
                candidate.bus,
                candidate.phy_address,
                Deadline::from_nanoseconds(deadline_ns),
            )
            .map_err(|error| SelectError::Mdio {
                alias_index: candidate.alias_index,
                error,
            })?;
            let Some(link_state) = link_state else {
                continue;
            };
            if selected
                .map(|port| port.alias_index <= candidate.alias_index)
                .unwrap_or(false)
            {
                continue;
            }
            selected = Some(SelectedPort {
                alias_index: candidate.alias_index,
                phy_address: candidate.phy_address,
                link_state,
            });
        }
        if let Some(selected) = selected {
            return Ok(selected);
        }

        let next_poll_ns = now_ns
            .checked_add(LINK_POLL_INTERVAL_NS)
            .map(|next| cmp::min(next, deadline_ns))
            .unwrap_or(deadline_ns);
        clock.wait_until(next_poll_ns);
    }
    Err(SelectError::NoLink)
}

#[cfg(ktest)]
mod tests {
    extern crate alloc;

    use alloc::{collections::VecDeque, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;
    use crate::phy::{BMCR_FULL_DUPLEX, BMCR_SPEED1000, BMSR_LINK_STATUS, MII_BMSR};

    struct FakeMdio {
        reads: VecDeque<Result<u16, MdioError>>,
        read_count: usize,
    }

    impl FakeMdio {
        fn new(reads: impl IntoIterator<Item = Result<u16, MdioError>>) -> Self {
            Self {
                reads: reads.into_iter().collect(),
                read_count: 0,
            }
        }

        fn linked() -> Self {
            Self::new([
                Ok(BMSR_LINK_STATUS),
                Ok(BMSR_LINK_STATUS),
                Ok(BMCR_SPEED1000 | BMCR_FULL_DUPLEX),
            ])
        }

        fn down(polls: usize) -> Self {
            Self::new((0..polls * 2).map(|_| Ok(0)))
        }
    }

    impl MdioBus for FakeMdio {
        fn read(
            &mut self,
            _phy_address: u8,
            register: u8,
            _deadline: Deadline,
        ) -> Result<u16, MdioError> {
            if self.read_count % 2 < 2 {
                assert!(register == MII_BMSR || self.read_count >= 2);
            }
            self.read_count += 1;
            self.reads.pop_front().unwrap()
        }

        fn write(
            &mut self,
            _phy_address: u8,
            _register: u8,
            _value: u16,
            _deadline: Deadline,
        ) -> Result<(), MdioError> {
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeClock {
        now_nanoseconds: u64,
        waits: Vec<u64>,
    }

    impl MonotonicClock for FakeClock {
        fn now_nanoseconds(&self) -> u64 {
            self.now_nanoseconds
        }

        fn wait_until(&mut self, target_nanoseconds: u64) {
            self.waits.push(target_nanoseconds);
            self.now_nanoseconds = target_nanoseconds;
        }
    }

    #[ktest]
    fn one_link_selects_its_port() {
        let mut port0 = FakeMdio::down(1);
        let mut port1 = FakeMdio::linked();
        let mut candidates = [
            PortCandidate::new(0, 0, &mut port0),
            PortCandidate::new(1, 0, &mut port1),
        ];

        let selected = select_linked_port(
            &mut candidates,
            &mut FakeClock::default(),
            Duration::from_secs(3),
        )
        .unwrap();

        assert_eq!(selected.alias_index(), 1);
        assert_eq!(selected.link_state().speed_mbps(), 1000);
    }

    #[ktest]
    fn lowest_alias_wins_when_both_links_are_up() {
        let mut port1 = FakeMdio::linked();
        let mut port0 = FakeMdio::linked();
        let mut candidates = [
            PortCandidate::new(1, 0, &mut port1),
            PortCandidate::new(0, 0, &mut port0),
        ];

        let selected = select_linked_port(
            &mut candidates,
            &mut FakeClock::default(),
            Duration::from_secs(3),
        )
        .unwrap();

        assert_eq!(selected.alias_index(), 0);
    }

    #[ktest]
    fn exact_deadline_is_not_polled_again() {
        let mut port0 = FakeMdio::down(2);
        let mut port1 = FakeMdio::down(2);
        let mut candidates = [
            PortCandidate::new(0, 0, &mut port0),
            PortCandidate::new(1, 0, &mut port1),
        ];
        let mut clock = FakeClock::default();

        assert_eq!(
            select_linked_port(
                &mut candidates,
                &mut clock,
                Duration::from_nanos(2 * LINK_POLL_INTERVAL_NS),
            ),
            Err(SelectError::NoLink)
        );
        assert_eq!(
            clock.waits,
            [LINK_POLL_INTERVAL_NS, 2 * LINK_POLL_INTERVAL_NS]
        );
        assert_eq!(port0.read_count, 4);
        assert_eq!(port1.read_count, 4);
    }

    #[ktest]
    fn mdio_failure_is_not_substituted_with_another_port() {
        let mut broken = FakeMdio::new([Err(MdioError::BusFault)]);
        let mut linked = FakeMdio::linked();
        let mut candidates = [
            PortCandidate::new(0, 0, &mut broken),
            PortCandidate::new(1, 0, &mut linked),
        ];

        assert_eq!(
            select_linked_port(
                &mut candidates,
                &mut FakeClock::default(),
                Duration::from_secs(3),
            ),
            Err(SelectError::Mdio {
                alias_index: 0,
                error: MdioError::BusFault,
            })
        );
    }

    #[ktest]
    fn rejects_a_selection_window_longer_than_the_boot_contract() {
        let mut port0 = FakeMdio::down(1);
        let mut port1 = FakeMdio::down(1);
        let mut candidates = [
            PortCandidate::new(0, 0, &mut port0),
            PortCandidate::new(1, 0, &mut port1),
        ];

        assert_eq!(
            select_linked_port(
                &mut candidates,
                &mut FakeClock::default(),
                Duration::from_nanos(MAX_LINK_SELECTION_NS + 1),
            ),
            Err(SelectError::InvalidTimeout)
        );
    }
}
