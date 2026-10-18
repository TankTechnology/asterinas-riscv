// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/<pid>/net` file support, which exposes the
//! network statistics visible to one task.
//!
//! This kernel has a single network namespace, so every task sees the same
//! interfaces; the directory exists because standard tools read
//! `/proc/net/dev`, and `/proc/net` is a link to `self/net`.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_pid_net.5.html>

use core::fmt::Write as _;

use aster_bigtcp::wire::{EthernetAddress, Ipv4Address};
use aster_util::printer::VmPrinter;

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
    net::{arp_entries, iface_statuses, ipv4_routes},
    prelude::*,
    thread::Thread,
};

/// The Linux numeric ID of the main routing table.
const MAIN_TABLE: u32 = 254;

/// Represents the inode at `/proc/<pid>/net`.
pub(super) struct NetDirOps(TidDirOps);

impl NetDirOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self(dir.clone()), parent, mkmod!(a+rx))
    }
}

impl ProcDirOps for NetDirOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        let parent = this_dir.this_weak().clone();
        match name {
            "dev" => Ok(DevFileOps::new_inode(parent)),
            "route" => Ok(RouteFileOps::new_inode(parent)),
            "arp" => Ok(ArpFileOps::new_inode(parent)),
            _ => return_errno_with_message!(Errno::ENOENT, "the file does not exist"),
        }
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        visit_listed_entries(
            offset,
            [
                ListedEntry::new("arp", InodeType::File),
                ListedEntry::new("dev", InodeType::File),
                ListedEntry::new("route", InodeType::File),
            ],
            visit_fn,
        )
    }
}

/// Represents the inode at `/proc/<pid>/net/dev`.
struct DevFileOps;

impl DevFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for DevFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "Inter-|   Receive                                                |  Transmit"
        )?;
        writeln!(
            printer,
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed"
        )?;

        for status in iface_statuses() {
            // Only byte and packet counters are instrumented today. The other
            // Linux columns remain zero until their events are tracked.
            let fields = [
                status.stats.rx_bytes,
                status.stats.rx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
                status.stats.tx_bytes,
                status.stats.tx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
            ];
            write!(printer, "{:>6}:", status.name)?;
            for value in fields {
                write!(printer, " {:>8}", value)?;
            }
            writeln!(printer)?;
        }

        Ok(printer.bytes_written())
    }
}

/// Represents the inode at `/proc/<pid>/net/route`.
struct RouteFileOps;

impl RouteFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

/// Formats an IPv4 address the way net-tools' `/proc/net/route` parser expects.
///
/// The file carries the address as the native-order `u32` rendered in hex, not
/// dotted quad. On a little-endian machine 10.0.2.0 is therefore `0002000A`.
fn route_addr(addr: Ipv4Address) -> u32 {
    u32::from_ne_bytes(addr.octets())
}

impl ProcFileOps for RouteFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"
        )?;

        for route in ipv4_routes() {
            // `/proc/net/route` only ever carried the main table; the local
            // table is what `ip route show table local` reads over netlink.
            if route.table_id != MAIN_TABLE {
                continue;
            }

            let prefix = route.dst.prefix_len();
            let mask = if prefix == 0 {
                0u32
            } else {
                u32::MAX << (32 - prefix)
            };
            let mask = u32::from_ne_bytes(mask.to_be_bytes());

            // RTF_UP, plus RTF_GATEWAY when the route has a next hop.
            let flags = if route.gateway.is_some() {
                0x0003
            } else {
                0x0001
            };

            writeln!(
                printer,
                "{}\t{:08X}\t{:08X}\t{:04X}\t0\t0\t{}\t{:08X}\t0\t0\t0",
                route.iface,
                route_addr(route.dst.network().address()),
                route_addr(route.gateway.unwrap_or(Ipv4Address::UNSPECIFIED)),
                flags,
                route.metric,
                mask,
            )?;
        }

        Ok(printer.bytes_written())
    }
}

/// Represents the inode at `/proc/<pid>/net/arp`.
struct ArpFileOps;

impl ArpFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

/// Renders an Ethernet address the way net-tools expects.
fn format_mac(mac: &EthernetAddress) -> String {
    let mut text = String::new();
    for (index, byte) in mac.0.iter().enumerate() {
        if index != 0 {
            text.push(':');
        }
        let _ = write!(text, "{:02x}", byte);
    }
    text
}

impl ProcFileOps for ArpFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "IP address       HW type     Flags       HW address            Mask     Device"
        )?;

        for entry in arp_entries() {
            // ATF_COM: the entry is complete, which is all this cache holds.
            writeln!(
                printer,
                "{:<15}  0x1         0x2         {:<21} *        {}",
                entry.ip,
                format_mac(&entry.mac),
                entry.iface,
            )?;
        }

        Ok(printer.bytes_written())
    }
}
