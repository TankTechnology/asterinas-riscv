// SPDX-License-Identifier: MPL-2.0

use alloc::format;
use core::{net::Ipv4Addr, slice::Iter, str::FromStr};

use aster_bigtcp::{
    device::WithDevice,
    iface::{InterfaceFlags, InterfaceType},
    wire::{EthernetAddress, Ipv4Address, Ipv4Cidr},
};
use aster_softirq::BottomHalfDisabled;
use spin::Once;

use super::{Iface, poll::poll_ifaces};
use crate::{
    net::iface::{broadcast, sched::PollScheduler},
    prelude::*,
};

static IFACES: Once<Vec<Arc<Iface>>> = Once::new();
static NETWORK_PROFILES: Once<Vec<BootNetworkProfile>> = Once::new();
static NEIGHBOR_PROFILES: Once<Vec<BootNeighborProfile>> = Once::new();

aster_cmdline::define_repeatable_kv_param!("asterinas.net", NETWORK_PROFILES);
aster_cmdline::define_repeatable_kv_param!("asterinas.neighbor", NEIGHBOR_PROFILES);

fn loopback_iface() -> &'static Arc<Iface> {
    &IFACES.get().unwrap()[0]
}

pub fn iter_all_ifaces() -> Iter<'static, Arc<Iface>> {
    IFACES.get().unwrap().iter()
}

const VIRTIO_DEVICE_NAME: &str = aster_virtio::device::network::DEVICE_NAME;

#[derive(Clone, Debug, Eq, PartialEq)]
struct BootNetworkProfile {
    device_key: String,
    address: Ipv4Cidr,
    gateway: Option<Ipv4Address>,
}

impl BootNetworkProfile {
    fn device_key(&self) -> &str {
        &self.device_key
    }

    const fn address(&self) -> Ipv4Cidr {
        self.address
    }

    const fn gateway(&self) -> Option<Ipv4Address> {
        self.gateway
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BootProfileError {
    DuplicateDevice,
    InvalidValue,
    UnknownDevice,
}

impl FromStr for BootNetworkProfile {
    type Err = BootProfileError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let mut fields = value.split(',');
        let key = fields.next().ok_or(BootProfileError::InvalidValue)?;
        let cidr = fields.next().ok_or(BootProfileError::InvalidValue)?;
        let gateway = fields.next();
        if fields.next().is_some() || !is_valid_device_key(key) {
            return Err(BootProfileError::InvalidValue);
        }

        let (address, prefix) = cidr.split_once('/').ok_or(BootProfileError::InvalidValue)?;
        if prefix.contains('/') {
            return Err(BootProfileError::InvalidValue);
        }
        let address = parse_unicast_ipv4(address)?;
        let prefix = prefix
            .parse::<u8>()
            .ok()
            .filter(|prefix| *prefix <= 32)
            .ok_or(BootProfileError::InvalidValue)?;
        let address = Ipv4Cidr::new(address, prefix);
        let gateway = gateway.map(parse_unicast_ipv4).transpose()?;
        if (address.prefix_len() < 31
            && (address.network().address() == address.address()
                || address.broadcast() == Some(address.address())))
            || gateway.is_some_and(|gateway| {
                !address.contains_addr(&gateway)
                    || gateway == address.address()
                    || gateway == address.network().address()
                    || address.broadcast() == Some(gateway)
            })
        {
            return Err(BootProfileError::InvalidValue);
        }

        Ok(Self {
            device_key: key.into(),
            address,
            gateway,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BootNeighborProfile {
    device_key: String,
    address: Ipv4Address,
    hardware_address: [u8; 6],
}

impl BootNeighborProfile {
    fn device_key(&self) -> &str {
        &self.device_key
    }

    const fn address(&self) -> Ipv4Address {
        self.address
    }

    const fn hardware_address(&self) -> [u8; 6] {
        self.hardware_address
    }
}

impl FromStr for BootNeighborProfile {
    type Err = BootProfileError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let mut fields = value.split(',');
        let device_key = fields.next().ok_or(BootProfileError::InvalidValue)?;
        let address = fields.next().ok_or(BootProfileError::InvalidValue)?;
        let hardware_address = fields.next().ok_or(BootProfileError::InvalidValue)?;
        if fields.next().is_some() || !is_valid_device_key(device_key) {
            return Err(BootProfileError::InvalidValue);
        }

        Ok(Self {
            device_key: device_key.into(),
            address: parse_unicast_ipv4(address)?,
            hardware_address: parse_unicast_ethernet(hardware_address)?,
        })
    }
}

fn is_valid_device_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 64
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn parse_unicast_ipv4(value: &str) -> Result<Ipv4Address, BootProfileError> {
    let address = value
        .parse::<Ipv4Addr>()
        .map_err(|_| BootProfileError::InvalidValue)?;
    if address.is_unspecified() || address.is_multicast() || address.is_broadcast() {
        return Err(BootProfileError::InvalidValue);
    }
    let [a, b, c, d] = address.octets();
    Ok(Ipv4Address::new(a, b, c, d))
}

fn parse_unicast_ethernet(value: &str) -> Result<[u8; 6], BootProfileError> {
    let mut fields = value.split(':');
    let mut bytes = [0; 6];
    for byte in &mut bytes {
        let field = fields.next().ok_or(BootProfileError::InvalidValue)?;
        if field.len() != 2 {
            return Err(BootProfileError::InvalidValue);
        }
        *byte = u8::from_str_radix(field, 16).map_err(|_| BootProfileError::InvalidValue)?;
    }
    if fields.next().is_some() || bytes == [0; 6] || bytes[0] & 1 != 0 {
        return Err(BootProfileError::InvalidValue);
    }
    Ok(bytes)
}

fn validate_profiles(
    profiles: &[BootNetworkProfile],
    device_keys: &[&str],
) -> Result<(), BootProfileError> {
    for (index, profile) in profiles.iter().enumerate() {
        if !device_keys.contains(&profile.device_key()) {
            return Err(BootProfileError::UnknownDevice);
        }
        if profiles[..index]
            .iter()
            .any(|other| other.device_key == profile.device_key)
        {
            return Err(BootProfileError::DuplicateDevice);
        }
    }
    Ok(())
}

fn validate_neighbors(
    neighbors: &[BootNeighborProfile],
    profiles: &[BootNetworkProfile],
    device_keys: &[&str],
) -> Result<(), BootProfileError> {
    for (index, neighbor) in neighbors.iter().enumerate() {
        if !device_keys.contains(&neighbor.device_key()) {
            return Err(BootProfileError::UnknownDevice);
        }
        if neighbors[..index].iter().any(|other| {
            other.device_key == neighbor.device_key && other.address == neighbor.address
        }) {
            return Err(BootProfileError::DuplicateDevice);
        }
        let profile = profiles
            .iter()
            .find(|profile| profile.device_key() == neighbor.device_key())
            .ok_or(BootProfileError::InvalidValue)?;
        if profile.gateway() != Some(neighbor.address()) {
            return Err(BootProfileError::InvalidValue);
        }
    }
    Ok(())
}

pub fn init() {
    IFACES.call_once(|| {
        let devices = aster_network::all_devices();
        let profiles = NETWORK_PROFILES.get().map(Vec::as_slice).unwrap_or(&[]);
        let device_keys: Vec<&str> = devices.iter().map(|device| device.key()).collect();
        let profiles = match validate_profiles(profiles, &device_keys) {
            Ok(()) => profiles,
            Err(error) => {
                warn!("ignoring invalid asterinas.net profiles: {:?}", error);
                &[]
            }
        };
        let neighbors = NEIGHBOR_PROFILES.get().map(Vec::as_slice).unwrap_or(&[]);
        let neighbors = match validate_neighbors(neighbors, profiles, &device_keys) {
            Ok(()) => neighbors,
            Err(error) => {
                warn!("ignoring invalid asterinas.neighbor profiles: {:?}", error);
                &[]
            }
        };
        let mut ifaces = Vec::with_capacity(devices.len() + 1);

        // Keep loopback first so physical interface indexes are deterministic.
        ifaces.push(new_loopback(true));

        for (index, registration) in devices.into_iter().enumerate() {
            let profile = profiles
                .iter()
                .find(|profile| profile.device_key() == registration.key());
            let static_neighbors: Vec<_> = neighbors
                .iter()
                .filter(|neighbor| neighbor.device_key() == registration.key())
                .map(|neighbor| {
                    (
                        neighbor.address(),
                        EthernetAddress(neighbor.hardware_address()),
                    )
                })
                .collect();
            let iface = new_ethernet(&registration, index, profile, &static_neighbors);
            let recv_iface = iface.clone();
            aster_network::register_recv_callback(registration.key(), move || recv_iface.poll());
            let send_iface = iface.clone();
            aster_network::register_send_callback(registration.key(), move || send_iface.poll());
            ifaces.push(iface);
        }

        ifaces
    });

    broadcast::init();

    poll_ifaces();
}

/// Creates a loopback interface for a new (non-initial) network namespace.
///
/// The interface starts down, as in Linux; it can be brought up from inside
/// the namespace (e.g., `ip link set lo up`).
pub(in crate::net) fn new_ns_loopback() -> Arc<Iface> {
    new_loopback(false)
}

fn new_loopback(up: bool) -> Arc<Iface> {
    use aster_bigtcp::{
        device::{Loopback, Medium},
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
            F: FnOnce(&mut Self::Device) -> R,
        {
            let mut device = self.0.lock();
            f(&mut device)
        }
    }

    // Loopback interfaces are always `LOOPBACK | LOWER_UP`; `UP | RUNNING`
    // are set only when the interface is administratively up (the initial
    // namespace's loopback starts up; a new namespace's loopback starts
    // down, as in Linux).
    let mut flags = InterfaceFlags::LOOPBACK | InterfaceFlags::LOWER_UP;
    if up {
        flags |= InterfaceFlags::UP | InterfaceFlags::RUNNING;
    }

    IpIface::new(
        Wrapper(Mutex::new(Loopback::new(Medium::Ip))),
        Ipv4Cidr::new(LOOPBACK_ADDRESS, LOOPBACK_ADDRESS_PREFIX_LEN),
        Some(Ipv6Cidr::new(
            LOOPBACK_IPV6_ADDRESS,
            LOOPBACK_IPV6_PREFIX_LEN,
        )),
        CString::new("lo").unwrap(),
        PollScheduler::new(),
        InterfaceType::LOOPBACK,
        flags,
    ) as Arc<Iface>
}

fn new_ethernet(
    registration: &aster_network::RegisteredNetworkDevice,
    index: usize,
    profile: Option<&BootNetworkProfile>,
    static_neighbors: &[(Ipv4Address, EthernetAddress)],
) -> Arc<Iface> {
    use aster_bigtcp::iface::EtherIface;
    use aster_network::AnyNetworkDevice;

    const VIRTIO_ADDRESS: Ipv4Address = Ipv4Address::new(10, 0, 2, 15);
    const VIRTIO_ADDRESS_PREFIX_LEN: u8 = 24; // mask: 255.255.255.0
    const VIRTIO_GATEWAY: Ipv4Address = Ipv4Address::new(10, 0, 2, 2);

    let device = registration.device();
    let ether_addr = device.lock().mac_addr().0;

    struct Wrapper(Arc<SpinLock<dyn AnyNetworkDevice, BottomHalfDisabled>>);

    impl WithDevice for Wrapper {
        type Device = dyn AnyNetworkDevice;

        fn with<F, R>(&self, f: F) -> R
        where
            F: FnOnce(&mut Self::Device) -> R,
        {
            let mut device = self.0.lock();
            f(&mut *device)
        }
    }

    // FIXME: These flags are currently hardcoded.
    // In the future, we should set appropriate values.
    let mut flags = InterfaceFlags::UP | InterfaceFlags::BROADCAST | InterfaceFlags::MULTICAST;
    if registration.is_link_up() {
        flags |= InterfaceFlags::RUNNING | InterfaceFlags::LOWER_UP;
    }

    let (ip_cidr, gateway) = match profile {
        Some(profile) => (Some(profile.address()), profile.gateway()),
        None if registration.key() == VIRTIO_DEVICE_NAME => (
            Some(Ipv4Cidr::new(VIRTIO_ADDRESS, VIRTIO_ADDRESS_PREFIX_LEN)),
            Some(VIRTIO_GATEWAY),
        ),
        None => (None, None),
    };

    EtherIface::new(
        Wrapper(device),
        EthernetAddress(ether_addr),
        ip_cidr,
        gateway,
        static_neighbors,
        CString::new(format!("eth{}", index)).unwrap(),
        PollScheduler::new(),
        flags,
    )
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn megrez_profile_has_static_address_and_no_fabricated_gateway() {
        let profile = BootNetworkProfile::from_str("eic7700-rj45,10.100.19.200/21").unwrap();

        assert_eq!(profile.device_key(), "eic7700-rj45");
        assert_eq!(profile.address().to_string(), "10.100.19.200/21");
        assert_eq!(profile.gateway(), None);
    }

    #[ktest]
    fn explicit_virtio_profile_preserves_an_optional_gateway() {
        let profile = BootNetworkProfile::from_str("Virtio-Net,10.0.2.15/24,10.0.2.2").unwrap();

        assert_eq!(profile.device_key(), "Virtio-Net");
        assert_eq!(profile.address().to_string(), "10.0.2.15/24");
        assert_eq!(profile.gateway().unwrap().to_string(), "10.0.2.2");

        let without_gateway = BootNetworkProfile::from_str("Virtio-Net,10.0.2.15/24").unwrap();
        assert_eq!(without_gateway.gateway(), None);
    }

    #[ktest]
    fn static_neighbor_must_match_the_configured_gateway() {
        let profile =
            BootNetworkProfile::from_str("eic7700-rj45,10.100.19.200/21,10.100.16.1").unwrap();
        let neighbor =
            BootNeighborProfile::from_str("eic7700-rj45,10.100.16.1,4c:d6:29:18:93:43").unwrap();

        assert_eq!(neighbor.device_key(), "eic7700-rj45");
        assert_eq!(neighbor.address().to_string(), "10.100.16.1");
        assert_eq!(
            neighbor.hardware_address(),
            [0x4c, 0xd6, 0x29, 0x18, 0x93, 0x43]
        );
        assert_eq!(
            validate_neighbors(&[neighbor], &[profile], &["eic7700-rj45"]),
            Ok(())
        );
    }

    #[ktest]
    fn static_neighbor_rejects_unsafe_or_unbound_entries() {
        for value in [
            "eic7700-rj45,10.100.16.1",
            "eic7700-rj45,10.100.16.1,ff:ff:ff:ff:ff:ff",
            "eic7700-rj45,10.100.16.1,01:00:5e:00:00:01",
            "eic7700-rj45,10.100.16.1,4c:d6:29:18:93",
            "eic7700-rj45,10.100.16.1,4c:d6:29:18:93:xx",
        ] {
            assert!(BootNeighborProfile::from_str(value).is_err(), "{value}");
        }

        let profile =
            BootNetworkProfile::from_str("eic7700-rj45,10.100.19.200/21,10.100.16.1").unwrap();
        let wrong_gateway =
            BootNeighborProfile::from_str("eic7700-rj45,10.100.16.2,4c:d6:29:18:93:43").unwrap();
        assert_eq!(
            validate_neighbors(&[wrong_gateway], &[profile.clone()], &["eic7700-rj45"]),
            Err(BootProfileError::InvalidValue)
        );
        let duplicate =
            BootNeighborProfile::from_str("eic7700-rj45,10.100.16.1,4c:d6:29:18:93:43").unwrap();
        assert_eq!(
            validate_neighbors(
                &[duplicate.clone(), duplicate],
                &[profile],
                &["eic7700-rj45"]
            ),
            Err(BootProfileError::DuplicateDevice)
        );
    }

    #[ktest]
    fn profile_parser_rejects_ambiguous_or_invalid_values() {
        for value in [
            "",
            "eic7700-rj45",
            "eic7700-rj45,10.100.19.200",
            "eic7700-rj45,10.100.19.200/33",
            "eic7700-rj45,224.0.0.1/21",
            "eic7700-rj45,10.100.16.0/21",
            "eic7700-rj45,10.100.23.255/21",
            "eic7700-rj45,10.100.19.200/21,10.100.19.200",
            "eic7700-rj45,10.100.19.200/21,10.100.16.0",
            "eic7700-rj45,10.100.19.200/21,10.100.23.255",
            "bad,key,10.100.19.200/21",
            "eic7700-rj45,10.100.19.200/21,10.0.2.2,extra",
        ] {
            assert_eq!(
                BootNetworkProfile::from_str(value),
                Err(BootProfileError::InvalidValue)
            );
        }
    }

    #[ktest]
    fn profile_set_rejects_duplicate_and_unknown_device_keys() {
        let megrez = BootNetworkProfile::from_str("eic7700-rj45,10.100.19.200/21").unwrap();
        let duplicate = megrez.clone();
        assert_eq!(
            validate_profiles(&[megrez.clone(), duplicate], &["eic7700-rj45"]),
            Err(BootProfileError::DuplicateDevice)
        );
        assert_eq!(
            validate_profiles(&[megrez], &["Virtio-Net"]),
            Err(BootProfileError::UnknownDevice)
        );
    }
}
