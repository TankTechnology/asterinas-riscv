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
    let Some(cmd) = GetIfConf::try_from_raw(raw_ioctl) else {
        return_errno_with_message!(Errno::ENOTTY, "socket ioctl is not supported");
    };
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
            request.data[..2].copy_from_slice(&(CSocketAddrFamily::AF_INET as u16).to_ne_bytes());
            request.data[4..8].copy_from_slice(&cidr.address().octets());
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
