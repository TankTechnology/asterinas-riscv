// SPDX-License-Identifier: MPL-2.0

use core::{
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
    time::Duration,
};

use aster_rights::ReadDupOp;
use ostd::sync::WaitQueue;

use super::{
    UnixStreamSocket,
    connected::Connected,
    init::Init,
    socket::{SHUT_READ_EVENTS, SHUT_WRITE_EVENTS},
};
use crate::{
    events::IoEvents,
    fs::file::FileLike,
    net::socket::{
        SocketAddr,
        unix::{
            addr::{UnixSocketAddrBound, UnixSocketAddrKey},
            cred::SocketCred,
            scm_graph::{PermanentEdge, SocketNode, StreamBacklogNode},
            stream::socket::OptionSet,
        },
        util::SockShutdownCmd,
    },
    prelude::*,
    process::signal::Pollee,
    util::net::SockType,
};

pub(super) struct Listener {
    backlog: Arc<Backlog>,
    owner_edge: Option<PermanentEdge>,
    is_write_shutdown: AtomicBool,
}

impl Listener {
    pub(super) fn new(
        addr: UnixSocketAddrBound,
        backlog: usize,
        is_read_shutdown: bool,
        is_write_shutdown: bool,
        pollee: Pollee,
        is_seqpacket: bool,
        owner: &SocketNode,
    ) -> Self {
        let (backlog, owner_edge) = BACKLOG_TABLE
            .add_backlog(addr, pollee, backlog, is_read_shutdown, is_seqpacket, owner)
            .unwrap();

        Self {
            backlog,
            owner_edge: Some(owner_edge),
            is_write_shutdown: AtomicBool::new(is_write_shutdown),
        }
    }

    pub(super) fn addr(&self) -> &UnixSocketAddrBound {
        self.backlog.addr()
    }

    pub(super) fn try_accept(
        &self,
        socket_type: SockType,
        is_nonblocking: bool,
    ) -> Result<(Arc<dyn FileLike>, SocketAddr)> {
        debug_assert!(
            socket_type == SockType::SOCK_STREAM || socket_type == SockType::SOCK_SEQPACKET
        );

        let connected = self.backlog.pop_incoming()?;

        let peer_addr = connected.peer_addr().into();
        let options = OptionSet::new_accepted(connected.is_pass_cred());

        let socket =
            UnixStreamSocket::new_connected(connected, options, is_nonblocking, socket_type);
        Ok((socket, peer_addr))
    }

    pub(super) fn listen(&self, backlog: usize) {
        self.backlog.set_backlog(backlog);
    }

    pub(super) fn shutdown(&self, cmd: SockShutdownCmd, pollee: &Pollee) {
        if cmd.shut_read() {
            self.backlog.shutdown();
        }

        if cmd.shut_write() {
            self.is_write_shutdown.store(true, Ordering::Relaxed);
            pollee.notify(SHUT_WRITE_EVENTS);
        }
    }

    pub(super) fn is_read_shutdown(&self) -> bool {
        self.backlog.is_shutdown()
    }

    pub(super) fn is_write_shutdown(&self) -> bool {
        self.is_write_shutdown.load(Ordering::Relaxed)
    }

    pub(super) fn set_pass_cred(&self, is_pass_cred: bool) {
        self.backlog
            .is_pass_cred
            .store(is_pass_cred, Ordering::Relaxed);
    }

    pub(super) fn check_io_events(&self) -> IoEvents {
        self.backlog.check_io_events()
    }

    pub(super) fn cred(&self) -> &SocketCred<ReadDupOp> {
        &self.backlog.listener_cred
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        // Drain pending connections first, then stop publishing this backlog, and only then detach
        // the listening socket's real strong-ownership edge. No peer can add a new pending storage
        // after shutdown, and no graph edge disappears while the listener can still reach it.
        self.backlog.shutdown();
        BACKLOG_TABLE.remove_backlog(&self.backlog.addr().to_key());
        drop(self.owner_edge.take());
    }
}

static BACKLOG_TABLE: BacklogTable = BacklogTable::new();

struct BacklogTable {
    backlog_sockets: RwLock<BTreeMap<UnixSocketAddrKey, Arc<Backlog>>>,
}

impl BacklogTable {
    const fn new() -> Self {
        Self {
            backlog_sockets: RwLock::new(BTreeMap::new()),
        }
    }

    fn add_backlog(
        &self,
        addr: UnixSocketAddrBound,
        pollee: Pollee,
        backlog: usize,
        is_shutdown: bool,
        is_seqpacket: bool,
        owner: &SocketNode,
    ) -> Option<(Arc<Backlog>, PermanentEdge)> {
        let addr_key = addr.to_key();

        let mut backlog_sockets = self.backlog_sockets.write();

        if backlog_sockets.contains_key(&addr_key) {
            return None;
        }

        let new_backlog = Arc::new(Backlog::new(
            addr,
            pollee,
            backlog,
            is_shutdown,
            is_seqpacket,
        ));
        // Establish `listener socket -> backlog` before the global table exposes the backlog to a
        // concurrent connect. The new backlog has no pending storage, so this edge cannot close a
        // cycle. Constructing it before insertion also leaves no table entry on failure/panic.
        let owner_edge = PermanentEdge::new(owner, &new_backlog.scm_node)
            .expect("a new empty stream backlog cannot close an SCM ownership cycle");
        let old_backlog = backlog_sockets.insert(addr_key, new_backlog.clone());
        debug_assert!(old_backlog.is_none());

        Some((new_backlog, owner_edge))
    }

    fn get_backlog(&self, addr_key: &UnixSocketAddrKey) -> Option<Arc<Backlog>> {
        self.backlog_sockets.read().get(addr_key).cloned()
    }

    fn remove_backlog(&self, addr_key: &UnixSocketAddrKey) {
        let old_backlog = self.backlog_sockets.write().remove(addr_key);
        debug_assert!(old_backlog.is_some());
    }
}

pub(super) struct Backlog {
    addr: UnixSocketAddrBound,
    pollee: Pollee,
    backlog: AtomicUsize,
    incoming_conns: SpinLock<Option<VecDeque<Connected>>>,
    scm_node: StreamBacklogNode,
    connect_wait_queue: WaitQueue,
    listener_cred: SocketCred<ReadDupOp>,
    is_pass_cred: AtomicBool,
    is_seqpacket: bool,
}

impl Backlog {
    fn new(
        addr: UnixSocketAddrBound,
        pollee: Pollee,
        backlog: usize,
        is_shutdown: bool,
        is_seqpacket: bool,
    ) -> Self {
        #[cfg(not(ktest))]
        let listener_cred = SocketCred::<ReadDupOp>::new_current();
        // The bare ktest runner has no POSIX current-thread context. Keep production construction
        // unchanged while making the test configuration use an explicit, owned credential.
        #[cfg(ktest)]
        let listener_cred = SocketCred::<ReadDupOp>::new_test_root();
        Self::new_with_cred(
            addr,
            pollee,
            backlog,
            is_shutdown,
            is_seqpacket,
            listener_cred,
        )
    }

    fn new_with_cred(
        addr: UnixSocketAddrBound,
        pollee: Pollee,
        backlog: usize,
        is_shutdown: bool,
        is_seqpacket: bool,
        listener_cred: SocketCred<ReadDupOp>,
    ) -> Self {
        let incoming_sockets = if is_shutdown {
            None
        } else {
            Some(VecDeque::with_capacity(backlog))
        };

        Self {
            addr,
            pollee,
            backlog: AtomicUsize::new(backlog),
            incoming_conns: SpinLock::new(incoming_sockets),
            scm_node: StreamBacklogNode::new(),
            connect_wait_queue: WaitQueue::new(),
            listener_cred,
            is_pass_cred: AtomicBool::new(false),
            is_seqpacket,
        }
    }

    fn addr(&self) -> &UnixSocketAddrBound {
        &self.addr
    }

    fn pop_incoming(&self) -> Result<Connected> {
        let mut locked_incoming_conns = self.incoming_conns.lock();

        let Some(incoming_conns) = &mut *locked_incoming_conns else {
            return_errno_with_message!(Errno::EINVAL, "the socket is shut down for reading");
        };
        let conn = incoming_conns.pop_front();

        drop(locked_incoming_conns);

        if conn.is_some() {
            self.pollee.invalidate();
            self.connect_wait_queue.wake_one();
        }

        conn.ok_or_else(|| Error::with_message(Errno::EAGAIN, "no pending connection is available"))
    }

    fn set_backlog(&self, backlog: usize) {
        let old_backlog = self.backlog.swap(backlog, Ordering::Relaxed);

        if old_backlog < backlog {
            self.connect_wait_queue.wake_all();
        }
    }

    fn shutdown(&self) {
        let pending = {
            let mut incoming_conns = self.incoming_conns.lock();
            incoming_conns.take()
        };
        // `Connected::drop` drains queued files and updates the SCM graph. Neither operation may
        // run under the backlog spin lock.
        drop(pending);

        self.pollee.notify(SHUT_READ_EVENTS);
        self.connect_wait_queue.wake_all();
    }

    fn is_shutdown(&self) -> bool {
        self.incoming_conns.lock().is_none()
    }

    fn check_io_events(&self) -> IoEvents {
        if self
            .incoming_conns
            .lock()
            .as_ref()
            .is_some_and(|conns| !conns.is_empty())
        {
            IoEvents::IN
        } else {
            IoEvents::empty()
        }
    }
}

impl Backlog {
    pub(super) fn push_incoming(
        &self,
        init: Init,
        pollee: Pollee,
        options: &OptionSet,
        is_seqpacket: bool,
        client_node: &SocketNode,
    ) -> Result<Connected, (Error, Init)> {
        if is_seqpacket != self.is_seqpacket {
            // FIXME: According to the Linux implementation, we should avoid this error by
            // maintaining two socket tables for SOCK_STREAM sockets and SOCK_SEQPACKET sockets
            // separately.
            return Err((
                Error::with_message(
                    Errno::ECONNREFUSED,
                    "the listening socket has a different socket type",
                ),
                init,
            ));
        }

        let mut locked_incoming_conns = self.incoming_conns.lock();

        let Some(incoming_conns) = &mut *locked_incoming_conns else {
            return Err((
                Error::with_message(
                    Errno::ECONNREFUSED,
                    "the listening socket is shut down for reading",
                ),
                init,
            ));
        };

        if incoming_conns.len() >= self.backlog.load(Ordering::Relaxed) {
            return Err((
                Error::with_message(
                    Errno::EAGAIN,
                    "the pending connection queue on the listening socket is full",
                ),
                init,
            ));
        }

        let (mut client_conn, mut server_conn) = init.into_connected(
            self.addr.clone(),
            pollee,
            self.listener_cred.dup().restrict(),
        );
        client_conn.attach_owner(client_node);
        server_conn.attach_owner(&self.scm_node);
        options.apply_to_connected(&client_conn);
        if self.is_pass_cred.load(Ordering::Relaxed) {
            server_conn.set_pass_cred(true);
        }

        incoming_conns.push_back(server_conn);
        self.pollee.notify(IoEvents::IN);

        Ok(client_conn)
    }

    /// Blocks until the backlogs are free and the `try_connect` succeeds, or until interrupted.
    pub(super) fn block_connect<F>(
        &self,
        timeout: Option<Duration>,
        mut try_connect: F,
    ) -> Result<()>
    where
        F: FnMut() -> Result<()>,
    {
        self.connect_wait_queue
            .pause_until_or_timeout(
                || match try_connect() {
                    Err(err) if err.error() == Errno::EAGAIN => None,
                    result => Some(result),
                },
                timeout.as_ref(),
            )
            .map_err(|err| match err.error() {
                Errno::ETIME => Error::with_message(Errno::EAGAIN, "the socket timeout expired"),
                _ => err,
            })?
    }
}

pub(super) fn get_backlog(server_key: &UnixSocketAddrKey) -> Result<Arc<Backlog>> {
    BACKLOG_TABLE.get_backlog(server_key).ok_or_else(|| {
        Error::with_message(
            Errno::ECONNREFUSED,
            "no socket is listening at the remote address",
        )
    })
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;
    use crate::net::socket::unix::{
        UnixSocketAddr,
        scm_graph::{ReservationError, ReservedEdges, permanent_edge_count, reserved_edge_count},
    };

    #[ktest]
    fn stream_listener_edges_block_preaccept_self_scm_and_release_lifetimes() {
        assert_listener_owner_lifetime(false, "STREAM");
    }

    #[ktest]
    fn seqpacket_listener_edges_block_preaccept_self_scm_and_release_lifetimes() {
        assert_listener_owner_lifetime(true, "SEQPACKET");
    }

    fn assert_listener_owner_lifetime(is_seqpacket: bool, socket_kind: &str) {
        ostd::early_println!("B1_LISTENER_OWNER_{}_BEGIN", socket_kind);
        let owner = SocketNode::new();
        let weak_owner = owner.downgrade();
        ostd::early_println!("B1_LISTENER_OWNER_{}_OWNER_CREATED", socket_kind);
        let addr = UnixSocketAddr::Unnamed.bind().unwrap();
        ostd::early_println!("B1_LISTENER_OWNER_{}_ADDRESS_BOUND", socket_kind);
        let listener = Listener::new(addr, 1, false, false, Pollee::new(), is_seqpacket, &owner);
        ostd::early_println!("B1_LISTENER_OWNER_{}_LISTENER_CREATED", socket_kind);
        let backlog_key = listener.addr().to_key();
        let backlog_node = listener.backlog.scm_node.clone();
        let weak_backlog = backlog_node.downgrade();

        assert_eq!(permanent_edge_count(&owner, &backlog_node), 1);

        let client_node = SocketNode::new();
        let client = push_incoming(&listener.backlog, &client_node, is_seqpacket);
        ostd::early_println!("B1_LISTENER_OWNER_{}_INCOMING_PUSHED", socket_kind);
        let pending_storage = client.storage_node();
        assert_eq!(permanent_edge_count(&backlog_node, &pending_storage), 1);

        // The pending server connection is owned by the listener backlog. Queuing the listener FD
        // in that connection would close owner -> backlog -> storage -> owner and must be rejected.
        let error = ReservedEdges::try_new(&pending_storage, &[owner.clone()])
            .err()
            .unwrap();
        assert_eq!(error, ReservationError::Cycle);
        assert_eq!(reserved_edge_count(&pending_storage, &owner), 0);
        ostd::early_println!("B1_LISTENER_OWNER_{}_CYCLE_REJECTED", socket_kind);

        listener.shutdown(SockShutdownCmd::SHUT_RD, &Pollee::new());
        assert_eq!(permanent_edge_count(&backlog_node, &pending_storage), 0);
        assert_eq!(permanent_edge_count(&owner, &backlog_node), 1);
        ostd::early_println!("B1_LISTENER_OWNER_{}_SHUTDOWN_DRAINED", socket_kind);

        drop(listener);
        assert!(BACKLOG_TABLE.get_backlog(&backlog_key).is_none());
        assert_eq!(permanent_edge_count(&owner, &backlog_node), 0);
        ostd::early_println!("B1_LISTENER_OWNER_{}_LISTENER_DROPPED", socket_kind);

        drop(client);
        ostd::early_println!("B1_LISTENER_OWNER_{}_CLIENT_DROPPED", socket_kind);
        drop(pending_storage);
        drop(owner);
        assert!(!weak_owner.is_alive());
        drop(backlog_node);
        assert!(!weak_backlog.is_alive());
        // Make every remaining strong non-graph reference explicit so the final marker also proves
        // that cleanup cannot be deferred to implicit local-variable drop order.
        drop(client_node);
        drop(backlog_key);
        drop(weak_owner);
        drop(weak_backlog);
        ostd::early_println!("B1_LISTENER_OWNER_{}_WEAKS_RELEASED", socket_kind);
    }

    #[ktest]
    fn backlog_full_fails_before_creating_owned_storage() {
        let backlog = new_backlog(0);
        let client_node = SocketNode::new();
        let (error, _init) = backlog
            .push_incoming(
                Init::new(),
                Pollee::new(),
                &OptionSet::new(),
                false,
                &client_node,
            )
            .err()
            .unwrap();

        assert_eq!(error.error(), Errno::EAGAIN);
        assert!(backlog.incoming_conns.lock().as_ref().unwrap().is_empty());
    }

    #[ktest]
    fn pending_owner_transfers_to_accepted_socket() {
        let backlog = new_backlog(1);
        let client_node = SocketNode::new();
        let client = push_incoming(&backlog, &client_node, false);
        let storage = client.storage_node();
        let weak_storage = storage.downgrade();

        assert_eq!(permanent_edge_count(&client_node, &storage), 1);
        assert_eq!(permanent_edge_count(&backlog.scm_node, &storage), 1);
        let mut pending = backlog.pop_incoming().unwrap();
        assert_eq!(permanent_edge_count(&backlog.scm_node, &storage), 1);

        // `UnixStreamSocket::new_connected` initializes the global SockFs mount, which is not
        // available in the bare ktest runner. Exercise its ownership-transfer operation directly.
        let accepted_node = SocketNode::new();
        pending.replace_owner(&accepted_node);
        assert_eq!(permanent_edge_count(&backlog.scm_node, &storage), 0);
        assert_eq!(permanent_edge_count(&accepted_node, &storage), 1);

        drop(pending);
        drop(client);
        assert_eq!(permanent_edge_count(&client_node, &storage), 0);
        drop(storage);
        assert!(!weak_storage.is_alive());
    }

    #[ktest]
    fn backlog_shutdown_drops_pending_owner_outside_lock() {
        let backlog = new_backlog(1);
        let client_node = SocketNode::new();
        let client = push_incoming(&backlog, &client_node, false);
        let storage = client.storage_node();

        assert_eq!(permanent_edge_count(&backlog.scm_node, &storage), 1);
        backlog.shutdown();
        assert!(backlog.incoming_conns.lock().is_none());
        assert_eq!(permanent_edge_count(&backlog.scm_node, &storage), 0);
        assert_eq!(permanent_edge_count(&client_node, &storage), 1);

        drop(client);
        assert_eq!(permanent_edge_count(&client_node, &storage), 0);
    }

    fn new_backlog(capacity: usize) -> Backlog {
        Backlog::new_with_cred(
            UnixSocketAddr::Unnamed.bind().unwrap(),
            Pollee::new(),
            capacity,
            false,
            false,
            SocketCred::<ReadDupOp>::new_test_root(),
        )
    }

    fn push_incoming(backlog: &Backlog, client_node: &SocketNode, is_seqpacket: bool) -> Connected {
        match backlog.push_incoming(
            Init::new(),
            Pollee::new(),
            &OptionSet::new(),
            is_seqpacket,
            client_node,
        ) {
            Ok(connected) => connected,
            Err((error, _init)) => panic!("unexpected connect error: {:?}", error),
        }
    }
}
