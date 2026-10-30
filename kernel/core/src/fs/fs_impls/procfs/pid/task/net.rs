// SPDX-License-Identifier: MPL-2.0

//! Network statistics visible through `/proc/[pid]/net`.

use aster_bigtcp::wire::IpAddress;
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
    prelude::*,
    thread::Thread,
};

/// The `/proc/[pid]/net` directory, also inherited by `/proc/[pid]/task/[tid]`.
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
            "tcp" => Ok(TcpFileOps::new_inode(parent)),
            "udp" => Ok(UdpFileOps::new_inode(parent)),
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
                ListedEntry::new("dev", InodeType::File),
                ListedEntry::new("route", InodeType::File),
                ListedEntry::new("tcp", InodeType::File),
                ListedEntry::new("udp", InodeType::File),
            ],
            visit_fn,
        )
    }
}

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

        for (name, stats) in crate::net::iface_stats() {
            // Only packet and byte counters are tracked today. The other Linux
            // columns remain zero until their corresponding events are tracked.
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
            write!(printer, "{:>6}:", name)?;
            for value in fields {
                write!(printer, " {:>8}", value)?;
            }
            writeln!(printer)?;
        }

        Ok(printer.bytes_written())
    }
}

struct RouteFileOps;

impl RouteFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for RouteFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "Iface\tDestination\tGateway\t\tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"
        )?;

        for route in crate::net::ipv4_route_snapshots() {
            if route.table != 254 {
                continue;
            }
            let name = route.iface.name().as_str().unwrap_or("?");
            let destination = u32::from_le_bytes(route.dst.network().address().octets());
            let gateway = route
                .gateway
                .map(|address| u32::from_le_bytes(address.octets()))
                .unwrap_or(0);
            let mask = if route.dst.prefix_len() == 0 {
                0
            } else {
                u32::MAX << (32 - route.dst.prefix_len())
            };
            let flags = if route.gateway.is_some() {
                0x0003
            } else {
                0x0001
            };
            writeln!(
                printer,
                "{}\t{:08X}\t{:08X}\t{:04X}\t0\t0\t0\t{:08X}\t0\t0\t0",
                name, destination, gateway, flags, mask
            )?;
        }

        Ok(printer.bytes_written())
    }
}

struct TcpFileOps;

impl TcpFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for TcpFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
        )?;
        for (index, socket) in crate::net::tcp_socket_snapshots().iter().enumerate() {
            writeln!(
                printer,
                "{:4}: {:08X}:{:04X} {:08X}:{:04X} {:02X} 00000000:00000000 00:00000000 00000000 0 0 0",
                index,
                ipv4_hex(socket.local_address),
                socket.local_port,
                ipv4_hex(socket.remote_address),
                socket.remote_port,
                socket.state
            )?;
        }
        Ok(printer.bytes_written())
    }
}

struct UdpFileOps;

impl UdpFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for UdpFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
        )?;
        for (index, socket) in crate::net::udp_socket_snapshots().iter().enumerate() {
            writeln!(
                printer,
                "{:4}: {:08X}:{:04X} {:08X}:{:04X} 07 00000000:00000000 00:00000000 00000000 0 0 0",
                index,
                ipv4_hex(socket.local_address),
                socket.local_port,
                ipv4_hex(socket.remote_address),
                socket.remote_port
            )?;
        }
        Ok(printer.bytes_written())
    }
}

fn ipv4_hex(address: IpAddress) -> u32 {
    match address {
        IpAddress::Ipv4(address) => u32::from_le_bytes(address.octets()),
        IpAddress::Ipv6(_) => 0,
    }
}
