// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/trace`, the tracing interfaces.
//!
//! Linux keeps these files on tracefs, mounted at `/sys/kernel/tracing`. This
//! kernel does not have a tracefs yet, so the same file layout lives under
//! procfs: the names, the read/write semantics and the record format are the
//! ones ftrace uses, which is what matters to a reader or a script; only the
//! mount point differs.
//!
//! ```text
//! /proc/trace/trace                 the buffer, as text (a snapshot)
//! /proc/trace/trace_pipe            the same, emptied when a read finishes
//! /proc/trace/tracing_on            0 / 1, the global switch
//! /proc/trace/available_events      subsystem:event, one per line
//! /proc/trace/trace_marker          write a message into the buffer
//! /proc/trace/events/<sub>/<ev>/enable   per-event switch
//! /proc/trace/events/<sub>/<ev>/format   the event's field format
//! ```

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::template::{
            ListedEntry, ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry,
            listed_entries_from_table, lookup_child_from_table, visit_listed_entries,
        },
        vfs::inode::Inode,
    },
    prelude::*,
    process::posix_thread::AsPosixThread,
    trace::{
        event::{self, TRACING_ON, TraceEvent},
        events::tracing::TRACE_MARKER,
        ring::{self, Record},
    },
};

/// The ring bounds of the read in progress; see `render_records`.
static SNAPSHOT: SpinLock<Option<[u64; ring::MAX_CPUS]>> = SpinLock::new(None);

/// The inode at `/proc/trace`.
pub struct TraceDirOps;

impl TraceDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [(
        &'static str,
        InodeType,
        fn(Weak<dyn Inode>) -> Arc<dyn Inode>,
    )] = &[
        ("trace", InodeType::File, |parent| {
            ProcFile::new(TraceFileOps { pipe: false }, parent, mkmod!(a+r))
        }),
        ("trace_pipe", InodeType::File, |parent| {
            ProcFile::new(TraceFileOps { pipe: true }, parent, mkmod!(a+r))
        }),
        ("tracing_on", InodeType::File, |parent| {
            ProcFile::new(TracingOnFileOps, parent, mkmod!(a+rw))
        }),
        ("available_events", InodeType::File, |parent| {
            ProcFile::new(AvailableEventsFileOps, parent, mkmod!(a+r))
        }),
        ("trace_marker", InodeType::File, |parent| {
            ProcFile::new(TraceMarkerFileOps, parent, mkmod!(a+w))
        }),
        ("profile", InodeType::File, |parent| {
            ProcFile::new(ProfileFileOps, parent, mkmod!(a+r))
        }),
        ("bpf", InodeType::File, |parent| {
            ProcFile::new(BpfFileOps, parent, mkmod!(a+r))
        }),
        ("events", InodeType::Dir, |parent| {
            ProcDir::new(EventsDirOps, parent, mkmod!(a+rx))
        }),
    ];
}

impl ProcDirOps for TraceDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if let Some(child) = lookup_child_from_table(name, Self::STATIC_ENTRIES, |f| {
            (f)(this_dir.this_weak().clone())
        }) {
            return Ok(child);
        }
        return_errno_with_message!(Errno::ENOENT, "the file does not exist");
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        visit_listed_entries(
            offset,
            listed_entries_from_table(Self::STATIC_ENTRIES),
            visit_fn,
        )
    }
}

/// `/proc/trace/trace` and `trace_pipe`.
struct TraceFileOps {
    /// Empty the buffer once a read reaches its end.
    pipe: bool,
}

impl ProcFileOps for TraceFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        render_records(offset, writer, self.pipe)
    }
}

/// `/proc/trace/tracing_on`.
struct TracingOnFileOps;

impl ProcFileOps for TracingOnFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let on = TRACING_ON.load(core::sync::atomic::Ordering::Relaxed);
        writeln!(printer, "{}", if on { 1 } else { 0 })?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let text = read_user_text(reader, 16)?;
        match text.trim() {
            "0" => TRACING_ON.store(false, core::sync::atomic::Ordering::Relaxed),
            "1" => TRACING_ON.store(true, core::sync::atomic::Ordering::Relaxed),
            _ => return_errno_with_message!(Errno::EINVAL, "write 0 or 1"),
        }
        // The function tracer's assembly stub cannot read this flag through
        // the event object, so it gets a mirror updated right here.
        crate::trace::function::sync_armed();
        Ok(text.len())
    }
}

/// `/proc/trace/trace_marker`: write a message into the buffer.
struct TraceMarkerFileOps;

impl ProcFileOps for TraceMarkerFileOps {
    fn read_at(&self, _offset: usize, _writer: &mut VmWriter) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "trace_marker is write-only");
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let text = read_user_text(reader, 512)?;
        let message = text.trim_end_matches('\n');
        // The marker is a plain message: the process and thread ids are the
        // ones writing the file. Like every other record it obeys the global
        // switch -- `tracing_on = 0` means the buffer takes nothing -- but it
        // needs no per-event enable: that is what `trace_marker` is for.
        if TRACING_ON.load(core::sync::atomic::Ordering::Relaxed) {
            let (pid, tid) = current_ids();
            ring::push(TRACE_MARKER.id(), pid, tid, message.as_bytes());
        }
        Ok(text.len())
    }
}

/// `/proc/trace/profile`: a histogram of the samples in the ring.
struct ProfileFileOps;

impl ProcFileOps for ProfileFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        crate::trace::profile::render_histogram(offset, writer)
    }
}

/// `/proc/trace/bpf`: the programs attached to trace events.
struct BpfFileOps;

impl ProcFileOps for BpfFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        crate::bpf::attach::render(&mut printer)
            .map_err(|_| Error::with_message(Errno::EIO, "failed to render the bpf table"))?;
        Ok(printer.bytes_written())
    }
}

/// `/proc/trace/available_events`.
struct AvailableEventsFileOps;
impl ProcFileOps for AvailableEventsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        for event in event::all_events() {
            writeln!(printer, "{}:{}", event.subsystem(), event.name())?;
        }
        Ok(printer.bytes_written())
    }
}

/// `/proc/trace/events`.
struct EventsDirOps;

impl ProcDirOps for EventsDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if event::subsystems().contains(&name) {
            // The subsystem name outlives the inode: it comes from the
            // event registry, which is static.
            let subsystem = event::subsystems()
                .iter()
                .copied()
                .find(|candidate| *candidate == name)
                .unwrap();
            return Ok(ProcDir::new(
                SubsystemDirOps { subsystem },
                this_dir.this_weak().clone(),
                mkmod!(a+rx),
            ));
        }
        return_errno_with_message!(Errno::ENOENT, "no such subsystem");
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        let entries = event::subsystems()
            .iter()
            .map(|name| ListedEntry::new(*name, InodeType::Dir));
        visit_listed_entries(offset, entries, visit_fn)
    }
}

/// `/proc/trace/events/<subsystem>`.
struct SubsystemDirOps {
    subsystem: &'static str,
}

impl ProcDirOps for SubsystemDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        let event = event::event_named(self.subsystem, name)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "no such event"))?;
        Ok(ProcDir::new(
            EventDirOps { event },
            this_dir.this_weak().clone(),
            mkmod!(a+rx),
        ))
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        let entries = event::events_of(self.subsystem)
            .map(|event| ListedEntry::new(event.name(), InodeType::Dir));
        visit_listed_entries(offset, entries, visit_fn)
    }
}

/// `/proc/trace/events/<subsystem>/<event>`.
struct EventDirOps {
    event: &'static TraceEvent,
}

impl ProcDirOps for EventDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        let parent = this_dir.this_weak().clone();
        match name {
            "enable" => Ok(ProcFile::new(
                EnableFileOps { event: self.event },
                parent,
                mkmod!(a+rw),
            )),
            "format" => Ok(ProcFile::new(
                FormatFileOps { event: self.event },
                parent,
                mkmod!(a+r),
            )),
            _ => return_errno_with_message!(Errno::ENOENT, "the file does not exist"),
        }
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        const ENTRIES: &[(&str, InodeType)] =
            &[("enable", InodeType::File), ("format", InodeType::File)];
        visit_listed_entries(
            offset,
            ENTRIES
                .iter()
                .map(|(name, type_)| ListedEntry::new(*name, *type_)),
            visit_fn,
        )
    }
}

/// `/proc/trace/events/<subsystem>/<event>/enable`.
struct EnableFileOps {
    event: &'static TraceEvent,
}

impl ProcFileOps for EnableFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", if self.event.enabled_flag() { 1 } else { 0 })?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let text = read_user_text(reader, 16)?;
        match text.trim() {
            "0" => self.event.set_enabled(false),
            "1" => self.event.set_enabled(true),
            _ => return_errno_with_message!(Errno::EINVAL, "write 0 or 1"),
        }
        crate::trace::function::sync_armed();
        Ok(text.len())
    }
}

/// `/proc/trace/events/<subsystem>/<event>/format`.
struct FormatFileOps {
    event: &'static TraceEvent,
}

impl ProcFileOps for FormatFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "name: {}", self.event.name())?;
        writeln!(printer, "ID: {}", self.event.id())?;
        writeln!(printer, "format:")?;
        writeln!(printer, "\t{}", self.event.format())?;
        Ok(printer.bytes_written())
    }
}

/// Reads up to `limit` bytes of a write into a string, tolerating garbage.
fn read_user_text(reader: &mut VmReader, limit: usize) -> Result<String> {
    let mut buffer = [0u8; 512];
    let limit = limit.min(buffer.len());
    let mut text = String::new();
    let mut consumed = 0;
    while consumed < limit {
        let read = reader
            .read_fallible(&mut VmWriter::from(&mut buffer[..]))
            .map_err(|(err, _)| err)?;
        if read == 0 {
            break;
        }
        text.push_str(core::str::from_utf8(&buffer[..read]).unwrap_or(""));
        consumed += read;
    }
    Ok(text)
}

/// The writing thread's process and thread ids, as the trace records show them.
fn current_ids() -> (u32, u32) {
    let process = current!();
    let thread = current_thread!();
    let tid = thread
        .as_posix_thread()
        .map(|posix_thread| posix_thread.tid())
        .unwrap_or(process.pid());
    (process.pid(), tid)
}

/// Renders the ring as ftrace-style lines, honouring `offset` and the writer's
/// room. Returns the number of bytes written.
///
/// A read that starts at offset 0 pins the ring bounds first: the records that
/// existed when reading began are the ones the file will hand out, so a busy
/// system cannot keep appending and turn `cat` into an endless stream.
fn render_records(offset: usize, writer: &mut VmWriter, clear_when_done: bool) -> Result<usize> {
    const HEADER: &[u8] = b"# tracer: nop\n";

    let bounds = if offset == 0 {
        let bounds = ring::heads();
        *SNAPSHOT.lock() = Some(bounds);
        bounds
    } else {
        SNAPSHOT.lock().unwrap_or_else(ring::heads)
    };

    let room = writer.avail();
    let mut sink = Sink {
        printer: VmPrinter::new_skip(writer, offset),
        skip: offset,
        room,
        stopped: false,
    };

    sink.push(HEADER)?;
    let mut complete = true;
    ring::for_each_record_bounded(
        |record| {
            if sink.stopped {
                complete = false;
                return false;
            }
            let mut line = LineBuf::new();
            if format_record(&mut line, &record).is_err() || sink.push(line.as_bytes()).is_err() {
                complete = false;
                return false;
            }
            true
        },
        bounds,
    );
    if complete && !sink.stopped {
        // The snapshot has been handed out in full.
        *SNAPSHOT.lock() = None;
        // A read that reached the end of the buffer consumes it, which is what
        // `trace_pipe` means; a read cut short by the writer's size leaves it.
        if clear_when_done {
            ring::clear();
        }
    }

    Ok(sink.printer.bytes_written())
}

/// Writes lines through a `VmPrinter` that skips the bytes before `offset`.
struct Sink<'a, 'b> {
    printer: VmPrinter<'a, 'b>,
    skip: usize,
    room: usize,
    stopped: bool,
}

impl Sink<'_, '_> {
    fn push(&mut self, line: &[u8]) -> Result<()> {
        let emitted = line.len().saturating_sub(self.skip);
        if emitted > self.room {
            self.stopped = true;
            return Ok(());
        }
        if self.printer.write_bytes(line).is_err() {
            self.stopped = true;
            return Ok(());
        }
        self.room -= emitted;
        self.skip = self.skip.saturating_sub(line.len());
        Ok(())
    }
}

/// A line-sized formatting buffer.
struct LineBuf {
    buf: [u8; 192],
    len: usize,
}

impl LineBuf {
    fn new() -> Self {
        Self {
            buf: [0; 192],
            len: 0,
        }
    }

    fn as_bytes(&self) -> &[u8] {
        &self.buf[..self.len]
    }
}

impl core::fmt::Write for LineBuf {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        let take = s.len().min(self.buf.len() - self.len);
        self.buf[self.len..self.len + take].copy_from_slice(&s.as_bytes()[..take]);
        self.len += take;
        Ok(())
    }
}

fn format_record(line: &mut LineBuf, record: &Record<'_>) -> core::fmt::Result {
    use core::fmt::Write;

    let (subsystem, name) = event::event_by_id(record.event)
        .map(|event| (event.subsystem(), event.name()))
        .unwrap_or(("unknown", "unknown"));
    write!(
        line,
        "{:>5}.{:06} [{:>3}] {}/{} {}:{}: ",
        record.timestamp.as_secs(),
        record.timestamp.subsec_micros(),
        record.cpu,
        record.pid,
        record.tid,
        subsystem,
        name,
    )?;

    // The function tracer stores a raw return address; everything else stores
    // text it formatted on the way in.
    if record.event == crate::trace::events::ftrace::FUNCTION.id() && record.data.len() >= 8 {
        let mut bytes = [0u8; 8];
        bytes.copy_from_slice(&record.data[..8]);
        let address = u64::from_le_bytes(bytes);
        match crate::trace::kallsyms::lookup_with_offset(address) {
            Some((symbol, 0)) => write!(line, "func={}\n", symbol),
            Some((symbol, offset)) => write!(line, "func={}+{:#x}\n", symbol, offset),
            None => write!(line, "func=<{:#x}>\n", address),
        }
    } else if record.event == crate::trace::events::profile::SAMPLE.id() {
        match crate::trace::profile::decode(record.data) {
            Some((pc, true)) => write!(line, "pc={:#x} user\n", pc),
            Some((pc, false)) => match crate::trace::kallsyms::lookup_with_offset(pc) {
                Some((symbol, 0)) => write!(line, "pc={}\n", symbol),
                Some((symbol, offset)) => write!(line, "pc={}+{:#x}\n", symbol, offset),
                None => write!(line, "pc={:#x}\n", pc),
            },
            None => write!(line, "<short sample>\n"),
        }
    } else {
        let payload = core::str::from_utf8(record.data).unwrap_or("<bytes>");
        write!(line, "{}\n", payload.trim_end_matches('\n'))
    }
}
