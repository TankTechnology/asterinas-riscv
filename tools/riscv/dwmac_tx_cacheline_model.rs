// SPDX-License-Identifier: MPL-2.0

//! Reduced model of CPU/DMA ownership sharing one non-coherent cache line.

const DESCRIPTORS_PER_LINE: usize = 4;

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
