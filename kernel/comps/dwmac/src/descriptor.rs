// SPDX-License-Identifier: MPL-2.0

//! Queue-zero DMA descriptor state transitions.

const DESCRIPTOR_OWN: u32 = 1 << 31;
const PACKET_SIZE_MASK: u32 = 0x7fff;
const RX_BUFFER1_VALID: u32 = 1 << 24;
const RX_ERROR_SUMMARY: u32 = 1 << 15;
const RX_FIRST_DESCRIPTOR: u32 = 1 << 29;
const RX_INTERRUPT_ON_COMPLETION: u32 = 1 << 30;
const RX_LAST_DESCRIPTOR: u32 = 1 << 28;
const TX_BUFFER1_SIZE_MASK: u32 = 0x3fff;
const TX_ERROR_SUMMARY: u32 = 1 << 15;
const TX_FIRST_DESCRIPTOR: u32 = 1 << 29;
const TX_LAST_DESCRIPTOR: u32 = 1 << 28;

/// A DMA-visible address stored in a normal DWMAC descriptor.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DmaAddress(u64);

impl DmaAddress {
    /// Creates a descriptor address from its integer representation.
    pub const fn new(address: u64) -> Self {
        Self(address)
    }
}

/// A rejected DMA descriptor state transition.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DescriptorError {
    FragmentedFrame,
    FrameTooLong,
    InvalidBufferAddress,
    InvalidBufferLength,
    OwnedByDma,
    ReceiveError,
    TransmitError,
}

/// A 16-byte DWMAC4/5 normal DMA descriptor.
#[repr(C, align(16))]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Pod)]
pub struct Descriptor {
    words: [u32; 4],
}

impl Descriptor {
    /// Creates a descriptor with no buffer assigned to it.
    pub const fn zeroed() -> Self {
        Self { words: [0; 4] }
    }

    /// Returns whether the descriptor is currently owned by the DMA engine.
    pub fn is_owned_by_dma(&self) -> bool {
        Self::control_owned_by_dma(self.control_word())
    }

    pub(super) const fn from_parts(body: [u32; 3], control: u32) -> Self {
        Self {
            words: [body[0], body[1], body[2], control],
        }
    }

    pub(super) const fn body_words(&self) -> [u32; 3] {
        [self.words[0], self.words[1], self.words[2]]
    }

    pub(super) const fn control_word(&self) -> u32 {
        self.words[3]
    }

    pub(super) const fn control_owned_by_dma(control: u32) -> bool {
        control & DESCRIPTOR_OWN != 0
    }

    /// Returns the buffer address encoded in the descriptor read format.
    pub fn buffer_address(&self) -> u64 {
        u64::from(self.words[0]) | (u64::from(self.words[1]) << 32)
    }

    /// Publishes one receive buffer to the DMA engine.
    pub fn publish_rx(
        &mut self,
        address: DmaAddress,
        capacity: usize,
    ) -> Result<(), DescriptorError> {
        self.ensure_cpu_owned()?;
        validate_buffer(address, capacity)?;

        self.write_address(address);
        self.words[2] = 0;
        self.words[3] = DESCRIPTOR_OWN | RX_BUFFER1_VALID | RX_INTERRUPT_ON_COMPLETION;
        Ok(())
    }

    /// Publishes one complete frame to the DMA engine for transmission.
    pub fn publish_tx(
        &mut self,
        address: DmaAddress,
        length: usize,
    ) -> Result<(), DescriptorError> {
        self.ensure_cpu_owned()?;
        validate_buffer(address, length)?;

        self.write_address(address);
        let packet_length = length as u32;
        self.words[2] = packet_length;
        self.words[3] = DESCRIPTOR_OWN | TX_FIRST_DESCRIPTOR | TX_LAST_DESCRIPTOR | packet_length;
        Ok(())
    }

    /// Takes one completed single-descriptor receive frame.
    pub fn take_completed_rx(&mut self, capacity: usize) -> Result<Option<usize>, DescriptorError> {
        if self.is_owned_by_dma() {
            return Ok(None);
        }

        let status = self.words[3];
        if status & (RX_FIRST_DESCRIPTOR | RX_LAST_DESCRIPTOR)
            != RX_FIRST_DESCRIPTOR | RX_LAST_DESCRIPTOR
        {
            return Err(DescriptorError::FragmentedFrame);
        }
        if status & RX_ERROR_SUMMARY != 0 {
            return Err(DescriptorError::ReceiveError);
        }
        let frame_length = (status & PACKET_SIZE_MASK) as usize;
        if frame_length == 0 || frame_length > capacity {
            return Err(DescriptorError::FrameTooLong);
        }

        self.words = [0; 4];
        Ok(Some(frame_length))
    }

    /// Reclaims a completed transmit descriptor and clears its state.
    pub fn reclaim_completed_tx(&mut self) -> Result<bool, DescriptorError> {
        if self.is_owned_by_dma() {
            return Ok(false);
        }
        if self.words == [0; 4] {
            return Ok(false);
        }
        if self.words[3] & TX_ERROR_SUMMARY != 0 {
            return Err(DescriptorError::TransmitError);
        }

        self.words = [0; 4];
        Ok(true)
    }

    fn ensure_cpu_owned(&self) -> Result<(), DescriptorError> {
        if self.is_owned_by_dma() {
            Err(DescriptorError::OwnedByDma)
        } else {
            Ok(())
        }
    }

    fn write_address(&mut self, address: DmaAddress) {
        self.words[0] = address.0 as u32;
        self.words[1] = (address.0 >> 32) as u32;
    }
}

fn validate_buffer(address: DmaAddress, length: usize) -> Result<(), DescriptorError> {
    if address.0 == 0 {
        return Err(DescriptorError::InvalidBufferAddress);
    }
    if length == 0 || length > TX_BUFFER1_SIZE_MASK as usize {
        return Err(DescriptorError::InvalidBufferLength);
    }
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn rx_descriptor_is_published_after_its_address() {
        let mut descriptor = Descriptor::zeroed();

        descriptor
            .publish_rx(DmaAddress::new(0x8000_0000), 2048)
            .unwrap();

        assert!(descriptor.is_owned_by_dma());
        assert_eq!(descriptor.buffer_address(), 0x8000_0000);
        assert_ne!(descriptor.words[3] & RX_BUFFER1_VALID, 0);
    }

    #[ktest]
    fn tx_descriptor_describes_one_complete_frame() {
        let mut descriptor = Descriptor::zeroed();

        descriptor
            .publish_tx(DmaAddress::new(0x9000_0000), 1514)
            .unwrap();

        assert!(descriptor.is_owned_by_dma());
        assert_eq!(descriptor.buffer_address(), 0x9000_0000);
        assert_eq!(descriptor.words[2] & TX_BUFFER1_SIZE_MASK, 1514);
        assert_eq!(descriptor.words[3] & PACKET_SIZE_MASK, 1514);
        assert_ne!(descriptor.words[3] & TX_FIRST_DESCRIPTOR, 0);
        assert_ne!(descriptor.words[3] & TX_LAST_DESCRIPTOR, 0);
    }

    #[ktest]
    fn rejects_invalid_buffer_lengths_and_owned_republication() {
        let mut descriptor = Descriptor::zeroed();
        assert_eq!(
            descriptor.publish_rx(DmaAddress::new(0x8000), 0),
            Err(DescriptorError::InvalidBufferLength)
        );
        assert_eq!(
            descriptor.publish_tx(DmaAddress::new(0x8000), 1 << 14),
            Err(DescriptorError::InvalidBufferLength)
        );

        descriptor
            .publish_rx(DmaAddress::new(0x8000), 2048)
            .unwrap();
        assert_eq!(
            descriptor.publish_rx(DmaAddress::new(0x9000), 2048),
            Err(DescriptorError::OwnedByDma)
        );
    }

    #[ktest]
    fn completed_rx_validates_frame_boundaries_and_errors() {
        let mut descriptor = Descriptor {
            words: [0, 0, 0, RX_FIRST_DESCRIPTOR | RX_LAST_DESCRIPTOR | 1500],
        };
        assert_eq!(descriptor.take_completed_rx(2048).unwrap(), Some(1500));

        descriptor.words[3] = RX_FIRST_DESCRIPTOR | RX_LAST_DESCRIPTOR | 4096;
        assert_eq!(
            descriptor.take_completed_rx(2048),
            Err(DescriptorError::FrameTooLong)
        );

        descriptor.words[3] = RX_LAST_DESCRIPTOR | 128;
        assert_eq!(
            descriptor.take_completed_rx(2048),
            Err(DescriptorError::FragmentedFrame)
        );

        descriptor.words[3] = RX_FIRST_DESCRIPTOR | RX_LAST_DESCRIPTOR | RX_ERROR_SUMMARY | 128;
        assert_eq!(
            descriptor.take_completed_rx(2048),
            Err(DescriptorError::ReceiveError)
        );
    }

    #[ktest]
    fn owned_rx_and_tx_completion_are_distinguished() {
        let mut rx = Descriptor {
            words: [0, 0, 0, DESCRIPTOR_OWN],
        };
        assert_eq!(rx.take_completed_rx(2048).unwrap(), None);

        let mut tx = Descriptor::zeroed();
        assert!(!tx.reclaim_completed_tx().unwrap());
        tx.publish_tx(DmaAddress::new(0x9000), 128).unwrap();
        assert!(!tx.reclaim_completed_tx().unwrap());
        tx.words[3] &= !DESCRIPTOR_OWN;
        assert!(tx.reclaim_completed_tx().unwrap());
        assert_eq!(tx.words, [0; 4]);

        tx.words[3] = TX_ERROR_SUMMARY;
        assert_eq!(
            tx.reclaim_completed_tx(),
            Err(DescriptorError::TransmitError)
        );
    }
}
