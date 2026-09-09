// SPDX-License-Identifier: MPL-2.0

//! Linux's per-open kernel log record interface and deferred reader notifications.

use core::sync::atomic::{AtomicU64, Ordering};

use aster_logger::klog::{self, ReadError};
use spin::Once;

use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, PerOpenFileOps, SeekFrom, StatusFlags},
        vfs::inode::FileOps,
    },
    prelude::*,
    process::{
        UserNamespace,
        credentials::capabilities::CapSet,
        posix_thread::AsPosixThread,
        signal::{PollHandle, Pollable, Pollee, Poller},
    },
    security::lsm::hooks as lsm_hooks,
};

static LOG_POLLEE: Once<Pollee> = Once::new();

/// Registers for new records without caching readiness shared across reader cursors.
pub(crate) fn register_poller(poller: &mut PollHandle) {
    LOG_POLLEE.get().unwrap().register_poller(
        poller,
        IoEvents::IN | IoEvents::RDNORM | IoEvents::ERR | IoEvents::PRI,
    );
}

pub(super) fn init_in_first_kthread() {
    LOG_POLLEE.call_once(Pollee::new);
    // Logging can occur while scheduler and observer locks are held. Waking a
    // task from the logger would recursively acquire them. The timer delivers
    // coalesced notifications after those contexts have released their locks.
    // This callback must never log or acquire the ring lock while notifying.
    ostd::timer::register_callback_on_cpu(|| {
        if klog::klog().take_pending() {
            LOG_POLLEE
                .get()
                .unwrap()
                .notify(IoEvents::IN | IoEvents::RDNORM | IoEvents::ERR | IoEvents::PRI);
        }
    });
}

/// Checks log access against the initial user namespace.
pub(crate) fn check_privileged() -> Result<()> {
    let thread = current_thread!();
    let posix_thread = thread.as_posix_thread().unwrap();
    let user_ns = UserNamespace::get_init_singleton();
    lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        user_ns.as_ref(),
        posix_thread,
        CapSet::SYSLOG,
    ))
    .or_else(|_| {
        // Retain Linux's historical CAP_SYS_ADMIN compatibility fallback.
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            user_ns.as_ref(),
            posix_thread,
            CapSet::SYS_ADMIN,
        ))
    })
}

pub(super) struct KmsgFile {
    // Serialize reads and seeks, including user copies, using a sleeping lock.
    // Poll only loads the cursor and therefore never waits for a user copy.
    operation: Mutex<()>,
    sequence: AtomicU64,
}

impl KmsgFile {
    pub(super) fn new() -> Self {
        Self {
            operation: Mutex::new(()),
            sequence: AtomicU64::new(klog::klog().bounds().0),
        }
    }

    fn try_read(&self, writer: &mut VmWriter) -> Result<usize> {
        let _operation = self.operation.lock();
        let record = match klog::klog().record(self.sequence.load(Ordering::Relaxed)) {
            Ok(record) => record,
            Err(ReadError::Empty) => return_errno!(Errno::EAGAIN),
            Err(ReadError::Overrun(oldest)) => {
                self.sequence.store(oldest, Ordering::Relaxed);
                return_errno_with_message!(Errno::EPIPE, "the log record was overwritten");
            }
        };

        // Linux consumes a record even if the buffer is too small or usercopy
        // fails. See devkmsg_read() in Linux v6.16 kernel/printk/printk.c.
        self.sequence
            .store(record.sequence() + 1, Ordering::Relaxed);
        let mut text = String::new();
        record.format_kmsg(&mut text).unwrap();
        if writer.avail() < text.len() {
            return_errno_with_message!(Errno::EINVAL, "the log record does not fit");
        }
        writer.write_fallible(&mut VmReader::from(text.as_bytes()))?;
        Ok(text.len())
    }
}

impl Pollable for KmsgFile {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        if let Some(poller) = poller {
            register_poller(poller);
        }
        let (first, next, _) = klog::klog().bounds();
        let sequence = self.sequence.load(Ordering::Relaxed);
        let events = if sequence < first {
            IoEvents::IN | IoEvents::RDNORM | IoEvents::ERR | IoEvents::PRI
        } else if sequence < next {
            IoEvents::IN | IoEvents::RDNORM
        } else {
            IoEvents::empty()
        };
        events & (mask | IoEvents::ALWAYS_POLL)
    }
}

impl FileOps for KmsgFile {
    fn read_at(
        &self,
        _offset: usize,
        writer: &mut VmWriter,
        status_flags: StatusFlags,
    ) -> Result<usize> {
        if status_flags.contains(StatusFlags::O_NONBLOCK) {
            return self.try_read(writer);
        }

        let mut poller = Poller::new(None);
        register_poller(poller.as_handle_mut());
        loop {
            match self.try_read(writer) {
                Err(error) if error.error() == Errno::EAGAIN => poller.wait()?,
                result => return result,
            }
        }
    }

    fn write_at(
        &self,
        _offset: usize,
        reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        let len = reader.remain();
        if len > klog::MESSAGE_CAPACITY {
            return_errno_with_message!(Errno::EINVAL, "the log message is too long");
        }
        if len == 0 {
            return Ok(0);
        }
        let mut message = vec![0; len];
        reader.read_fallible(&mut VmWriter::from(message.as_mut_slice()))?;
        let (priority, message) = parse_priority(&message);
        klog::klog().push(priority, message);
        Ok(len)
    }
}

impl PerOpenFileOps for KmsgFile {
    fn check_open_access(&self, access_mode: AccessMode) -> Result<()> {
        if access_mode.is_readable() && klog::klog().dmesg_restrict() {
            check_privileged()?;
        }
        Ok(())
    }

    fn seek(&self, pos: SeekFrom) -> Option<Result<usize>> {
        Some(self.seek_record(pos))
    }

    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn check_positional_io(&self) -> Result<()> {
        return_errno_with_message!(Errno::ESPIPE, "positional log I/O is not supported");
    }

    fn is_offset_aware(&self) -> bool {
        false
    }
}

impl KmsgFile {
    fn seek_record(&self, pos: SeekFrom) -> Result<usize> {
        let _operation = self.operation.lock();
        let (first, next, cleared) = klog::klog().bounds();
        let sequence = match pos {
            SeekFrom::Start(0) => first,
            SeekFrom::End(0) => next,
            SeekFrom::Data(0) => cleared,
            SeekFrom::Current(0) | SeekFrom::Hole(0) => {
                return_errno_with_message!(Errno::EINVAL, "the log seek origin is invalid");
            }
            _ => return_errno_with_message!(Errno::ESPIPE, "the log seek offset must be zero"),
        };
        self.sequence.store(sequence, Ordering::Relaxed);
        Ok(0)
    }
}

fn parse_priority(message: &[u8]) -> (u16, &[u8]) {
    const DEFAULT_PRIORITY: u16 = (1 << 3) | 4;
    let Some(rest) = message.strip_prefix(b"<") else {
        return (DEFAULT_PRIORITY, message);
    };
    let mut priority = 0u32;
    for (index, byte) in rest.iter().copied().enumerate() {
        if byte == b'>' {
            // Linux uses an eight-bit facility and never allows users to
            // impersonate facility zero (kernel-generated records).
            let facility = ((priority >> 3) & 0xff).max(1) as u16;
            return ((facility << 3) | (priority as u16 & 7), &rest[index + 1..]);
        }
        if !byte.is_ascii_digit() {
            break;
        }
        let Some(next) = priority
            .checked_mul(10)
            .and_then(|value| value.checked_add((byte - b'0') as u32))
        else {
            break;
        };
        priority = next;
    }
    (DEFAULT_PRIORITY, message)
}
