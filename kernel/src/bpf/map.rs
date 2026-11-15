// SPDX-License-Identifier: MPL-2.0

//! eBPF maps: the key/value stores programs and userspace share.
//!
//! Two types are implemented. `BPF_MAP_TYPE_ARRAY` is a preallocated array
//! indexed by a `u32` key, so a lookup or update never allocates and is safe
//! to reach from an interrupt context. `BPF_MAP_TYPE_HASH` is a fixed-capacity
//! open-addressed table with the same property: a new value is built outside
//! the lock and moved into a free slot under it.
//!
//! Keys are at most [`MAX_KEY_SIZE`] bytes, which lets the interpreter carry a
//! map element pointer in a register (see [`Map::elem_read`]).

use core::sync::atomic::{AtomicU64, Ordering};

use ostd::sync::LocalIrqDisabled;

use crate::{
    fs::{
        file::{AccessMode, FileCommon, FileLike, StatusFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};
use crate::events::IoEvents;

pub const BPF_MAP_TYPE_HASH: u32 = 1;
pub const BPF_MAP_TYPE_ARRAY: u32 = 2;

/// Update flags, with Linux's numbering.
#[allow(dead_code)] // the default flag, spelled for readers of the tool
pub const BPF_ANY: u64 = 0;
pub const BPF_NOEXIST: u64 = 1;
pub const BPF_EXIST: u64 = 2;

/// Largest key the interpreter can address (it keeps the key in a register).
pub const MAX_KEY_SIZE: u32 = 8;
/// Largest value a map may hold.
pub const MAX_VALUE_SIZE: u32 = 4096;
/// Largest entry count, and the total storage a map may commit.
pub const MAX_ENTRIES: u32 = 1 << 16;
pub const MAX_MAP_BYTES: usize = 16 << 20;

/// A map shared by a file descriptor and the programs that reference it.
pub struct Map {
    map_type: u32,
    key_size: u32,
    value_size: u32,
    max_entries: u32,
    name: String,
    storage: SpinLock<Storage, LocalIrqDisabled>,
    /// Lookups from programs, for `/proc/trace/bpf` and the loader tool.
    lookups: AtomicU64,
    updates: AtomicU64,
}

enum Storage {
    Array(Vec<u8>),
    Hash(Vec<HashSlot>),
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
    fn empty(value_size: u32) -> Self {
        Self {
            key: [0; MAX_KEY_SIZE as usize],
            used: false,
            value: vec![0; value_size as usize],
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
        if value_size == 0 || value_size > MAX_VALUE_SIZE {
            return_errno_with_message!(Errno::EINVAL, "unsupported map value size");
        }
        if max_entries == 0 || max_entries > MAX_ENTRIES {
            return_errno_with_message!(Errno::EINVAL, "unsupported map entry count");
        }

        let storage = match map_type {
            BPF_MAP_TYPE_ARRAY => {
                if key_size != 4 {
                    return_errno_with_message!(Errno::EINVAL, "an array map needs a 4-byte key");
                }
                let bytes = (max_entries as usize) * (value_size as usize);
                if bytes > MAX_MAP_BYTES {
                    return_errno_with_message!(Errno::EINVAL, "the map is too large");
                }
                Storage::Array(vec![0; bytes])
            }
            BPF_MAP_TYPE_HASH => {
                if key_size == 0 || key_size > MAX_KEY_SIZE {
                    return_errno_with_message!(Errno::EINVAL, "unsupported map key size");
                }
                let bytes = (max_entries as usize) * (value_size as usize);
                if bytes > MAX_MAP_BYTES {
                    return_errno_with_message!(Errno::EINVAL, "the map is too large");
                }
                Storage::Hash(
                    (0..max_entries)
                        .map(|_| HashSlot::empty(value_size))
                        .collect(),
                )
            }
            _ => return_errno_with_message!(Errno::EINVAL, "unsupported map type"),
        };

        Ok(Arc::new(Self {
            map_type,
            key_size,
            value_size,
            max_entries,
            name: name.to_string(),
            storage: SpinLock::new(storage),
            lookups: AtomicU64::new(0),
            updates: AtomicU64::new(0),
        }))
    }

    pub fn map_type(&self) -> u32 {
        self.map_type
    }

    pub fn key_size(&self) -> u32 {
        self.key_size
    }

    pub fn value_size(&self) -> u32 {
        self.value_size
    }

    pub fn max_entries(&self) -> u32 {
        self.max_entries
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

    /// Reads one element into `out`. Returns `ENOENT` if the key is absent.
    pub fn lookup(&self, key: &[u8], out: &mut [u8]) -> Result<()> {
        let key = self.key_u64(key)?;
        if out.len() < self.value_size as usize {
            return_errno_with_message!(Errno::EINVAL, "the output buffer is too small");
        }
        self.lookups.fetch_add(1, Ordering::Relaxed);

        let storage = self.storage.lock();
        let value = match &*storage {
            Storage::Array(bytes) => {
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * self.value_size as usize;
                &bytes[start..start + self.value_size as usize]
            }
            Storage::Hash(slots) => {
                let Some(slot) = Self::hash_find(slots, key) else {
                    return_errno_with_message!(Errno::ENOENT, "the key is absent");
                };
                &slots[slot].value
            }
        };
        out[..self.value_size as usize].copy_from_slice(value);
        Ok(())
    }

    /// Writes one element, honoring `BPF_ANY` / `BPF_NOEXIST` / `BPF_EXIST`.
    pub fn update(&self, key: &[u8], value: &[u8], flags: u64) -> Result<()> {
        let key = self.key_u64(key)?;
        if value.len() < self.value_size as usize {
            return_errno_with_message!(Errno::EINVAL, "the value buffer is too small");
        }
        if flags > BPF_EXIST {
            return_errno_with_message!(Errno::EINVAL, "unknown update flag");
        }
        self.updates.fetch_add(1, Ordering::Relaxed);

        match self.map_type {
            BPF_MAP_TYPE_ARRAY => {
                if flags == BPF_NOEXIST {
                    return_errno_with_message!(Errno::EEXIST, "an array element always exists");
                }
                let mut storage = self.storage.lock();
                let Storage::Array(bytes) = &mut *storage else {
                    unreachable!()
                };
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * self.value_size as usize;
                bytes[start..start + self.value_size as usize]
                    .copy_from_slice(&value[..self.value_size as usize]);
                Ok(())
            }
            BPF_MAP_TYPE_HASH => {
                let mut storage = self.storage.lock();
                let Storage::Hash(slots) = &mut *storage else {
                    unreachable!()
                };
                match Self::hash_find(slots, key) {
                    Some(index) => {
                        if flags == BPF_NOEXIST {
                            return_errno_with_message!(Errno::EEXIST, "the key exists");
                        }
                        slots[index].value[..].copy_from_slice(&value[..self.value_size as usize]);
                        Ok(())
                    }
                    None => {
                        if flags == BPF_EXIST {
                            return_errno_with_message!(Errno::ENOENT, "the key is absent");
                        }
                        let Some(index) = Self::hash_free_slot(slots) else {
                            return_errno_with_message!(Errno::ENOSPC, "the map is full");
                        };
                        slots[index].key = Self::key_bytes(key);
                        slots[index].used = true;
                        slots[index].value[..].copy_from_slice(&value[..self.value_size as usize]);
                        Ok(())
                    }
                }
            }
            _ => unreachable!(),
        }
    }

    pub fn delete(&self, key: &[u8]) -> Result<()> {
        let key = self.key_u64(key)?;
        if self.map_type != BPF_MAP_TYPE_HASH {
            return_errno_with_message!(Errno::EINVAL, "an array map does not support delete");
        }
        let mut storage = self.storage.lock();
        let Storage::Hash(slots) = &mut *storage else {
            unreachable!()
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
        }
    }

    /// Copies `out.len()` bytes at `offset` out of the element `key`.
    pub fn elem_read_bytes(&self, key: u64, offset: usize, out: &mut [u8]) -> Result<()> {
        let storage = self.storage.lock();
        let value = self.value_of(&storage, key)?;
        if offset + out.len() > value.len() {
            return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
        }
        out.copy_from_slice(&value[offset..offset + out.len()]);
        Ok(())
    }

    /// Copies `bytes` at `offset` into the element `key`, which must exist.
    pub fn elem_write_bytes(&self, key: u64, offset: usize, bytes: &[u8]) -> Result<()> {
        let mut storage = self.storage.lock();
        let value = self.value_of_mut(&mut storage, key)?;
        if offset + bytes.len() > value.len() {
            return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
        }
        value[offset..offset + bytes.len()].copy_from_slice(bytes);
        Ok(())
    }

    /// Makes `key` writable: an array element must be in range, a hash element
    /// is created here (zeroed) when the flags allow it.
    pub fn ensure_elem(&self, key: u64, flags: u64) -> Result<()> {
        match self.map_type {
            BPF_MAP_TYPE_ARRAY => {
                if flags == BPF_NOEXIST {
                    return_errno_with_message!(Errno::EEXIST, "an array element always exists");
                }
                if (key as usize) >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                Ok(())
            }
            _ => {
                let mut storage = self.storage.lock();
                let Storage::Hash(slots) = &mut *storage else {
                    unreachable!()
                };
                match Self::hash_find(slots, key) {
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
                        let Some(index) = Self::hash_free_slot(slots) else {
                            return_errno_with_message!(Errno::ENOSPC, "the map is full");
                        };
                        slots[index].key = Self::key_bytes(key);
                        slots[index].used = true;
                        slots[index].value.fill(0);
                        Ok(())
                    }
                }
            }
        }
    }

    fn value_of<'a>(&self, storage: &'a Storage, key: u64) -> Result<&'a [u8]> {
        match storage {
            Storage::Array(bytes) => {
                let index = key as usize;
                if index >= self.max_entries as usize {
                    return_errno_with_message!(Errno::ENOENT, "the array index is out of range");
                }
                let start = index * self.value_size as usize;
                Ok(&bytes[start..start + self.value_size as usize])
            }
            Storage::Hash(slots) => {
                let Some(index) = Self::hash_find(slots, key) else {
                    return_errno_with_message!(Errno::ENOENT, "the key is absent");
                };
                Ok(&slots[index].value)
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
                let start = index * self.value_size as usize;
                Ok(&mut bytes[start..start + self.value_size as usize])
            }
            Storage::Hash(slots) => {
                let Some(index) = Self::hash_find(slots, key) else {
                    return_errno_with_message!(Errno::ENOENT, "the key is absent");
                };
                Ok(&mut slots[index].value)
            }
        }
    }

    /// Whether the element `key` can be addressed (an array element always
    /// can; a hash element only while it exists).
    pub fn elem_exists(&self, key: u64) -> bool {
        match self.map_type {
            BPF_MAP_TYPE_ARRAY => (key as usize) < self.max_entries as usize,
            _ => {
                let storage = self.storage.lock();
                let Storage::Hash(slots) = &*storage else {
                    return false;
                };
                Self::hash_find(slots, key).is_some()
            }
        }
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

    fn hash_free_slot(slots: &[HashSlot]) -> Option<usize> {
        slots.iter().position(|slot| !slot.used)
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
    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDWR
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(
        self: Arc<Self>,
        fd_flags: crate::fs::file::file_table::FdFlags,
    ) -> Box<dyn core::fmt::Display> {
        super::fdinfo(&self.common, self.access_mode(), fd_flags, "map")
    }

    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }
}
