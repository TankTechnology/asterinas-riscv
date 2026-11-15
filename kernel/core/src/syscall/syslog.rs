// SPDX-License-Identifier: MPL-2.0

//! Linux's legacy kernel log actions, sharing records with `/dev/kmsg`.

use aster_logger::klog::{self, LogRecord, ReadError};

use super::SyscallReturn;
use crate::{device::kmsg, prelude::*, process::signal::Poller};

pub fn sys_syslog(action: i32, buf: Vaddr, len: i32, ctx: &Context) -> Result<SyscallReturn> {
    // Linux checks permissions before validating the action or its arguments.
    // Only READ_ALL and SIZE_BUFFER can be unprivileged when restriction is off.
    // Reference: Linux v6.16 kernel/printk/printk.c, do_syslog().
    let is_restricted_action =
        action != LogAction::ReadAll as i32 && action != LogAction::SizeBuffer as i32;
    if klog::klog().dmesg_restrict() || is_restricted_action {
        kmsg::check_privileged()?;
    }
    let action = LogAction::try_from(action)?;

    let result = match action {
        LogAction::Close | LogAction::Open => 0,
        LogAction::Read | LogAction::ReadAll | LogAction::ReadClear => {
            if buf == 0 || len < 0 {
                return_errno_with_message!(Errno::EINVAL, "the log buffer or length is invalid");
            }
            if len == 0 {
                return Ok(SyscallReturn::Return(0));
            }
            let user_space = ctx.user_space();
            let mut writer = user_space.writer(buf, len as usize)?;
            match action {
                LogAction::Read => read(&mut writer)?,
                LogAction::ReadAll | LogAction::ReadClear => read_all(&mut writer, action)?,
                _ => unreachable!(),
            }
        }
        LogAction::Clear => {
            klog::klog().clear_to(klog::klog().bounds().1);
            0
        }
        LogAction::ConsoleOff => {
            klog::klog().disable_console();
            0
        }
        LogAction::ConsoleOn => {
            klog::klog().enable_console();
            0
        }
        LogAction::ConsoleLevel => {
            if !(1..=8).contains(&len) {
                return_errno_with_message!(Errno::EINVAL, "the console log level is invalid");
            }
            klog::klog().set_console_level(len as u8);
            0
        }
        LogAction::SizeUnread => SYSLOG_CURSOR.lock().size_unread(),
        LogAction::SizeBuffer => klog::klog().capacity(),
    };
    Ok(SyscallReturn::Return(result as isize))
}

#[repr(i32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
enum LogAction {
    Close = 0,
    Open = 1,
    Read = 2,
    ReadAll = 3,
    ReadClear = 4,
    Clear = 5,
    ConsoleOff = 6,
    ConsoleOn = 7,
    ConsoleLevel = 8,
    SizeUnread = 9,
    SizeBuffer = 10,
}

// This sleeping mutex only serializes destructive cursor operations. Neither
// waiting for a new record nor READ_ALL/CLEAR holds it. The ring lock is always
// released by record() before formatting, allocation, or usercopy begins.
static SYSLOG_CURSOR: Mutex<ReadCursor> = Mutex::new(ReadCursor {
    sequence: 0,
    partial: 0,
});

struct ReadCursor {
    sequence: u64,
    partial: usize,
}

fn read(writer: &mut VmWriter) -> Result<usize> {
    let mut poller = Poller::new(None);
    kmsg::register_poller(poller.as_handle_mut());
    loop {
        let result = SYSLOG_CURSOR.lock().try_read(writer);
        match result {
            Err(error) if error.error() == Errno::EAGAIN => {
                poller.wait().map_err(|error| match error.error() {
                    Errno::EINTR => Error::new(Errno::ERESTARTSYS),
                    _ => error,
                })?;
            }
            result => return result,
        }
    }
}

impl ReadCursor {
    fn try_read(&mut self, writer: &mut VmWriter) -> Result<usize> {
        let end = klog::klog().bounds().1;
        let mut written = 0;
        while self.sequence < end && writer.has_avail() {
            let record = match klog::klog().record(self.sequence) {
                Ok(record) => record,
                Err(ReadError::Overrun(oldest)) => {
                    self.sequence = oldest;
                    self.partial = 0;
                    continue;
                }
                Err(ReadError::Empty) => break,
            };
            let text = syslog_text(&record);
            let available = text.len() - self.partial;
            if written != 0 && available > writer.avail() {
                break;
            }
            let count = available.min(writer.avail());
            let start = self.partial;
            if count == available {
                self.sequence = record.sequence() + 1;
                self.partial = 0;
            } else {
                self.partial += count;
            }
            match writer.write_fallible(&mut VmReader::from(&text[start..start + count])) {
                Ok(count) => written += count,
                Err((error, _)) if written == 0 => return Err(error.into()),
                Err(_) => break,
            }
        }
        if written == 0 {
            return_errno!(Errno::EAGAIN);
        }
        Ok(written)
    }

    fn size_unread(&mut self) -> usize {
        let (first, end, _) = klog::klog().bounds();
        if self.sequence < first {
            self.sequence = first;
            self.partial = 0;
        }
        let mut total = 0;
        for sequence in self.sequence..end {
            if let Ok(record) = klog::klog().record(sequence) {
                let mut len = 0;
                record.write_syslog(|bytes| len += bytes.len());
                total += if sequence == self.sequence {
                    len - self.partial
                } else {
                    len
                };
            }
        }
        total
    }
}

fn read_all(writer: &mut VmWriter, action: LogAction) -> Result<usize> {
    let (first, end, cleared) = klog::klog().bounds();
    let mut records = VecDeque::new();
    let mut total = 0;
    // Select the newest suffix of whole formatted records that fits. Stop at
    // the snapshot tail so concurrent writes cannot extend or be cleared by
    // this operation. Ring overwrites can only shorten the retained suffix.
    for sequence in (first.max(cleared)..end).rev() {
        let Ok(record) = klog::klog().record(sequence) else {
            break;
        };
        let text = syslog_text(&record);
        if text.len() > writer.avail() - total {
            break;
        }
        total += text.len();
        records.push_front((sequence, text));
    }

    for (sequence, text) in records {
        if let Err((error, _)) = writer.write_fallible(&mut VmReader::from(text.as_slice())) {
            if action == LogAction::ReadClear {
                klog::klog().clear_to(sequence);
            }
            return Err(error.into());
        }
    }
    if action == LogAction::ReadClear {
        klog::klog().clear_to(end);
    }
    Ok(total)
}

fn syslog_text(record: &LogRecord) -> Vec<u8> {
    let mut text = Vec::new();
    record.write_syslog(|bytes| text.extend_from_slice(bytes));
    text
}
