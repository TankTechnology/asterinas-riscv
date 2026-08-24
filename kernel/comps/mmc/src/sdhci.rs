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
    ShortBusy,
    Long,
}

/// Direction of a command's data phase.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DataDirection {
    Read,
}

/// A command submitted to the SD host controller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Command {
    pub index: u8,
    pub argument: u32,
    pub response: ResponseType,
    pub data: Option<DataDirection>,
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

    pub const fn read_single_block(lba: u32) -> Self {
        Self::new(17, lba, ResponseType::Short, Some(DataDirection::Read))
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
        }
    }

    /// Encodes the SDHCI command register value.
    pub const fn command_bits(self) -> u16 {
        let response = match self.response {
            ResponseType::None => 0,
            ResponseType::Long => 1,
            ResponseType::Short => 2,
            ResponseType::ShortBusy => 3,
        };
        let checks = match self.response {
            ResponseType::None => 0,
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

        assert_eq!(Command::idle().command_bits(), 0x0000);
        assert_eq!(Command::send_if_cond(0x1aa).command_bits(), (8 << 8) | 0x1a);
        assert_eq!(Command::app_prefix(1).command_bits(), (55 << 8) | 0x1a);
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
    fn eic7700_constants_are_frozen() {
        assert_eq!(eic7700::INTERNAL_CLOCK_STABLE, 0x10010101);
        assert_eq!(eic7700::HOST_VALUE_STABLE, 1);
        assert_eq!(eic7700::SD_PHY_DELAY_CODE, 0x55);
    }
}
