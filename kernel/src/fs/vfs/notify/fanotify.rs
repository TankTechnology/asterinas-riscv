// SPDX-License-Identifier: MPL-2.0

use core::{
    fmt::Display,
    sync::atomic::{AtomicU32, Ordering},
};

use crate::{
    events::IoEvents,
    fs::{
        file::{
            AccessMode, FileCommon, FileLike, SettableStatusFlags, StatusFlags, file_table::FdFlags,
        },
        pseudofs::AnonInodeFs,
        vfs::{
            inode::Inode,
            inode_ext::InodeExt,
            notify::{FsEventSubscriber, FsEvents},
            path::Path,
        },
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};

/// `FAN_NOFD` — the event carries no file descriptor.
const FAN_NOFD: i32 = -1;

/// `FANOTIFY_METADATA_VERSION` from `linux/fanotify.h`.
const FANOTIFY_METADATA_VERSION: u8 = 3;

/// `struct fanotify_event_metadata` from `linux/fanotify.h`.
///
/// 24 bytes: `event_len`(4) + `vers`(1) + `reserved`(1) + `metadata_len`(2) +
/// `mask`(8, 8-aligned) + `fd`(4) + `pid`(4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct FanotifyEventMetadata {
    event_len: u32,
    vers: u8,
    reserved: u8,
    metadata_len: u16,
    mask: u64,
    fd: i32,
    pid: i32,
}

const _: () = assert!(size_of::<FanotifyEventMetadata>() == 24);

impl FanotifyEventMetadata {
    fn new(mask: u32) -> Self {
        let pid = crate::process::Process::current().map_or(FAN_NOFD, |p| p.pid() as i32);
        Self {
            event_len: size_of::<Self>() as u32,
            vers: FANOTIFY_METADATA_VERSION,
            reserved: 0,
            metadata_len: size_of::<Self>() as u16,
            mask: mask as u64,
            fd: FAN_NOFD,
            pid,
        }
    }
}

/// A fanotify mark on a single inode.
///
/// A mark is a subscriber registered on an inode's
/// [`FsEventPublisher`](crate::fs::vfs::notify::FsEventPublisher). Unlike
/// inotify (which assigns watch descriptors), fanotify addresses marks by mask
/// directly, so the subscriber stores just the event mask it is interested in.
struct FanotifySubscriber {
    mask: AtomicU32,
    fanotify_file: Weak<FanotifyFile>,
}

impl FanotifySubscriber {
    fn new(fanotify_file: Weak<FanotifyFile>, mask: u32) -> Self {
        Self {
            mask: AtomicU32::new(mask),
            fanotify_file,
        }
    }

    fn or_mask(&self, mask: u32) {
        self.mask.fetch_or(mask, Ordering::Relaxed);
    }
}

impl FsEventSubscriber for FanotifySubscriber {
    fn deliver_event(&self, event: FsEvents, _name: Option<String>) -> bool {
        // fanotify has no IGNORED event (unlike inotify), so there is nothing to
        // do for a publisher teardown notification.
        if event.contains(FsEvents::IN_IGNORED) {
            return false;
        }

        let interesting = self.interesting_events();
        if !event.intersects(interesting) {
            return false;
        }

        // Deliver only the bits the mark was registered for. `FsEvents::ISDIR` and
        // `FsEvents::EVENT_ON_CHILD` already share their bit values with
        // `FAN_ONDIR` / `FAN_EVENT_ON_CHILD`, so the raw bits are ABI-correct.
        let mask = (event & interesting).bits();
        if let Some(file) = self.fanotify_file.upgrade() {
            file.receive_event(mask);
        }

        false
    }

    fn interesting_events(&self) -> FsEvents {
        FsEvents::from_bits_truncate(self.mask.load(Ordering::Relaxed))
    }
}

/// An entry in the fanotify file's mark table.
struct MarkEntry {
    inode: Weak<dyn Inode>,
    subscriber: Weak<FanotifySubscriber>,
}

/// A file-like object that provides fanotify functionality.
///
/// `FanotifyFile` accepts events from fanotify marks on various inodes and
/// returns them to userspace as `struct fanotify_event_metadata` records on
/// `read(2)`. Only the notification class is implemented (no permission
/// events), and events carry no file descriptor (`fd == FAN_NOFD`).
pub struct FanotifyFile {
    // Marks registered on inodes. Kept as weak refs so a mark dies with its inode.
    marks: SpinLock<Vec<MarkEntry>>,
    // Serialises concurrent `read()` operations.
    read_mutex: Mutex<()>,
    // A bounded queue of fanotify event metadata.
    event_queue: SpinLock<VecDeque<FanotifyEventMetadata>>,
    // The maximum capacity of the event queue.
    queue_capacity: usize,
    // A pollable object for this fanotify file.
    pollee: Pollee,
    // The common state for this fanotify file.
    common: FileCommon,
    // A weak reference to this fanotify file.
    this: Weak<FanotifyFile>,
}

/// The default maximum capacity of the event queue.
const DEFAULT_MAX_QUEUED_EVENTS: usize = 16384;

impl Drop for FanotifyFile {
    fn drop(&mut self) {
        let marks = self.marks.get_mut();
        for entry in marks.drain(..) {
            let (Some(inode), Some(subscriber)) =
                (entry.inode.upgrade(), entry.subscriber.upgrade())
            else {
                continue;
            };
            if inode
                .fs_event_publisher()
                .unwrap()
                .remove_subscriber(&(subscriber as _))
            {
                inode.fs().fs_event_subscriber_stats().remove_subscriber();
            }
        }
    }
}

impl FanotifyFile {
    /// Creates a new fanotify file.
    pub fn new(is_nonblocking: bool) -> Result<Arc<Self>> {
        let pseudo_path = AnonInodeFs::new_path(|_| "anon_inode:fanotify".to_string());
        let status_flags = if is_nonblocking {
            StatusFlags::O_NONBLOCK
        } else {
            StatusFlags::empty()
        };

        Ok(Arc::new_cyclic(|weak_self| Self {
            marks: SpinLock::new(Vec::new()),
            read_mutex: Mutex::new(()),
            event_queue: SpinLock::new(VecDeque::new()),
            queue_capacity: DEFAULT_MAX_QUEUED_EVENTS,
            pollee: Pollee::new(),
            common: FileCommon::new(pseudo_path, status_flags),
            this: weak_self.clone(),
        }))
    }

    /// Adds (or OR-extends) a mark on a path.
    pub fn add_mark(&self, path: &Path, mask: u32) -> Result<()> {
        let inode_weak = Arc::downgrade(path.inode());

        // Try to find and extend an existing mark first.
        {
            let marks = self.marks.lock();
            for entry in marks.iter() {
                if !Weak::ptr_eq(&entry.inode, &inode_weak) {
                    continue;
                }
                let Some(subscriber) = entry.subscriber.upgrade() else {
                    continue;
                };
                subscriber.or_mask(mask);
                path.inode()
                    .fs_event_publisher()
                    .unwrap()
                    .update_subscriber_events();
                return Ok(());
            }
        }

        // Create a new subscriber and register it.
        let subscriber = Arc::new(FanotifySubscriber::new(self.this.clone(), mask));
        let dyn_subscriber = subscriber.clone() as Arc<dyn FsEventSubscriber>;

        let inode = path.inode();
        if !inode
            .fs_event_publisher_or_init()
            .add_subscriber(dyn_subscriber)
        {
            return_errno_with_message!(
                Errno::ENOENT,
                "adding a fanotify mark to a deleted inode is not supported yet"
            );
        }
        inode.fs().fs_event_subscriber_stats().add_subscriber();

        let entry = MarkEntry {
            inode: inode_weak,
            subscriber: Arc::downgrade(&subscriber),
        };
        self.marks.lock().push(entry);

        Ok(())
    }

    /// Removes a mark from a path.
    pub fn remove_mark(&self, path: &Path) -> Result<()> {
        let inode_weak = Arc::downgrade(path.inode());

        let idx = {
            let marks = self.marks.lock();
            marks
                .iter()
                .position(|entry| Weak::ptr_eq(&entry.inode, &inode_weak))
        };
        let Some(idx) = idx else {
            return_errno_with_message!(Errno::ENOENT, "no fanotify mark on this inode");
        };

        let entry = self.marks.lock().remove(idx);
        let (inode, subscriber) = match (entry.inode.upgrade(), entry.subscriber.upgrade()) {
            (Some(inode), Some(subscriber)) => (inode, subscriber),
            _ => return Ok(()),
        };

        if inode
            .fs_event_publisher()
            .unwrap()
            .remove_subscriber(&(subscriber as _))
        {
            inode.fs().fs_event_subscriber_stats().remove_subscriber();
        }

        Ok(())
    }

    /// Removes all marks from this fanotify file.
    pub fn flush(&self) -> usize {
        let mut marks = self.marks.lock();
        let mut count = 0;
        for entry in marks.drain(..) {
            let (Some(inode), Some(subscriber)) =
                (entry.inode.upgrade(), entry.subscriber.upgrade())
            else {
                continue;
            };
            if inode
                .fs_event_publisher()
                .unwrap()
                .remove_subscriber(&(subscriber as _))
            {
                inode.fs().fs_event_subscriber_stats().remove_subscriber();
            }
            count += 1;
        }
        count
    }

    /// Queues a fanotify event to be read by userspace.
    fn receive_event(&self, mask: u32) {
        let meta = FanotifyEventMetadata::new(mask);

        {
            let mut event_queue = self.event_queue.lock();
            // If the queue is full, drop the event (Linux queues an overflow event;
            // we keep it simple and drop, which is observable but not ABI-breaking).
            if event_queue.len() >= self.queue_capacity {
                return;
            }
            event_queue.push_back(meta);
        }
        self.pollee.notify(IoEvents::IN);
    }

    /// Pops an event from the queue.
    fn pop_event(&self) -> Option<FanotifyEventMetadata> {
        let mut event_queue = self.event_queue.lock();
        let event = event_queue.pop_front();
        if event_queue.is_empty() {
            self.pollee.invalidate();
        }
        event
    }

    /// Tries to read events from the queue into the user buffer.
    fn try_read(&self, writer: &mut VmWriter) -> Result<usize> {
        let _guard = self.read_mutex.lock();

        let mut size = 0;
        let mut consumed = 0;
        while let Some(meta) = self.pop_event() {
            match writer.write_val(&meta) {
                Ok(()) => {
                    size += size_of::<FanotifyEventMetadata>();
                    consumed += 1;
                }
                Err(err) => {
                    self.event_queue.lock().push_front(meta);
                    if consumed == 0 {
                        return Err(err.into());
                    }
                    return Ok(size);
                }
            }
        }

        if consumed == 0 {
            return_errno_with_message!(Errno::EAGAIN, "no fanotify events are available");
        }
        Ok(size)
    }

    fn check_io_events(&self) -> IoEvents {
        if self.event_queue.lock().is_empty() {
            IoEvents::empty()
        } else {
            IoEvents::IN
        }
    }
}

impl Pollable for FanotifyFile {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee
            .poll_with(mask, poller, || self.check_io_events())
    }
}

impl FileLike for FanotifyFile {
    fn read(&self, writer: &mut VmWriter) -> Result<usize> {
        if self.common.is_nonblocking() {
            self.try_read(writer)
        } else {
            self.wait_events(IoEvents::IN, None, || self.try_read(writer))
        }
    }

    fn settable_status_flags(&self) -> SettableStatusFlags {
        SettableStatusFlags::minimal().with_o_async()
    }

    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDONLY
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            inner: Arc<FanotifyFile>,
            fd_flags: FdFlags,
        }

        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                let mut flags =
                    self.inner.common.status_flags().bits() | self.inner.access_mode() as u32;
                if self.fd_flags.contains(FdFlags::CLOEXEC) {
                    flags |= crate::fs::file::CreationFlags::O_CLOEXEC.bits();
                }

                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", flags)?;
                writeln!(f, "mnt_id:\t{}", AnonInodeFs::mount_node().id())?;
                writeln!(f, "ino:\t{}", AnonInodeFs::shared_inode().ino())?;

                for entry in self.inner.marks.lock().iter() {
                    let (Some(inode), Some(subscriber)) =
                        (entry.inode.upgrade(), entry.subscriber.upgrade())
                    else {
                        continue;
                    };
                    let mask = subscriber.interesting_events().bits();
                    let sdev = inode.fs().sb().fsid;
                    writeln!(
                        f,
                        "fanotify ino:{:x} sdev:{:x} mflags:0 mask:{:x} ignored_mask:0",
                        inode.ino(),
                        sdev,
                        mask
                    )?;
                }

                Ok(())
            }
        }

        Box::new(FdInfo {
            inner: self,
            fd_flags,
        })
    }
}
