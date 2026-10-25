// SPDX-License-Identifier: MPL-2.0

#![no_std]

extern crate alloc;

/// Log prefix for messages emitted by this crate.
macro_rules! __log_prefix {
    () => {
        "ring-buffer: "
    };
}

use alloc::sync::Arc;
use core::{
    marker::PhantomData,
    num::Wrapping,
    ops::Deref,
    sync::atomic::{AtomicUsize, Ordering},
};

use inherit_methods_macro::inherit_methods;
use ostd::mm::{
    FrameAllocOptions, HasSize, Infallible, PAGE_SIZE, SegmentedSegment, VmIo, VmReader, VmWriter,
    heap::ReservedPoolBuffer,
    io::util::{HasVmReaderWriter, VmReaderWriterIdentity},
};
use ostd_pod::Pod;

/// Requests above this many pages try the reserved heap pool first.
///
/// Only the 64 KiB class (16 pages) is worth taking from the pool; smaller
/// buffers are cheap to allocate contiguously from the buddy allocator.
const POOL_MIN_PAGES: usize = 4;

/// Pages kept free in the shared pool for the heap's own large slots.
const POOL_RESERVE_PAGES: usize = 1024;

static POOL_HITS: AtomicUsize = AtomicUsize::new(0);
static POOL_MISSES: AtomicUsize = AtomicUsize::new(0);

fn log_pool_event(hit: bool, nframes: usize) {
    let (count, what) = if hit {
        (
            POOL_HITS.fetch_add(1, Ordering::Relaxed) + 1,
            "pool hit",
        )
    } else {
        (
            POOL_MISSES.fetch_add(1, Ordering::Relaxed) + 1,
            "pool miss, segmented fallback",
        )
    };
    if count <= 8 || count % 256 == 0 {
        ostd::warn!(
            "{}: pages={} total={}",
            what,
            nframes,
            count
        );
    }
}

/// A zero-length slice used by [`Storage::Empty`].
static EMPTY_BYTES: [u8; 0] = [];

/// Returns a mutable zero-length slice with a `'static` lifetime.
///
/// Only [`Storage::Empty`] uses this, and a capacity-zero ring buffer never
/// writes through it (the push paths short-circuit on `capacity == 0`), so
/// the shared zero-length slice is never aliased for an actual access.
#[allow(static_mut_refs)]
fn empty_writer_slice() -> &'static mut [u8] {
    static mut EMPTY: [u8; 0] = [];
    unsafe { &mut *(&raw mut EMPTY) }
}

/// The backing storage of a ring buffer.
///
/// It is either a segmented allocation (physically scattered, virtually
/// contiguous) or a buffer from the reserved contiguous pool. Callers only
/// need [`HasVmReaderWriter`] and [`HasSize`].
pub enum Storage {
    /// No storage: the last-resort allocation path failed and the buffer was
    /// degraded to capacity zero. It reports size zero, holds no items, and
    /// accepts no pushes.
    Empty,
    /// A segmented allocation.
    Segmented(SegmentedSegment<()>),
    /// A buffer from the reserved heap pool.
    Pooled(ReservedPoolBuffer),
}

impl HasSize for Storage {
    fn size(&self) -> usize {
        match self {
            Self::Empty => 0,
            Self::Segmented(segment) => segment.size(),
            Self::Pooled(buffer) => buffer.size(),
        }
    }
}

impl HasVmReaderWriter for Storage {
    type Types = VmReaderWriterIdentity;

    fn reader(&self) -> VmReader<'_, Infallible> {
        match self {
            Self::Empty => VmReader::from(&EMPTY_BYTES[..]),
            Self::Segmented(segment) => segment.reader(),
            Self::Pooled(buffer) => buffer.reader(),
        }
    }

    fn writer(&self) -> VmWriter<'_, Infallible> {
        match self {
            Self::Empty => VmWriter::from(empty_writer_slice()),
            Self::Segmented(segment) => segment.writer(),
            Self::Pooled(buffer) => buffer.writer(),
        }
    }
}

/// A lock-free single-producer single-consumer (SPSC) FIFO ring buffer.
///
/// This ring buffer is backed by a [`SegmentedSegment<()>`] or a buffer from
/// the reserved heap pool, and provides non-blocking
/// `push`/`pop` and `push_slice`/`pop_slice` operations for `T: Pod` items.
/// It is designed for concurrent use where one thread produces items and
/// another consumes them without requiring locks.
///
/// # Constraints
///
/// - The capacity must be a power of two.
/// - Items must implement the [`Pod`] trait for safe memory operations.
///
/// # Usage Patterns
///
/// For concurrent SPSC usage, call [`split`](Self::split) to obtain a
/// [`Producer`] and [`Consumer`] pair that can be safely used from
/// different threads. For single-threaded usage, the `push`/`pop` methods
/// can be called directly on a mutable reference.
///
/// # Example
///
/// ```ignore
/// use ring_buffer::RingBuffer;
///
/// let rb = RingBuffer::<u8>::new(16);
/// let (mut producer, mut consumer) = rb.split();
///
/// producer.push(42).unwrap();
/// assert_eq!(consumer.pop(), Some(42));
/// ```
pub struct RingBuffer<T> {
    storage: Storage,
    capacity: usize,
    tail: AtomicUsize,
    head: AtomicUsize,
    phantom: PhantomData<T>,
}

/// The producer half of a [`RingBuffer`].
///
/// A `Producer` has exclusive rights to push items into the ring buffer.
/// It can be safely used from one thread while a [`Consumer`] operates
/// on the same ring buffer from another thread.
pub struct Producer<T, R: Deref<Target = RingBuffer<T>>> {
    rb: R,
    phantom: PhantomData<T>,
}

/// The consumer half of a [`RingBuffer`].
///
/// A `Consumer` has exclusive rights to pop items from the ring buffer.
/// It can be safely used from one thread while a [`Producer`] operates
/// on the same ring buffer from another thread.
pub struct Consumer<T, R: Deref<Target = RingBuffer<T>>> {
    rb: R,
    phantom: PhantomData<T>,
}

/// A producer backed by an `Arc<RingBuffer<T>>`.
pub type RbProducer<T> = Producer<T, Arc<RingBuffer<T>>>;

/// A consumer backed by an `Arc<RingBuffer<T>>`.
pub type RbConsumer<T> = Consumer<T, Arc<RingBuffer<T>>>;

impl<T> RingBuffer<T> {
    const T_SIZE: usize = size_of::<T>();

    /// Creates a new ring buffer with the specified capacity.
    ///
    /// Under memory fragmentation the buddy allocator may be unable to provide
    /// the requested contiguous run. Since the buffer is accessed through a
    /// single [`Segment`], the capacity is halved until the allocation fits
    /// (down to one page). The effective capacity is stored in the buffer and
    /// reported by [`capacity`](Self::capacity), so all callers stay consistent.
    ///
    /// # Panics
    ///
    /// Panics if `capacity` is not a power of two. If even a one-page buffer
    /// cannot be allocated, the buffer degrades to capacity zero with a
    /// warning instead of panicking.
    pub fn new(capacity: usize) -> Self {
        assert!(
            capacity.is_power_of_two(),
            "capacity must be a power of two"
        );

        let mut capacity = capacity;
        loop {
            let nframes = capacity
                .checked_mul(Self::T_SIZE)
                .unwrap()
                .div_ceil(PAGE_SIZE);

            // Large buffers (the 64 KiB class) come from the reserved heap
            // pool, which is physically contiguous and needs no page-table
            // work. A reserve keeps the heap's own large slots supplied.
            if nframes > POOL_MIN_PAGES {
                if let Some(buffer) = ReservedPoolBuffer::alloc(nframes, POOL_RESERVE_PAGES) {
                    log_pool_event(true, nframes);
                    return Self {
                        storage: Storage::Pooled(buffer),
                        capacity,
                        tail: AtomicUsize::new(0),
                        head: AtomicUsize::new(0),
                        phantom: PhantomData,
                    };
                }
                log_pool_event(false, nframes);
            }

            match FrameAllocOptions::new().zeroed(false).alloc_segmented(nframes) {
                Ok(segment) => {
                    return Self {
                        storage: Storage::Segmented(segment),
                        capacity,
                        tail: AtomicUsize::new(0),
                        head: AtomicUsize::new(0),
                        phantom: PhantomData,
                    };
                }
                Err(_) if nframes > 1 && capacity > 1 => capacity /= 2,
                Err(err) => {
                    ostd::warn!(
                        "cannot allocate {} page(s) for a capacity of {}: {:?}; \
                         degrading to an empty ring buffer",
                        nframes,
                        capacity,
                        err
                    );
                    return Self {
                        storage: Storage::Empty,
                        capacity: 0,
                        tail: AtomicUsize::new(0),
                        head: AtomicUsize::new(0),
                        phantom: PhantomData,
                    };
                }
            }
        }
    }

    /// Splits the ring buffer into a producer and consumer pair.
    ///
    /// The returned [`RbProducer`] and [`RbConsumer`] share ownership of the
    /// underlying buffer via `Arc` and can be used concurrently from different threads.
    pub fn split(self) -> (RbProducer<T>, RbConsumer<T>) {
        let producer = Producer {
            rb: Arc::new(self),
            phantom: PhantomData,
        };
        let consumer = Consumer {
            rb: Arc::clone(&producer.rb),
            phantom: PhantomData,
        };
        (producer, consumer)
    }

    /// Returns the capacity of the ring buffer in number of items.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Returns a reference to the underlying memory segment.
    ///
    /// This is intended for advanced use cases that require direct memory access.
    pub fn segment(&self) -> &Storage {
        &self.storage
    }

    /// Returns `true` if the ring buffer contains no items.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Returns `true` if the ring buffer is at full capacity.
    pub fn is_full(&self) -> bool {
        self.free_len() == 0
    }

    /// Returns the number of items currently in the ring buffer.
    pub fn len(&self) -> usize {
        // Implementation notes: This subtraction only makes sense if either the head or the tail
        // is considered frozen; if both are volatile, the number of the items may become negative
        // due to race conditions. This is always true with a `RingBuffer` or a pair of
        // `RbProducer` and `RbConsumer`.
        (self.tail() - self.head()).0
    }

    /// Returns the number of items that can be pushed before the buffer is full.
    pub fn free_len(&self) -> usize {
        self.capacity - self.len()
    }

    /// Returns the head counter value.
    ///
    /// This represents the cumulative number of items that have been read from
    /// the ring buffer since creation. The value wraps on overflow.
    pub fn head(&self) -> Wrapping<usize> {
        Wrapping(self.head.load(Ordering::Acquire))
    }

    /// Returns the tail counter value.
    ///
    /// This represents the cumulative number of items that have been written to
    /// the ring buffer since creation. The value wraps on overflow.
    pub fn tail(&self) -> Wrapping<usize> {
        Wrapping(self.tail.load(Ordering::Acquire))
    }

    /// Advances the tail by `len` items starting from `tail`.
    ///
    /// This is an internal method. External users should use the safe
    /// `commit_write` method on `Producer` instead.
    pub(crate) fn advance_tail(&self, mut tail: Wrapping<usize>, len: usize) {
        tail += len;
        self.tail.store(tail.0, Ordering::Release);
    }

    /// Advances the head by `len` items starting from `head`.
    ///
    /// This is an internal method. External users should use the safe
    /// `commit_read` method on `Consumer` instead.
    pub(crate) fn advance_head(&self, mut head: Wrapping<usize>, len: usize) {
        head += len;
        self.head.store(head.0, Ordering::Release);
    }

    /// Resets the head to the current tail, effectively draining the buffer.
    ///
    /// This is an internal method. External users should use `Consumer::clear` instead.
    pub(crate) fn reset_head(&self) {
        let new_head = self.tail();
        self.head.store(new_head.0, Ordering::Release);
    }

    /// Resets the ring buffer to an empty state.
    ///
    /// This method requires exclusive access (`&mut self`) and should only be
    /// called when no concurrent producers or consumers are operating on the buffer.
    pub fn clear(&mut self) {
        self.tail.store(0, Ordering::Release);
        self.head.store(0, Ordering::Release);
    }
}

impl RingBuffer<u8> {
    /// Commits a read operation by advancing the head pointer.
    ///
    /// This method is intended for advanced use cases where the caller reads
    /// data directly from the backing segment and needs to update the head.
    /// For normal use, prefer `Consumer::pop` or `Consumer::pop_slice`.
    ///
    /// # Panics
    ///
    /// Panics if `len` exceeds the number of available items in the buffer.
    pub fn commit_read(&mut self, len: usize) {
        assert!(
            len <= self.len(),
            "commit_read: len exceeds available items"
        );
        let head = self.head();
        self.advance_head(head, len);
    }
}

impl<T: Pod> RingBuffer<T> {
    /// Pushes a single item into the ring buffer.
    ///
    /// Returns `Some(())` on success, or `None` if the buffer is full.
    pub fn push(&mut self, item: T) -> Option<()> {
        let mut producer = Producer {
            rb: self,
            phantom: PhantomData,
        };
        producer.push(item)
    }

    /// Pushes all items from the slice into the ring buffer.
    ///
    /// Returns `Some(())` if all items were successfully pushed, or `None` if
    /// there is not enough free space to fit all items. This is an all-or-nothing
    /// operation; no items are pushed if the slice cannot fit entirely.
    pub fn push_slice(&mut self, items: &[T]) -> Option<()> {
        let mut producer = Producer {
            rb: self,
            phantom: PhantomData,
        };
        producer.push_slice(items)
    }

    /// Pops a single item from the ring buffer.
    ///
    /// Returns `Some(item)` on success, or `None` if the buffer is empty.
    pub fn pop(&mut self) -> Option<T> {
        let mut consumer = Consumer {
            rb: self,
            phantom: PhantomData,
        };
        consumer.pop()
    }

    /// Pops items from the ring buffer into the provided slice.
    ///
    /// Returns `Some(())` if all slots in the slice were filled, or `None` if
    /// there are not enough items available. This is an all-or-nothing operation;
    /// no items are popped if the slice cannot be filled entirely.
    pub fn pop_slice(&mut self, items: &mut [T]) -> Option<()> {
        let mut consumer = Consumer {
            rb: self,
            phantom: PhantomData,
        };
        consumer.pop_slice(items)
    }
}

impl<T: Pod, R: Deref<Target = RingBuffer<T>>> Producer<T, R> {
    const T_SIZE: usize = size_of::<T>();

    /// Pushes a single item into the ring buffer.
    ///
    /// Returns `Some(())` on success, or `None` if the buffer is full.
    pub fn push(&mut self, item: T) -> Option<()> {
        let rb = &self.rb;
        if rb.capacity == 0 || rb.is_full() {
            return None;
        }

        let tail = rb.tail();
        let offset = tail.0 & (rb.capacity - 1);
        let byte_offset = offset * Self::T_SIZE;

        let mut writer = rb.storage.writer();
        writer.skip(byte_offset);
        writer.write_val(&item).unwrap();

        rb.advance_tail(tail, 1);
        Some(())
    }

    /// Pushes all items from the slice into the ring buffer.
    ///
    /// Returns `Some(())` if all items were successfully pushed, or `None` if
    /// there is not enough free space. This is an all-or-nothing operation;
    /// no items are pushed if the slice cannot fit entirely.
    pub fn push_slice(&mut self, items: &[T]) -> Option<()> {
        let rb = &self.rb;
        let nitems = items.len();
        if rb.capacity == 0 || rb.free_len() < nitems {
            return None;
        }

        let tail = rb.tail();
        let offset = tail.0 & (rb.capacity - 1);
        let byte_offset = offset * Self::T_SIZE;

        if offset + nitems > rb.capacity {
            // Write into two separate parts due to wraparound.
            rb.storage
                .write_slice(byte_offset, &items[..rb.capacity - offset])
                .unwrap();
            rb.storage
                .write_slice(0, &items[rb.capacity - offset..])
                .unwrap();
        } else {
            rb.storage.write_slice(byte_offset, items).unwrap();
        }

        rb.advance_tail(tail, nitems);
        Some(())
    }
}

#[inherit_methods(from = "self.rb")]
impl<T, R: Deref<Target = RingBuffer<T>>> Producer<T, R> {
    pub fn capacity(&self) -> usize;
    pub fn is_empty(&self) -> bool;
    pub fn is_full(&self) -> bool;
    pub fn len(&self) -> usize;
    pub fn free_len(&self) -> usize;
    pub fn head(&self) -> Wrapping<usize>;
    pub fn tail(&self) -> Wrapping<usize>;
    pub fn segment(&self) -> &Storage;
}

impl<T, R: Deref<Target = RingBuffer<T>>> Producer<T, R> {
    /// Commits a write operation by advancing the tail pointer.
    ///
    /// This method is intended for advanced use cases where the caller writes
    /// data directly to the backing segment and needs to update the tail.
    /// For normal use, prefer `Producer::push` or `Producer::push_slice`.
    ///
    /// # Panics
    ///
    /// Panics if `len` exceeds the available free space in the buffer.
    pub fn commit_write(&self, len: usize) {
        assert!(
            len <= self.free_len(),
            "commit_write: len exceeds free space"
        );
        let tail = self.tail();
        self.rb.advance_tail(tail, len);
    }
}

impl<T: Pod, R: Deref<Target = RingBuffer<T>>> Consumer<T, R> {
    const T_SIZE: usize = size_of::<T>();

    /// Pops a single item from the ring buffer.
    ///
    /// Returns `Some(item)` on success, or `None` if the buffer is empty.
    pub fn pop(&mut self) -> Option<T> {
        let rb = &self.rb;
        if rb.capacity == 0 || rb.is_empty() {
            return None;
        }

        let head = rb.head();
        let offset = head.0 & (rb.capacity - 1);
        let byte_offset = offset * Self::T_SIZE;

        let mut reader = rb.storage.reader();
        reader.skip(byte_offset);
        let item = reader.read_val::<T>().unwrap();

        rb.advance_head(head, 1);
        Some(item)
    }

    /// Pops items from the ring buffer into the provided slice.
    ///
    /// Returns `Some(())` if all slots in the slice were filled, or `None` if
    /// there are not enough items available. This is an all-or-nothing operation;
    /// no items are popped if the slice cannot be filled entirely.
    pub fn pop_slice(&mut self, items: &mut [T]) -> Option<()> {
        let rb = &self.rb;
        let nitems = items.len();
        if rb.capacity == 0 || rb.len() < nitems {
            return None;
        }

        let head = rb.head();
        let offset = head.0 & (rb.capacity - 1);
        let byte_offset = offset * Self::T_SIZE;

        if offset + nitems > rb.capacity {
            // Read from two separate parts due to wraparound.
            rb.storage
                .read_slice(byte_offset, &mut items[..rb.capacity - offset])
                .unwrap();
            rb.storage
                .read_slice(0, &mut items[rb.capacity - offset..])
                .unwrap();
        } else {
            rb.storage.read_slice(byte_offset, items).unwrap();
        }

        rb.advance_head(head, nitems);
        Some(())
    }

    /// Discards `count` items from the ring buffer without reading them.
    ///
    /// # Panics
    ///
    /// Panics if `count` exceeds the number of available items in the buffer.
    pub fn skip(&mut self, count: usize) {
        let rb = &self.rb;
        let len = rb.len();
        assert!(len >= count, "skip: count exceeds available items");

        let head = rb.head();
        rb.advance_head(head, count);
    }

    /// Discards all items from the ring buffer.
    ///
    /// After this call, the buffer will be empty from the consumer's perspective.
    pub fn clear(&mut self) {
        self.rb.reset_head();
    }
}

#[inherit_methods(from = "self.rb")]
impl<T, R: Deref<Target = RingBuffer<T>>> Consumer<T, R> {
    pub fn capacity(&self) -> usize;
    pub fn is_empty(&self) -> bool;
    pub fn is_full(&self) -> bool;
    pub fn len(&self) -> usize;
    pub fn free_len(&self) -> usize;
    pub fn head(&self) -> Wrapping<usize>;
    pub fn tail(&self) -> Wrapping<usize>;
    pub fn segment(&self) -> &Storage;
}

impl<T, R: Deref<Target = RingBuffer<T>>> Consumer<T, R> {
    /// Returns the underlying ring buffer.
    pub fn ring_buffer(&self) -> &RingBuffer<T> {
        &self.rb
    }

    /// Commits a read operation by advancing the head pointer.
    ///
    /// This method is intended for advanced use cases where the caller reads
    /// data directly from the backing segment and needs to update the head.
    /// For normal use, prefer `Consumer::pop` or `Consumer::pop_slice`.
    ///
    /// # Panics
    ///
    /// Panics if `len` exceeds the number of available items in the buffer.
    pub fn commit_read(&mut self, len: usize) {
        assert!(
            len <= self.len(),
            "commit_read: len exceeds available items"
        );
        let head = self.head();
        self.rb.advance_head(head, len);
    }
}

#[cfg(ktest)]
mod test;
