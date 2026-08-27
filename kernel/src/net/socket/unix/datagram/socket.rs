// SPDX-License-Identifier: MPL-2.0

use core::{
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use aster_rights::ReadDupOp;

use super::message::{MessageQueue, MessageReceiver};
use crate::{
    events::IoEvents,
    fs::{
        file::{FileCommon, StatusFlags},
        pseudofs::SockFs,
    },
    net::socket::{
        Socket,
        options::{Error as SocketError, PeerCred, SocketOption, macros::sock_option_mut},
        private::SocketPrivate,
        unix::{
            CUserCred, UnixSocketAddr,
            cred::SocketCred,
            ctrl_msg::AuxiliaryData,
            scm_graph::{PermanentEdge, SocketNode},
        },
        util::{
            MessageHeader, RecvFlags, RecvOutput, SendFlags, SockShutdownCmd, SocketAddr,
            options::{
                GetSocketLevelOption, SetSocketLevelOption, SocketOptionSet, SocketTimeouts,
            },
        },
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::{MultiRead, MultiWrite, net::SockType},
};

pub struct UnixDatagramSocket {
    scm_node: SocketNode,
    local_receiver: MessageReceiver,
    remote_queue: RwLock<Option<RemoteQueue>>,
    options: RwLock<OptionSet>,
    timeouts: SocketTimeouts,
    // Since datagram sockets are not connection-oriented, they typically lack well-defined peer
    // credentials. According to the Linux implementation, however, peer credentials are recorded
    // when a socket pair is created using the `socketpair` system call.
    peer_cred: Option<SocketCred>,

    is_write_shutdown: AtomicBool,
    common: FileCommon,
}

struct RemoteQueue {
    _owner_edge: PermanentEdge,
    queue: Arc<MessageQueue>,
}

impl RemoteQueue {
    fn new(owner: &SocketNode, queue: Arc<MessageQueue>) -> Self {
        // In Slice 3 no queued SCM edge is recorded, so a datagram queue cannot yet reach a
        // socket. Slice 6 must make connect/reconnect fallible before adding queue-to-socket
        // committed edges; this `expect` is deliberately not a permanent graph invariant.
        let owner_edge = PermanentEdge::new(owner, queue.scm_node())
            .expect("datagram queues cannot own sockets while the legacy SCM policy is active");
        Self {
            _owner_edge: owner_edge,
            queue,
        }
    }

    fn queue(&self) -> &Arc<MessageQueue> {
        &self.queue
    }
}

#[derive(Clone, Debug)]
struct OptionSet {
    socket: SocketOptionSet,
}

impl OptionSet {
    pub(self) fn new() -> Self {
        Self {
            socket: SocketOptionSet::new_unix_datagram(),
        }
    }
}

impl UnixDatagramSocket {
    pub fn new(is_nonblocking: bool) -> Arc<Self> {
        Arc::new(Self::new_raw(is_nonblocking))
    }

    pub fn new_pair(is_nonblocking: bool) -> (Arc<Self>, Arc<Self>) {
        let mut socket_a = Self::new_raw(is_nonblocking);
        let mut socket_b = Self::new_raw(is_nonblocking);

        let cred = SocketCred::<ReadDupOp>::new_current();
        socket_a.peer_cred = Some(cred.dup().restrict());
        socket_b.peer_cred = Some(cred.restrict());

        let remote_queue_a = socket_a.remote_queue.get_mut();
        let remote_queue_b = socket_b.remote_queue.get_mut();

        *remote_queue_a = Some(RemoteQueue::new(
            &socket_a.scm_node,
            socket_b.local_receiver.queue().clone(),
        ));
        *remote_queue_b = Some(RemoteQueue::new(
            &socket_b.scm_node,
            socket_a.local_receiver.queue().clone(),
        ));

        (Arc::new(socket_a), Arc::new(socket_b))
    }

    fn new_raw(is_nonblocking: bool) -> Self {
        let status_flags = if is_nonblocking {
            StatusFlags::O_NONBLOCK
        } else {
            StatusFlags::empty()
        };
        let scm_node = SocketNode::new();
        let local_receiver = MessageReceiver::new(&scm_node);
        Self {
            scm_node,
            local_receiver,
            remote_queue: RwLock::new(None),
            options: RwLock::new(OptionSet::new()),
            timeouts: SocketTimeouts::new(),
            peer_cred: None,
            is_write_shutdown: AtomicBool::new(false),
            common: FileCommon::new(SockFs::new_path(), status_flags),
        }
    }

    fn do_send(
        &self,
        reader: &mut dyn MultiRead,
        mut aux_data: AuxiliaryData,
        remote: Option<UnixSocketAddr>,
        flags: SendFlags,
        timeout: Option<Duration>,
    ) -> Result<usize> {
        if self.is_write_shutdown.load(Ordering::Relaxed) {
            return_errno_with_message!(Errno::EPIPE, "the socket is shut down for writing");
        }

        let queue = if let Some(remote_addr) = remote.as_ref() {
            let connected_addr = remote_addr.connect()?;
            MessageQueue::lookup_bound(&connected_addr)?
        } else {
            let remote_queue = self.remote_queue.read();
            remote_queue
                .as_ref()
                .map(|remote| remote.queue().clone())
                .ok_or_else(|| {
                    Error::with_message(Errno::ENOTCONN, "the socket is not connected")
                })?
        };

        let res = if self.is_nonblocking() || flags.contains(SendFlags::MSG_DONTWAIT) {
            queue.try_send(reader, &mut aux_data, &self.local_receiver)
        } else {
            queue.block_send(timeout, || {
                queue.try_send(reader, &mut aux_data, &self.local_receiver)
            })
        };

        // A connected socket will automatically be disconnected if the remote has been closed.
        if remote.is_none() && res.is_err_and(|err| err.error() == Errno::ECONNREFUSED) {
            disconnect_remote_if_matches(&self.remote_queue, &queue);
        }

        res
    }

    fn check_io_events(&self) -> IoEvents {
        // POLLOUT should be reported as long as there is space in the socket's send buffer.
        // Currently, we only limit the size of the receive buffer, not the send buffer. Therefore,
        // POLLOUT is always reported.
        let mut io_events = IoEvents::OUT;

        io_events |= self.local_receiver.check_io_events();

        if self.is_write_shutdown.load(Ordering::Relaxed) && io_events.contains(IoEvents::RDHUP) {
            io_events |= IoEvents::HUP;
        }

        io_events
    }
}

fn replace_remote_queue(
    remote_queue: &RwLock<Option<RemoteQueue>>,
    owner: &SocketNode,
    queue: Arc<MessageQueue>,
) {
    let mut remote_queue = remote_queue.write();
    // Construct the new edge before replacing the old wrapper. The state lock remains held while
    // both graph operations run, so observers see neither a missing edge nor a stale connection.
    *remote_queue = Some(RemoteQueue::new(owner, queue));
}

fn disconnect_remote_if_matches(
    remote_queue: &RwLock<Option<RemoteQueue>>,
    failed_queue: &Arc<MessageQueue>,
) {
    let mut remote_queue = remote_queue.write();
    if remote_queue
        .as_ref()
        .is_some_and(|remote| Arc::ptr_eq(remote.queue(), failed_queue))
    {
        // Dropping the wrapper under the state lock removes the matching graph edge atomically.
        *remote_queue = None;
    }
}

impl Drop for UnixDatagramSocket {
    fn drop(&mut self) {
        // Explicitly detach the socket-to-remote-queue edge before field destruction. The local
        // receiver drains its queue and detaches the local edge in its own `Drop` implementation.
        *self.remote_queue.write() = None;
    }
}

impl Pollable for UnixDatagramSocket {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.local_receiver
            .pollee()
            .poll_with(mask, poller, || self.check_io_events())
    }
}

impl SocketPrivate for UnixDatagramSocket {
    fn is_nonblocking(&self) -> bool {
        self.common.is_nonblocking()
    }
}

impl Socket for UnixDatagramSocket {
    fn bind(&self, socket_addr: SocketAddr) -> Result<()> {
        let addr = UnixSocketAddr::try_from(socket_addr)?;
        self.local_receiver.bind(addr)
    }

    fn connect(&self, socket_addr: SocketAddr) -> Result<()> {
        let remote_addr = UnixSocketAddr::try_from(socket_addr)?;

        let connected_addr = remote_addr.connect()?;
        let queue = MessageQueue::lookup_bound(&connected_addr)?;

        replace_remote_queue(&self.remote_queue, &self.scm_node, queue);

        Ok(())
    }

    fn shutdown(&self, cmd: SockShutdownCmd) -> Result<()> {
        let mut io_events = IoEvents::empty();

        if cmd.shut_read() {
            self.local_receiver.shutdown();
            io_events |= IoEvents::IN | IoEvents::RDHUP | IoEvents::HUP;
        }

        if cmd.shut_write() {
            self.is_write_shutdown.store(true, Ordering::Relaxed);
            io_events |= IoEvents::HUP;
        }

        self.local_receiver.pollee().notify(io_events);

        Ok(())
    }

    fn addr(&self) -> Result<SocketAddr> {
        Ok(self.local_receiver.addr().into())
    }

    fn peer_addr(&self) -> Result<SocketAddr> {
        let remote_queue = self.remote_queue.read();
        match remote_queue.as_ref() {
            Some(remote) => Ok(remote.queue().addr().into()),
            None => return_errno_with_message!(Errno::ENOTCONN, "the socket is not connected"),
        }
    }

    fn get_option(&self, option: &mut dyn SocketOption) -> Result<()> {
        sock_option_mut!(match option {
            socket_errors @ SocketError => {
                // TODO: Support socket errors for UNIX sockets
                socket_errors.set(None);
                return Ok(());
            }
            _ => (),
        });

        // Deal with UNIX-socket-specific socket-level options
        match do_unix_getsockopt(option, self) {
            Err(err) if err.error() == Errno::ENOPROTOOPT => (),
            res => return res,
        }

        let options = self.options.read();

        // Deal with socket-level options
        match options
            .socket
            .get_option(option, &(&self.local_receiver, &self.timeouts))
        {
            Err(err) if err.error() == Errno::ENOPROTOOPT => (),
            res => return res,
        }

        // TODO: Deal with socket options from other levels
        warn!("only socket-level options are supported");

        return_errno_with_message!(Errno::ENOPROTOOPT, "the socket option to get is unknown")
    }

    fn set_option(&self, option: &dyn SocketOption) -> Result<()> {
        let mut options = self.options.write();

        match options
            .socket
            .set_option(option, &(&self.local_receiver, &self.timeouts))
        {
            Err(err) if err.error() == Errno::ENOPROTOOPT => {
                // TODO: Deal with socket options from other levels
                warn!("only socket-level options are supported");
                return_errno_with_message!(
                    Errno::ENOPROTOOPT,
                    "the socket option to get is unknown"
                )
            }
            res => res.map(|_need_iface_poll| ()),
        }
    }

    fn sendmsg(
        &self,
        reader: &mut dyn MultiRead,
        message_header: MessageHeader,
        flags: SendFlags,
    ) -> Result<usize> {
        // TODO: Deal with flags
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let MessageHeader {
            addr,
            control_messages,
        } = message_header;

        let remote_addr = match addr {
            Some(addr) => Some(addr.try_into()?),
            None => None,
        };

        let auxiliary_data = AuxiliaryData::from_control(control_messages)?;

        self.do_send(
            reader,
            auxiliary_data,
            remote_addr,
            flags,
            self.timeouts.send_timeout(),
        )
    }

    fn recvmsg(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, MessageHeader)> {
        // TODO: Deal with flags
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let mut try_recv = || self.local_receiver.try_recv(writer, flags);
        let (output, control_messages, peer_addr) =
            if self.is_nonblocking() || flags.contains(RecvFlags::MSG_DONTWAIT) {
                try_recv()
            } else {
                self.block_on(IoEvents::IN, self.timeouts.recv_timeout(), try_recv)
            }?;

        let message_header = MessageHeader::new(Some(peer_addr.into()), control_messages);

        Ok((output, message_header))
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }
}

fn do_unix_getsockopt(option: &mut dyn SocketOption, socket: &UnixDatagramSocket) -> Result<()> {
    sock_option_mut!(match option {
        socket_peer_cred @ PeerCred => {
            let peer_cred = socket
                .peer_cred
                .as_ref()
                .map(SocketCred::to_effective_c_cred)
                .unwrap_or_else(CUserCred::new_invalid);
            socket_peer_cred.set(peer_cred);
        }
        _ => return_errno_with_message!(
            Errno::ENOPROTOOPT,
            "the socket option to get is not UNIX-socket-specific"
        ),
    });

    Ok(())
}

impl GetSocketLevelOption for (&MessageReceiver, &SocketTimeouts) {
    fn socket_type(&self) -> SockType {
        SockType::SOCK_DGRAM
    }

    fn is_listening(&self) -> bool {
        false
    }

    fn socket_timeouts(&self) -> Option<&SocketTimeouts> {
        Some(self.1)
    }
}

impl SetSocketLevelOption for (&MessageReceiver, &SocketTimeouts) {
    fn set_pass_cred(&self, pass_cred: bool) {
        // TODO: According to the Linux man pages, "When this option is set and the socket
        // is not yet connected, a unique name in the abstract namespace will be generated
        // automatically." See <https://man7.org/linux/man-pages/man7/unix.7.html> for
        // details.

        self.0.set_pass_cred(pass_cred);
    }

    fn socket_timeouts(&self) -> Option<&SocketTimeouts> {
        Some(self.1)
    }
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;
    use crate::{
        net::socket::unix::scm_graph::permanent_edge_count,
        thread::{Thread, kernel_thread::ThreadOptions},
    };

    #[ktest]
    fn socketpair_style_local_and_remote_edges_have_exact_multiplicity() {
        let first_socket = SocketNode::new();
        let second_socket = SocketNode::new();
        let first_receiver = MessageReceiver::new(&first_socket);
        let second_receiver = MessageReceiver::new(&second_socket);
        let first_queue = first_receiver.queue_node();
        let second_queue = second_receiver.queue_node();

        let first_remote = RemoteQueue::new(&first_socket, second_receiver.queue().clone());
        let second_remote = RemoteQueue::new(&second_socket, first_receiver.queue().clone());
        assert_eq!(permanent_edge_count(&first_socket, &first_queue), 1);
        assert_eq!(permanent_edge_count(&first_socket, &second_queue), 1);
        assert_eq!(permanent_edge_count(&second_socket, &second_queue), 1);
        assert_eq!(permanent_edge_count(&second_socket, &first_queue), 1);

        drop(first_remote);
        drop(second_remote);
        assert_eq!(permanent_edge_count(&first_socket, &first_queue), 1);
        assert_eq!(permanent_edge_count(&first_socket, &second_queue), 0);
        assert_eq!(permanent_edge_count(&second_socket, &second_queue), 1);
        assert_eq!(permanent_edge_count(&second_socket, &first_queue), 0);

        drop(first_receiver);
        drop(second_receiver);
        assert_eq!(permanent_edge_count(&first_socket, &first_queue), 0);
        assert_eq!(permanent_edge_count(&second_socket, &second_queue), 0);
    }

    #[ktest]
    fn reconnect_replaces_remote_edge_without_a_stale_owner() {
        let socket = SocketNode::new();
        let first_queue_owner = SocketNode::new();
        let second_queue_owner = SocketNode::new();
        let first_receiver = MessageReceiver::new(&first_queue_owner);
        let second_receiver = MessageReceiver::new(&second_queue_owner);
        let first_queue = first_receiver.queue_node();
        let second_queue = second_receiver.queue_node();
        let remote = RwLock::new(None);

        replace_remote_queue(&remote, &socket, first_receiver.queue().clone());
        assert_eq!(permanent_edge_count(&socket, &first_queue), 1);
        replace_remote_queue(&remote, &socket, second_receiver.queue().clone());
        assert_eq!(permanent_edge_count(&socket, &first_queue), 0);
        assert_eq!(permanent_edge_count(&socket, &second_queue), 1);

        *remote.write() = None;
        assert_eq!(permanent_edge_count(&socket, &second_queue), 0);
    }

    #[ktest]
    fn peer_close_disconnect_only_removes_the_matching_remote_edge() {
        let socket = SocketNode::new();
        let first_queue_owner = SocketNode::new();
        let second_queue_owner = SocketNode::new();
        let first_receiver = MessageReceiver::new(&first_queue_owner);
        let second_receiver = MessageReceiver::new(&second_queue_owner);
        let first_queue = first_receiver.queue_node();
        let second_queue = second_receiver.queue_node();
        let remote = RwLock::new(None);

        replace_remote_queue(&remote, &socket, first_receiver.queue().clone());
        disconnect_remote_if_matches(&remote, second_receiver.queue());
        assert_eq!(permanent_edge_count(&socket, &first_queue), 1);
        assert_eq!(permanent_edge_count(&socket, &second_queue), 0);

        disconnect_remote_if_matches(&remote, first_receiver.queue());
        assert!(remote.read().is_none());
        assert_eq!(permanent_edge_count(&socket, &first_queue), 0);
    }

    #[ktest]
    fn reconnect_racing_peer_close_converges_on_the_new_remote_edge() {
        let socket = SocketNode::new();
        let first_queue_owner = SocketNode::new();
        let second_queue_owner = SocketNode::new();
        let first_receiver = MessageReceiver::new(&first_queue_owner);
        let second_receiver = MessageReceiver::new(&second_queue_owner);
        let first_queue = first_receiver.queue_node();
        let second_queue = second_receiver.queue_node();
        let remote = Arc::new(RwLock::new(Some(RemoteQueue::new(
            &socket,
            first_receiver.queue().clone(),
        ))));
        let is_starting = Arc::new(AtomicBool::new(false));

        let reconnect_thread = {
            let remote = remote.clone();
            let socket = socket.clone();
            let new_queue = second_receiver.queue().clone();
            let is_starting = is_starting.clone();
            ThreadOptions::new(move || {
                wait_for_start(&is_starting);
                replace_remote_queue(&remote, &socket, new_queue);
            })
            .spawn()
        };
        let peer_close_thread = {
            let remote = remote.clone();
            let failed_queue = first_receiver.queue().clone();
            let is_starting = is_starting.clone();
            ThreadOptions::new(move || {
                wait_for_start(&is_starting);
                disconnect_remote_if_matches(&remote, &failed_queue);
            })
            .spawn()
        };

        is_starting.store(true, Ordering::Release);
        reconnect_thread.join();
        peer_close_thread.join();
        assert_eq!(permanent_edge_count(&socket, &first_queue), 0);
        assert_eq!(permanent_edge_count(&socket, &second_queue), 1);
        assert!(
            remote
                .read()
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current.queue(), second_receiver.queue()))
        );

        *remote.write() = None;
        assert_eq!(permanent_edge_count(&socket, &second_queue), 0);
    }

    fn wait_for_start(is_starting: &AtomicBool) {
        while !is_starting.load(Ordering::Acquire) {
            Thread::yield_now();
        }
    }
}
