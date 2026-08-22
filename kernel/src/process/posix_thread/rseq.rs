// SPDX-License-Identifier: MPL-2.0

use crate::prelude::*;

/// A restartable-sequence (rseq) area registered by a thread.
///
/// See `man 2 rseq` and `include/uapi/linux/rseq.h`. We store the bare minimum
/// needed to unregister the area on thread exit: the area address.
#[derive(Debug, Clone, Copy)]
pub struct Rseq {
    /// User-space address of the `struct rseq` area.
    pub ptr: Vaddr,
}

/// `RSEQ_FLAG_UNREGISTER` — unregister instead of register.
pub const RSEQ_FLAG_UNREGISTER: u32 = 1 << 0;

/// Minimum size of a `struct rseq` (cpu_id_start + cpu_id + rseq_cs + flags +
/// node_id + mm_cid, padded to 32 bytes by the ABI's `aligned(4 * sizeof(u64))`).
pub const RSEQ_MIN_SIZE: usize = 32;

/// The ABI alignment of a `struct rseq` area: 32 bytes.
pub const RSEQ_ALIGN: usize = 32;

/// `RSEQ_CPU_ID_UNINITIALIZED` — marks an unregistered/dead rseq area.
pub const RSEQ_CPU_ID_UNINITIALIZED: u32 = u32::MAX;

/// Byte offset of `struct rseq::cpu_id` (the field reset on unregister).
pub const RSEQ_CPU_ID_OFFSET: usize = 4;

/// Byte offset of the signature (`RSEQ_SIG`) within the rseq area.
pub const RSEQ_SIG_OFFSET: usize = 32;
