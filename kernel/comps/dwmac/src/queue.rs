// SPDX-License-Identifier: MPL-2.0

//! Non-coherent queue-zero DMA rings and their bounded cursor state.

extern crate alloc;

use alloc::{sync::Arc, vec::Vec};

use aster_network::{RxBuffer, TxBuffer, dma_pool::DmaPool};
use ostd::mm::{
    HasDaddr, PAGE_SIZE, VmIo,
    dma::{DmaStream, FromAndToDevice, FromDevice, ToDevice},
};
use spin::Once;

use crate::descriptor::{Descriptor, DescriptorError, DmaAddress};

pub const QUEUE_SIZE: usize = 64;
pub const BUFFER_SIZE: usize = 2048;
pub const POLL_BUDGET: usize = 32;
const TX_RING_OFFSET: usize = 0;
const RX_RING_OFFSET: usize = QUEUE_SIZE * size_of::<Descriptor>();
const RING_BYTES: usize = RX_RING_OFFSET + QUEUE_SIZE * size_of::<Descriptor>();
const MAX_FRAME_SIZE: usize = 1514;
const POOL_INIT_PAGES: usize = 32;
const POOL_HIGH_WATERMARK: usize = 64;

static RX_POOL: Once<Arc<DmaPool<FromDevice>>> = Once::new();
static TX_POOL: Once<Arc<DmaPool<ToDevice>>> = Once::new();

/// A bounded queue-state failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueueError {
    Allocation,
    Descriptor(DescriptorError),
    DmaAccess,
    Full,
    InvalidFrame,
    InvalidCapacity,
    NotReady,
}

impl From<DescriptorError> for QueueError {
    fn from(error: DescriptorError) -> Self {
        Self::Descriptor(error)
    }
}

/// Result of one bounded completion scan.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PollResult {
    pub processed: usize,
    pub more_pending: bool,
}

/// Producer/consumer state shared by the hardware-backed TX and RX rings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RingState {
    capacity: usize,
    producer: usize,
    consumer: usize,
    used: usize,
}

impl RingState {
    /// Creates an empty ring with a nonzero capacity.
    pub const fn new(capacity: usize) -> Self {
        assert!(capacity > 0);
        Self {
            capacity,
            producer: 0,
            consumer: 0,
            used: 0,
        }
    }

    /// Reserves the next producer slot without overwriting live work.
    pub fn reserve(&mut self) -> Result<usize, QueueError> {
        if self.used == self.capacity {
            return Err(QueueError::Full);
        }
        let slot = self.producer;
        self.producer = (self.producer + 1) % self.capacity;
        self.used += 1;
        Ok(slot)
    }

    /// Reclaims the oldest outstanding slot.
    pub fn reclaim_one(&mut self) -> Option<usize> {
        if self.used == 0 {
            return None;
        }
        let slot = self.consumer;
        self.consumer = (self.consumer + 1) % self.capacity;
        self.used -= 1;
        Some(slot)
    }

    /// Returns the oldest outstanding slot without reclaiming it.
    pub const fn consumer(&self) -> Option<usize> {
        if self.used == 0 {
            None
        } else {
            Some(self.consumer)
        }
    }

    /// Reclaims completed slots up to an explicit work budget.
    pub fn reclaim_bounded(
        &mut self,
        budget: usize,
        mut is_complete: impl FnMut(usize) -> bool,
    ) -> PollResult {
        let mut processed = 0;
        while processed < budget && self.used > 0 && is_complete(self.consumer) {
            self.reclaim_one();
            processed += 1;
        }
        PollResult {
            processed,
            more_pending: self.used > 0 && is_complete(self.consumer),
        }
    }

    /// Returns the number of outstanding slots.
    pub const fn used(&self) -> usize {
        self.used
    }
}

/// DMA addresses used to program DWMAC queue zero.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct QueueAddresses {
    pub tx_ring: usize,
    pub rx_ring: usize,
    pub initial_tx_tail: usize,
    pub initial_rx_tail: usize,
}

/// One fresh 64-entry receive/transmit queue pair.
pub(super) struct DmaQueue {
    ring: DmaStream<FromAndToDevice>,
    rx_buffers: Vec<Option<RxBuffer>>,
    tx_buffers: Vec<Option<TxBuffer>>,
    rx_head: usize,
    rx_resume_tail: usize,
    rx_tail_to_write: Option<usize>,
    tx: RingState,
}

impl DmaQueue {
    pub fn new() -> Result<Self, QueueError> {
        const { assert!(RING_BYTES <= PAGE_SIZE) };
        RX_POOL
            .call_once(|| DmaPool::new(BUFFER_SIZE, POOL_INIT_PAGES, POOL_HIGH_WATERMARK, false));
        TX_POOL
            .call_once(|| DmaPool::new(BUFFER_SIZE, POOL_INIT_PAGES, POOL_HIGH_WATERMARK, false));
        let rx_pool = RX_POOL.get().unwrap();
        let ring = DmaStream::alloc(1, false).map_err(|_| QueueError::Allocation)?;
        let rx_ring = ring.daddr() + RX_RING_OFFSET;
        let mut queue = Self {
            ring,
            rx_buffers: (0..QUEUE_SIZE).map(|_| None).collect(),
            tx_buffers: (0..QUEUE_SIZE).map(|_| None).collect(),
            rx_head: 0,
            rx_resume_tail: rx_ring + QUEUE_SIZE * size_of::<Descriptor>(),
            rx_tail_to_write: None,
            tx: RingState::new(QUEUE_SIZE),
        };
        for slot in 0..QUEUE_SIZE {
            let buffer = RxBuffer::new(0, rx_pool).map_err(|_| QueueError::Allocation)?;
            let mut descriptor = Descriptor::zeroed();
            descriptor.publish_rx(DmaAddress::new(buffer.daddr() as u64), BUFFER_SIZE)?;
            queue.write_descriptor(false, slot, &descriptor)?;
            queue.rx_buffers[slot] = Some(buffer);
        }
        queue
            .ring
            .sync_to_device(0..RING_BYTES)
            .map_err(|_| QueueError::DmaAccess)?;
        Ok(queue)
    }

    pub fn addresses(&self) -> QueueAddresses {
        let tx_ring = self.ring.daddr() + TX_RING_OFFSET;
        let rx_ring = self.ring.daddr() + RX_RING_OFFSET;
        QueueAddresses {
            tx_ring,
            rx_ring,
            initial_tx_tail: tx_ring,
            initial_rx_tail: rx_ring + QUEUE_SIZE * size_of::<Descriptor>(),
        }
    }

    pub fn can_send(&self) -> bool {
        self.tx.used() < QUEUE_SIZE
    }

    pub fn can_receive(&self) -> bool {
        self.read_descriptor(false, self.rx_head)
            .is_ok_and(|descriptor| !descriptor.is_owned_by_dma())
    }

    pub fn send(&mut self, packet: &[u8]) -> Result<usize, QueueError> {
        if packet.is_empty() || packet.len() > MAX_FRAME_SIZE {
            return Err(QueueError::InvalidFrame);
        }
        self.reclaim_tx(POLL_BUDGET)?;
        let tx_pool = TX_POOL.get().ok_or(QueueError::Allocation)?;
        let buffer =
            TxBuffer::new(&[0u8; 0], packet, tx_pool).map_err(|_| QueueError::Allocation)?;
        let slot = self.tx.reserve()?;
        if self.tx_buffers[slot].is_some() {
            return Err(QueueError::Full);
        }
        let mut descriptor = Descriptor::zeroed();
        descriptor.publish_tx(DmaAddress::new(buffer.daddr() as u64), packet.len())?;
        self.write_descriptor(true, slot, &descriptor)?;
        self.tx_buffers[slot] = Some(buffer);
        let next = (slot + 1) % QUEUE_SIZE;
        Ok(self.addresses().tx_ring + next * size_of::<Descriptor>())
    }

    pub fn receive(&mut self) -> Result<RxBuffer, QueueError> {
        let slot = self.rx_head;
        let mut descriptor = self.read_descriptor(false, slot)?;
        let length = match descriptor.take_completed_rx(BUFFER_SIZE) {
            Ok(Some(length)) => length,
            Ok(None) => return Err(QueueError::NotReady),
            Err(error) => {
                let address = self.rx_buffers[slot]
                    .as_ref()
                    .ok_or(QueueError::DmaAccess)?
                    .daddr();
                let mut replacement = Descriptor::zeroed();
                replacement.publish_rx(DmaAddress::new(address as u64), BUFFER_SIZE)?;
                self.write_descriptor(false, slot, &replacement)?;
                self.advance_rx();
                return Err(QueueError::Descriptor(error));
            }
        };
        let rx_pool = RX_POOL.get().ok_or(QueueError::Allocation)?;
        let replacement = RxBuffer::new(0, rx_pool).map_err(|_| QueueError::Allocation)?;
        descriptor.publish_rx(DmaAddress::new(replacement.daddr() as u64), BUFFER_SIZE)?;
        self.write_descriptor(false, slot, &descriptor)?;
        let mut completed = self.rx_buffers[slot]
            .replace(replacement)
            .ok_or(QueueError::DmaAccess)?;
        completed.set_payload_len(length);
        self.advance_rx();
        Ok(completed)
    }

    pub fn reclaim_tx(&mut self, budget: usize) -> Result<PollResult, QueueError> {
        let mut processed = 0;
        while processed < budget {
            let Some(slot) = self.tx.consumer() else {
                break;
            };
            let mut descriptor = self.read_descriptor(true, slot)?;
            if !descriptor.reclaim_completed_tx()? {
                break;
            }
            self.write_descriptor(true, slot, &descriptor)?;
            self.tx_buffers[slot].take().ok_or(QueueError::DmaAccess)?;
            self.tx.reclaim_one();
            processed += 1;
        }
        let more_pending = self.tx.consumer().is_some_and(|slot| {
            self.read_descriptor(true, slot)
                .is_ok_and(|descriptor| !descriptor.is_owned_by_dma())
        });
        Ok(PollResult {
            processed,
            more_pending,
        })
    }

    pub fn take_rx_tail(&mut self) -> Option<usize> {
        self.rx_tail_to_write.take()
    }

    pub fn rx_resume_tail(&self) -> usize {
        self.rx_resume_tail
    }

    fn advance_rx(&mut self) {
        (self.rx_head, self.rx_resume_tail) =
            next_rx_position(self.addresses().rx_ring, self.rx_head);
        self.rx_tail_to_write = Some(self.rx_resume_tail);
    }

    fn descriptor_offset(is_tx: bool, slot: usize) -> usize {
        let base = if is_tx {
            TX_RING_OFFSET
        } else {
            RX_RING_OFFSET
        };
        base + slot * size_of::<Descriptor>()
    }

    fn read_descriptor(&self, is_tx: bool, slot: usize) -> Result<Descriptor, QueueError> {
        let offset = Self::descriptor_offset(is_tx, slot);
        self.ring
            .sync_from_device(offset..offset + size_of::<Descriptor>())
            .map_err(|_| QueueError::DmaAccess)?;
        self.ring
            .read_val(offset)
            .map_err(|_| QueueError::DmaAccess)
    }

    fn write_descriptor(
        &self,
        is_tx: bool,
        slot: usize,
        descriptor: &Descriptor,
    ) -> Result<(), QueueError> {
        let offset = Self::descriptor_offset(is_tx, slot);
        self.ring
            .write_val(offset, descriptor)
            .map_err(|_| QueueError::DmaAccess)?;
        self.ring
            .sync_to_device(offset..offset + size_of::<Descriptor>())
            .map_err(|_| QueueError::DmaAccess)
    }
}

fn next_rx_position(rx_ring: usize, current_head: usize) -> (usize, usize) {
    let next_head = (current_head + 1) % QUEUE_SIZE;
    let tail = rx_ring + next_head * size_of::<Descriptor>();
    (next_head, tail)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn ring_wraps_and_reports_full_without_overwrite() {
        let mut ring = RingState::new(64);
        for expected in 0..64 {
            assert_eq!(ring.reserve().unwrap(), expected);
        }
        assert_eq!(ring.reserve(), Err(QueueError::Full));
        for expected in 0..64 {
            assert_eq!(ring.reclaim_one(), Some(expected));
        }
        assert_eq!(ring.reclaim_one(), None);
        assert_eq!(ring.reserve().unwrap(), 0);
    }

    #[ktest]
    fn polling_never_exceeds_its_budget() {
        let mut ring = RingState::new(64);
        for _ in 0..40 {
            ring.reserve().unwrap();
        }

        let result = ring.reclaim_bounded(32, |_| true);

        assert_eq!(result.processed, 32);
        assert!(result.more_pending);
        assert_eq!(ring.used(), 8);
    }

    #[ktest]
    fn receive_tail_wraps_across_three_complete_rings() {
        let rx_ring = 0x8000_0000;
        let mut head = 0;

        for completed in 1..=QUEUE_SIZE * 3 {
            let (next_head, tail) = next_rx_position(rx_ring, head);
            head = next_head;

            assert_eq!(head, completed % QUEUE_SIZE);
            assert_eq!(tail, rx_ring + head * size_of::<Descriptor>());
        }
    }
}
