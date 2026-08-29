// SPDX-License-Identifier: MPL-2.0

//! Reduced model of CPU/DMA ownership sharing one non-coherent cache line.

const DESCRIPTORS_PER_LINE: usize = 4;
const DESCRIPTOR_BYTES: u64 = 16;
const DESCRIPTOR_OWN: u32 = 1 << 31;
const RX_INTERRUPT_ON_COMPLETION: u32 = 1 << 30;
const RX_BUFFER1_VALID: u32 = 1 << 24;
const TX_FIRST_DESCRIPTOR: u32 = 1 << 29;
const TX_LAST_DESCRIPTOR: u32 = 1 << 28;
const DMA_RX_BUFFER_UNAVAILABLE: u32 = 1 << 7;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct NormalDescriptor([u32; 4]);

fn normal_rx_descriptor(address: u64, capacity: usize) -> NormalDescriptor {
    assert_eq!(capacity, 2048);
    NormalDescriptor([
        address as u32,
        (address >> 32) as u32,
        0,
        DESCRIPTOR_OWN | RX_INTERRUPT_ON_COMPLETION | RX_BUFFER1_VALID,
    ])
}

fn normal_tx_descriptor(address: u64, length: usize) -> NormalDescriptor {
    assert!(length <= 0x3fff);
    NormalDescriptor([
        address as u32,
        (address >> 32) as u32,
        length as u32,
        DESCRIPTOR_OWN | TX_FIRST_DESCRIPTOR | TX_LAST_DESCRIPTOR | length as u32,
    ])
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct RingContract {
    length_register: u32,
    initial_tx_tail: u64,
    initial_rx_tail: u64,
    base: u64,
    entries: u64,
}

impl RingContract {
    fn new(base: u64, entries: usize) -> Self {
        let entries = u64::try_from(entries).unwrap();
        assert!(entries > 0);
        Self {
            length_register: u32::try_from(entries - 1).unwrap(),
            initial_tx_tail: base,
            initial_rx_tail: base + entries * DESCRIPTOR_BYTES,
            base,
            entries,
        }
    }

    fn next_tail(self, slot: usize) -> u64 {
        let next = (u64::try_from(slot).unwrap() + 1) % self.entries;
        self.base + next * DESCRIPTOR_BYTES
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct StatusAcknowledgement {
    write_one_to_clear: u32,
    remaining: u32,
    resume_rx: bool,
}

fn acknowledge_channel_status(observed: u32, known: u32) -> StatusAcknowledgement {
    StatusAcknowledgement {
        write_one_to_clear: observed & known,
        remaining: observed & !known,
        resume_rx: observed & DMA_RX_BUFFER_UNAVAILABLE != 0,
    }
}

#[derive(Clone, Copy)]
struct CachedLine {
    memory_own: [bool; DESCRIPTORS_PER_LINE],
    cpu_cache: Option<[bool; DESCRIPTORS_PER_LINE]>,
    dirty: bool,
}

impl CachedLine {
    const fn new() -> Self {
        Self {
            memory_own: [false; DESCRIPTORS_PER_LINE],
            cpu_cache: None,
            dirty: false,
        }
    }

    fn cpu_publish(&mut self, slot: usize) {
        let cache = self.cpu_cache.get_or_insert(self.memory_own);
        cache[slot] = true;
        self.dirty = true;
    }

    fn cpu_flush_line(&mut self) {
        if self.dirty {
            self.memory_own = self.cpu_cache.unwrap();
        }
        self.cpu_cache = None;
        self.dirty = false;
    }

    fn dma_complete(&mut self, slot: usize) {
        self.memory_own[slot] = false;
    }

    fn cpu_observes_owned(&mut self, slot: usize) -> bool {
        self.cpu_cache.get_or_insert(self.memory_own)[slot]
    }
}

#[test]
fn packed_streaming_descriptors_can_lose_a_completion() {
    let mut line = CachedLine::new();
    line.cpu_publish(0);
    line.cpu_flush_line();

    // The CPU prepares the adjacent descriptor before DMA completes slot zero.
    line.cpu_publish(1);
    line.dma_complete(0);
    line.cpu_flush_line();

    assert!(line.cpu_observes_owned(0));
}

#[test]
fn uncached_descriptor_writes_preserve_an_adjacent_completion() {
    let mut memory_own = [false; DESCRIPTORS_PER_LINE];
    memory_own[0] = true;
    memory_own[1] = true;
    memory_own[0] = false;

    assert!(!memory_own[0]);
    assert!(memory_own[1]);
}

#[derive(Clone, Copy, Debug)]
struct PublicationVisibility {
    body: bool,
    own: bool,
    tail: bool,
}

impl PublicationVisibility {
    const fn is_invalid(self) -> bool {
        (self.own && !self.body) || (self.tail && !self.own)
    }
}

#[test]
fn unbarriered_publication_admits_incomplete_dma_work() {
    let possible = PublicationVisibility {
        body: false,
        own: true,
        tail: true,
    };

    assert!(possible.is_invalid());
}

#[test]
fn staged_publication_excludes_incomplete_dma_work() {
    for bits in 0u8..8 {
        let visible = PublicationVisibility {
            body: bits & 1 != 0,
            own: bits & 2 != 0,
            tail: bits & 4 != 0,
        };
        let allowed_by_barriers = (!visible.own || visible.body) && (!visible.tail || visible.own);

        if allowed_by_barriers {
            assert!(!visible.is_invalid(), "invalid visible state: {visible:?}");
        }
    }
}

#[test]
fn completion_read_barrier_excludes_stale_body_after_own_clear() {
    let unbarriered_observation = (false, true);
    assert_eq!(unbarriered_observation, (false, true));

    let observations_with_read_barrier = [(false, false), (true, true)];
    assert!(!observations_with_read_barrier.contains(&unbarriered_observation));
}

#[test]
fn dwmac_5_20_receive_descriptor_preserves_address_and_control_bits() {
    let descriptor = normal_rx_descriptor(0x1_2345_6789, 2048);

    assert_eq!(descriptor.0[0], 0x2345_6789);
    assert_eq!(descriptor.0[1], 1);
    assert_eq!(descriptor.0[2], 0);
    assert_eq!(
        descriptor.0[3],
        DESCRIPTOR_OWN | RX_INTERRUPT_ON_COMPLETION | RX_BUFFER1_VALID
    );
}

#[test]
fn dwmac_5_20_transmit_descriptor_is_one_complete_buffer() {
    let descriptor = normal_tx_descriptor(0x1_2345_6789, 1514);

    assert_eq!(descriptor.0[0], 0x2345_6789);
    assert_eq!(descriptor.0[1], 1);
    assert_eq!(descriptor.0[2], 1514);
    assert_eq!(
        descriptor.0[3],
        DESCRIPTOR_OWN | TX_FIRST_DESCRIPTOR | TX_LAST_DESCRIPTOR | 1514
    );
}

#[test]
fn dwmac_5_20_ring_length_and_tail_boundaries_are_exact() {
    let contract = RingContract::new(0x1_0000_0000, 64);

    assert_eq!(contract.length_register, 63);
    assert_eq!(contract.initial_tx_tail, 0x1_0000_0000);
    assert_eq!(contract.initial_rx_tail, 0x1_0000_0400);
    assert_eq!(contract.next_tail(0), 0x1_0000_0010);
    assert_eq!(contract.next_tail(63), 0x1_0000_0000);
}

#[test]
fn dwmac_5_20_status_acknowledges_only_known_w1c_bits() {
    let known = (1 << 12) | DMA_RX_BUFFER_UNAVAILABLE | (1 << 6);
    let unknown = 1 << 30;
    let acknowledgement = acknowledge_channel_status(known | unknown, known);

    assert_eq!(acknowledgement.write_one_to_clear, known);
    assert_eq!(acknowledgement.remaining, unknown);
    assert!(acknowledgement.resume_rx);
}
