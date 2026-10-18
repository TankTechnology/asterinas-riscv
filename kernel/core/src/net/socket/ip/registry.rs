// SPDX-License-Identifier: MPL-2.0

//! Registry of live IPv4 and IPv6 sockets.
//!
//! The kernel exposes socket state through two views: the text files under
//! `/proc/<pid>/net` and the `NETLINK_SOCK_DIAG` dump. Both must describe the
//! same sockets, so they read one registry instead of each walking a different
//! structure.
//!
//! Entries hold a weak reference. A socket drops out of the registry when the
//! last strong reference goes away; enumeration prunes the dead entries.

use spin::Once;

use crate::{
    net::socket::{Socket, ip::StreamSocket, util::SocketAddr},
    prelude::*,
    process::posix_thread::AsPosixThread,
    thread::Thread,
};

/// Linux `TCP_ESTABLISHED`, used as the state for connected UDP sockets.
pub(crate) const TCP_ESTABLISHED: u8 = 1;
pub(crate) const TCP_SYN_SENT: u8 = 2;
pub(crate) const TCP_CLOSE: u8 = 7;
pub(crate) const TCP_LISTEN: u8 = 10;

/// The transport protocol of a registered socket.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum InetProtocol {
    Tcp,
    Udp,
}

/// The part of a socket's state that monitoring interfaces need.
pub(crate) trait InetSocket: Send + Sync {
    fn inet_protocol(&self) -> InetProtocol;
    fn inet_local(&self) -> Option<SocketAddr>;
    fn inet_remote(&self) -> Option<SocketAddr>;
    /// Linux TCP state code; UDP uses `TCP_ESTABLISHED` when connected and
    /// `TCP_CLOSE` otherwise.
    fn inet_state(&self) -> u8;
}

/// One row of a socket listing.
#[derive(Debug)]
pub(crate) struct InetSocketSnapshot {
    pub protocol: InetProtocol,
    pub local: SocketAddr,
    pub remote: Option<SocketAddr>,
    pub state: u8,
    pub uid: u32,
    pub inode: u64,
}

struct Entry {
    socket: Weak<dyn InetSocket>,
    uid: u32,
    inode: u64,
}

static REGISTRY: Once<Mutex<Vec<Entry>>> = Once::new();

fn registry() -> &'static Mutex<Vec<Entry>> {
    REGISTRY.call_once(|| Mutex::new(Vec::new()))
}

/// Registers a socket and returns the inode number reported for it.
///
/// `inode` is the sockfs inode number, the same value `/proc/<pid>/fd` prints
/// as `socket:[N]`, so tools can attribute a row to a process.
pub(crate) fn register<T: InetSocket + 'static>(socket: &Arc<T>, inode: u64) -> u64 {
    let uid = current_uid();
    let socket = Arc::downgrade(&(Arc::clone(socket) as Arc<dyn InetSocket>));

    let mut entries = registry().lock();
    entries.retain(|entry| entry.socket.strong_count() > 0);
    entries.push(Entry { socket, uid, inode });

    inode
}

/// Returns a snapshot of every registry entry that still has a live socket and
/// a bound local port.
pub(crate) fn inet_sockets() -> Vec<InetSocketSnapshot> {
    let mut entries = registry().lock();
    entries.retain(|entry| entry.socket.strong_count() > 0);

    let mut snapshots = Vec::with_capacity(entries.len());
    for entry in entries.iter() {
        let Some(socket) = entry.socket.upgrade() else {
            continue;
        };
        let Some(local) = socket.inet_local() else {
            continue;
        };
        // Linux keeps unbound sockets out of the inet tables. A zero local
        // port is how an unbound or freshly created socket appears here.
        if socket_addr_port(&local) == 0 {
            continue;
        }
        snapshots.push(InetSocketSnapshot {
            protocol: socket.inet_protocol(),
            local,
            remote: socket.inet_remote(),
            state: socket.inet_state(),
            uid: entry.uid,
            inode: entry.inode,
        });
    }

    snapshots
}

fn current_uid() -> u32 {
    Thread::current()
        .and_then(|thread| {
            thread
                .as_posix_thread()
                .map(|posix_thread| u32::from(posix_thread.credentials().fsuid()))
        })
        .unwrap_or(0)
}

pub(crate) fn socket_addr_port(addr: &SocketAddr) -> u16 {
    match addr {
        SocketAddr::IPv4(_, port) | SocketAddr::IPv6(_, port) => *port,
        _ => 0,
    }
}

/// Returns the sockfs inode number of a socket.
///
/// This is the number `/proc/<pid>/fd` renders as `socket:[N]`; sock_diag and
/// `/proc/net/*` must report it so `ss -p` can attribute a row to a process.
pub(crate) fn socket_inode(common: &crate::fs::file::FileCommon) -> u64 {
    common.path().inode().ino() as u64
}

impl InetSocket for StreamSocket {
    fn inet_protocol(&self) -> InetProtocol {
        InetProtocol::Tcp
    }

    fn inet_local(&self) -> Option<SocketAddr> {
        Socket::addr(self).ok()
    }

    fn inet_remote(&self) -> Option<SocketAddr> {
        Socket::peer_addr(self).ok()
    }

    fn inet_state(&self) -> u8 {
        self.tcp_state_code()
    }
}

impl InetSocket for crate::net::socket::ip::DatagramSocket {
    fn inet_protocol(&self) -> InetProtocol {
        InetProtocol::Udp
    }

    fn inet_local(&self) -> Option<SocketAddr> {
        Socket::addr(self).ok()
    }

    fn inet_remote(&self) -> Option<SocketAddr> {
        Socket::peer_addr(self).ok()
    }

    fn inet_state(&self) -> u8 {
        if Socket::peer_addr(self).is_ok() {
            TCP_ESTABLISHED
        } else {
            TCP_CLOSE
        }
    }
}
