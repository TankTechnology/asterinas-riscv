// SPDX-License-Identifier: MPL-2.0

//! Bounded IEEE 802.3 Clause 22 PHY status handling.

pub const MII_BMCR: u8 = 0;
pub const MII_BMSR: u8 = 1;
pub const MII_ADVERTISE: u8 = 4;
pub const MII_LINK_PARTNER_ABILITY: u8 = 5;
pub const MII_CONTROL1000: u8 = 9;
pub const MII_STATUS1000: u8 = 10;

pub const BMCR_SPEED1000: u16 = 1 << 6;
pub const BMCR_FULL_DUPLEX: u16 = 1 << 8;
pub const BMCR_AUTONEG_ENABLE: u16 = 1 << 12;
pub const BMCR_SPEED100: u16 = 1 << 13;
pub const BMSR_LINK_STATUS: u16 = 1 << 2;
pub const BMSR_AUTONEG_COMPLETE: u16 = 1 << 5;
pub const ADVERTISE_10_HALF: u16 = 1 << 5;
pub const ADVERTISE_10_FULL: u16 = 1 << 6;
pub const ADVERTISE_100_HALF: u16 = 1 << 7;
pub const ADVERTISE_100_FULL: u16 = 1 << 8;
pub const ADVERTISE_1000_HALF: u16 = 1 << 8;
pub const ADVERTISE_1000_FULL: u16 = 1 << 9;
pub const ADVERTISE_PAUSE_CAP: u16 = 1 << 10;
pub const ADVERTISE_PAUSE_ASYM: u16 = 1 << 11;
pub const PARTNER_1000_HALF: u16 = 1 << 10;
pub const PARTNER_1000_FULL: u16 = 1 << 11;

/// An absolute monotonic deadline carried through one MDIO operation.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Deadline(u64);

impl Deadline {
    /// Creates a deadline from monotonic nanoseconds.
    pub const fn from_nanoseconds(nanoseconds: u64) -> Self {
        Self(nanoseconds)
    }

    /// Returns the absolute monotonic deadline in nanoseconds.
    pub const fn as_nanoseconds(self) -> u64 {
        self.0
    }
}

/// A bounded Clause 22 transaction or link-decoding failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MdioError {
    BusFault,
    InvalidAddress,
    NoCommonMode,
    TimedOut,
}

/// The bounded MDIO operations required by PHY discovery.
pub trait MdioBus {
    fn read(&mut self, phy_address: u8, register: u8, deadline: Deadline)
    -> Result<u16, MdioError>;

    fn write(
        &mut self,
        phy_address: u8,
        register: u8,
        value: u16,
        deadline: Deadline,
    ) -> Result<(), MdioError>;
}

/// A resolved Ethernet PHY link mode.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LinkState {
    speed_mbps: u16,
    is_full_duplex: bool,
    tx_pause: bool,
    rx_pause: bool,
}

impl LinkState {
    /// Creates a resolved link mode.
    pub const fn new(speed_mbps: u16, is_full_duplex: bool) -> Self {
        Self {
            speed_mbps,
            is_full_duplex,
            tx_pause: false,
            rx_pause: false,
        }
    }

    const fn negotiated(
        speed_mbps: u16,
        is_full_duplex: bool,
        advertisement: u16,
        partner: u16,
    ) -> Self {
        let (tx_pause, rx_pause) = resolve_pause(advertisement, partner, is_full_duplex);
        Self {
            speed_mbps,
            is_full_duplex,
            tx_pause,
            rx_pause,
        }
    }

    /// Returns the negotiated link speed in megabits per second.
    pub const fn speed_mbps(self) -> u16 {
        self.speed_mbps
    }

    /// Returns whether the link uses full-duplex transmission.
    pub const fn is_full_duplex(self) -> bool {
        self.is_full_duplex
    }

    /// Returns whether the MAC may send IEEE 802.3 pause frames.
    pub const fn tx_pause(self) -> bool {
        self.tx_pause
    }

    /// Returns whether the MAC honors received IEEE 802.3 pause frames.
    pub const fn rx_pause(self) -> bool {
        self.rx_pause
    }
}

/// Reads and resolves the current Clause 22 link state.
pub fn read_link_state(
    bus: &mut dyn MdioBus,
    phy_address: u8,
    deadline: Deadline,
) -> Result<Option<LinkState>, MdioError> {
    if phy_address > 31 {
        return Err(MdioError::InvalidAddress);
    }

    bus.read(phy_address, MII_BMSR, deadline)?;
    let basic_status = bus.read(phy_address, MII_BMSR, deadline)?;
    if basic_status & BMSR_LINK_STATUS == 0 {
        return Ok(None);
    }

    let basic_control = bus.read(phy_address, MII_BMCR, deadline)?;
    if basic_control & BMCR_AUTONEG_ENABLE == 0 {
        return decode_forced_mode(basic_control).map(Some);
    }
    if basic_status & BMSR_AUTONEG_COMPLETE == 0 {
        return Ok(None);
    }

    let gigabit_advertisement = bus.read(phy_address, MII_CONTROL1000, deadline)?;
    let gigabit_partner = bus.read(phy_address, MII_STATUS1000, deadline)?;
    let advertisement = bus.read(phy_address, MII_ADVERTISE, deadline)?;
    let partner = bus.read(phy_address, MII_LINK_PARTNER_ABILITY, deadline)?;
    decode_negotiated_mode(
        gigabit_advertisement,
        gigabit_partner,
        advertisement,
        partner,
    )
    .map(Some)
}

fn decode_forced_mode(basic_control: u16) -> Result<LinkState, MdioError> {
    let is_1000 = basic_control & BMCR_SPEED1000 != 0;
    let is_100 = basic_control & BMCR_SPEED100 != 0;
    if is_1000 && is_100 {
        return Err(MdioError::NoCommonMode);
    }
    let speed_mbps = if is_1000 {
        1000
    } else if is_100 {
        100
    } else {
        10
    };
    Ok(LinkState::new(
        speed_mbps,
        basic_control & BMCR_FULL_DUPLEX != 0,
    ))
}

fn decode_negotiated_mode(
    gigabit_advertisement: u16,
    gigabit_partner: u16,
    advertisement: u16,
    partner: u16,
) -> Result<LinkState, MdioError> {
    let common_gigabit = gigabit_advertisement & (gigabit_partner >> 2);
    if common_gigabit & ADVERTISE_1000_FULL != 0 {
        return Ok(LinkState::negotiated(1000, true, advertisement, partner));
    }
    if common_gigabit & ADVERTISE_1000_HALF != 0 {
        return Ok(LinkState::new(1000, false));
    }

    let common = advertisement & partner;
    for (capability, speed_mbps, is_full_duplex) in [
        (ADVERTISE_100_FULL, 100, true),
        (ADVERTISE_100_HALF, 100, false),
        (ADVERTISE_10_FULL, 10, true),
        (ADVERTISE_10_HALF, 10, false),
    ] {
        if common & capability != 0 {
            return Ok(LinkState::negotiated(
                speed_mbps,
                is_full_duplex,
                advertisement,
                partner,
            ));
        }
    }
    Err(MdioError::NoCommonMode)
}

// This is the IEEE 802.3 pause resolution table used by Linux phylib.
// See `linkmode_resolve_pause()` in
// <https://github.com/torvalds/linux/blob/master/drivers/net/phy/linkmode.c>.
const fn resolve_pause(advertisement: u16, partner: u16, is_full_duplex: bool) -> (bool, bool) {
    if !is_full_duplex {
        return (false, false);
    }

    let common = advertisement & partner;
    if common & ADVERTISE_PAUSE_CAP != 0 {
        return (true, true);
    }
    if common & ADVERTISE_PAUSE_ASYM == 0 {
        return (false, false);
    }

    let tx_pause = partner & ADVERTISE_PAUSE_CAP != 0;
    let rx_pause = advertisement & ADVERTISE_PAUSE_CAP != 0;
    (tx_pause, rx_pause)
}

#[cfg(ktest)]
mod tests {
    extern crate alloc;

    use alloc::{collections::VecDeque, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;

    struct FakeMdio {
        reads: VecDeque<Result<u16, MdioError>>,
        calls: Vec<(u8, u8, u64)>,
    }

    impl FakeMdio {
        fn new(reads: impl IntoIterator<Item = u16>) -> Self {
            Self {
                reads: reads.into_iter().map(Ok).collect(),
                calls: Vec::new(),
            }
        }
    }

    impl MdioBus for FakeMdio {
        fn read(
            &mut self,
            phy_address: u8,
            register: u8,
            deadline: Deadline,
        ) -> Result<u16, MdioError> {
            self.calls
                .push((phy_address, register, deadline.as_nanoseconds()));
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

    #[ktest]
    fn bmsr_link_status_is_read_twice_because_it_is_latched_low() {
        let deadline = Deadline::from_nanoseconds(3_000_000_000);
        let mut bus = FakeMdio::new([0, BMSR_LINK_STATUS, BMCR_SPEED1000 | BMCR_FULL_DUPLEX]);

        let state = read_link_state(&mut bus, 0, deadline).unwrap();

        assert_eq!(state, Some(LinkState::new(1000, true)));
        assert_eq!(bus.calls[0..2], [(0, MII_BMSR, 3_000_000_000); 2]);
    }

    #[ktest]
    fn auto_negotiation_decodes_1000_100_and_10_megabit_modes() {
        let cases = [
            (
                [
                    BMSR_LINK_STATUS,
                    BMSR_LINK_STATUS | BMSR_AUTONEG_COMPLETE,
                    BMCR_AUTONEG_ENABLE,
                    ADVERTISE_1000_FULL,
                    PARTNER_1000_FULL,
                    0,
                    0,
                ],
                LinkState::new(1000, true),
            ),
            (
                [
                    BMSR_LINK_STATUS,
                    BMSR_LINK_STATUS | BMSR_AUTONEG_COMPLETE,
                    BMCR_AUTONEG_ENABLE,
                    0,
                    0,
                    ADVERTISE_100_FULL,
                    ADVERTISE_100_FULL,
                ],
                LinkState::new(100, true),
            ),
            (
                [
                    BMSR_LINK_STATUS,
                    BMSR_LINK_STATUS | BMSR_AUTONEG_COMPLETE,
                    BMCR_AUTONEG_ENABLE,
                    0,
                    0,
                    ADVERTISE_10_HALF,
                    ADVERTISE_10_HALF,
                ],
                LinkState::new(10, false),
            ),
        ];

        for (reads, expected) in cases {
            let mut bus = FakeMdio::new(reads);
            assert_eq!(
                read_link_state(&mut bus, 0, Deadline::from_nanoseconds(9)).unwrap(),
                Some(expected)
            );
        }
    }

    #[ktest]
    fn link_down_does_not_read_mode_registers() {
        let mut bus = FakeMdio::new([BMSR_LINK_STATUS, 0]);

        assert_eq!(
            read_link_state(&mut bus, 0, Deadline::from_nanoseconds(10)).unwrap(),
            None
        );
        assert_eq!(bus.calls.len(), 2);
    }

    #[ktest]
    fn rejects_completed_autonegotiation_without_a_common_mode() {
        let mut bus = FakeMdio::new([
            BMSR_LINK_STATUS,
            BMSR_LINK_STATUS | BMSR_AUTONEG_COMPLETE,
            BMCR_AUTONEG_ENABLE,
            0,
            0,
            0,
            0,
        ]);

        assert_eq!(
            read_link_state(&mut bus, 0, Deadline::from_nanoseconds(10)),
            Err(MdioError::NoCommonMode)
        );
    }
}
