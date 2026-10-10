// SPDX-License-Identifier: MPL-2.0

use core::{net::Ipv4Addr, slice::Iter, str::FromStr};

use aster_bigtcp::{
    device::{AnyNetworkDevice, WithDevice},
    iface::{InterfaceFlags, InterfaceName, InterfaceType},
    wire::{Ipv4Address, Ipv4Cidr},
};
use aster_softirq::BottomHalfDisabled;
use spin::Once;

use super::{Iface, poll::poll_ifaces};
use crate::{net::iface::sched::PollScheduler, prelude::*};

static IFACES: Once<Vec<Arc<Iface>>> = Once::new();
static NETWORK_PROFILES: Once<Vec<BootNetworkProfile>> = Once::new();

aster_cmdline::define_repeatable_kv_param!("asterinas.net", NETWORK_PROFILES);

#[derive(Clone, Debug, Eq, PartialEq)]
struct BootNetworkProfile {
    device_key: String,
    address: Ipv4Cidr,
    gateway: Ipv4Address,
}

impl FromStr for BootNetworkProfile {
    type Err = &'static str;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let mut fields = value.split(',');
        let device_key = fields.next().ok_or("missing device key")?;
        let cidr = fields.next().ok_or("missing IPv4 address")?;
        let gateway = fields.next().ok_or("missing gateway")?;
        if fields.next().is_some()
            || device_key.is_empty()
            || !device_key
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        {
            return Err("invalid network profile fields");
        }

        let (address, prefix) = cidr.split_once('/').ok_or("invalid IPv4 CIDR")?;
        let address = address
            .parse::<Ipv4Addr>()
            .map_err(|_| "invalid IPv4 address")?;
        let prefix = prefix.parse::<u8>().map_err(|_| "invalid prefix")?;
        if prefix > 32 {
            return Err("invalid prefix");
        }
        let gateway = gateway.parse::<Ipv4Addr>().map_err(|_| "invalid gateway")?;
        let [a, b, c, d] = address.octets();
        let [ga, gb, gc, gd] = gateway.octets();
        let address = Ipv4Cidr::new(Ipv4Address::new(a, b, c, d), prefix);
        let gateway = Ipv4Address::new(ga, gb, gc, gd);
        if !address.contains_addr(&gateway)
            || gateway == address.address()
            || gateway == address.network().address()
            || address.broadcast() == Some(gateway)
        {
            return Err("gateway is not usable on the configured network");
        }

        Ok(Self {
            device_key: device_key.into(),
            address,
            gateway,
        })
    }
}

fn virtio_iface() -> Option<&'static Arc<Iface>> {
    IFACES.get().unwrap().get(1)
}

pub(in crate::net) fn iter_all_ifaces() -> Iter<'static, Arc<Iface>> {
    IFACES.get().unwrap().iter()
}

// TODO: Support multiple network devices and avoid the hardcoded device name.
const VIRTIO_DEVICE_NAME: &str = aster_virtio::device::network::DEVICE_NAME;

pub(in crate::net) fn init() {
    IFACES.call_once(|| {
        let mut ifaces = Vec::with_capacity(2);

        // Initialize loopback before virtio
        // to ensure the loopback interface index is ahead of virtio.
        ifaces.push(new_loopback());

        if let Some(iface_virtio) = new_virtio() {
            ifaces.push(iface_virtio);
        }

        ifaces
    });

    if let Some(iface_virtio) = virtio_iface() {
        let callback = || iface_virtio.poll();
        aster_network::register_recv_callback(VIRTIO_DEVICE_NAME, callback);
        aster_network::register_send_callback(VIRTIO_DEVICE_NAME, callback);
    }

    poll_ifaces();
}

fn new_loopback() -> Arc<Iface> {
    use aster_bigtcp::{
        device::Loopback,
        iface::IpIface,
        wire::{Ipv6Address, Ipv6Cidr},
    };

    const LOOPBACK_ADDRESS: Ipv4Address = Ipv4Address::new(127, 0, 0, 1);
    const LOOPBACK_ADDRESS_PREFIX_LEN: u8 = 8; // mask: 255.0.0.0
    const LOOPBACK_IPV6_ADDRESS: Ipv6Address = Ipv6Address::new(0, 0, 0, 0, 0, 0, 0, 1);
    const LOOPBACK_IPV6_PREFIX_LEN: u8 = 128;

    struct Wrapper(Mutex<Loopback>);

    impl WithDevice for Wrapper {
        type Device = Loopback;

        fn with<F, R>(&self, f: F) -> R
        where
            F: FnOnce(&mut dyn AnyNetworkDevice) -> R,
        {
            let mut device = self.0.lock();
            f(&mut *device)
        }
    }

    // FIXME: These flags are currently hardcoded.
    // In the future, we should set appropriate values.
    let flags = InterfaceFlags::UP
        | InterfaceFlags::LOOPBACK
        | InterfaceFlags::RUNNING
        | InterfaceFlags::LOWER_UP;

    IpIface::new(
        Wrapper(Mutex::new(Loopback::new())),
        Ipv4Cidr::new(LOOPBACK_ADDRESS, LOOPBACK_ADDRESS_PREFIX_LEN),
        Some(Ipv6Cidr::new(
            LOOPBACK_IPV6_ADDRESS,
            LOOPBACK_IPV6_PREFIX_LEN,
        )),
        InterfaceName::from_str_truncated("lo"),
        PollScheduler::new(),
        InterfaceType::LOOPBACK,
        flags,
    ) as Arc<Iface>
}

fn new_virtio() -> Option<Arc<Iface>> {
    use aster_bigtcp::iface::EtherIface;

    const VIRTIO_ADDRESS: Ipv4Address = Ipv4Address::new(10, 0, 2, 15);
    const VIRTIO_ADDRESS_PREFIX_LEN: u8 = 24; // mask: 255.255.255.0
    const VIRTIO_GATEWAY: Ipv4Address = Ipv4Address::new(10, 0, 2, 2);

    let virtio_net = aster_network::get_device(VIRTIO_DEVICE_NAME)?;
    let profile = NETWORK_PROFILES.get().and_then(|profiles| {
        profiles
            .iter()
            .find(|profile| profile.device_key == VIRTIO_DEVICE_NAME)
    });
    let (ip_cidr, gateway) = profile
        .map(|profile| (profile.address, profile.gateway))
        .unwrap_or((
            Ipv4Cidr::new(VIRTIO_ADDRESS, VIRTIO_ADDRESS_PREFIX_LEN),
            VIRTIO_GATEWAY,
        ));

    let ether_addr = virtio_net.lock().mac_addr();

    struct Wrapper(Arc<SpinLock<dyn AnyNetworkDevice, BottomHalfDisabled>>);

    impl WithDevice for Wrapper {
        type Device = aster_virtio::device::network::device::NetworkDevice;

        fn with<F, R>(&self, f: F) -> R
        where
            F: FnOnce(&mut dyn AnyNetworkDevice) -> R,
        {
            let mut device = self.0.lock();
            f(&mut *device)
        }
    }

    // FIXME: These flags are currently hardcoded.
    // In the future, we should set appropriate values.
    let flags = InterfaceFlags::UP
        | InterfaceFlags::BROADCAST
        | InterfaceFlags::RUNNING
        | InterfaceFlags::MULTICAST
        | InterfaceFlags::LOWER_UP;

    Some(EtherIface::new(
        Wrapper(virtio_net),
        ether_addr,
        ip_cidr,
        gateway,
        InterfaceName::from_str_truncated("eth0"),
        PollScheduler::new(),
        flags,
    ))
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn explicit_virtio_profile_is_parsed() {
        let profile = BootNetworkProfile::from_str("Virtio-Net,10.0.2.15/24,10.0.2.2").unwrap();
        assert_eq!(profile.device_key, "Virtio-Net");
        assert_eq!(profile.address.to_string(), "10.0.2.15/24");
        assert_eq!(profile.gateway.to_string(), "10.0.2.2");
    }

    #[ktest]
    fn invalid_network_profile_is_rejected() {
        for value in [
            "",
            "Virtio-Net",
            "Virtio-Net,10.0.2.15",
            "Virtio-Net,10.0.2.15/33,10.0.2.2",
            "Virtio-Net,10.0.2.15/24,10.0.3.2",
            "Virtio-Net,10.0.2.15/24,10.0.2.15",
            "bad/key,10.0.2.15/24,10.0.2.2",
            "Virtio-Net,10.0.2.15/24,10.0.2.2,extra",
        ] {
            assert!(BootNetworkProfile::from_str(value).is_err(), "{value}");
        }
    }
}
