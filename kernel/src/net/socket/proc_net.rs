// SPDX-License-Identifier: MPL-2.0

//! The sockets one network namespace exposes through `/proc/net`.
//!
//! `/proc/net/tcp`, `/proc/net/udp` and `/proc/net/unix` are snapshots of the
//! sockets that belong to a namespace. The socket objects themselves live in
//! their own modules and their state is split across files (the TCP state
//! machine, the UNIX socket state machine, the file description's inode), so
//! this module defines a small trait each socket type implements and a
//! registry that the namespace holds.
//!
//! The registry stores weak references: a socket that has been closed and
//! dropped disappears from the next snapshot without any unregistration
//! protocol.
//!
//! Lock order: the registry lock is only held while upgrading the weak
//! references, never while a socket is being inspected. A socket's own state
//! lock is taken under no registry lock, so a process reading `/proc/net`
//! cannot be part of a cycle with the network stack.

use aster_bigtcp::{
    socket::ConnectState,
    wire::{IpAddress, IpEndpoint},
};

use crate::{prelude::*, util::net::SockType};

/// The transport protocol of an Internet socket, as `/proc/net` names it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum InetProtocol {
    Tcp,
    Udp,
}

/// One Internet socket, in the terms `/proc/net/tcp` and `/proc/net/udp` print.
#[derive(Clone, Debug)]
pub(crate) struct InetSocketEntry {
    pub protocol: InetProtocol,
    pub local: IpEndpoint,
    pub remote: Option<IpEndpoint>,
    /// The Linux TCP state code (`01` established, `0A` listen, ...). UDP
    /// sockets report `01` when connected and `07` otherwise.
    pub state: u8,
    pub inode: u64,
    pub uid: u32,
}

/// One AF_UNIX socket, in the terms `/proc/net/unix` prints.
#[derive(Clone, Debug)]
pub(crate) struct UnixSocketEntry {
    /// The bound path, if the socket has one; unnamed sockets have none.
    pub path: Option<String>,
    pub socket_type: SockType,
    /// The Linux `SS_*` state: `01` unconnected, `03` connected.
    pub state: u8,
    /// Whether the socket accepts connections (a listening stream socket).
    pub accepting: bool,
    pub inode: u64,
}

#[derive(Clone, Debug)]
pub(crate) enum ProcNetEntry {
    Inet(InetSocketEntry),
    Unix(UnixSocketEntry),
}

impl ProcNetEntry {
    /// The `/proc/net` file this entry belongs to.
    ///
    /// IPv4-mapped IPv6 sockets are listed with their IPv6 file, as Linux
    /// does; the address family of the endpoint decides.
    pub fn file_name(&self) -> &'static str {
        match self {
            ProcNetEntry::Inet(entry) => match (entry.protocol, entry.local.addr) {
                (InetProtocol::Tcp, IpAddress::Ipv4(_)) => "tcp",
                (InetProtocol::Tcp, IpAddress::Ipv6(_)) => "tcp6",
                (InetProtocol::Udp, IpAddress::Ipv4(_)) => "udp",
                (InetProtocol::Udp, IpAddress::Ipv6(_)) => "udp6",
            },
            ProcNetEntry::Unix(_) => "unix",
        }
    }
}

/// A socket that can describe itself to `/proc/net`.
pub(crate) trait ProcNetSocket: Send + Sync {
    /// Appends this socket's rows to `out`.
    ///
    /// A socket that is not yet visible to `/proc/net` (for example a TCP
    /// socket that has only been bound) appends nothing.
    fn proc_net_entries(&self, out: &mut Vec<ProcNetEntry>);
}

/// The weak-reference registry held by a [`NetNamespace`].
///
/// [`NetNamespace`]: crate::net::net_ns::NetNamespace
pub(crate) struct ProcNetRegistry {
    sockets: Mutex<Vec<Weak<dyn ProcNetSocket>>>,
}

impl ProcNetRegistry {
    pub fn new() -> Self {
        Self {
            sockets: Mutex::new(Vec::new()),
        }
    }

    /// Registers a socket. The registry does not keep it alive.
    pub fn register(&self, socket: Weak<dyn ProcNetSocket>) {
        let mut sockets = self.sockets.lock();
        // Drop the entries of sockets that were closed and dropped; otherwise
        // a long-lived namespace would accumulate one dead weak reference per
        // socket it has ever seen.
        sockets.retain(|socket| socket.strong_count() > 0);
        sockets.push(socket);
    }

    /// Counts every live registered socket, including the ones `/proc/net`
    /// does not list (an unbound UDP socket, a TCP socket that is only bound).
    ///
    /// This is the per-namespace part of Linux's `sockets: used` line.
    pub fn live_count(&self) -> usize {
        let mut sockets = self.sockets.lock();
        sockets.retain(|socket| socket.strong_count() > 0);
        sockets.len()
    }

    /// Takes a snapshot of every live socket in the namespace.
    pub fn snapshot(&self) -> Vec<ProcNetEntry> {
        let sockets: Vec<Arc<dyn ProcNetSocket>> = {
            let mut sockets = self.sockets.lock();
            sockets.retain(|socket| socket.strong_count() > 0);
            sockets.iter().filter_map(Weak::upgrade).collect()
        };

        let mut entries = Vec::new();
        for socket in sockets {
            socket.proc_net_entries(&mut entries);
        }
        entries
    }
}

impl Default for ProcNetRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// The Linux TCP state code for a smoltcp TCP state.
pub(crate) fn tcp_state_code(state: smoltcp::socket::tcp::State) -> u8 {
    use smoltcp::socket::tcp::State;
    match state {
        State::Established => 0x01,
        State::SynSent => 0x02,
        State::SynReceived => 0x03,
        State::FinWait1 => 0x04,
        State::FinWait2 => 0x05,
        State::TimeWait => 0x06,
        State::Closed => 0x07,
        State::CloseWait => 0x08,
        State::LastAck => 0x09,
        State::Listen => 0x0a,
        State::Closing => 0x0b,
    }
}

/// The Linux TCP state code for a connection that has not completed the
/// handshake yet.
pub(crate) fn connect_state_code(state: ConnectState) -> u8 {
    match state {
        ConnectState::Connecting => 0x02, // SYN_SENT
        ConnectState::Connected => 0x01,  // ESTABLISHED
        ConnectState::Refused => 0x07,    // CLOSE
    }
}

/// Formats one endpoint the way `/proc/net/tcp` does: the address in host
/// byte order as hexadecimal, a colon, then the port.
///
/// The address is written as the u32 value of the address in little-endian
/// order, which is why `127.0.0.1` reads `0100007F`. IPv6 addresses are four
/// such 32-bit words.
pub(crate) fn format_endpoint(endpoint: IpEndpoint) -> String {
    let address = match endpoint.addr {
        IpAddress::Ipv4(address) => alloc::format!("{:08X}", u32::from_le_bytes(address.octets())),
        IpAddress::Ipv6(address) => {
            let octets = address.octets();
            let mut text = String::with_capacity(32);
            for word in octets.chunks(4) {
                let value = u32::from_le_bytes([word[0], word[1], word[2], word[3]]);
                text.push_str(&alloc::format!("{:08X}", value));
            }
            text
        }
    };
    alloc::format!("{}:{:04X}", address, endpoint.port)
}

/// `00000000:0000` — what Linux prints for "no endpoint".
pub(crate) const UNSPECIFIED_ENDPOINT_TEXT: &str = "00000000:0000";

#[cfg(ktest)]
mod tests {
    use aster_bigtcp::wire::{Ipv4Address, Ipv6Address};
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn endpoints_are_formatted_in_linux_byte_order() {
        let loopback = IpEndpoint::new(IpAddress::Ipv4(Ipv4Address::LOCALHOST), 22);
        assert_eq!(format_endpoint(loopback), "0100007F:0016");

        let board = IpEndpoint::new(IpAddress::Ipv4(Ipv4Address::new(58, 206, 232, 122)), 3080);
        assert_eq!(format_endpoint(board), "7AE8CE3A:0C08");

        let loopback6 = IpEndpoint::new(IpAddress::Ipv6(Ipv6Address::LOCALHOST), 80);
        assert_eq!(
            format_endpoint(loopback6),
            "00000000000000000000000001000000:0050"
        );
    }

    #[ktest]
    fn tcp_states_map_to_the_numbers_tools_read() {
        use smoltcp::socket::tcp::State;
        assert_eq!(tcp_state_code(State::Established), 0x01);
        assert_eq!(tcp_state_code(State::SynSent), 0x02);
        assert_eq!(tcp_state_code(State::TimeWait), 0x06);
        assert_eq!(tcp_state_code(State::Listen), 0x0a);
        assert_eq!(tcp_state_code(State::Closing), 0x0b);
    }

    #[ktest]
    fn entries_land_in_the_file_their_address_family_names() {
        let v4 = IpEndpoint::new(IpAddress::Ipv4(Ipv4Address::LOCALHOST), 1);
        let v6 = IpEndpoint::new(IpAddress::Ipv6(Ipv6Address::LOCALHOST), 1);
        let inet = |local| {
            ProcNetEntry::Inet(InetSocketEntry {
                protocol: InetProtocol::Tcp,
                local,
                remote: None,
                state: 0x0a,
                inode: 1,
                uid: 0,
            })
        };
        assert_eq!(inet(v4).file_name(), "tcp");
        assert_eq!(inet(v6).file_name(), "tcp6");
    }
}
