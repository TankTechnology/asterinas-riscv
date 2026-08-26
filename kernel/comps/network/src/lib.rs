// SPDX-License-Identifier: MPL-2.0

#![no_std]
#![deny(unsafe_code)]
#![feature(trait_alias)]

mod buffer;
pub mod dma_pool;
mod driver;

extern crate alloc;
#[macro_use]
extern crate ostd_pod;

use alloc::{collections::BTreeMap, string::String, sync::Arc, vec::Vec};
use core::{any::Any, fmt::Debug};

use aster_bigtcp::device::DeviceCapabilities;
use aster_softirq::{
    BottomHalfDisabled, SoftIrqLine,
    softirq_id::{NETWORK_RX_SOFTIRQ_ID, NETWORK_TX_SOFTIRQ_ID},
};
pub use buffer::{RxBuffer, TxBuffer, TxBufferBuilder};
use component::{ComponentInitError, init_component};
use ostd::sync::SpinLock;
use spin::Once;

#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct EthernetAddr(pub [u8; 6]);

#[derive(Clone, Copy, Debug)]
pub enum NetError {
    NotReady,
    Busy,
    NoMemory,
}

pub trait AnyNetworkDevice: Send + Sync + Any + Debug {
    // ================Device Information=================

    fn mac_addr(&self) -> EthernetAddr;
    fn capabilities(&self) -> DeviceCapabilities;

    // ================Device Operation===================

    fn can_receive(&self) -> bool;
    fn can_send(&self) -> bool;

    /// Receives a packet from network. If packet is ready, returns a `RxBuffer` containing the packet.
    /// Otherwise, return [`NetError::NotReady`].
    fn receive(&mut self) -> Result<RxBuffer, NetError>;

    /// Sends a packet to network.
    fn send(&mut self, packet: &[u8]) -> Result<(), NetError>;

    /// Frees processes tx buffers.
    fn free_processed_tx_buffers(&mut self);

    /// Notifies the device driver that a polling operation has ended.
    ///
    /// The driver can assume that the device remains protected by acquiring a poll lock
    /// for the entire duration of the polling process.
    /// Thus two polling process cannot happen simultaneously.
    fn notify_poll_end(&mut self);
}

pub trait NetDeviceCallback = Fn() + Send + Sync + 'static;

pub fn register_device(
    name: String,
    is_link_up: bool,
    device: Arc<SpinLock<dyn AnyNetworkDevice, BottomHalfDisabled>>,
) -> Result<(), RegisterDeviceError> {
    COMPONENT
        .get()
        .unwrap()
        .network_device_table
        .lock()
        .register(name, is_link_up, device)
}

pub fn get_device(str: &str) -> Option<Arc<SpinLock<dyn AnyNetworkDevice, BottomHalfDisabled>>> {
    let table = COMPONENT.get().unwrap().network_device_table.lock();
    let callbacks = table.entries.get(str)?;
    Some(callbacks.device.clone())
}

/// Registers callback which will be called when receiving message.
///
/// Since the callback will be called in softirq context,
/// the callback function should _not_ sleep.
pub fn register_recv_callback(name: &str, callback: impl NetDeviceCallback) {
    let device_table = COMPONENT.get().unwrap().network_device_table.lock();
    let Some(callbacks) = device_table.entries.get(name) else {
        return;
    };
    callbacks.recv_callbacks.lock().push(Arc::new(callback));
}

/// Registers a callback that will be invoked
/// when the device has completed sending a packet.
///
/// Since this callback is executed in a softirq context,
/// the callback function should _not_ block or sleep.
///
/// Please note that the callback may not be called every time a packet is sent.
/// The driver may skip certain callbacks for performance optimization.
pub fn register_send_callback(name: &str, callback: impl NetDeviceCallback) {
    let device_table = COMPONENT.get().unwrap().network_device_table.lock();
    let Some(callbacks) = device_table.entries.get(name) else {
        return;
    };
    callbacks.send_callbacks.lock().push(Arc::new(callback));
}

fn handle_rx_softirq() {
    let device_table = COMPONENT.get().unwrap().network_device_table.lock();
    // TODO: We should handle network events for just one device per softirq,
    // rather than processing events for all devices.
    // This issue should be addressed once new network devices are added.
    for callback_set in device_table.entries.values() {
        let recv_callbacks = callback_set.recv_callbacks.lock();
        for callback in recv_callbacks.iter() {
            callback();
        }
    }
}

fn handle_tx_softirq() {
    let device_table = COMPONENT.get().unwrap().network_device_table.lock();
    // TODO: We should handle network events for just one device per softirq,
    // rather than processing events for all devices.
    // This issue should be addressed once new network devices are added.
    for callback_set in device_table.entries.values() {
        let can_send = {
            let mut device = callback_set.device.lock();
            device.free_processed_tx_buffers();
            device.can_send()
        };

        if !can_send {
            continue;
        }

        let send_callbacks = callback_set.send_callbacks.lock();
        for callback in send_callbacks.iter() {
            callback();
        }
    }
}

/// Raises softirq for handling transmission events
pub fn raise_send_softirq() {
    SoftIrqLine::get(NETWORK_TX_SOFTIRQ_ID).raise();
}

/// Raises softirq for handling reception events
pub fn raise_receive_softirq() {
    SoftIrqLine::get(NETWORK_RX_SOFTIRQ_ID).raise();
}

pub fn all_devices() -> Vec<RegisteredNetworkDevice> {
    let network_devs = COMPONENT.get().unwrap().network_device_table.lock();
    network_devs.snapshot()
}

/// One immutable snapshot of a registered Ethernet device.
#[derive(Clone)]
pub struct RegisteredNetworkDevice {
    key: String,
    is_link_up: bool,
    device: NetworkDeviceRef,
}

impl RegisteredNetworkDevice {
    pub fn key(&self) -> &str {
        &self.key
    }

    pub const fn is_link_up(&self) -> bool {
        self.is_link_up
    }

    pub fn device(&self) -> NetworkDeviceRef {
        self.device.clone()
    }
}

/// A rejected stable network-device registration key.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RegisterDeviceError {
    DuplicateKey,
    InvalidKey,
}

static COMPONENT: Once<Component> = Once::new();

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    let component = Component::init()?;
    COMPONENT.call_once(|| component);

    SoftIrqLine::get(NETWORK_TX_SOFTIRQ_ID).enable(handle_tx_softirq);
    SoftIrqLine::get(NETWORK_RX_SOFTIRQ_ID).enable(handle_rx_softirq);

    Ok(())
}

type NetDeviceCallbackListRef = Arc<SpinLock<Vec<Arc<dyn NetDeviceCallback>>, BottomHalfDisabled>>;
type NetworkDeviceRef = Arc<SpinLock<dyn AnyNetworkDevice, BottomHalfDisabled>>;

struct Component {
    network_device_table: SpinLock<DeviceRegistry, BottomHalfDisabled>,
}

struct DeviceRegistry {
    entries: BTreeMap<String, NetworkDeviceIrqCallbackSet>,
}

impl DeviceRegistry {
    fn new() -> Self {
        Self {
            entries: BTreeMap::new(),
        }
    }

    fn register(
        &mut self,
        key: String,
        is_link_up: bool,
        device: NetworkDeviceRef,
    ) -> Result<(), RegisterDeviceError> {
        if !is_valid_device_key(&key) {
            return Err(RegisterDeviceError::InvalidKey);
        }
        if self.entries.contains_key(&key) {
            return Err(RegisterDeviceError::DuplicateKey);
        }
        self.entries
            .insert(key, NetworkDeviceIrqCallbackSet::new(device, is_link_up));
        Ok(())
    }

    fn snapshot(&self) -> Vec<RegisteredNetworkDevice> {
        self.entries
            .iter()
            .map(|(key, callbacks)| RegisteredNetworkDevice {
                key: key.clone(),
                is_link_up: callbacks.is_link_up,
                device: callbacks.device.clone(),
            })
            .collect()
    }
}

fn is_valid_device_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 64
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

/// The send callbacks and recv callbacks for a network device
struct NetworkDeviceIrqCallbackSet {
    device: NetworkDeviceRef,
    is_link_up: bool,
    recv_callbacks: NetDeviceCallbackListRef,
    send_callbacks: NetDeviceCallbackListRef,
}

impl NetworkDeviceIrqCallbackSet {
    fn new(device: NetworkDeviceRef, is_link_up: bool) -> Self {
        Self {
            device,
            is_link_up,
            recv_callbacks: Arc::new(SpinLock::new(Vec::new())),
            send_callbacks: Arc::new(SpinLock::new(Vec::new())),
        }
    }
}

impl Component {
    pub fn init() -> Result<Self, ComponentInitError> {
        Ok(Self {
            network_device_table: SpinLock::new(DeviceRegistry::new()),
        })
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[derive(Debug)]
    struct FakeNetworkDevice;

    impl AnyNetworkDevice for FakeNetworkDevice {
        fn mac_addr(&self) -> EthernetAddr {
            EthernetAddr([0x02, 0, 0, 0, 0, 1])
        }

        fn capabilities(&self) -> DeviceCapabilities {
            DeviceCapabilities::default()
        }

        fn can_receive(&self) -> bool {
            false
        }

        fn can_send(&self) -> bool {
            false
        }

        fn receive(&mut self) -> Result<RxBuffer, NetError> {
            Err(NetError::NotReady)
        }

        fn send(&mut self, _packet: &[u8]) -> Result<(), NetError> {
            Err(NetError::NotReady)
        }

        fn free_processed_tx_buffers(&mut self) {}

        fn notify_poll_end(&mut self) {}
    }

    fn fake_device() -> NetworkDeviceRef {
        Arc::new(SpinLock::new(FakeNetworkDevice))
    }

    #[ktest]
    fn registry_snapshot_is_sorted_and_preserves_link_metadata() {
        let mut registry = DeviceRegistry::new();
        registry
            .register("z-device".into(), false, fake_device())
            .unwrap();
        registry
            .register("a-device".into(), true, fake_device())
            .unwrap();

        let snapshot = registry.snapshot();
        assert_eq!(snapshot[0].key(), "a-device");
        assert!(snapshot[0].is_link_up());
        assert_eq!(snapshot[1].key(), "z-device");
        assert!(!snapshot[1].is_link_up());
    }

    #[ktest]
    fn registry_rejects_duplicate_and_unsafe_keys_without_replacement() {
        let mut registry = DeviceRegistry::new();
        registry
            .register("eic7700-rj45".into(), true, fake_device())
            .unwrap();

        assert_eq!(
            registry.register("eic7700-rj45".into(), false, fake_device()),
            Err(RegisterDeviceError::DuplicateKey)
        );
        assert_eq!(
            registry.register("bad,key".into(), true, fake_device()),
            Err(RegisterDeviceError::InvalidKey)
        );
        let snapshot = registry.snapshot();
        assert_eq!(snapshot.len(), 1);
        assert!(snapshot[0].is_link_up());
    }
}
