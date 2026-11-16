// SPDX-License-Identifier: MPL-2.0

//! The `net` class: the `/sys/class/net` view of every network interface.
//!
//! `ip`, `udev`, `systemd-networkd` and network managers read an interface's
//! hardware address, MTU and flags from here rather than from `/proc/net`,
//! and they refuse to act on an interface they cannot find. The attributes are
//! the ones those tools open: `address`, `addr_len`, `broadcast`, `ifindex`,
//! `flags`, `mtu`, `operstate`, `type`, `speed` and `duplex`.
//!
//! The interface is created by the network stack, so this module only builds
//! the class view of interfaces that already exist. `statistics/` is not
//! published yet; `IfaceStatsSnapshot` has the counters, but the class API
//! exposes flat attributes only.

use core::fmt::Write;

use aster_bigtcp::{iface::Iface as BigtcpIface, wire::EthernetAddress};
use aster_device::{
    class::{self, Class, ClassDevice, ClassHandle},
    common::{Attr, SysStr},
};

use crate::{
    net::iface::{ext::BigtcpExt, iter_all_ifaces},
    prelude::*,
};

/// A network interface as the class sees it.
type Iface = dyn BigtcpIface<BigtcpExt>;

/// The `net` class.
pub(super) struct NetClass;

impl Class for NetClass {
    const NAME: &'static str = "net";
    type Device = Arc<Iface>;

    fn dev_attrs(&self) -> &'static [Attr<ClassDevice<Self>>] {
        ATTRS
    }
}

type NetDevice = ClassDevice<NetClass>;

const ATTRS: &[Attr<NetDevice>] = &[
    Attr::ro("address", |dev, writer| {
        let iface = dev.payload();
        // A non-Ethernet interface (loopback) reports an all-zero address.
        let address = iface.ethernet_addr().unwrap_or(EthernetAddress([0; 6]));
        write_mac(writer, &address)?;
        Ok(())
    }),
    Attr::ro("addr_len", |dev, writer| {
        let len = if dev.payload().ethernet_addr().is_some() {
            6
        } else {
            0
        };
        writeln!(writer, "{}", len)?;
        Ok(())
    }),
    Attr::ro("broadcast", |_dev, writer| {
        write_mac(writer, &EthernetAddress::BROADCAST)?;
        Ok(())
    }),
    Attr::ro("ifindex", |dev, writer| {
        writeln!(writer, "{}", dev.payload().index())?;
        Ok(())
    }),
    Attr::ro("mtu", |dev, writer| {
        writeln!(writer, "{}", dev.payload().mtu())?;
        Ok(())
    }),
    Attr::ro("flags", |dev, writer| {
        writeln!(writer, "0x{:x}", dev.payload().flags().bits())?;
        Ok(())
    }),
    Attr::ro("operstate", |dev, writer| {
        use aster_bigtcp::iface::InterfaceFlags;
        let flags = dev.payload().flags();
        let state = if flags.contains(InterfaceFlags::UP) && flags.contains(InterfaceFlags::RUNNING)
        {
            "up"
        } else if flags.contains(InterfaceFlags::UP) {
            "unknown"
        } else {
            "down"
        };
        writeln!(writer, "{}", state)?;
        Ok(())
    }),
    Attr::ro("type", |dev, writer| {
        // ARPHRD_ETHER = 1, ARPHRD_LOOPBACK = 772.
        let type_ = if dev.payload().ethernet_addr().is_some() {
            1
        } else {
            772
        };
        writeln!(writer, "{}", type_)?;
        Ok(())
    }),
    Attr::ro("speed", |dev, writer| {
        // Only Ethernet interfaces report a link speed; it is unknown to the
        // software stack, so report the conventional "unknown" (-1) value.
        let speed: i32 = if dev.payload().ethernet_addr().is_some() {
            -1
        } else {
            0
        };
        writeln!(writer, "{}", speed)?;
        Ok(())
    }),
    Attr::ro("duplex", |_dev, writer| {
        writeln!(writer, "unknown")?;
        Ok(())
    }),
];

fn write_mac(writer: &mut dyn Write, address: &EthernetAddress) -> core::fmt::Result {
    let [a, b, c, d, e, f] = address.0;
    writeln!(writer, "{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}", a, b, c, d, e, f)
}

static NET_CLASS: spin::Once<Arc<ClassHandle<NetClass>>> = spin::Once::new();

/// Publishes every existing interface under `/sys/class/net`.
pub(super) fn init_in_first_kthread() {
    let class = NET_CLASS.call_once(|| class::register(NetClass).expect("failed to register the net class"));
    for iface in iter_all_ifaces() {
        let name = iface.name().as_cstr().to_str().unwrap_or("eth?");
        let device = NetDevice::builder(class, SysStr::from(name), iface.clone()).build();
        if let Err(error) = aster_device::add_device(&device) {
            warn!("failed to register /sys/class/net/{}: {:?}", name, error);
        }
    }
}
