// SPDX-License-Identifier: MPL-2.0

mod balancing;

use core::{
    alloc::Layout,
    ops::{DerefMut, Range},
    sync::atomic::{AtomicUsize, Ordering},
};

use ostd::{
    cpu::{CpuId, PinCurrentCpu, all_cpus},
    cpu_local,
    irq::DisabledLocalIrqGuard,
    mm::Paddr,
    sync::{LocalIrqDisabled, SpinLock, SpinLockGuard},
};

use crate::chunk::{BuddyOrder, greater_order_of, lesser_order_of, size_of_order, split_to_chunks};

use super::set::BuddySet;

/// The global free buddies.
static GLOBAL_POOL: SpinLock<BuddySet<MAX_BUDDY_ORDER>, LocalIrqDisabled> =
    SpinLock::new(BuddySet::new_empty());
/// A snapshot of the total size of the global free buddies, not precise.
static GLOBAL_POOL_SIZE: AtomicUsize = AtomicUsize::new(0);

// CPU-local free buddies.
cpu_local! {
    static LOCAL_POOL: SpinLock<BuddySet<MAX_LOCAL_BUDDY_ORDER>, LocalIrqDisabled> =
        SpinLock::new(BuddySet::new_empty());
}

/// Maximum supported order of the buddy system.
///
/// i.e., it is the number of classes of free blocks. It determines the
/// maximum size of each allocation.
///
/// A maximum buddy order of 32 supports up to 4KiB*2^31 = 8 TiB of chunks.
const MAX_BUDDY_ORDER: BuddyOrder = 32;

/// Maximum supported order of the buddy system for CPU-local buddy system.
///
/// Since large blocks are rarely allocated, caching such blocks will lead
/// to much fragmentation.
///
/// Lock guards are also allocated on stack. We can limit the stack usage
/// for common paths in this way.
///
/// Keep allocations of 512 KiB and above in the global pool. Large contiguous
/// allocations must be returned to the same buddy set so that their lifetime
/// does not split mergeable buddies across CPUs. In particular, the default
/// OSTD task stack is 512 KiB and is allocated frequently by process-heavy
/// workloads.
///
/// A maximum local buddy order of 7 supports chunks up to 4KiB*2^6 = 256 KiB.
const MAX_LOCAL_BUDDY_ORDER: BuddyOrder = 7;

pub(super) fn alloc(guard: &DisabledLocalIrqGuard, layout: Layout) -> Option<Paddr> {
    let local_pool_lock = LOCAL_POOL.get_with(guard);
    let mut local_pool = local_pool_lock.lock();
    let mut global_pool = OnDemandGlobalLock::new();

    let size_order = greater_order_of(layout.size());
    let align_order = greater_order_of(layout.align());
    let order = size_order.max(align_order);

    let mut chunk_addr = None;

    if order < MAX_LOCAL_BUDDY_ORDER {
        chunk_addr = local_pool.alloc_chunk(order);
    }

    // Fall back to the global free lists if the local free lists are empty.
    if chunk_addr.is_none() {
        chunk_addr = global_pool.get().alloc_chunk(order);
    }
    if chunk_addr.is_none() {
        // Do not hold a local lock while walking the other local pools. All
        // allocation paths acquire local locks before the global lock, and
        // dropping both here preserves that order during reclamation.
        drop(global_pool);
        drop(local_pool);
        chunk_addr = alloc_from_remote_pool(guard.current_cpu(), order);

        global_pool = OnDemandGlobalLock::new();
        local_pool = local_pool_lock.lock();
    }
    if chunk_addr.is_none() {
        // A large enough chunk may be split across multiple local pools. Move
        // their bounded caches back into the global buddy set so that adjacent
        // chunks can coalesce, then retry once.
        drop(global_pool);
        drop(local_pool);
        reclaim_local_pools(guard.current_cpu());

        global_pool = OnDemandGlobalLock::new();
        chunk_addr = global_pool.get().alloc_chunk(order);
        local_pool = local_pool_lock.lock();
    }

    // If the alignment order is larger than the size order, we need to split
    // the chunk and return the rest part back to the free lists.
    let allocated_size = size_of_order(order);
    if allocated_size > layout.size()
        && let Some(chunk_addr) = chunk_addr
    {
        do_dealloc(
            &mut local_pool,
            &mut global_pool,
            [(chunk_addr + layout.size(), allocated_size - layout.size())].into_iter(),
        );
    }

    balancing::balance(local_pool.deref_mut(), &mut global_pool);

    chunk_addr
}

pub(super) fn alloc_in(
    guard: &DisabledLocalIrqGuard,
    layout: Layout,
    paddr_range: Range<Paddr>,
) -> Option<Paddr> {
    let local_pool_lock = LOCAL_POOL.get_with(guard);
    let mut local_pool = local_pool_lock.lock();
    let mut global_pool = OnDemandGlobalLock::new();

    let size_order = greater_order_of(layout.size());
    let align_order = greater_order_of(layout.align());
    let order = size_order.max(align_order);

    let mut chunk_addr = None;
    if order < MAX_LOCAL_BUDDY_ORDER {
        chunk_addr = local_pool.alloc_chunk_in(order, paddr_range.clone());
    }
    if chunk_addr.is_none() {
        chunk_addr = global_pool.get().alloc_chunk_in(order, paddr_range.clone());
    }
    if chunk_addr.is_none() {
        drop(global_pool);
        drop(local_pool);
        chunk_addr = alloc_from_remote_pool_in(guard.current_cpu(), order, paddr_range.clone());

        global_pool = OnDemandGlobalLock::new();
        local_pool = local_pool_lock.lock();
    }
    if chunk_addr.is_none() {
        drop(global_pool);
        drop(local_pool);
        reclaim_local_pools(guard.current_cpu());

        global_pool = OnDemandGlobalLock::new();
        chunk_addr = global_pool.get().alloc_chunk_in(order, paddr_range);
        local_pool = local_pool_lock.lock();
    }

    let allocated_size = size_of_order(order);
    if allocated_size > layout.size()
        && let Some(chunk_addr) = chunk_addr
    {
        do_dealloc(
            &mut local_pool,
            &mut global_pool,
            [(chunk_addr + layout.size(), allocated_size - layout.size())].into_iter(),
        );
    }

    balancing::balance(local_pool.deref_mut(), &mut global_pool);
    chunk_addr
}

pub(super) fn dealloc(
    guard: &DisabledLocalIrqGuard,
    segments: impl Iterator<Item = (Paddr, usize)>,
) {
    let local_pool_lock = LOCAL_POOL.get_with(guard);
    let mut local_pool = local_pool_lock.lock();
    let mut global_pool = OnDemandGlobalLock::new();

    do_dealloc(&mut local_pool, &mut global_pool, segments);

    balancing::balance(local_pool.deref_mut(), &mut global_pool);
}

/// Takes a suitable free buddy stranded in another CPU's local pool.
fn alloc_from_remote_pool(current_cpu: CpuId, order: BuddyOrder) -> Option<Paddr> {
    if order >= MAX_LOCAL_BUDDY_ORDER {
        return None;
    }

    all_cpus()
        .filter(|&cpu| cpu != current_cpu)
        .find_map(|cpu| LOCAL_POOL.get_on_cpu(cpu).lock().alloc_chunk(order))
}

/// Takes a suitable bounded free buddy stranded in another CPU's local pool.
fn alloc_from_remote_pool_in(
    current_cpu: CpuId,
    order: BuddyOrder,
    paddr_range: Range<Paddr>,
) -> Option<Paddr> {
    if order >= MAX_LOCAL_BUDDY_ORDER {
        return None;
    }

    all_cpus()
        .filter(|&cpu| cpu != current_cpu)
        .find_map(|cpu| {
            LOCAL_POOL
                .get_on_cpu(cpu)
                .lock()
                .alloc_chunk_in(order, paddr_range.clone())
        })
}

/// Reclaims free buddies stranded in CPU-local pools under memory pressure.
///
/// Each local pool is locked before the global pool, matching the normal
/// allocation/deallocation lock order. Re-inserting chunks into the global
/// set lets buddies owned by different CPUs coalesce again. The local cache
/// size limit keeps this slow path bounded.
fn reclaim_local_pools(current_cpu: CpuId) {
    for cpu in all_cpus() {
        // Prefer reclaiming idle remote caches before the current hot cache.
        if cpu != current_cpu {
            drain_local_pool(cpu);
        }
    }
    drain_local_pool(current_cpu);
}

fn drain_local_pool(cpu: CpuId) {
    let mut local_pool = LOCAL_POOL.get_on_cpu(cpu).lock();
    if local_pool.total_size() == 0 {
        return;
    }

    let mut global_pool = OnDemandGlobalLock::new();
    local_pool.drain_into(&mut *global_pool.get());
}

pub(super) fn add_free_memory(_guard: &DisabledLocalIrqGuard, addr: Paddr, size: usize) {
    let mut global_pool = OnDemandGlobalLock::new();

    split_to_chunks(addr, size).for_each(|(addr, order)| {
        global_pool.get().insert_chunk(addr, order);
    });
}

fn do_dealloc(
    local_pool: &mut BuddySet<MAX_LOCAL_BUDDY_ORDER>,
    global_pool: &mut OnDemandGlobalLock,
    segments: impl Iterator<Item = (Paddr, usize)>,
) {
    segments.for_each(|(addr, size)| {
        split_to_chunks(addr, size).for_each(|(addr, order)| {
            if order >= MAX_LOCAL_BUDDY_ORDER {
                global_pool.get().insert_chunk(addr, order);
            } else {
                local_pool.insert_chunk(addr, order);
            }
        });
    });
}

type GlobalLockGuard = SpinLockGuard<'static, BuddySet<MAX_BUDDY_ORDER>, LocalIrqDisabled>;

/// An on-demand guard that locks the global pool when needed.
///
/// It helps to avoid unnecessarily locking the global pool, and also avoids
/// repeatedly locking the global pool when it is needed multiple times.
struct OnDemandGlobalLock {
    guard: Option<GlobalLockGuard>,
}

impl OnDemandGlobalLock {
    fn new() -> Self {
        Self { guard: None }
    }

    fn get(&mut self) -> &mut GlobalLockGuard {
        self.guard.get_or_insert_with(|| GLOBAL_POOL.lock())
    }

    /// Returns the size of the global pool.
    ///
    /// If the global pool is locked, returns the actual size of the global pool.
    /// Otherwise, returns the last snapshot of the global pool size by loading
    /// [`GLOBAL_POOL_SIZE`].
    fn get_global_size(&self) -> usize {
        if let Some(guard) = self.guard.as_ref() {
            guard.total_size()
        } else {
            GLOBAL_POOL_SIZE.load(Ordering::Relaxed)
        }
    }
}

impl Drop for OnDemandGlobalLock {
    fn drop(&mut self) {
        // Updates [`GLOBAL_POOL_SIZE`] if the global pool is locked.
        if let Some(guard) = self.guard.as_ref() {
            GLOBAL_POOL_SIZE.store(guard.total_size(), Ordering::Relaxed);
        }
    }
}
