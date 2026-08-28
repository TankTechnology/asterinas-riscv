// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{Ordering, fence};

pub(super) fn initialize() {}

pub(super) fn dma_write_barrier() {
    fence(Ordering::SeqCst);
}

pub(super) fn dma_read_barrier() {
    fence(Ordering::SeqCst);
}
