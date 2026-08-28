// SPDX-License-Identifier: MPL-2.0

use core::{fmt, mem};

use aster_bigtcp::socket::ReceiveBehavior;
use aster_rights::ReadOp;
use ostd::task::Task;

use super::{
    CUserCred, UnixDatagramSocket, UnixStreamSocket, cred::SocketCred, scm_graph::SocketNode,
};
use crate::{
    events::EpollFile,
    fs::file::{
        FileLike, InodeHandle,
        file_table::{FdFlags, get_file_fast},
    },
    net::socket::util::{CControlHeader, ControlMessage, RecvFlags},
    prelude::*,
    process::{
        PidFile, UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread,
    },
    security::lsm::hooks as lsm_hooks,
    util::net::CSocketOptionLevel,
};

#[derive(Debug)]
pub struct UnixControlMessage(Message);

#[derive(Debug)]
enum Message {
    Files(FileMessage),
    Cred(CredMessage),
}

impl UnixControlMessage {
    pub fn read_from(header: &CControlHeader, reader: &mut VmReader) -> Result<Option<Self>> {
        debug_assert_eq!(header.level(), Some(CSocketOptionLevel::SOL_SOCKET));

        let Ok(type_) = CControlType::try_from(header.type_()) else {
            warn!("unsupported control message type in {:?}", header);
            reader.skip(header.payload_len());
            return Ok(None);
        };

        match type_ {
            CControlType::SCM_RIGHTS => {
                let msg = FileMessage::read_from(header, reader)?;
                Ok(Some(Self(Message::Files(msg))))
            }
            CControlType::SCM_CREDENTIALS => {
                let msg = CredMessage::read_from(header, reader)?;
                Ok(Some(Self(Message::Cred(msg))))
            }
            _ => {
                warn!("unsupported control message type in {:?}", header);
                reader.skip(header.payload_len());
                Ok(None)
            }
        }
    }

    pub fn write_to(&self, writer: &mut VmWriter) -> Result<(CControlHeader, RecvFlags)> {
        match &self.0 {
            Message::Files(msg) => msg.write_to(writer),
            Message::Cred(msg) => msg.write_to(writer),
        }
    }
}

struct FileMessage {
    files: Vec<Arc<dyn FileLike>>,
}

impl Debug for FileMessage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("FileMessage")
            .field("len", &self.files.len())
            .finish_non_exhaustive()
    }
}

/// The maximum number of the file descriptors in the control messages.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.15/source/include/net/scm.h#L18>.
const MAX_NR_FILES: usize = 253;

impl FileMessage {
    fn read_from(header: &CControlHeader, reader: &mut VmReader) -> Result<Self> {
        let payload_len = header.payload_len();
        if !payload_len.is_multiple_of(size_of::<i32>()) {
            return_errno_with_message!(Errno::EINVAL, "the SCM_RIGHTS message is invalid");
        }
        let nfiles = payload_len / size_of::<i32>();

        // "Attempting to send an array larger than this limit causes sendmsg(2) to fail with the
        // error EINVAL." -- Reference: <https://man7.org/linux/man-pages/man7/unix.7.html>.
        if nfiles > MAX_NR_FILES {
            return_errno_with_message!(Errno::EINVAL, "the SCM_RIGHTS message is too large");
        }
        // TODO: "[the ETOOMANYREFS error] occurs if the number of "in-flight" file descriptors
        // exceeds the RLIMIT_NOFILE resource limit and the caller does not have the
        // CAP_SYS_RESOURCE capability."

        let mut files = Vec::with_capacity(nfiles);

        let current = Task::current().unwrap();
        let mut file_table = current.as_thread_local().unwrap().borrow_file_table_mut();
        for _ in 0..nfiles {
            let fd = reader.read_val::<i32>()?;
            let file = get_file_fast!(&mut file_table, fd.try_into()?).into_owned();
            files.push(file);
        }

        Ok(FileMessage { files })
    }

    fn write_to(&self, writer: &mut VmWriter) -> Result<(CControlHeader, RecvFlags)> {
        let nfiles = self
            .files
            .len()
            .min(CControlHeader::payload_len_from_total(writer.avail())? / size_of::<i32>());
        if nfiles == 0 {
            return_errno_with_message!(Errno::EINVAL, "the control message buffer is too small");
        }
        let output_flags = if nfiles < self.files.len() {
            RecvFlags::MSG_CTRUNC
        } else {
            RecvFlags::empty()
        };

        let header = CControlHeader::new(
            CSocketOptionLevel::SOL_SOCKET,
            CControlType::SCM_RIGHTS as i32,
            nfiles * size_of::<i32>(),
        );
        writer.write_val::<CControlHeader>(&header)?;

        let current = Task::current().unwrap();
        let file_table = current.as_thread_local().unwrap().borrow_file_table();
        for file in self.files[..nfiles].iter() {
            // TODO: Deal with the `O_CLOEXEC` flag.
            let fd = file_table
                .unwrap()
                .write()
                .insert(file.clone(), FdFlags::empty());
            // Perhaps we should remove the inserted files from the file table if we cannot write
            // the file descriptor back to user space? However, even Linux cannot handle every
            // corner case (https://elixir.bootlin.com/linux/v6.15.2/source/net/core/scm.c#L357).
            writer.write_val::<i32>(&(fd.into()))?;
        }

        Ok((header, output_flags))
    }
}

#[derive(Debug)]
struct CredMessage {
    cred: CUserCred,
}

impl CredMessage {
    fn read_from(header: &CControlHeader, reader: &mut VmReader) -> Result<Self> {
        if header.payload_len() != size_of::<CUserCred>() {
            return_errno_with_message!(Errno::EINVAL, "the SCM_CREDENTIALS message is invalid");
        }

        let cred = reader.read_val()?;

        Ok(Self { cred })
    }

    fn write_to(&self, writer: &mut VmWriter) -> Result<(CControlHeader, RecvFlags)> {
        let payload_len =
            size_of::<CUserCred>().min(CControlHeader::payload_len_from_total(writer.avail())?);
        let output_flags = if payload_len != size_of::<CUserCred>() {
            RecvFlags::MSG_CTRUNC
        } else {
            RecvFlags::empty()
        };

        let header = CControlHeader::new(
            CSocketOptionLevel::SOL_SOCKET,
            CControlType::SCM_CREDENTIALS as i32,
            payload_len,
        );
        writer.write_val(&header)?;
        writer.write_fallible(&mut VmReader::from(self.cred.as_bytes()))?;

        Ok((header, output_flags))
    }
}

/// Control message types.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.13/source/include/linux/socket.h#L178>.
#[expect(non_camel_case_types)]
#[repr(i32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
enum CControlType {
    SCM_RIGHTS = 1,
    SCM_CREDENTIALS = 2,
    SCM_SECURITY = 3,
    SCM_PIDFD = 4,
}

/// Auxiliary data associated with UNIX messages.
///
/// In UNIX sockets, one can send payload bytes with multiple control messages. If these control
/// messages need to be sent to a remote endpoint, they are packaged in this type and transmitted.
///
/// We use this type instead of transmitting control messages directly to the remote endpoint
/// because control messages of the same type (e.g., files) can be merged and missing control
/// messages of certain types (e.g., credentials) can be supplied automatically according to socket
/// option settings.
#[derive(Default)]
pub(super) struct AuxiliaryData {
    files: Vec<Arc<dyn FileLike>>,
    cred: Option<SocketCred>,
    scm_files: ScmFiles,
}

#[derive(Default)]
struct ScmFiles {
    /// One stable node per AF_UNIX FD occurrence, including duplicates.
    passed_sockets: Vec<SocketNode>,
    has_stream_socket: bool,
    has_datagram_socket: bool,
    has_unsupported_file: bool,
}

enum ScmFileClass {
    DirectStream(SocketNode),
    DirectDatagram(SocketNode),
    ProvenLeaf,
    /// A file type that may strongly own another file but does not expose that ownership to B1.
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LegacySendPolicy {
    Allow,
    RequireSysAdmin,
}

impl AuxiliaryData {
    /// Builds the auxiliary data from the control messages.
    pub(super) fn from_control(ctrl_msgs: Vec<ControlMessage>) -> Result<Self> {
        let mut files = Vec::new();
        let mut cred = None;

        for ctrl_msg in ctrl_msgs.into_iter() {
            // Control messages of other protocols are not expected on UNIX
            // sockets; ignore them.
            let ControlMessage::Unix(unix_ctrl_msg) = ctrl_msg else {
                continue;
            };

            match unix_ctrl_msg.0 {
                Message::Files(FileMessage {
                    files: mut msg_files,
                }) => {
                    if msg_files.len() > MAX_NR_FILES - files.len() {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "the SCM_RIGHTS message is too large"
                        );
                    }
                    files.append(&mut msg_files);
                }
                Message::Cred(CredMessage { cred: msg_cred }) => {
                    let cur_cred = SocketCred::<ReadOp>::new_current();
                    if cur_cred.to_real_c_cred() != msg_cred {
                        // FIXME: Allow this if we're root or have the CAP_SYS_ADMIN capability.
                        return_errno_with_message!(
                            Errno::EPERM,
                            "setting others' credentials is not allowed"
                        );
                    }
                    cred = Some(cur_cred);
                }
            }
        }

        let scm_files = ScmFiles::classify(&files);

        Ok(Self {
            files,
            cred,
            scm_files,
        })
    }

    /// Applies the compatibility policy that predates B1 graph enforcement.
    ///
    /// Slice 4 deliberately gates only direct stream/seqpacket descriptors, exactly as the old
    /// `from_control` check did. Direct datagram and unsupported containers are classified for
    /// later B1 slices but remain accepted here, so this split does not expand or restrict any
    /// syscall behavior.
    pub(super) fn enforce_legacy_send_policy(&self) -> Result<()> {
        self.enforce_legacy_send_policy_with(|| {
            warn!("UNIX sockets in SCM_RIGHTS messages can leak kernel resource");

            let current = current_thread!();
            let posix_thread = current.as_posix_thread().unwrap();
            lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
                UserNamespace::get_init_singleton().as_ref(),
                posix_thread,
                CapSet::SYS_ADMIN,
            ))
        })
    }

    fn enforce_legacy_send_policy_with(
        &self,
        require_sys_admin: impl FnOnce() -> Result<()>,
    ) -> Result<()> {
        match self.scm_files.legacy_send_policy() {
            LegacySendPolicy::Allow => Ok(()),
            LegacySendPolicy::RequireSysAdmin => require_sys_admin(),
        }
    }

    /// Returns stable socket identities with one entry per SCM_RIGHTS FD occurrence.
    pub(super) fn passed_sockets(&self) -> &[SocketNode] {
        &self.scm_files.passed_sockets
    }

    /// Returns whether B1 encountered a file container whose strong ownership is not modeled.
    pub(super) fn has_unsupported_file(&self) -> bool {
        self.scm_files.has_unsupported_file
    }

    /// Returns whether this batch must remain closed until Slice 6 tracks datagram queue edges.
    pub(super) fn has_datagram_socket_pending_slice6(&self) -> bool {
        self.scm_files.has_datagram_socket
    }

    #[cfg(ktest)]
    pub(super) fn new_test_scm(
        passed_sockets: Vec<SocketNode>,
        has_unsupported_file: bool,
    ) -> Self {
        Self {
            files: Vec::new(),
            cred: None,
            scm_files: ScmFiles {
                has_stream_socket: !passed_sockets.is_empty(),
                has_datagram_socket: false,
                passed_sockets,
                has_unsupported_file,
            },
        }
    }

    #[cfg(ktest)]
    pub(super) fn new_test_datagram_scm(passed_socket: SocketNode) -> Self {
        Self {
            files: Vec::new(),
            cred: None,
            scm_files: ScmFiles {
                passed_sockets: vec![passed_socket],
                has_stream_socket: false,
                has_datagram_socket: true,
                has_unsupported_file: false,
            },
        }
    }

    /// Fills the current credentials if there are no credentials.
    pub(super) fn fill_cred(&mut self) {
        if self.cred.is_none() {
            self.cred = Some(SocketCred::<ReadOp>::new_current());
        }
    }

    /// Generates the control messages from the auxiliary data.
    pub(super) fn generate_control(
        &mut self,
        behavior: ReceiveBehavior,
        is_pass_cred: bool,
    ) -> Vec<ControlMessage> {
        let mut ctrl_msgs = Vec::new();

        let Self { files, cred, .. } = self;

        if is_pass_cred {
            let unix_ctrl_msg = UnixControlMessage(Message::Cred(CredMessage {
                cred: cred
                    .as_ref()
                    .map(SocketCred::to_real_c_cred)
                    .unwrap_or_else(CUserCred::new_overflow),
            }));
            ctrl_msgs.push(ControlMessage::Unix(unix_ctrl_msg));
        }

        if !files.is_empty() {
            let files = match behavior {
                ReceiveBehavior::Recv => mem::take(files),
                ReceiveBehavior::Peek => files.clone(),
            };
            let unix_ctrl_msg = UnixControlMessage(Message::Files(FileMessage { files }));
            ctrl_msgs.push(ControlMessage::Unix(unix_ctrl_msg));
        }

        ctrl_msgs
    }

    /// Returns whether the auxiliary data contains nothing.
    pub(super) fn is_empty(&self) -> bool {
        self.files.is_empty()
            && self.cred.is_none()
            && self.scm_files.passed_sockets.is_empty()
            && !self.scm_files.has_unsupported_file
    }

    /// Returns whether the auxiliary data can be treated as a subset of the other one.
    ///
    /// In stream sockets, we can receive more bytes at once if the current auxiliary data is a
    /// subset of the subsequent auxiliary data.
    pub(super) fn is_subset_of(&self, other: &Self, is_pass_cred: bool) -> bool {
        if !self.files.is_empty() {
            return false;
        }

        if is_pass_cred
            && self.cred.as_ref().map(SocketCred::to_real_c_cred)
                != other.cred.as_ref().map(SocketCred::to_real_c_cred)
        {
            return false;
        }

        true
    }
}

impl ScmFiles {
    fn classify(files: &[Arc<dyn FileLike>]) -> Self {
        Self::from_classes(files.iter().map(classify_scm_file))
    }

    fn from_classes(classes: impl IntoIterator<Item = ScmFileClass>) -> Self {
        let mut result = Self::default();

        for class in classes {
            match class {
                ScmFileClass::DirectStream(node) => {
                    result.has_stream_socket = true;
                    result.passed_sockets.push(node);
                }
                ScmFileClass::DirectDatagram(node) => {
                    result.has_datagram_socket = true;
                    result.passed_sockets.push(node);
                }
                ScmFileClass::ProvenLeaf => {}
                ScmFileClass::Unsupported => result.has_unsupported_file = true,
            }
        }

        result
    }

    fn legacy_send_policy(&self) -> LegacySendPolicy {
        if self.has_stream_socket {
            LegacySendPolicy::RequireSysAdmin
        } else {
            LegacySendPolicy::Allow
        }
    }
}

fn classify_scm_file(file: &Arc<dyn FileLike>) -> ScmFileClass {
    if let Some(socket) = file.downcast_ref::<UnixStreamSocket>() {
        return ScmFileClass::DirectStream(socket.scm_node().clone());
    }
    if let Some(socket) = file.downcast_ref::<UnixDatagramSocket>() {
        return ScmFileClass::DirectDatagram(socket.scm_node().clone());
    }

    // Regular files, pipe/FIFO handles, and explicitly audited per-open handles do not own
    // arbitrary file descriptions. Other `InodeHandle` implementations are not safe to
    // generalize: a loop-device open file may strongly retain its backing `FileLike`.
    // Epoll's interest and ready sets retain only weak watched-file references. Other socket
    // families also cannot own AF_UNIX file descriptions. A pidfd retains only a weak process
    // reference, so it cannot keep that process or any file description in its table alive.
    if is_proven_leaf(
        file.downcast_ref::<InodeHandle>()
            .is_some_and(InodeHandle::is_scm_rights_proven_leaf),
        file.downcast_ref::<EpollFile>().is_some(),
        file.as_socket().is_some(),
        file.downcast_ref::<PidFile>().is_some(),
    ) {
        return ScmFileClass::ProvenLeaf;
    }

    // Defaulting unknown `FileLike` implementations to a leaf would make the ownership proof
    // invalid as new strong-owning containers are added. Slice 4 records but does not reject this
    // class; later B1 policy must reject it until its ownership is modeled.
    ScmFileClass::Unsupported
}

fn is_proven_leaf(
    is_safe_inode: bool,
    is_epoll: bool,
    is_non_unix_socket: bool,
    is_pidfd: bool,
) -> bool {
    is_safe_inode || is_epoll || is_non_unix_socket || is_pidfd
}

#[cfg(ktest)]
mod test {
    use core::fmt::Display;

    use ostd::prelude::ktest;

    use super::*;
    use crate::{
        events::IoEvents,
        fs::file::{AccessMode, FileCommon},
        process::signal::{PollHandle, Pollable},
    };

    #[ktest]
    fn classifies_file_shapes_and_marks_unknown() {
        assert!(is_proven_leaf(true, false, false, false));
        assert!(is_proven_leaf(false, true, false, false));
        assert!(is_proven_leaf(false, false, true, false));
        assert!(is_proven_leaf(false, false, false, true));
        assert!(!is_proven_leaf(false, false, false, false));

        let stream = SocketNode::new();
        let datagram = SocketNode::new();
        let classes = ScmFiles::from_classes([
            ScmFileClass::ProvenLeaf,
            ScmFileClass::DirectStream(stream),
            ScmFileClass::DirectDatagram(datagram),
            ScmFileClass::ProvenLeaf,
            ScmFileClass::Unsupported,
        ]);
        assert_eq!(classes.passed_sockets.len(), 2);
        assert!(classes.has_stream_socket);
        assert!(classes.has_datagram_socket);
        assert!(classes.has_unsupported_file);

        let unknown = Arc::new(UnknownFile) as Arc<dyn FileLike>;
        assert!(matches!(
            classify_scm_file(&unknown),
            ScmFileClass::Unsupported
        ));
    }

    #[ktest]
    fn merges_headers_preserves_duplicate_occurrences_and_keeps_legacy_policy() {
        let duplicate = Arc::new(UnknownFile) as Arc<dyn FileLike>;
        let auxiliary_data = AuxiliaryData::from_control(vec![
            file_control(vec![duplicate.clone(), duplicate.clone()]),
            file_control(vec![duplicate.clone()]),
        ])
        .unwrap();

        assert_eq!(auxiliary_data.files.len(), 3);
        assert!(Arc::ptr_eq(&auxiliary_data.files[0], &duplicate));
        assert!(Arc::ptr_eq(
            &auxiliary_data.files[0],
            &auxiliary_data.files[1]
        ));
        assert!(Arc::ptr_eq(
            &auxiliary_data.files[1],
            &auxiliary_data.files[2]
        ));
        assert_eq!(auxiliary_data.passed_sockets().len(), 0);
        assert!(auxiliary_data.has_unsupported_file());
        assert_eq!(
            auxiliary_data.scm_files.legacy_send_policy(),
            LegacySendPolicy::Allow
        );

        // The metadata path separately proves that duplicate AF_UNIX occurrences are retained.
        let stream = SocketNode::new();
        let datagram = SocketNode::new();
        let scm_files = ScmFiles::from_classes([
            ScmFileClass::DirectStream(stream.clone()),
            ScmFileClass::DirectDatagram(datagram),
            ScmFileClass::DirectStream(stream),
        ]);
        assert_eq!(scm_files.passed_sockets.len(), 3);
        assert_eq!(
            scm_files.legacy_send_policy(),
            LegacySendPolicy::RequireSysAdmin
        );

        let auxiliary_data = AuxiliaryData {
            files: Vec::new(),
            cred: None,
            scm_files,
        };
        let mut capability_checks = 0;
        let err = auxiliary_data
            .enforce_legacy_send_policy_with(|| {
                capability_checks += 1;
                Err(Error::with_message(
                    Errno::EPERM,
                    "test caller lacks CAP_SYS_ADMIN",
                ))
            })
            .unwrap_err();
        assert_eq!(err.error(), Errno::EPERM);
        assert_eq!(capability_checks, 1);

        // Datagram and unsupported files were accepted before Slice 4 and remain accepted.
        for scm_files in [
            ScmFiles::from_classes([ScmFileClass::DirectDatagram(SocketNode::new())]),
            ScmFiles::from_classes([ScmFileClass::Unsupported]),
        ] {
            let auxiliary_data = AuxiliaryData {
                files: Vec::new(),
                cred: None,
                scm_files,
            };
            auxiliary_data
                .enforce_legacy_send_policy_with(|| {
                    panic!("legacy policy must gate only direct stream/seqpacket FDs")
                })
                .unwrap();
        }
    }

    fn file_control(files: Vec<Arc<dyn FileLike>>) -> ControlMessage {
        ControlMessage::Unix(UnixControlMessage(Message::Files(FileMessage { files })))
    }

    struct UnknownFile;

    impl Pollable for UnknownFile {
        fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
            IoEvents::empty()
        }
    }

    impl FileLike for UnknownFile {
        fn access_mode(&self) -> AccessMode {
            AccessMode::O_RDONLY
        }

        fn common(&self) -> &FileCommon {
            panic!("synthetic unknown file has no filesystem path")
        }

        fn dump_proc_fdinfo(self: Arc<Self>, _fd_flags: FdFlags) -> Box<dyn Display> {
            Box::new("scm-unknown")
        }
    }
}
