// SPDX-License-Identifier: MPL-2.0

//! eBPF maps: the key/value stores programs and userspace share.
//!
//! Four types are implemented:
//!
//! - `BPF_MAP_TYPE_ARRAY` is a preallocated array indexed by a `u32` key, so a
//!   lookup or update never allocates and is safe to reach from an interrupt
//!   context.
//! - `BPF_MAP_TYPE_HASH` is a fixed-capacity open-addressed table with the same
//!   property: a value is written into a preallocated slot under the lock.
//! - The two `PERCPU_*` variants keep one value per CPU, so a program can
//!   count without coordination. A program sees its own CPU's value; userspace
//!   reads all of them.
//! - `BPF_MAP_TYPE_RINGBUF` is a byte ring in a `Vmo`, shared with userspace
//!   through `mmap`. Programs append records with `bpf_ringbuf_output`; the
//!   reader side is an ordinary mapping, so no wakeup mechanism is involved.
//!
//! Keys are at most [`MAX_KEY_SIZE`] bytes, which lets the interpreter carry a
//! map element pointer in a register (see [`Map::elem_read_bytes`]).

use core::sync::atomic::{AtomicU64, Ordering};

use ostd::sync::LocalIrqDisabled;

use crate::fs::file::MappableObject;
use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, FileCommon, FileLike, Mappable, StatusFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
    vm::page_cache::{Vmo, VmoOptions},
};

pub const BPF_MAP_TYPE_HASH: u32 = 1;
pub const BPF_MAP_TYPE_ARRAY: u32 = 2;
pub const BPF_MAP_TYPE_PERCPU_HASH: u32 = 5;
pub const BPF_MAP_TYPE_PERCPU_ARRAY: u32 = 6;
pub const BPF_MAP_TYPE_RINGBUF: u32 = 27;

/// Update flags, with Linux's numbering.
#[allow(dead_code)] // the default flag, spelled for readers of the tool
pub const BPF_ANY: u64 = 0;
pub const BPF_NOEXIST: u64 = 1;
pub const BPF_EXIST: u64 = 2;

/// Largest key the interpreter can address (it keeps the key in a register).
pub const MAX_KEY_SIZE: u32 = 8;
/// Largest value a map may hold, per key and per CPU.
pub const MAX_VALUE_SIZE: u32 = 4096;
/// Largest entry count, and the total storage a map may commit.
pub const MAX_ENTRIES: u32 = 1 << 16;
pub const MAX_MAP_BYTES: usize = 16 << 20;

/// Largest record a program may append to a ring buffer.
pub const MAX_RINGBUF_RECORD: usize = 512;

/// Where the data area of a ring buffer starts; the first page is the header.
pub const RINGBUF_DATA_OFFSET: usize = PAGE_SIZE;
/// A record header that tells the reader to skip to the start of the buffer.
pub const RINGBUF_WRAP_MARKER: u32 = u32::MAX;
/// Offset of the reader-owned position inside the header page.
pub const RINGBUF_CONSUMER_OFFSET: usize = 0;
/// Offset of the writer-owned position inside the header page.
pub const RINGBUF_PRODUCER_OFFSET: usize = 8;
/// Offset of the data area's size inside the header page.
pub const RINGBUF_CAPACITY_OFFSET: usize = 16;

/// A map shared by a file descriptor and the programs that reference it.
pub struct Map {
    map_type: u32,
    key_size: u32,
    value_size: u32,
    max_entries: u32,
    /// Bytes one element occupies per CPU (the value size, rounded up to 8 for
    /// a per-CPU map so every CPU's slot stays aligned).
    stride: u32,
    /// CPUs a per-CPU element has a value for; 1 for every other type.
    nr_cpus: u32,
    /// Whether the element has one value per CPU at all.
    per_cpu: bool,
    name: String,
    storage: SpinLock<Storage, LocalIrqDisabled>,
    /// Lookups from programs, for `/proc/trace/bpf` and the loader tool.
    lookups: AtomicU64,
    updates: AtomicU64,
    dropped: AtomicU64,
}

enum Storage {
    Array(Vec<u8>),
    Hash(Vec<HashSlot>),
    Ring(Ring),
}

/// The kernel half of a ring buffer map.
///
/// The header page and the data area live in one `Vmo`, which userspace maps:
///
/// ```text
/// 0   u64  consumer position   (userspace writes)
/// 8   u64  producer position   (the kernel writes)
/// 16  u64  data capacity in bytes
/// 24  u32  record alignment (8)
/// -- page boundary --
/// 4096     data area, records of {u32 len, u32 unused, payload, padding}
/// ```
///
/// A record never straddles the end of the data area: when one would, a header
/// with [`RINGBUF_WRAP_MARKER`] fills the tail and the record starts at zero.
struct Ring {
    vmo: Arc<Vmo>,
    capacity: usize,
}

#[derive(Clone)]
struct HashSlot {
    key: [u8; MAX_KEY_SIZE as usize],
    used: bool,
    value: Vec<u8>,
}

impl HashSlot {
    /// An unused slot whose value storage is already allocated, so that an
    /// insert never allocates while the map's lock is held.
    fn empty(value_len: usize) -> Self {
        Self {
            key: [0; MAX_KEY_SIZE as usize],
            used: false,
            value: vec![0; value_len],
        }
    }
}

impl Map {
    pub fn create(
        map_type: u32,
        key_size: u32,
        value_size: u32,
        max_entries: u32,
        name: &str,
    ) -> Result<Arc<Self>> {
        if map_type == BPF_MAP_TYPE_RINGBUF {
            return Self::create_ring(max_entries, name);
        }

        let per_cpu = matches!(
            map_type,
            BPF_MAP_TYPE_PERCPU_HASH | BPF_MAP_TYPE_PERCPU_ARRAY
        );
        if !per_cpu && map_type != BPF_MAP_TYPE_HASH && map_type != BPF_MAP_TYPE_ARRAY {
            return_errno_with_message!(Errno::EINVAL, "unsupported map type");
        }
        if value_size == 0 || value_size > MAX_VALUE_SIZE {
            return_errno_with_message!(Errno::EINVAL, "unsupported map value size");
        }
        if max_entries == 0 || max_entries > MAX_ENTRIES {
            return_errno_with_message!(Errno::EINVAL, "unsupported map entry count");
        }

        let nr_cpus = if per_cpu {
            ostd::cpu::num_cpus().max(1) as u32
        } else {
            1
        };
        let stride = if per_cpu {
            value_size.next_multiple_of(8)
        } else {
            value_size
        };
        let value_len = (stride * nr_cpus) as usize;

        let is_array = matches!(map_type, BPF_MAP_TYPE_ARRAY | BPF_MAP_TYPE_PERCPU_ARRAY);
        if is_array && key_size != 4 {
            return_errno_with_message!(Errno::EINVAL, "an array map needs a 4-byte key");
        }
        if !is_array && (key_size == 0 || key_size > MAX_KEY_SIZE) {
            return_errno_with_message!(Errno::EINVAL, "unsupported map key size");
        }
        let bytes = (max_entries as usize).saturating_mul(value_len);
        if bytes > MAX_MAP_BYTES {
            return_errno_with_message!(Errno::EINVAL, "the map is too large");
        }

        let storage = if is_array {
            Storage::Array(vec![0; bytes])
        } else {
            Storage::Hash(
                (0..max_entries)
                    .map(|_| HashSlot::empty(value_len))
                    .collect(),
            )
        };

        Ok(Arc::new(Self {
            map_type,
            key_size,
            value_size,
            max_entries,
            stride,
            nr_cpus,
            per_cpu,
            name: name.to_string(),
            storage: SpinLock::new(storage),
            lookups: AtomicU64::new(0),
            updates: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
        }))
    }

    fn create_ring(capacity: u32, name: &str) -> Result<Arc<Self>> {
        let capacity = (capacity as usize).clamp(PAGE_SIZE, MAX_MAP_BYTES);
        let capacity = capacity & !7;
        let vmo = VmoOptions::new(RINGBUF_DATA_OFFSET + capacity).alloc()?;

        // Commit every page now, so a program appending a record never faults
        // (and therefore never allocates) while the map's lock is held.
        let zeroes = vec![0u8; RINGBUF_DATA_OFFSET + capacity];
        vmo.write(0, &mut VmReader::from(zeroes.as_slice()).to_fallible())?;
        vmo.write(
            RINGBUF_CAPACITY_OFFSET,
            &mut VmReader::from((capacity as u64).to_le_bytes().as_slice()).to_fallible(),
        )?;

        Ok(Arc::new(Self {
            map_type: BPF_MAP_TYPE_RINGBUF,
            key_size: 0,
            value_size: 0,
            max_entries: capacity as u32,
            stride: 0,
            nr_cpus: 1,
            per_cpu: false,
            name: name.to_string(),
            storage: SpinLock::new(Storage::Ring(Ring { vmo, capacity })),
            lookups: AtomicU64::new(0),
            updates: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
        }))
    }

    pub fn map_type(&self) -> u32 {
        self.map_type
    }

    pub fn key_size(&self) -> u32 {
        self.key_size
    }

    /// One CPU's value size (the whole element for a map that is not per-CPU).
    pub fn value_size(&self) -> u32 {
        self.value_size
    }

    /// The size of the whole element as userspace sees it.
    pub fn element_size(&self) -> usize {
        (self.stride as usize) * (self.nr_cpus as usize)
    }

    pub fn max_entries(&self) -> u32 {
        self.max_entries
    }

    #[allow(dead_code)] // asked by the interfaces when a map is listed
    pub fn is_per_cpu(&self) -> bool {
        self.per_cpu
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn lookups(&self) -> u64 {
        self.lookups.load(Ordering::Relaxed)
    }

    pub fn updates(&self) -> u64 {
        self.updates.load(Ordering::Relaxed)
    }

    #[allow(dead_code)] // shown by /proc/trace/bpf once the ring is listed
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    /// The mapping userspace gets for a ring buffer map, if this is one.
    pub fn mappable(&self) -> Option<Arc<Vmo>> {
        let storage = self.storage.lock();
        let Storage::Ring(ring) = &*storage else {
            return None;
        };
        Some(ring.vmo.clone())
    }

    /// Reads one element into `out`. Returns `ENOENT` if the key is absent.
    pub fn lookup(&self, key: &[u8], out: &mut [u8]) -> Result<()> {
        let key = self.key_u64(key)?;
        let element_size = self.element_size();
        if out.len() < element_size {
            return_errno_with_message!(Errno::EINVAL, "the output buffer is too small");
        }
        self.lookups.fetch_add(1, Ordering::Relaxed);

        let storage = self.storage.lock();
        let value = self.value_of(&storage, key)?;
        out[..value.len()].copy_from_slice(value);
        Ok(())
    }

    /// Writes one element, honoring `BPF_ANY` / `BPF_NOEXIST` / `BPF_EXIST`.
    pub fn update(&self, key: &[u8], value: &[u8], flags: u64) -> Result<()> {
        let key = self.key_u64(key)?;
        let element_size = self.element_size();
        if value.len() < element_size {
            return_errno_with_message!(Errno::EINVAL, "the value buffer is too small");
        }
        if flags > BPF_EXIST {
            return_errno_with_message!(Errno::EINVAL, "unknown update flag");
        }
        self.updates.fetch_add(1, Ordering::Relaxed);

        let mut storage = self.storage.lock();
        match &mut *storage {
            Storage::Array(bytes) => {
                if flags == BPF_NOEXIST {
                    return_errno_with_message!(Errno::EEXIST, "an array element always exists");
                }
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * element_size;
                bytes[start..start + element_size].copy_from_slice(&value[..element_size]);
                Ok(())
            }
            Storage::Hash(slots) => match Self::hash_find(slots, key) {
                Some(index) => {
                    if flags == BPF_NOEXIST {
                        return_errno_with_message!(Errno::EEXIST, "the key exists");
                    }
                    slots[index].value[..].copy_from_slice(&value[..element_size]);
                    Ok(())
                }
                None => {
                    if flags == BPF_EXIST {
                        return_errno_with_message!(Errno::ENOENT, "the key is absent");
                    }
                    let Some(index) = Self::hash_free_slot(slots, key) else {
                        return_errno_with_message!(Errno::ENOSPC, "the map is full");
                    };
                    slots[index].key = Self::key_bytes(key);
                    slots[index].used = true;
                    slots[index].value[..].copy_from_slice(&value[..element_size]);
                    Ok(())
                }
            },
            Storage::Ring(_) => {
                return_errno_with_message!(Errno::EINVAL, "a ring buffer has no elements")
            }
        }
    }

    pub fn delete(&self, key: &[u8]) -> Result<()> {
        let key = self.key_u64(key)?;
        let mut storage = self.storage.lock();
        let Storage::Hash(slots) = &mut *storage else {
            return_errno_with_message!(
                Errno::EINVAL,
                "only a hash map supports delete, and a ring has no keys"
            );
        };
        let Some(index) = Self::hash_find(slots, key) else {
            return_errno_with_message!(Errno::ENOENT, "the key is absent");
        };
        // Delete by moving the hole forward through the probe chain: an entry
        // is pulled into the hole only when its own home is not inside the gap
        // (hole, cursor], because moving it would take it off its own probe
        // path. Once the hole reaches a free slot the chain is closed.
        let capacity = slots.len();
        slots[index].used = false;
        let mut hole = index;
        let mut cursor = (index + 1) % capacity;
        loop {
            if !slots[cursor].used {
                slots[hole].used = false;
                return Ok(());
            }
            let home = Self::hash_home(&slots[cursor].key, capacity);
            let in_gap = if hole < cursor {
                home > hole && home <= cursor
            } else {
                home > hole || home <= cursor
            };
            if !in_gap {
                slots.swap(hole, cursor);
                hole = cursor;
            }
            cursor = (cursor + 1) % capacity;
        }
    }

    /// Iterates the next key after `key` (or the first when `None`).
    pub fn next_key(&self, key: Option<&[u8]>) -> Result<Option<Vec<u8>>> {
        let start = match key {
            Some(key) => Some(self.key_u64(key)?),
            None => None,
        };
        let storage = self.storage.lock();
        match &*storage {
            Storage::Array(_) => {
                let next = start.map_or(0, |index| index.saturating_add(1));
                if next >= self.max_entries as u64 {
                    return Ok(None);
                }
                Ok(Some((next as u32).to_le_bytes().to_vec()))
            }
            Storage::Hash(slots) => {
                let mut best: Option<u64> = None;
                for slot in slots.iter().filter(|slot| slot.used) {
                    let candidate = u64::from_le_bytes(slot.key);
                    let after = start.is_none_or(|start| candidate > start);
                    if after && best.is_none_or(|best| candidate < best) {
                        best = Some(candidate);
                    }
                }
                Ok(best.map(|key| Self::key_bytes(key).to_vec()))
            }
            Storage::Ring(_) => Ok(None),
        }
    }

    /// Copies `out.len()` bytes at `offset` out of the element `key`, as the
    /// program running on `cpu` sees it.
    pub fn elem_read_bytes(&self, cpu: u32, key: u64, offset: usize, out: &mut [u8]) -> Result<()> {
        let storage = self.storage.lock();
        let value = self.value_of(&storage, key)?;
        let base = self.cpu_offset(cpu)?;
        let value = &value[base..];
        if offset + out.len() > value.len() {
            return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
        }
        out.copy_from_slice(&value[offset..offset + out.len()]);
        Ok(())
    }

    /// Copies `bytes` at `offset` into the element `key`, in the slot of `cpu`.
    pub fn elem_write_bytes(&self, cpu: u32, key: u64, offset: usize, bytes: &[u8]) -> Result<()> {
        let mut storage = self.storage.lock();
        let base = self.cpu_offset(cpu)?;
        let value = self.value_of_mut(&mut storage, key)?;
        let value = &mut value[base..];
        if offset + bytes.len() > value.len() {
            return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
        }
        value[offset..offset + bytes.len()].copy_from_slice(bytes);
        Ok(())
    }

    /// Makes `key` writable: an array element must be in range, a hash element
    /// is created here (zeroed) when the flags allow it.
    pub fn ensure_elem(&self, key: u64, flags: u64) -> Result<()> {
        let mut storage = self.storage.lock();
        match &mut *storage {
            Storage::Array(_) => {
                if flags == BPF_NOEXIST {
                    return_errno_with_message!(Errno::EEXIST, "an array element always exists");
                }
                if (key as usize) >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                Ok(())
            }
            Storage::Hash(slots) => match Self::hash_find(slots, key) {
                Some(_) => {
                    if flags == BPF_NOEXIST {
                        return_errno_with_message!(Errno::EEXIST, "the key exists");
                    }
                    Ok(())
                }
                None => {
                    if flags == BPF_EXIST {
                        return_errno_with_message!(Errno::ENOENT, "the key is absent");
                    }
                    let Some(index) = Self::hash_free_slot(slots, key) else {
                        return_errno_with_message!(Errno::ENOSPC, "the map is full");
                    };
                    slots[index].key = Self::key_bytes(key);
                    slots[index].used = true;
                    slots[index].value.fill(0);
                    Ok(())
                }
            },
            Storage::Ring(_) => {
                return_errno_with_message!(Errno::EINVAL, "a ring buffer has no elements")
            }
        }
    }

    /// Whether the element `key` can be addressed (an array element always
    /// can; a hash element only while it exists).
    pub fn elem_exists(&self, key: u64) -> bool {
        let storage = self.storage.lock();
        match &*storage {
            Storage::Array(_) => (key as usize) < self.max_entries as usize,
            Storage::Hash(slots) => Self::hash_find(slots, key).is_some(),
            Storage::Ring(_) => false,
        }
    }

    /// Appends one record to a ring buffer map.
    ///
    /// Returns the number of bytes the reader will see, or `EAGAIN` when the
    /// buffer is full (the record is dropped, as Linux's does without
    /// `BPF_RB_FORCE_WAKEUP`).
    pub fn ring_output(&self, bytes: &[u8]) -> Result<u32> {
        if bytes.is_empty() || bytes.len() > MAX_RINGBUF_RECORD {
            return_errno_with_message!(Errno::EINVAL, "unsupported ring buffer record size");
        }

        let storage = self.storage.lock();
        let Storage::Ring(ring) = &*storage else {
            return_errno_with_message!(Errno::EINVAL, "the map is not a ring buffer");
        };
        let capacity = ring.capacity;

        let mut header = [0u8; 16];
        ring.vmo.read(
            RINGBUF_CONSUMER_OFFSET,
            &mut VmWriter::from(header.as_mut_slice()).to_fallible(),
        )?;
        let consumer = u64::from_le_bytes(header[0..8].try_into().unwrap());
        let producer = u64::from_le_bytes(header[8..16].try_into().unwrap());

        let total = (8 + bytes.len()).next_multiple_of(8) as u64;
        if producer.wrapping_sub(consumer) + total > capacity as u64 {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return_errno_with_message!(Errno::EAGAIN, "the ring buffer is full");
        }

        let mut producer = producer;
        let mut start = (producer % capacity as u64) as usize;
        // A record never straddles the end: fill the tail with a wrap marker.
        if start + total as usize > capacity {
            let mut marker = [0u8; 8];
            marker[0..4].copy_from_slice(&RINGBUF_WRAP_MARKER.to_le_bytes());
            ring.vmo.write(
                RINGBUF_DATA_OFFSET + start,
                &mut VmReader::from(marker.as_slice()).to_fallible(),
            )?;
            producer += (capacity - start) as u64;
            start = 0;
        }

        let mut record = Vec::with_capacity(8 + bytes.len());
        record.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
        record.extend_from_slice(&0u32.to_le_bytes());
        record.extend_from_slice(bytes);
        record.resize(total as usize, 0);
        ring.vmo.write(
            RINGBUF_DATA_OFFSET + start,
            &mut VmReader::from(record.as_slice()).to_fallible(),
        )?;

        producer += total;
        ring.vmo.write(
            RINGBUF_PRODUCER_OFFSET,
            &mut VmReader::from(producer.to_le_bytes().as_slice()).to_fallible(),
        )?;
        drop(storage);

        self.updates.fetch_add(1, Ordering::Relaxed);
        Ok(bytes.len() as u32)
    }

    fn cpu_offset(&self, cpu: u32) -> Result<usize> {
        // A map that is not per-CPU has one value, whichever CPU asks.
        if !self.per_cpu {
            return Ok(0);
        }
        if cpu >= self.nr_cpus {
            return_errno_with_message!(Errno::EINVAL, "the CPU index is out of range");
        }
        Ok((cpu as usize) * (self.stride as usize))
    }

    fn key_u64(&self, key: &[u8]) -> Result<u64> {
        if key.len() != self.key_size as usize {
            return_errno_with_message!(Errno::EINVAL, "the key has the wrong size");
        }
        Ok(read_le(key))
    }

    fn key_bytes(key: u64) -> [u8; MAX_KEY_SIZE as usize] {
        let mut bytes = [0; MAX_KEY_SIZE as usize];
        bytes.copy_from_slice(&key.to_le_bytes());
        bytes
    }

    fn value_of<'a>(&self, storage: &'a Storage, key: u64) -> Result<&'a [u8]> {
        match storage {
            Storage::Array(bytes) => {
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * self.element_size();
                Ok(&bytes[start..start + self.element_size()])
            }
            Storage::Hash(slots) => {
                let Some(index) = Self::hash_find(slots, key) else {
                    return_errno_with_message!(Errno::ENOENT, "the key is absent");
                };
                Ok(&slots[index].value)
            }
            Storage::Ring(_) => {
                return_errno_with_message!(Errno::EINVAL, "a ring buffer has no elements")
            }
        }
    }

    fn value_of_mut<'a>(&self, storage: &'a mut Storage, key: u64) -> Result<&'a mut [u8]> {
        match storage {
            Storage::Array(bytes) => {
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * self.element_size();
                Ok(&mut bytes[start..start + self.element_size()])
            }
            Storage::Hash(slots) => {
                let Some(index) = Self::hash_find(slots, key) else {
                    return_errno_with_message!(Errno::ENOENT, "the key is absent");
                };
                Ok(&mut slots[index].value)
            }
            Storage::Ring(_) => {
                return_errno_with_message!(Errno::EINVAL, "a ring buffer has no elements")
            }
        }
    }

    fn hash_home(key: &[u8; MAX_KEY_SIZE as usize], capacity: usize) -> usize {
        // FNV-1a, then fold into the table.
        let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
        for byte in key {
            hash ^= *byte as u64;
            hash = hash.wrapping_mul(0x1000_0000_01b3);
        }
        (hash as usize) % capacity
    }

    fn hash_find(slots: &[HashSlot], key: u64) -> Option<usize> {
        let needle = Self::key_bytes(key);
        let home = Self::hash_home(&needle, slots.len());
        for step in 0..slots.len() {
            let index = (home + step) % slots.len();
            if !slots[index].used {
                return None;
            }
            if slots[index].key == needle {
                return Some(index);
            }
        }
        None
    }

    /// The slot a new entry must go into: the first free one along the key's
    /// own probe path. Taking any free slot instead would put the entry where
    /// a lookup never looks.
    fn hash_free_slot(slots: &[HashSlot], key: u64) -> Option<usize> {
        let needle = Self::key_bytes(key);
        let home = Self::hash_home(&needle, slots.len());
        for step in 0..slots.len() {
            let index = (home + step) % slots.len();
            if !slots[index].used {
                return Some(index);
            }
        }
        None
    }
}

/// Reads up to eight little-endian bytes as an integer.
pub fn read_le(bytes: &[u8]) -> u64 {
    let mut buf = [0u8; 8];
    let len = bytes.len().min(8);
    buf[..len].copy_from_slice(&bytes[..len]);
    u64::from_le_bytes(buf)
}

/// The file object behind a map file descriptor.
pub struct MapFile {
    map: Arc<Map>,
    common: FileCommon,
    pollee: Pollee,
}

impl MapFile {
    pub fn new(map: Arc<Map>) -> Self {
        Self {
            map,
            common: FileCommon::new(
                AnonInodeFs::new_path(|_| "anon_inode:[bpf-map]".to_string()),
                AccessMode::O_RDWR,
                StatusFlags::empty(),
            ),
            pollee: Pollee::new(),
        }
    }

    pub fn map(&self) -> &Arc<Map> {
        &self.map
    }
}

impl Pollable for MapFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee.poll_with(mask, None, IoEvents::empty)
    }
}

impl FileLike for MapFile {

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn mappable(&self, _request: crate::vm::vmar::FileMmapRequest) -> Result<MappableObject<'_>> {
        match self.map.mappable() {
            Some(vmo) => Ok(MappableObject::Vmo(vmo)),
            None => return_errno_with_message!(Errno::ENODEV, "only a ring buffer is mappable"),
        }
    }

    fn dump_proc_fdinfo(
        self: Arc<Self>,
        fd_flags: crate::fs::file::file_table::FdFlags,
    ) -> Box<dyn core::fmt::Display> {
        super::fdinfo(&self.common, self.common.access_mode(), fd_flags, "map")
    }

    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }
}
