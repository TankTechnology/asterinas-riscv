// SPDX-License-Identifier: MPL-2.0

//! The network view visible to one task's network namespace.
//!
//! This directory is what `/proc/net` resolves to. The files are the ones
//! Linux exposes for a wired host: the interface counters (`dev`), the socket
//! tables (`tcp`, `tcp6`, `udp`, `udp6`, `raw`, `raw6`, `unix`), the routing
//! table (`route`) and the neighbour table (`arp`). `netstat`, `ss`'s
//! `/proc`-based peers, `lsof` and monitoring scripts read these.

use aster_bigtcp::wire::{IpAddress, IpEndpoint};
use aster_util::printer::{VmPrinter, VmPrinterError};

use super::TidDirOps;
use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::template::{
            ListedEntry, ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry,
            visit_listed_entries,
        },
        vfs::inode::Inode,
    },
    net::{
        net_ns::NetNamespace,
        socket::proc_net::{
            InetProtocol, InetSocketEntry, ProcNetEntry, UnixSocketEntry, format_endpoint,
        },
    },
    prelude::*,
    process::posix_thread::AsPosixThread,
    thread::Thread,
};

/// One file in `/proc/net`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NetFileKind {
    Dev,
    Tcp,
    Tcp6,
    Udp,
    Udp6,
    Raw,
    Raw6,
    Unix,
    Route,
    Arp,
    SockStat,
    SockStat6,
    Snmp,
    NetStat,
    Snmp6,
}

const NET_FILES: [(&str, NetFileKind); 15] = [
    ("dev", NetFileKind::Dev),
    ("tcp", NetFileKind::Tcp),
    ("tcp6", NetFileKind::Tcp6),
    ("udp", NetFileKind::Udp),
    ("udp6", NetFileKind::Udp6),
    ("raw", NetFileKind::Raw),
    ("raw6", NetFileKind::Raw6),
    ("unix", NetFileKind::Unix),
    ("route", NetFileKind::Route),
    ("arp", NetFileKind::Arp),
    ("sockstat", NetFileKind::SockStat),
    ("sockstat6", NetFileKind::SockStat6),
    ("snmp", NetFileKind::Snmp),
    ("netstat", NetFileKind::NetStat),
    ("snmp6", NetFileKind::Snmp6),
];

impl NetFileKind {
    fn from_name(name: &str) -> Option<Self> {
        NET_FILES
            .iter()
            .find(|(file_name, _)| *file_name == name)
            .map(|(_, kind)| *kind)
    }

    fn name(self) -> &'static str {
        NET_FILES
            .iter()
            .find(|(_, kind)| *kind == self)
            .map(|(name, _)| *name)
            .unwrap()
    }

    /// The transport protocol this file shows, if it is a TCP/UDP table.
    fn inet_protocol(self) -> Option<InetProtocol> {
        match self {
            NetFileKind::Tcp | NetFileKind::Tcp6 => Some(InetProtocol::Tcp),
            NetFileKind::Udp | NetFileKind::Udp6 => Some(InetProtocol::Udp),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum IpAddressFamily {
    V4,
    V6,
}

/// The `/proc/[pid]/net` directory, also inherited by `/proc/[pid]/task/[tid]`.
pub(super) struct NetDirOps(TidDirOps);

impl NetDirOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self(dir.clone()), parent, mkmod!(a+rx))
    }

    fn net_ns(&self) -> Result<Arc<NetNamespace>> {
        let thread = self
            .0
            .thread()
            .ok_or_else(|| Error::with_message(Errno::ESRCH, "the thread does not exist"))?;
        let ns_proxy = thread.as_posix_thread().unwrap().ns_proxy().lock();
        let proxy = ns_proxy
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "the thread has exited"))?;
        Ok(proxy.net_ns().clone())
    }
}

impl ProcDirOps for NetDirOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        let Some(kind) = NetFileKind::from_name(name) else {
            return_errno_with_message!(Errno::ENOENT, "the file does not exist");
        };
        Ok(NetFileOps::new_inode(
            self.net_ns()?,
            kind,
            this_dir.this_weak().clone(),
        ))
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        self.net_ns()?;
        let entries = NET_FILES.map(|(name, _)| ListedEntry::new(name, InodeType::File));
        visit_listed_entries(offset, entries, visit_fn)
    }
}

struct NetFileOps {
    net_ns: Arc<NetNamespace>,
    kind: NetFileKind,
}

impl NetFileOps {
    fn new_inode(
        net_ns: Arc<NetNamespace>,
        kind: NetFileKind,
        parent: Weak<dyn Inode>,
    ) -> Arc<dyn Inode> {
        ProcFile::new(Self { net_ns, kind }, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for NetFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match self.kind {
            NetFileKind::Dev => show_dev(&mut printer, &self.net_ns)?,
            NetFileKind::Unix => show_unix(&mut printer, &self.net_ns)?,
            NetFileKind::Route => show_route(&mut printer, &self.net_ns)?,
            NetFileKind::Arp => show_arp(&mut printer)?,
            NetFileKind::SockStat => show_sockstat(&mut printer, &self.net_ns, false)?,
            NetFileKind::SockStat6 => show_sockstat(&mut printer, &self.net_ns, true)?,
            NetFileKind::Snmp => show_snmp(&mut printer, &self.net_ns, false)?,
            NetFileKind::NetStat => show_netstat(&mut printer, &self.net_ns, false)?,
            NetFileKind::Snmp6 => show_snmp(&mut printer, &self.net_ns, true)?,
            NetFileKind::Raw | NetFileKind::Raw6 => {
                // Raw sockets are not implemented, so the table is empty, but
                // the file must exist: readers treat a missing file as "the
                // kernel predates this feature".
                writeln!(
                    printer,
                    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
                )?;
            }
            kind => show_inet(&mut printer, &self.net_ns, kind)?,
        }
        Ok(printer.bytes_written())
    }
}

fn show_dev(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
) -> core::result::Result<(), VmPrinterError> {
    writeln!(
        printer,
        "Inter-|   Receive                                                |  Transmit"
    )?;
    writeln!(
        printer,
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed"
    )?;

    for iface in net_ns.ifaces() {
        let stats = iface.stats();
        // Only packet and byte counters are instrumented today. The other
        // Linux columns remain zero until their corresponding events are tracked.
        let fields = [
            stats.rx_bytes,
            stats.rx_packets,
            0,
            0,
            0,
            0,
            0,
            0,
            stats.tx_bytes,
            stats.tx_packets,
            0,
            0,
            0,
            0,
            0,
            0,
        ];
        write!(printer, "{:>6}:", iface.name().to_str().unwrap_or("?"))?;
        for value in fields {
            write!(printer, " {:>8}", value)?;
        }
        writeln!(printer)?;
    }

    Ok(())
}

fn show_inet(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
    kind: NetFileKind,
) -> core::result::Result<(), VmPrinterError> {
    let Some(protocol) = kind.inet_protocol() else {
        return Ok(());
    };

    match protocol {
        InetProtocol::Tcp => writeln!(
            printer,
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
        )?,
        InetProtocol::Udp => writeln!(
            printer,
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops"
        )?,
    }

    let entries = net_ns.proc_net_sockets().snapshot();
    let mut index = 0;
    for entry in entries {
        if entry.file_name() != kind.name() {
            continue;
        }
        let ProcNetEntry::Inet(entry) = entry else {
            continue;
        };
        show_inet_row(printer, index, &entry)?;
        index += 1;
    }

    Ok(())
}

fn show_inet_row(
    printer: &mut VmPrinter<'_, '_>,
    index: usize,
    entry: &InetSocketEntry,
) -> core::result::Result<(), VmPrinterError> {
    let local = format_endpoint(entry.local);
    let remote = match entry.remote {
        Some(remote) => format_endpoint(remote),
        None => unspecified_endpoint(entry.local).to_string(),
    };
    match entry.protocol {
        InetProtocol::Tcp => writeln!(
            printer,
            "{:4}: {} {} {:02X} 00000000:00000000 00:00000000 00000000 {:5} {:8} {}",
            index, local, remote, entry.state, entry.uid, 0, entry.inode
        )?,
        InetProtocol::Udp => writeln!(
            printer,
            "{:4}: {} {} {:02X} 00000000:00000000 00:00000000 00000000 {:5} {:8} {} 2 0000000000000000 0",
            index, local, remote, entry.state, entry.uid, 0, entry.inode
        )?,
    }
    Ok(())
}

fn address_family(endpoint: IpEndpoint) -> IpAddressFamily {
    match endpoint.addr {
        IpAddress::Ipv4(_) => IpAddressFamily::V4,
        IpAddress::Ipv6(_) => IpAddressFamily::V6,
    }
}

const UNSPECIFIED_V4: &str = "00000000:0000";
const UNSPECIFIED_V6: &str = "00000000000000000000000000000000:0000";

fn unspecified_endpoint(local: IpEndpoint) -> &'static str {
    match address_family(local) {
        IpAddressFamily::V4 => UNSPECIFIED_V4,
        IpAddressFamily::V6 => UNSPECIFIED_V6,
    }
}

fn show_unix(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
) -> core::result::Result<(), VmPrinterError> {
    writeln!(
        printer,
        "Num       RefCount Protocol Flags    Type St Inode Path"
    )?;

    let entries = net_ns.proc_net_sockets().snapshot();
    for entry in entries {
        let ProcNetEntry::Unix(entry) = entry else {
            continue;
        };
        show_unix_row(printer, &entry)?;
    }
    Ok(())
}

fn show_unix_row(
    printer: &mut VmPrinter<'_, '_>,
    entry: &UnixSocketEntry,
) -> core::result::Result<(), VmPrinterError> {
    // `Num` is a kernel address in Linux; tools do not interpret it, so the
    // inode identifies the row here as well.
    writeln!(
        printer,
        "{:08X}: {:08X} {:08X} {:08X} {:04X} {:02X} {:5} {}",
        entry.inode,
        2,
        0,
        if entry.accepting { 0x0001_0000 } else { 0 },
        entry.socket_type as u32,
        entry.state,
        entry.inode,
        entry.path.as_deref().unwrap_or("")
    )?;
    Ok(())
}

fn show_route(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
) -> core::result::Result<(), VmPrinterError> {
    writeln!(
        printer,
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"
    )?;

    for iface in net_ns.ifaces() {
        let Some(cidr) = iface.ipv4_cidr() else {
            continue;
        };
        let name = iface.name().to_str().unwrap_or("?");
        let mask = cidr.netmask();
        let network =
            aster_bigtcp::wire::Ipv4Address::from_bits(cidr.address().to_bits() & mask.to_bits());

        // The connected route for the interface's own subnet.
        writeln!(
            printer,
            "{}\t{:08X}\t{:08X}\t{:04X}\t{}\t{}\t{}\t{:08X}\t{}\t{}\t{}",
            name,
            u32::from_le_bytes(network.octets()),
            0,
            0x0001,
            0,
            0,
            0,
            u32::from_le_bytes(mask.octets()),
            0,
            0,
            0
        )?;

        // The default route, when the interface has a gateway.
        if let Some(gateway) = iface.ipv4_gateway() {
            writeln!(
                printer,
                "{}\t{:08X}\t{:08X}\t{:04X}\t{}\t{}\t{}\t{:08X}\t{}\t{}\t{}",
                name,
                0,
                u32::from_le_bytes(gateway.octets()),
                0x0003,
                0,
                0,
                0,
                0,
                0,
                0,
                0
            )?;
        }
    }
    Ok(())
}

/// Counts the sockets one address family holds, split the way
/// `/proc/net/sockstat` reports them.
#[derive(Default)]
struct SocketCounts {
    /// Every TCP socket in this address family, whatever its state.
    tcp_total: usize,
    /// TCP sockets that are not in the CLOSED state.
    ///
    /// `ss -s` derives the "closed" line as
    /// `alloc - (tcp4_hashed + tcp6_hashed - timewait)`, where the two
    /// "hashed" numbers are the `inuse` fields of `sockstat` and `sockstat6`.
    /// Reporting the non-closed count here makes that subtraction land on the
    /// real number of CLOSED sockets.
    tcp_non_closed: usize,
    tcp_time_wait: usize,
    tcp_established: usize,
    udp_in_use: usize,
    unix: usize,
}

fn count_sockets(net_ns: &NetNamespace, ipv6: bool) -> SocketCounts {
    let mut counts = SocketCounts::default();
    for entry in net_ns.proc_net_sockets().snapshot() {
        match entry {
            ProcNetEntry::Inet(entry) => {
                let is_v6 = address_family(entry.local) == IpAddressFamily::V6;
                if is_v6 != ipv6 {
                    continue;
                }
                match entry.protocol {
                    InetProtocol::Tcp => {
                        counts.tcp_total += 1;
                        if entry.state != 0x07 {
                            counts.tcp_non_closed += 1;
                        }
                        if entry.state == 0x06 {
                            counts.tcp_time_wait += 1;
                        }
                        if entry.state == 0x01 {
                            counts.tcp_established += 1;
                        }
                    }
                    InetProtocol::Udp => counts.udp_in_use += 1,
                }
            }
            ProcNetEntry::Unix(_) if !ipv6 => counts.unix += 1,
            ProcNetEntry::Unix(_) => {}
        }
    }
    counts
}

/// `/proc/net/sockstat` and `/proc/net/sockstat6`.
///
/// `ss -s` reads these two files before it tries netlink, and a missing file
/// makes it print an error instead of a summary.
fn show_sockstat(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
    ipv6: bool,
) -> core::result::Result<(), VmPrinterError> {
    let counts = count_sockets(net_ns, ipv6);
    if ipv6 {
        writeln!(printer, "TCP6: inuse {}", counts.tcp_non_closed)?;
        writeln!(printer, "UDP6: inuse {}", counts.udp_in_use)?;
        writeln!(printer, "UDPLITE6: inuse 0")?;
        writeln!(printer, "RAW6: inuse 0")?;
        writeln!(printer, "FRAG6: inuse 0 memory 0")?;
    } else {
        let v4 = counts;
        let v6 = count_sockets(net_ns, true);
        // `ss -s` prints alloc + tw as the TCP total, so `alloc` has to be
        // every TCP socket except the ones in TIME_WAIT. The two `inuse`
        // fields are the non-closed sockets of each family; together with
        // `alloc` they make `ss` compute the real CLOSED count.
        let total = v4.tcp_total + v6.tcp_total;
        // `tw` is the namespace's TIME_WAIT count in Linux, which covers both
        // address families; `alloc` excludes exactly that so iproute2's
        // `alloc + tw` still equals the total and `closed` cancels to the real
        // CLOSED count.
        let time_wait = v4.tcp_time_wait + v6.tcp_time_wait;
        let alloc = total.saturating_sub(time_wait);
        writeln!(printer, "sockets: used {}", net_ns.socket_count())?;
        writeln!(
            printer,
            "TCP: inuse {} orphan 0 tw {} alloc {} mem 0",
            v4.tcp_non_closed, time_wait, alloc
        )?;
        writeln!(printer, "UDP: inuse {} mem 0", v4.udp_in_use)?;
        writeln!(printer, "UDPLITE: inuse 0")?;
        writeln!(printer, "RAW: inuse 0")?;
        writeln!(printer, "FRAG: inuse 0 memory 0")?;
    }
    Ok(())
}

/// `/proc/net/snmp`.
///
/// The field names are Linux's; the counters this kernel does not track are
/// reported as zero rather than omitted, because readers parse by position.
fn show_snmp(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
    ipv6: bool,
) -> core::result::Result<(), VmPrinterError> {
    let counts = count_sockets(net_ns, ipv6);
    if ipv6 {
        writeln!(
            printer,
            "Ip6: Forwarding DefaultHopLimit InReceives InHdrErrors InAddrErrors"
        )?;
        writeln!(printer, "Ip6: 0 64 0 0 0")?;
        writeln!(
            printer,
            "Icmp6: InMsgs InErrors InDestUnreachs OutMsgs OutErrors"
        )?;
        writeln!(printer, "Icmp6: 0 0 0 0 0")?;
        writeln!(
            printer,
            "Tcp6: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors"
        )?;
        writeln!(
            printer,
            "Tcp6: 1 200 120000 -1 0 0 0 0 {} 0 0 0 0 0 0",
            counts.tcp_established
        )?;
        writeln!(
            printer,
            "Udp6: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti"
        )?;
        writeln!(printer, "Udp6: 0 0 0 0 0 0 0 0")?;
        return Ok(());
    }

    writeln!(
        printer,
        "Ip: Forwarding DefaultTTL InReceives InHdrErrors InAddrErrors ForwDatagrams InUnknownProtos InDiscards InDelivers OutRequests OutDiscards OutNoRoutes ReasmTimeout ReasmReqds ReasmOKs ReasmFails FragOKs FragFails FragCreates"
    )?;
    writeln!(printer, "Ip: 1 64 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")?;
    writeln!(
        printer,
        "Icmp: InMsgs InErrors InCsumErrors InDestUnreachs InTimeExcds InParmProbs InSrcQuenchs InRedirects InEchos InEchoReps InTimestamps InTimestampReps InAddrMasks InAddrMaskReps OutMsgs OutErrors OutDestUnreachs OutTimeExcds OutParmProbs OutSrcQuenchs OutRedirects OutEchos OutEchoReps OutTimestamps OutTimestampReps OutAddrMasks OutAddrMaskReps"
    )?;
    writeln!(
        printer,
        "Icmp: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    )?;
    writeln!(
        printer,
        "Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors"
    )?;
    // Linux's Tcp MIB is per netns and counts established connections of both
    // address families, so the IPv4 file reports the v4 + v6 total.
    let established = counts.tcp_established + count_sockets(net_ns, true).tcp_established;
    writeln!(
        printer,
        "Tcp: 1 200 120000 -1 0 0 0 0 {} 0 0 0 0 0 0",
        established
    )?;
    writeln!(
        printer,
        "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti"
    )?;
    writeln!(printer, "Udp: 0 0 0 0 0 0 0 0")?;
    Ok(())
}

/// `/proc/net/netstat`: the extended counters `ss -s` reads for retransmits.
fn show_netstat(
    printer: &mut VmPrinter<'_, '_>,
    net_ns: &NetNamespace,
    ipv6: bool,
) -> core::result::Result<(), VmPrinterError> {
    let _ = count_sockets(net_ns, ipv6);
    writeln!(
        printer,
        "TcpExt: SyncookiesSent SyncookiesRecv SyncookiesFailed EmbryonicRsts PruneCalled RcvPruned OfoPruned OutOfWindowIcmps LockDroppedIcmps ArpFilter TW TWRecycled TWKilled PAWSPassive PAWSActive PAWSEstab DelayedACKs DelayedACKLocked DelayedACKLost ListenOverflows ListenDrops TCPHPHits TCPPureAcks TCPHPAcks TCPRenoRecovery TCPSackRecovery TCPSACKReneging TCPFACKReorder TCPSACKReorder TCPRenoReorder TCPTSReorder TCPFullUndo TCPPartialUndo TCPDSACKUndo TCPLossUndo TCPLostRetransmit TCPRenoFailures TCPSackFailures TCPLossFailures TCPFastRetrans TCPSlowStartRetrans TCPTimeouts TCPLossProbes TCPLossProbeRecovery TCPRenoRecoveryFail TCPSackRecoveryFail TCPRcvCollapsed TCPDSACKOldSent TCPDSACKOfoSent TCPDSACKRecv TCPDSACKOfoRecv TCPAbortOnData TCPAbortOnClose TCPAbortOnMemory TCPAbortOnTimeout TCPAbortOnLinger TCPAbortFailed TCPMemoryPressures TCPBacklogDrop"
    )?;
    write!(printer, "TcpExt:")?;
    for _ in 0..59 {
        write!(printer, " 0")?;
    }
    writeln!(printer)?;
    Ok(())
}

fn show_arp(printer: &mut VmPrinter<'_, '_>) -> core::result::Result<(), VmPrinterError> {
    // The neighbor cache lives inside the Ethernet interface and is not
    // exposed; an empty table with the right header is still useful, because
    // readers distinguish it from an unimplemented file.
    writeln!(
        printer,
        "IP address       HW type     Flags       HW address            Mask     Device"
    )?;
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn every_listed_net_file_resolves_to_its_kind() {
        for (name, kind) in NET_FILES {
            assert_eq!(NetFileKind::from_name(name), Some(kind));
            assert_eq!(kind.name(), name);
        }
        assert_eq!(NetFileKind::from_name("socket"), None);
    }

    #[ktest]
    fn inet_files_carry_their_protocol() {
        assert_eq!(NetFileKind::Tcp.inet_protocol(), Some(InetProtocol::Tcp));
        assert_eq!(NetFileKind::Udp6.inet_protocol(), Some(InetProtocol::Udp));
        assert_eq!(NetFileKind::Unix.inet_protocol(), None);
    }
}
