// SPDX-License-Identifier: MPL-2.0

//! Pipes implementation.
//!
//! This module provides both anonymous and named pipes for inter-process communication.

pub(super) use anon_pipe::AnonPipeInode;
pub(crate) use anon_pipe::new_file_pair;
pub(super) use common::Pipe;
pub(crate) use common::PipeHandle;

use core::sync::atomic::{AtomicUsize, Ordering};

use crate::prelude::*;

/// The maximum capacity of a pipe buffer (`F_SETPIPE_SZ`), writable through
/// `/proc/sys/fs/pipe-max-size`.
///
/// The default matches Linux: 1 MiB.
pub static PIPE_MAX_SIZE: AtomicUsize = AtomicUsize::new(1024 * 1024);

/// Sets [`PIPE_MAX_SIZE`], rejecting values Linux would refuse.
pub fn set_pipe_max_size(value: usize) -> Result<()> {
    // Linux caps the value at 2^31 - 1 bytes.
    if value > (1 << 30) {
        return_errno_with_message!(Errno::EINVAL, "the pipe size limit is too large");
    }
    PIPE_MAX_SIZE.store(value, Ordering::Relaxed);
    Ok(())
}

mod anon_pipe;
mod common;
