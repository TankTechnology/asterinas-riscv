// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use ostd::timer::Jiffies;

use super::SyscallReturn;
use crate::{
    fs,
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
};

static READ_DETAIL_PROFILE: AtomicBool = AtomicBool::new(false);
static READ_DETAIL_EVENTS: AtomicU64 = AtomicU64::new(0);
const READ_DETAIL_SLOW_THRESHOLD_JIFFIES: u64 = 64;
const READ_DETAIL_LOG_LIMIT: u64 = 256;

aster_cmdline::define_flag_param_early!("asterinas.read_detail_profile", READ_DETAIL_PROFILE);

pub fn sys_read(
    raw_fd: RawFileDesc,
    user_buf_addr: Vaddr,
    buf_len: usize,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "raw_fd = {}, user_buf_ptr = 0x{:x}, buf_len = 0x{:x}",
        raw_fd, user_buf_addr, buf_len
    );

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);

    // Capture the identity of slow reads before entering the file
    // implementation.  This is deliberately opt-in and bounded: the normal
    // read path does not allocate a path string or consult the timer.
    let detail_start = READ_DETAIL_PROFILE
        .load(Ordering::Relaxed)
        .then(Jiffies::elapsed);

    // According to <https://man7.org/linux/man-pages/man2/read.2.html>, if
    // the user specified an empty buffer, we should detect errors by checking
    // the file descriptor. If no errors detected, return 0 successfully.
    let read_len = {
        if buf_len != 0 {
            let user_space = ctx.user_space();
            let mut writer = user_space.writer(user_buf_addr, buf_len)?;
            file.read(&mut writer)
        } else {
            file.read_bytes(&mut [])
        }
    }
    .map_err(|err| match err.error() {
        Errno::EINTR => Error::new(Errno::ERESTARTSYS),
        _ => err,
    })?;

    if let Some(start) = detail_start {
        let elapsed = Jiffies::elapsed().as_u64().saturating_sub(start.as_u64());
        if elapsed >= READ_DETAIL_SLOW_THRESHOLD_JIFFIES {
            let event = READ_DETAIL_EVENTS.fetch_add(1, Ordering::Relaxed) + 1;
            if event <= READ_DETAIL_LOG_LIMIT {
                let detail_type = file.path().type_();
                let detail_name = file.path().name();
                ostd::early_println!(
                    "ASTERINAS_READ_DETAIL event={} pid={} tid={} fd={} len={} result={} elapsed_jiffies={} inode_type={:?} path_name={}",
                    event,
                    ctx.process.pid(),
                    ctx.posix_thread.tid(),
                    raw_fd,
                    buf_len,
                    read_len,
                    elapsed,
                    detail_type,
                    detail_name,
                );
            }
        }
    }

    if read_len > 0 {
        fs::vfs::notify::on_access(&file);
    }
    Ok(SyscallReturn::Return(read_len as _))
}
