// SPDX-License-Identifier: MPL-2.0

//! Legacy socket ioctls for network interface discovery.

use ostd::mm::VmIo;

use crate::{
    context::current_userspace,
    net::net_ns::current_net_ns,
    prelude::*,
    util::{
        ioctl::{InOutData, RawIoctl, ioc},
        net::CSocketAddrFamily,
    },
};

const IFNAMSIZ: usize = 16;
const SIOCGIFFLAGS: u32 = 0x8913;
const SIOCGIFADDR: u32 = 0x8915;
const SIOCGIFBRDADDR: u32 = 0x8919;
const SIOCGIFNETMASK: u32 = 0x891b;
const SIOCGIFMTU: u32 = 0x8921;
const SIOCGIFHWADDR: u32 = 0x8927;
const SIOCGIFINDEX: u32 = 0x8933;

/// The native 64-bit Linux `struct ifconf`.
#[repr(C)]
#[derive(Clone, Copy, Pod)]
struct CIfConf {
    len: i32,
    _padding: u32,
    buf: usize,
}

/// The native 64-bit Linux `struct ifreq` with a `sockaddr_in` in its union.
#[repr(C)]
#[derive(Clone, Copy, Pod)]
struct CIfReq {
    name: [u8; IFNAMSIZ],
    data: [u8; 24],
}

const _: () = assert!(size_of::<CIfConf>() == 16);
const _: () = assert!(size_of::<CIfReq>() == 40);

type GetIfConf = ioc!(SIOCGIFCONF, 0x8912, InOutData<CIfConf>);

pub(super) fn handle(raw_ioctl: RawIoctl) -> Result<i32> {
    if let Some(cmd) = GetIfConf::try_from_raw(raw_ioctl) {
        return get_ifconf(raw_ioctl, cmd);
    }

    match raw_ioctl.cmd() {
        SIOCGIFFLAGS | SIOCGIFADDR | SIOCGIFBRDADDR | SIOCGIFNETMASK | SIOCGIFMTU
        | SIOCGIFHWADDR | SIOCGIFINDEX => get_ifreq(raw_ioctl),
        _ => return_errno_with_message!(Errno::ENOTTY, "socket ioctl is not supported"),
    }
}

fn get_ifreq(raw_ioctl: RawIoctl) -> Result<i32> {
    let mut request: CIfReq = current_userspace!().read_val(raw_ioctl.arg())?;
    let name_len = request
        .name
        .iter()
        .position(|&byte| byte == 0)
        .unwrap_or(IFNAMSIZ);
    let net_ns = current_net_ns();
    let iface = net_ns
        .ifaces()
        .iter()
        .find(|iface| iface.name().to_bytes() == &request.name[..name_len])
        .ok_or_else(|| Error::with_message(Errno::ENODEV, "network interface not found"))?;

    match raw_ioctl.cmd() {
        SIOCGIFFLAGS => {
            // SIOCGIFFLAGS exposes the 16-bit Linux ifreq flags field.
            request.data[..2].copy_from_slice(&(iface.flags().bits() as u16).to_ne_bytes());
        }
        SIOCGIFADDR => {
            let cidr = iface.ipv4_cidr().ok_or_else(|| {
                Error::with_message(Errno::EADDRNOTAVAIL, "interface has no IPv4 address")
            })?;
            set_ipv4_addr(&mut request, cidr.address().octets());
        }
        SIOCGIFBRDADDR => {
            let address = iface
                .broadcast_addr()
                .map(|addr| addr.octets())
                .unwrap_or([0; 4]);
            set_ipv4_addr(&mut request, address);
        }
        SIOCGIFNETMASK => {
            let cidr = iface.ipv4_cidr().ok_or_else(|| {
                Error::with_message(Errno::EADDRNOTAVAIL, "interface has no IPv4 address")
            })?;
            let mask = u32::MAX
                .checked_shl(u32::from(32 - cidr.prefix_len()))
                .unwrap_or(0);
            set_ipv4_addr(&mut request, mask.to_be_bytes());
        }
        SIOCGIFMTU => {
            let mtu = i32::try_from(iface.mtu())
                .map_err(|_| Error::with_message(Errno::EINVAL, "interface MTU is too large"))?;
            request.data[..4].copy_from_slice(&mtu.to_ne_bytes());
        }
        SIOCGIFHWADDR => {
            request.data[..16].fill(0);
            request.data[..2].copy_from_slice(&(iface.type_() as u16).to_ne_bytes());
            if let Some(address) = iface.ethernet_addr() {
                request.data[2..8].copy_from_slice(&address.0);
            }
        }
        SIOCGIFINDEX => {
            let index = i32::try_from(iface.index())
                .map_err(|_| Error::with_message(Errno::EINVAL, "interface index is too large"))?;
            request.data[..4].copy_from_slice(&index.to_ne_bytes());
        }
        _ => unreachable!(),
    }

    current_userspace!().write_val(raw_ioctl.arg(), &request)?;
    Ok(0)
}

fn set_ipv4_addr(request: &mut CIfReq, address: [u8; 4]) {
    request.data[..16].fill(0);
    request.data[..2].copy_from_slice(&(CSocketAddrFamily::AF_INET as u16).to_ne_bytes());
    request.data[4..8].copy_from_slice(&address);
}

fn get_ifconf(raw_ioctl: RawIoctl, cmd: GetIfConf) -> Result<i32> {
    // The nested buffer belongs to the caller; only complete ifreq entries are copied.
    let config = cmd.read()?;
    let net_ns = current_net_ns();
    let ifaces = net_ns
        .ifaces()
        .iter()
        .filter_map(|iface| iface.ipv4_cidr().map(|cidr| (iface, cidr)));

    let mut written = 0usize;
    for (iface, cidr) in ifaces {
        if config.buf != 0 {
            let Some(next) = written.checked_add(size_of::<CIfReq>()) else {
                return_errno_with_message!(Errno::EINVAL, "too many interfaces");
            };
            if next > config.len.max(0) as usize {
                break;
            }
            let mut request = CIfReq {
                name: [0; IFNAMSIZ],
                data: [0; 24],
            };
            let name = iface.name().to_bytes();
            let len = name.len().min(IFNAMSIZ - 1);
            request.name[..len].copy_from_slice(&name[..len]);
            set_ipv4_addr(&mut request, cidr.address().octets());
            let addr = config.buf.checked_add(written).ok_or_else(|| {
                Error::with_message(Errno::EFAULT, "the interface buffer address overflowed")
            })?;
            current_userspace!().write_bytes(addr, request.as_bytes())?;
        }
        written = written
            .checked_add(size_of::<CIfReq>())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "too many interfaces"))?;
    }
    let length = i32::try_from(written)
        .map_err(|_| Error::with_message(Errno::EINVAL, "too many interfaces"))?;
    current_userspace!().write_val(raw_ioctl.arg(), &length)?;
    Ok(0)
}
