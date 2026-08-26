// SPDX-License-Identifier: MPL-2.0

//! DWMAC4/5 queue-zero register layout and checked fields.

const RX_BUFFER_SIZE: RegisterField = RegisterField::new(0x0000_7ffe, 1);

/// A word-aligned MMIO register offset from the DWMAC base.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RegisterOffset(u32);

impl RegisterOffset {
    const fn new(offset: u32) -> Self {
        assert!(offset.is_multiple_of(4));
        Self(offset)
    }

    /// Returns the byte offset from the controller MMIO base.
    pub const fn offset(self) -> u32 {
        self.0
    }
}

/// A rejected DWMAC register encoding.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RegisterValueError {
    OutOfRange,
}

/// A contiguous bit field in a 32-bit DWMAC register.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RegisterField {
    mask: u32,
    shift: u32,
}

impl RegisterField {
    /// Describes a register field with its shifted mask and bit position.
    pub const fn new(mask: u32, shift: u32) -> Self {
        assert!(mask != 0 && shift < 32 && mask.trailing_zeros() == shift);
        Self { mask, shift }
    }

    /// Encodes a value after proving that it fits in the field.
    pub const fn encode(self, value: u32) -> Result<u32, RegisterValueError> {
        let maximum = self.mask >> self.shift;
        if value > maximum {
            return Err(RegisterValueError::OutOfRange);
        }
        Ok((value << self.shift) & self.mask)
    }

    /// Replaces this field in an existing register value.
    pub fn replace(self, register: u32, value: u32) -> Result<u32, RegisterValueError> {
        let encoded = self.encode(value)?;
        Ok((register & !self.mask) | encoded)
    }
}

pub const MAC_CONFIGURATION: RegisterOffset = RegisterOffset::new(0x0000);
pub const MAC_PACKET_FILTER: RegisterOffset = RegisterOffset::new(0x0008);
pub const MAC_INTERRUPT_STATUS: RegisterOffset = RegisterOffset::new(0x00b0);
pub const MAC_INTERRUPT_ENABLE: RegisterOffset = RegisterOffset::new(0x00b4);
pub const MAC_MDIO_ADDRESS: RegisterOffset = RegisterOffset::new(0x0200);
pub const MAC_MDIO_DATA: RegisterOffset = RegisterOffset::new(0x0204);
pub const MAC_ADDRESS0_HIGH: RegisterOffset = RegisterOffset::new(0x0300);
pub const MAC_ADDRESS0_LOW: RegisterOffset = RegisterOffset::new(0x0304);
pub const DMA_MODE: RegisterOffset = RegisterOffset::new(0x1000);
pub const DMA_SYSTEM_BUS_MODE: RegisterOffset = RegisterOffset::new(0x1004);
pub const DMA_STATUS: RegisterOffset = RegisterOffset::new(0x1008);
pub const DMA_AXI_BUS_MODE: RegisterOffset = RegisterOffset::new(0x1028);
pub const DMA_CHANNEL0_CONTROL: RegisterOffset = RegisterOffset::new(0x1100);
pub const DMA_CHANNEL0_TX_CONTROL: RegisterOffset = RegisterOffset::new(0x1104);
pub const DMA_CHANNEL0_RX_CONTROL: RegisterOffset = RegisterOffset::new(0x1108);
pub const DMA_CHANNEL0_TX_DESCRIPTOR_LIST_HIGH: RegisterOffset = RegisterOffset::new(0x1110);
pub const DMA_CHANNEL0_TX_DESCRIPTOR_LIST: RegisterOffset = RegisterOffset::new(0x1114);
pub const DMA_CHANNEL0_RX_DESCRIPTOR_LIST_HIGH: RegisterOffset = RegisterOffset::new(0x1118);
pub const DMA_CHANNEL0_RX_DESCRIPTOR_LIST: RegisterOffset = RegisterOffset::new(0x111c);
pub const DMA_CHANNEL0_TX_TAIL_POINTER: RegisterOffset = RegisterOffset::new(0x1120);
pub const DMA_CHANNEL0_RX_TAIL_POINTER: RegisterOffset = RegisterOffset::new(0x1128);
pub const DMA_CHANNEL0_TX_RING_LENGTH: RegisterOffset = RegisterOffset::new(0x112c);
pub const DMA_CHANNEL0_RX_RING_LENGTH: RegisterOffset = RegisterOffset::new(0x1130);
pub const DMA_CHANNEL0_INTERRUPT_ENABLE: RegisterOffset = RegisterOffset::new(0x1134);
pub const DMA_CHANNEL0_STATUS: RegisterOffset = RegisterOffset::new(0x1160);

/// Encodes a descriptor count for the DWMAC ring-length register.
pub fn encode_ring_length(entries: usize) -> Result<u32, RegisterValueError> {
    let encoded = entries
        .checked_sub(1)
        .and_then(|value| u16::try_from(value).ok())
        .ok_or(RegisterValueError::OutOfRange)?;
    Ok(u32::from(encoded))
}

/// Encodes a receive buffer capacity for the channel receive-control register.
pub fn encode_rx_buffer_size(capacity: usize) -> Result<u32, RegisterValueError> {
    let capacity = u32::try_from(capacity).map_err(|_| RegisterValueError::OutOfRange)?;
    if capacity == 0 {
        return Err(RegisterValueError::OutOfRange);
    }
    RX_BUFFER_SIZE.encode(capacity)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn queue_zero_register_offsets_match_the_dwmac4_layout() {
        assert_eq!(MAC_CONFIGURATION.offset(), 0x0000);
        assert_eq!(MAC_MDIO_ADDRESS.offset(), 0x0200);
        assert_eq!(DMA_MODE.offset(), 0x1000);
        assert_eq!(DMA_CHANNEL0_TX_CONTROL.offset(), 0x1104);
        assert_eq!(DMA_CHANNEL0_RX_CONTROL.offset(), 0x1108);
        assert_eq!(DMA_CHANNEL0_TX_DESCRIPTOR_LIST.offset(), 0x1114);
        assert_eq!(DMA_CHANNEL0_RX_DESCRIPTOR_LIST.offset(), 0x111c);
        assert_eq!(DMA_CHANNEL0_STATUS.offset(), 0x1160);
    }

    #[ktest]
    fn checked_field_rejects_values_outside_its_mask() {
        let field = RegisterField::new(0x0000_ff00, 8);
        assert_eq!(field.encode(0xff).unwrap(), 0x0000_ff00);
        assert_eq!(field.encode(0x100), Err(RegisterValueError::OutOfRange));
    }

    #[ktest]
    fn queue_lengths_and_rx_buffer_size_use_hardware_encoding() {
        assert_eq!(encode_ring_length(64).unwrap(), 63);
        assert_eq!(encode_ring_length(0), Err(RegisterValueError::OutOfRange));
        assert_eq!(encode_rx_buffer_size(2048).unwrap(), 2048 << 1);
        assert_eq!(
            encode_rx_buffer_size(1 << 14),
            Err(RegisterValueError::OutOfRange)
        );
    }
}
