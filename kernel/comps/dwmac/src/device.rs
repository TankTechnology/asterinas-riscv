// SPDX-License-Identifier: MPL-2.0

//! Asterinas network-device adapter for the selected EIC7700 GMAC.

extern crate alloc;

use alloc::{string::ToString, sync::Arc};
use core::{fmt, hint::spin_loop, time::Duration};

use aster_bigtcp::device::{Checksum, DeviceCapabilities, Medium};
use aster_network::{AnyNetworkDevice, EthernetAddr, NetError, RxBuffer};
use aster_softirq::BottomHalfDisabled;
use ostd::{
    arch::irq::{DeferredMappedIrqLine, IRQ_CHIP},
    irq::IrqLine,
    sync::SpinLock,
};

use crate::{
    arch::{MegrezPlatform, PlatformError, SelectedPortInfo},
    queue::{DmaQueue, POLL_BUDGET, QUEUE_SIZE, QueueAddresses, QueueError},
    regs::{
        DMA_CHANNEL0_CONTROL, DMA_CHANNEL0_INTERRUPT_ENABLE, DMA_CHANNEL0_RX_CONTROL,
        DMA_CHANNEL0_RX_DESCRIPTOR_LIST, DMA_CHANNEL0_RX_DESCRIPTOR_LIST_HIGH,
        DMA_CHANNEL0_RX_RING_LENGTH, DMA_CHANNEL0_RX_TAIL_POINTER, DMA_CHANNEL0_STATUS,
        DMA_CHANNEL0_TX_CONTROL, DMA_CHANNEL0_TX_DESCRIPTOR_LIST,
        DMA_CHANNEL0_TX_DESCRIPTOR_LIST_HIGH, DMA_CHANNEL0_TX_RING_LENGTH,
        DMA_CHANNEL0_TX_TAIL_POINTER, DMA_MODE, DMA_SYSTEM_BUS_MODE, MAC_ADDRESS0_HIGH,
        MAC_ADDRESS0_LOW, MAC_CONFIGURATION, MAC_INTERRUPT_ENABLE, MAC_PACKET_FILTER,
        encode_ring_length, encode_rx_buffer_size,
    },
};

const DEVICE_NAME: &str = "eic7700-rj45";
const DMA_SOFTWARE_RESET: u32 = 1;
const DMA_ADDRESS_ALIGNED_BEATS: u32 = 1 << 12;
const DMA_AXI_BURSTS_16_8_4: u32 = (1 << 3) | (1 << 2) | (1 << 1);
const DMA_AXI_WRITE_LIMIT_2: u32 = 2 << 24;
const DMA_AXI_READ_LIMIT_2: u32 = 2 << 16;
const DMA_TX_PBL_32: u32 = 32 << 16;
const DMA_TX_OPERATE_ON_SECOND_PACKET: u32 = 1 << 4;
const DMA_TX_START: u32 = 1;
const DMA_RX_PBL_32: u32 = 32 << 16;
const DMA_RX_START: u32 = 1;
const DMA_INTERRUPT_NORMAL_4_10: u32 = 1 << 15;
const DMA_INTERRUPT_ABNORMAL_4_10: u32 = 1 << 14;
const DMA_INTERRUPT_FATAL_BUS: u32 = 1 << 12;
const DMA_INTERRUPT_RX: u32 = 1 << 6;
const DMA_INTERRUPT_TX: u32 = 1;
const DMA_INTERRUPT_MASK: u32 = DMA_INTERRUPT_NORMAL_4_10
    | DMA_INTERRUPT_ABNORMAL_4_10
    | DMA_INTERRUPT_FATAL_BUS
    | DMA_INTERRUPT_RX
    | DMA_INTERRUPT_TX;
const DMA_STATUS_FATAL_BUS: u32 = 1 << 12;
const DMA_STATUS_KNOWN: u32 = 0x003f_fdc7;
const MAC_CONFIG_DUPLEX: u32 = 1 << 13;
const MAC_CONFIG_FAST_ETHERNET: u32 = 1 << 14;
const MAC_CONFIG_PORT_SELECT: u32 = 1 << 15;
const MAC_CONFIG_TX_ENABLE: u32 = 1 << 1;
const MAC_CONFIG_RX_ENABLE: u32 = 1;
const MAC_ADDRESS_ENABLE: u32 = 1 << 31;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum DeviceError {
    Dma(QueueError),
    Irq,
    Platform(PlatformError),
    RegisterEncoding,
    ResetTimeout,
}

impl From<PlatformError> for DeviceError {
    fn from(error: PlatformError) -> Self {
        Self::Platform(error)
    }
}

impl From<QueueError> for DeviceError {
    fn from(error: QueueError) -> Self {
        Self::Dma(error)
    }
}

pub(super) fn register(mut platform: MegrezPlatform) -> Result<(), DeviceError> {
    let selected = platform.select_linked()?;
    let queue = DmaQueue::new()?;
    stop_both_controllers(&platform)?;
    reset_dma(&platform, selected.alias_index)?;
    configure_selected(&platform, selected, queue.addresses())?;

    let irq_line = IrqLine::alloc().map_err(|_| DeviceError::Irq)?;
    let irq_chip = IRQ_CHIP.get().ok_or(DeviceError::Irq)?;
    let mut irq = irq_chip
        .map_fdt_pin_to_masked(selected.interrupt_source, irq_line)
        .map_err(|_| DeviceError::Irq)?;
    irq.on_active_and_mask(|_| {
        aster_network::raise_receive_softirq();
        aster_network::raise_send_softirq();
    })
    .map_err(|_| DeviceError::Irq)?;

    let device = DwmacDevice {
        platform,
        selected,
        queue,
        irq,
        fatal: false,
        capabilities: ethernet_capabilities(),
    };
    device.write(DMA_CHANNEL0_INTERRUPT_ENABLE.offset(), DMA_INTERRUPT_MASK)?;
    ostd::info!(
        "ASTERINAS_GMAC_SELECTED key={} alias={} speed={}Mbps duplex={} mac={:02x?}",
        DEVICE_NAME,
        selected.alias_index,
        selected.link_state.speed_mbps(),
        selected.link_state.is_full_duplex(),
        selected.mac_address,
    );
    let device = Arc::new(SpinLock::<_, BottomHalfDisabled>::new(device));
    aster_network::register_device(DEVICE_NAME.to_string(), device.clone());
    device.lock().irq.rearm().map_err(|_| DeviceError::Irq)?;
    Ok(())
}

fn stop_both_controllers(platform: &MegrezPlatform) -> Result<(), DeviceError> {
    for alias in [0, 1] {
        platform.write_gmac(alias, DMA_CHANNEL0_INTERRUPT_ENABLE.offset() as usize, 0)?;
        let tx = platform.read_gmac(alias, DMA_CHANNEL0_TX_CONTROL.offset() as usize)?;
        platform.write_gmac(
            alias,
            DMA_CHANNEL0_TX_CONTROL.offset() as usize,
            tx & !DMA_TX_START,
        )?;
        let rx = platform.read_gmac(alias, DMA_CHANNEL0_RX_CONTROL.offset() as usize)?;
        platform.write_gmac(
            alias,
            DMA_CHANNEL0_RX_CONTROL.offset() as usize,
            rx & !DMA_RX_START,
        )?;
        let mac = platform.read_gmac(alias, MAC_CONFIGURATION.offset() as usize)?;
        platform.write_gmac(
            alias,
            MAC_CONFIGURATION.offset() as usize,
            mac & !(MAC_CONFIG_TX_ENABLE | MAC_CONFIG_RX_ENABLE),
        )?;
    }
    Ok(())
}

fn reset_dma(platform: &MegrezPlatform, alias: u8) -> Result<(), DeviceError> {
    let mode = platform.read_gmac(alias, DMA_MODE.offset() as usize)?;
    platform.write_gmac(alias, DMA_MODE.offset() as usize, mode | DMA_SOFTWARE_RESET)?;
    let deadline = aster_time::read_monotonic_time()
        .checked_add(Duration::from_secs(1))
        .ok_or(DeviceError::ResetTimeout)?;
    loop {
        if platform.read_gmac(alias, DMA_MODE.offset() as usize)? & DMA_SOFTWARE_RESET == 0 {
            return Ok(());
        }
        if aster_time::read_monotonic_time() >= deadline {
            return Err(DeviceError::ResetTimeout);
        }
        spin_loop();
    }
}

fn configure_selected(
    platform: &MegrezPlatform,
    selected: SelectedPortInfo,
    addresses: QueueAddresses,
) -> Result<(), DeviceError> {
    let alias = selected.alias_index;
    let write = |offset: u32, value| platform.write_gmac(alias, offset as usize, value);
    let split = |address: usize| (address as u32, (address as u64 >> 32) as u32);
    let (tx_low, tx_high) = split(addresses.tx_ring);
    let (rx_low, rx_high) = split(addresses.rx_ring);

    write(
        DMA_SYSTEM_BUS_MODE.offset(),
        DMA_ADDRESS_ALIGNED_BEATS
            | DMA_AXI_WRITE_LIMIT_2
            | DMA_AXI_READ_LIMIT_2
            | DMA_AXI_BURSTS_16_8_4,
    )?;
    write(DMA_CHANNEL0_CONTROL.offset(), 0)?;
    write(DMA_CHANNEL0_TX_DESCRIPTOR_LIST_HIGH.offset(), tx_high)?;
    write(DMA_CHANNEL0_TX_DESCRIPTOR_LIST.offset(), tx_low)?;
    write(DMA_CHANNEL0_RX_DESCRIPTOR_LIST_HIGH.offset(), rx_high)?;
    write(DMA_CHANNEL0_RX_DESCRIPTOR_LIST.offset(), rx_low)?;
    let ring_length = encode_ring_length(QUEUE_SIZE).map_err(|_| DeviceError::RegisterEncoding)?;
    write(DMA_CHANNEL0_TX_RING_LENGTH.offset(), ring_length)?;
    write(DMA_CHANNEL0_RX_RING_LENGTH.offset(), ring_length)?;
    write(
        DMA_CHANNEL0_TX_TAIL_POINTER.offset(),
        addresses.initial_tx_tail as u32,
    )?;
    write(
        DMA_CHANNEL0_RX_TAIL_POINTER.offset(),
        addresses.initial_rx_tail as u32,
    )?;
    let rx_buffer = encode_rx_buffer_size(crate::queue::BUFFER_SIZE)
        .map_err(|_| DeviceError::RegisterEncoding)?;
    write(
        DMA_CHANNEL0_TX_CONTROL.offset(),
        DMA_TX_PBL_32 | DMA_TX_OPERATE_ON_SECOND_PACKET,
    )?;
    write(DMA_CHANNEL0_RX_CONTROL.offset(), DMA_RX_PBL_32 | rx_buffer)?;
    let mac_low = u32::from_le_bytes([
        selected.mac_address[0],
        selected.mac_address[1],
        selected.mac_address[2],
        selected.mac_address[3],
    ]);
    let mac_high = u32::from(selected.mac_address[4])
        | (u32::from(selected.mac_address[5]) << 8)
        | MAC_ADDRESS_ENABLE;
    write(MAC_ADDRESS0_HIGH.offset(), mac_high)?;
    write(MAC_ADDRESS0_LOW.offset(), mac_low)?;
    write(MAC_PACKET_FILTER.offset(), 0)?;
    write(MAC_INTERRUPT_ENABLE.offset(), 0)?;
    write(DMA_CHANNEL0_STATUS.offset(), DMA_STATUS_KNOWN)?;
    write(
        DMA_CHANNEL0_TX_CONTROL.offset(),
        DMA_TX_PBL_32 | DMA_TX_OPERATE_ON_SECOND_PACKET | DMA_TX_START,
    )?;
    write(
        DMA_CHANNEL0_RX_CONTROL.offset(),
        DMA_RX_PBL_32 | rx_buffer | DMA_RX_START,
    )?;
    write(
        MAC_CONFIGURATION.offset(),
        mac_configuration(selected) | MAC_CONFIG_TX_ENABLE | MAC_CONFIG_RX_ENABLE,
    )
    .map_err(DeviceError::Platform)
}

fn mac_configuration(selected: SelectedPortInfo) -> u32 {
    let duplex = if selected.link_state.is_full_duplex() {
        MAC_CONFIG_DUPLEX
    } else {
        0
    };
    let speed = match selected.link_state.speed_mbps() {
        1000 => 0,
        100 => MAC_CONFIG_PORT_SELECT | MAC_CONFIG_FAST_ETHERNET,
        10 => MAC_CONFIG_PORT_SELECT,
        _ => 0,
    };
    duplex | speed
}

fn ethernet_capabilities() -> DeviceCapabilities {
    let mut capabilities = DeviceCapabilities::default();
    capabilities.medium = Medium::Ethernet;
    capabilities.max_transmission_unit = 1514;
    capabilities.max_burst_size = None;
    capabilities.checksum.ipv4 = Checksum::Both;
    capabilities.checksum.icmpv4 = Checksum::Both;
    capabilities.checksum.tcp = Checksum::Both;
    capabilities.checksum.udp = Checksum::Both;
    capabilities
}

struct DwmacDevice {
    platform: MegrezPlatform,
    selected: SelectedPortInfo,
    queue: DmaQueue,
    irq: DeferredMappedIrqLine,
    fatal: bool,
    capabilities: DeviceCapabilities,
}

impl DwmacDevice {
    fn read(&self, offset: u32) -> Result<u32, DeviceError> {
        self.platform
            .read_gmac(self.selected.alias_index, offset as usize)
            .map_err(DeviceError::Platform)
    }

    fn write(&self, offset: u32, value: u32) -> Result<(), DeviceError> {
        self.platform
            .write_gmac(self.selected.alias_index, offset as usize, value)
            .map_err(DeviceError::Platform)
    }

    fn service_status(&mut self) {
        let Ok(status) = self.read(DMA_CHANNEL0_STATUS.offset()) else {
            self.fatal = true;
            return;
        };
        if status & DMA_STATUS_FATAL_BUS != 0 {
            self.fatal = true;
        }
        if self
            .write(DMA_CHANNEL0_STATUS.offset(), status & DMA_STATUS_KNOWN)
            .is_err()
        {
            self.fatal = true;
        }
    }
}

impl AnyNetworkDevice for DwmacDevice {
    fn mac_addr(&self) -> EthernetAddr {
        EthernetAddr(self.selected.mac_address)
    }

    fn capabilities(&self) -> DeviceCapabilities {
        self.capabilities.clone()
    }

    fn can_receive(&self) -> bool {
        !self.fatal && self.queue.can_receive()
    }

    fn can_send(&self) -> bool {
        !self.fatal && self.queue.can_send()
    }

    fn receive(&mut self) -> Result<RxBuffer, NetError> {
        let packet = self.queue.receive();
        if let Some(tail) = self.queue.take_rx_tail()
            && self
                .write(DMA_CHANNEL0_RX_TAIL_POINTER.offset(), tail as u32)
                .is_err()
        {
            self.fatal = true;
            return Err(NetError::NotReady);
        }
        match packet {
            Ok(buffer) => Ok(buffer),
            Err(QueueError::Allocation) => Err(NetError::NoMemory),
            Err(QueueError::NotReady) => Err(NetError::NotReady),
            Err(_) => {
                self.service_status();
                Err(NetError::NotReady)
            }
        }
    }

    fn send(&mut self, packet: &[u8]) -> Result<(), NetError> {
        let tail = match self.queue.send(packet) {
            Ok(tail) => tail,
            Err(QueueError::Allocation) => return Err(NetError::NoMemory),
            Err(QueueError::Full) => return Err(NetError::Busy),
            Err(_) => return Err(NetError::NotReady),
        };
        self.write(DMA_CHANNEL0_TX_TAIL_POINTER.offset(), tail as u32)
            .map_err(|_| NetError::NotReady)
    }

    fn free_processed_tx_buffers(&mut self) {
        if self.queue.reclaim_tx(POLL_BUDGET).is_err() {
            self.fatal = true;
        }
    }

    fn notify_poll_end(&mut self) {
        self.service_status();
        if !self.fatal && self.irq.rearm().is_err() {
            self.fatal = true;
        }
    }
}

impl fmt::Debug for DwmacDevice {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("DwmacDevice")
            .field("alias_index", &self.selected.alias_index)
            .field("version", &self.selected.version)
            .field("mac_address", &self.selected.mac_address)
            .field("link_state", &self.selected.link_state)
            .field("fatal", &self.fatal)
            .finish_non_exhaustive()
    }
}
