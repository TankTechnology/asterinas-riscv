// SPDX-License-Identifier: MPL-2.0

//! SD Host Controller Interface register and command definitions.

/// Standard SDHCI registers used by the bounded PIO implementation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Register {
    BlockSize,
    Argument,
    TransferMode,
    Command,
    Response0,
    BufferData,
    PresentState,
    ClockControl,
    SoftwareReset,
    InterruptStatus,
}

impl Register {
    /// Returns the byte offset from the SDHCI register window.
    pub const fn offset(self) -> usize {
        match self {
            Self::BlockSize => 0x04,
            Self::Argument => 0x08,
            Self::TransferMode => 0x0c,
            Self::Command => 0x0e,
            Self::Response0 => 0x10,
            Self::BufferData => 0x20,
            Self::PresentState => 0x24,
            Self::ClockControl => 0x2c,
            Self::SoftwareReset => 0x2f,
            Self::InterruptStatus => 0x30,
        }
    }
}

/// Response format requested from the host controller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResponseType {
    None,
    Short,
    /// A short response without valid CRC or command-index fields, such as R3.
    ShortNoChecks,
    ShortBusy,
    Long,
}

/// Direction of a command's data phase.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DataDirection {
    Read,
    Write,
}

/// A command submitted to the SD host controller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Command {
    pub index: u8,
    pub argument: u32,
    pub response: ResponseType,
    pub data: Option<DataDirection>,
    block_count: u16,
}

impl Command {
    pub const fn idle() -> Self {
        Self::new(0, 0, ResponseType::None, None)
    }

    pub const fn send_if_cond(argument: u32) -> Self {
        Self::new(8, argument, ResponseType::Short, None)
    }

    pub const fn app_prefix(rca: u16) -> Self {
        Self::new(55, (rca as u32) << 16, ResponseType::Short, None)
    }

    pub const fn app_op_cond(argument: u32) -> Self {
        Self::new(41, argument, ResponseType::ShortNoChecks, None)
    }

    pub const fn read_single_block(lba: u32) -> Self {
        Self::new(17, lba, ResponseType::Short, Some(DataDirection::Read))
    }

    pub const fn write_single_block(lba: u32) -> Self {
        Self::new(24, lba, ResponseType::Short, Some(DataDirection::Write))
    }

    pub const fn read_multiple_blocks(lba: u32, block_count: u16) -> Self {
        Self::new_data(18, lba, DataDirection::Read, block_count)
    }

    pub const fn write_multiple_blocks(lba: u32, block_count: u16) -> Self {
        Self::new_data(25, lba, DataDirection::Write, block_count)
    }

    pub const fn new(
        index: u8,
        argument: u32,
        response: ResponseType,
        data: Option<DataDirection>,
    ) -> Self {
        Self {
            index,
            argument,
            response,
            data,
            block_count: if data.is_some() { 1 } else { 0 },
        }
    }

    const fn new_data(
        index: u8,
        argument: u32,
        direction: DataDirection,
        block_count: u16,
    ) -> Self {
        Self {
            index,
            argument,
            response: ResponseType::Short,
            data: Some(direction),
            block_count,
        }
    }

    pub const fn block_count(self) -> usize {
        self.block_count as usize
    }

    pub const fn has_valid_block_count(self) -> bool {
        match self.data {
            Some(_) => self.block_count != 0,
            None => self.block_count == 0,
        }
    }

    pub const fn transfer_mode_bits(self) -> u16 {
        let direction = match self.data {
            Some(DataDirection::Read) => 1 << 4,
            Some(DataDirection::Write) | None => 0,
        };
        let multiple = if self.block_count > 1 {
            (1 << 5) | (1 << 2) | (1 << 1)
        } else {
            0
        };
        direction | multiple
    }

    /// Encodes the SDHCI command register value.
    pub const fn command_bits(self) -> u16 {
        let response = match self.response {
            ResponseType::None => 0,
            ResponseType::Long => 1,
            ResponseType::Short | ResponseType::ShortNoChecks => 2,
            ResponseType::ShortBusy => 3,
        };
        let checks = match self.response {
            ResponseType::None | ResponseType::ShortNoChecks => 0,
            ResponseType::Long => 1 << 3,
            ResponseType::Short | ResponseType::ShortBusy => (1 << 3) | (1 << 4),
        };
        let data = if self.data.is_some() { 1 << 5 } else { 0 };
        ((self.index as u16) << 8) | data | checks | response
    }
}

/// Stable failures surfaced by the SDHCI protocol core.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HostError {
    Timeout,
    CommandCrc,
    CommandIndex,
    DataCrc,
    DataEndBit,
    Unsupported,
}

/// Error context captured from one command interrupt status value.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct CommandFailure {
    pub index: u8,
    pub status: u32,
    pub error: HostError,
}

/// Error context captured while waiting for one data-phase interrupt.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct DataFailure {
    pub wanted: u32,
    pub status: u32,
    pub error: HostError,
}

/// Returns the highest-priority SDHCI interrupt error represented by `status`.
pub const fn decode_interrupt_error(status: u32) -> Option<HostError> {
    if status & (1 << 16) != 0 {
        Some(HostError::Timeout)
    } else if status & (1 << 17) != 0 {
        Some(HostError::CommandCrc)
    } else if status & (1 << 19) != 0 {
        Some(HostError::CommandIndex)
    } else if status & (1 << 21) != 0 {
        Some(HostError::DataCrc)
    } else if status & (1 << 22) != 0 {
        Some(HostError::DataEndBit)
    } else {
        None
    }
}

pub(crate) const fn decode_command_failure(index: u8, status: u32) -> Option<CommandFailure> {
    match decode_interrupt_error(status) {
        Some(error) => Some(CommandFailure {
            index,
            status,
            error,
        }),
        None => None,
    }
}

pub(crate) const fn decode_data_failure(wanted: u32, status: u32) -> Option<DataFailure> {
    match decode_interrupt_error(status) {
        Some(error) => Some(DataFailure {
            wanted,
            status,
            error,
        }),
        None => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Eic7700CoreClock {
    pub divisor: u16,
    pub select_416mhz: bool,
}

/// Selects the external EIC7700 MSHC core clock, following the vendor driver's
/// `eswin_sdhci_set_core_clock` policy.
pub(crate) const fn eic7700_core_clock_config(
    requested_hz: u32,
) -> Result<Eic7700CoreClock, HostError> {
    if requested_hz == 0 {
        return Err(HostError::Unsupported);
    }
    let (source_hz, select_416mhz) = if 416_000_000 % requested_hz == 0 {
        (416_000_000u32, true)
    } else {
        (400_000_000u32, false)
    };
    let divisor = source_hz.div_ceil(requested_hz);
    if divisor == 0 || divisor > 0x0fff {
        return Err(HostError::Unsupported);
    }
    Ok(Eic7700CoreClock {
        divisor: divisor as u16,
        select_416mhz,
    })
}

/// EIC7700-specific values from the upstream DesignWare MSHC driver.
pub mod eic7700 {
    pub const INTERNAL_CLOCK_STABLE: u32 = (1 << 28) | (1 << 16) | (1 << 8) | 1;
    pub const HOST_VALUE_STABLE: u32 = 1;
    pub const SD_PHY_DELAY_CODE: u8 = 0x55;
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn standard_register_offsets_are_frozen() {
        assert_eq!(Register::BlockSize.offset(), 0x04);
        assert_eq!(Register::Argument.offset(), 0x08);
        assert_eq!(Register::TransferMode.offset(), 0x0c);
        assert_eq!(Register::Command.offset(), 0x0e);
        assert_eq!(Register::Response0.offset(), 0x10);
        assert_eq!(Register::BufferData.offset(), 0x20);
        assert_eq!(Register::PresentState.offset(), 0x24);
        assert_eq!(Register::ClockControl.offset(), 0x2c);
        assert_eq!(Register::SoftwareReset.offset(), 0x2f);
        assert_eq!(Register::InterruptStatus.offset(), 0x30);
    }

    #[ktest]
    fn command_encodings_match_sdhci_contract() {
        let cmd17 = Command::read_single_block(7);
        assert_eq!(cmd17.index, 17);
        assert_eq!(cmd17.argument, 7);
        assert_eq!(cmd17.response, ResponseType::Short);
        assert_eq!(cmd17.data, Some(DataDirection::Read));
        assert_eq!(cmd17.command_bits(), (17 << 8) | 0x3a);

        let cmd24 = Command::write_single_block(9);
        assert_eq!(cmd24.index, 24);
        assert_eq!(cmd24.argument, 9);
        assert_eq!(cmd24.response, ResponseType::Short);
        assert_eq!(cmd24.data, Some(DataDirection::Write));
        assert_eq!(cmd24.command_bits(), (24 << 8) | 0x3a);

        assert_eq!(Command::idle().command_bits(), 0x0000);
        assert_eq!(Command::send_if_cond(0x1aa).command_bits(), (8 << 8) | 0x1a);
        assert_eq!(Command::app_prefix(1).command_bits(), (55 << 8) | 0x1a);
        assert_eq!(
            Command::app_op_cond(0x40ff_8000).command_bits(),
            (41 << 8) | 0x02
        );

        let cmd18 = Command::read_multiple_blocks(11, 8);
        assert_eq!(cmd18.index, 18);
        assert_eq!(cmd18.argument, 11);
        assert_eq!(cmd18.data, Some(DataDirection::Read));
        assert_eq!(cmd18.block_count(), 8);
        assert_eq!(cmd18.command_bits(), (18 << 8) | 0x3a);
        assert_eq!(cmd18.transfer_mode_bits(), 0x36);

        let cmd25 = Command::write_multiple_blocks(19, 8);
        assert_eq!(cmd25.index, 25);
        assert_eq!(cmd25.argument, 19);
        assert_eq!(cmd25.data, Some(DataDirection::Write));
        assert_eq!(cmd25.block_count(), 8);
        assert_eq!(cmd25.command_bits(), (25 << 8) | 0x3a);
        assert_eq!(cmd25.transfer_mode_bits(), 0x26);
        assert_eq!(cmd17.transfer_mode_bits(), 0x10);
        assert_eq!(cmd24.transfer_mode_bits(), 0);
        assert!(cmd18.has_valid_block_count());
        assert!(!Command::read_multiple_blocks(0, 0).has_valid_block_count());
        assert!(Command::idle().has_valid_block_count());
    }

    #[ktest]
    fn eic7700_core_clock_policy_matches_the_vendor_driver() {
        assert_eq!(
            eic7700_core_clock_config(400_000),
            Ok(Eic7700CoreClock {
                divisor: 1040,
                select_416mhz: true,
            })
        );
        assert_eq!(
            eic7700_core_clock_config(25_000_000),
            Ok(Eic7700CoreClock {
                divisor: 16,
                select_416mhz: false,
            })
        );
        assert_eq!(eic7700_core_clock_config(0), Err(HostError::Unsupported));
    }

    #[ktest]
    fn interrupt_errors_are_classified() {
        assert_eq!(decode_interrupt_error(1 << 16), Some(HostError::Timeout));
        assert_eq!(decode_interrupt_error(1 << 17), Some(HostError::CommandCrc));
        assert_eq!(
            decode_interrupt_error(1 << 19),
            Some(HostError::CommandIndex)
        );
        assert_eq!(decode_interrupt_error(1 << 21), Some(HostError::DataCrc));
        assert_eq!(decode_interrupt_error(1 << 22), Some(HostError::DataEndBit));
        assert_eq!(decode_interrupt_error(0), None);
    }

    #[ktest]
    fn command_failures_retain_index_and_raw_status() {
        assert_eq!(
            decode_command_failure(8, (1 << 15) | (1 << 17)),
            Some(CommandFailure {
                index: 8,
                status: (1 << 15) | (1 << 17),
                error: HostError::CommandCrc,
            })
        );
        assert_eq!(decode_command_failure(8, 0), None);
    }

    #[ktest]
    fn data_failures_retain_waited_event_and_raw_status() {
        assert_eq!(
            decode_data_failure(1 << 5, (1 << 15) | (1 << 21)),
            Some(DataFailure {
                wanted: 1 << 5,
                status: (1 << 15) | (1 << 21),
                error: HostError::DataCrc,
            })
        );
        assert_eq!(decode_data_failure(1 << 5, 0), None);
    }

    #[ktest]
    fn eic7700_constants_are_frozen() {
        assert_eq!(eic7700::INTERNAL_CLOCK_STABLE, 0x10010101);
        assert_eq!(eic7700::HOST_VALUE_STABLE, 1);
        assert_eq!(eic7700::SD_PHY_DELAY_CODE, 0x55);
    }
}
