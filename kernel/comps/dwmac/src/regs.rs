// SPDX-License-Identifier: MPL-2.0

//! DWMAC4/5 queue-zero register layout and checked fields.

const RX_BUFFER_SIZE: RegisterField = RegisterField::new(0x0000_7ffe, 1);
const MAC_FEATURE1_RX_FIFO_SIZE: RegisterField = RegisterField::new(0x0000_001f, 0);
const MAC_FEATURE1_TX_FIFO_SIZE: RegisterField = RegisterField::new(0x0000_07c0, 6);
const MAC_RX_QUEUE0_ENABLE: RegisterField = RegisterField::new(0x0000_0003, 0);
const MTL_TX_QUEUE0_ENABLE: RegisterField = RegisterField::new(0x0000_000c, 2);
const MTL_TX_QUEUE0_SIZE: RegisterField = RegisterField::new(0x01ff_0000, 16);
const MTL_RX_QUEUE0_SIZE: RegisterField = RegisterField::new(0x3ff0_0000, 20);
const MTL_RX_QUEUE0_FLOW_CONTROL_DEACTIVATION: RegisterField = RegisterField::new(0x0001_c000, 14);
const MTL_RX_QUEUE0_FLOW_CONTROL_ACTIVATION: RegisterField = RegisterField::new(0x0000_0700, 8);
const MTL_RX_MISSED_PACKET_COUNT: RegisterField = RegisterField::new(0x07ff_0000, 16);
const MTL_RX_FIFO_OVERFLOW_PACKET_COUNT: RegisterField = RegisterField::new(0x0000_07ff, 0);
const MAC_DEBUG_TX_CONTROLLER_STATE: RegisterField = RegisterField::new(0x0006_0000, 17);
const MAC_DEBUG_RX_FIFO_STATE: RegisterField = RegisterField::new(0x0000_0006, 1);
const MTL_RX_DEBUG_PACKETS: RegisterField = RegisterField::new(0x3fff_0000, 16);
const MTL_RX_DEBUG_FILL_LEVEL: RegisterField = RegisterField::new(0x0000_0030, 4);
const MTL_RX_DEBUG_READ_STATE: RegisterField = RegisterField::new(0x0000_0006, 1);
const DMA_DEBUG_TX_STATE0: RegisterField = RegisterField::new(0x0000_f000, 12);
const DMA_DEBUG_RX_STATE0: RegisterField = RegisterField::new(0x0000_0f00, 8);

const MAC_RX_QUEUE_ENABLED_DCB: u32 = 2;
const MTL_TX_QUEUE_ENABLED: u32 = 2;
const MTL_TX_STORE_AND_FORWARD: u32 = 1 << 1;
const MTL_RX_STORE_AND_FORWARD: u32 = 1 << 5;
const MTL_RX_ENHANCED_FLOW_CONTROL: u32 = 1 << 7;
const MAC_TX_FLOW_CONTROL_ENABLE: u32 = 1 << 1;
const MAC_RX_FLOW_CONTROL_ENABLE: u32 = 1;
const MAC_TX_FLOW_CONTROL_BUSY: u32 = 1;
const MAC_PAUSE_TIME: u32 = 0xffff << 16;
const FIFO_SIZE_UNIT_BYTES: u32 = 128;
const MINIMUM_FLOW_CONTROL_FIFO_BYTES: u32 = 4096;
const DMA_ADDRESS_ALIGNED_BEATS: u32 = 1 << 12;
const DMA_EXTENDED_ADDRESS_MODE: u32 = 1 << 11;
const DMA_AXI_BURSTS_16_8_4: u32 = (1 << 3) | (1 << 2) | (1 << 1);
const DMA_AXI_WRITE_LIMIT_2: u32 = 2 << 24;
const DMA_AXI_READ_LIMIT_2: u32 = 2 << 16;
const DMA_INTERRUPT_NORMAL: u32 = 1 << 16;
const DMA_INTERRUPT_ABNORMAL: u32 = 1 << 15;
const DMA_INTERRUPT_NORMAL_4_10: u32 = 1 << 15;
const DMA_INTERRUPT_ABNORMAL_4_10: u32 = 1 << 14;
const DMA_INTERRUPT_FATAL_BUS: u32 = 1 << 12;
const DMA_INTERRUPT_RX_PROCESS_STOPPED: u32 = 1 << 8;
const DMA_INTERRUPT_RX_BUFFER_UNAVAILABLE: u32 = 1 << 7;
const DMA_INTERRUPT_RX: u32 = 1 << 6;
const DMA_INTERRUPT_TX: u32 = 1;
const DWMAC_4_10_VERSION: u8 = 0x41;

/// A word-aligned MMIO register offset from the DWMAC base.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RegisterOffset(u32);

impl RegisterOffset {
    const fn new(offset: u32) -> Self {
        assert!(offset.is_multiple_of(4));
        Self(offset)
    }

    /// Returns the byte offset from the controller MMIO base.
    pub const fn offset(self) -> u32 {
        self.0
    }
}

/// A rejected DWMAC register encoding.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RegisterValueError {
    OutOfRange,
    ReadbackMismatch,
}

/// A contiguous bit field in a 32-bit DWMAC register.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RegisterField {
    mask: u32,
    shift: u32,
}

impl RegisterField {
    /// Describes a register field with its shifted mask and bit position.
    pub const fn new(mask: u32, shift: u32) -> Self {
        assert!(mask != 0 && shift < 32 && mask.trailing_zeros() == shift);
        Self { mask, shift }
    }

    /// Encodes a value after proving that it fits in the field.
    pub const fn encode(self, value: u32) -> Result<u32, RegisterValueError> {
        let maximum = self.mask >> self.shift;
        if value > maximum {
            return Err(RegisterValueError::OutOfRange);
        }
        Ok((value << self.shift) & self.mask)
    }

    /// Replaces this field in an existing register value.
    pub fn replace(self, register: u32, value: u32) -> Result<u32, RegisterValueError> {
        let encoded = self.encode(value)?;
        Ok((register & !self.mask) | encoded)
    }

    const fn decode(self, register: u32) -> u32 {
        (register & self.mask) >> self.shift
    }
}

/// Queue-zero register values required before starting the MAC and DMA engines.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct QueueZeroConfiguration {
    pub mac_rx_queue_control0: u32,
    pub mtl_tx_operation_mode: u32,
    pub mtl_rx_operation_mode: u32,
}

/// Queue-zero flow-control values negotiated with the link partner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FlowControlConfiguration {
    pub rx_fifo_bytes: u32,
    pub mtl_rx_operation_mode: u32,
    pub mac_tx_flow_control_queue0: u32,
    pub mac_rx_flow_control: u32,
}

/// One read-only snapshot of the MAC, MTL, and DMA receive/transmit state machines.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FlowControlDebugSnapshot {
    pub mac_tx_controller_state: u32,
    pub mac_tx_engine_active: bool,
    pub mac_rx_fifo_state: u32,
    pub mac_rx_engine_active: bool,
    pub mtl_rx_queued_packets: u32,
    pub mtl_rx_fill_level: u32,
    pub mtl_rx_read_state: u32,
    pub mtl_rx_write_active: bool,
    pub dma_tx_state: u32,
    pub dma_rx_state: u32,
    pub mtl_rx_overflow: bool,
}

/// One clear-on-read queue-zero receive-loss register sample.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MtlRxLossSnapshot {
    pub missed_packets: u32,
    pub missed_counter_overflow: bool,
    pub fifo_overflow_packets: u32,
    pub fifo_counter_overflow: bool,
}

pub const MAC_CONFIGURATION: RegisterOffset = RegisterOffset::new(0x0000);
pub const MAC_PACKET_FILTER: RegisterOffset = RegisterOffset::new(0x0008);
pub const MAC_QUEUE0_TX_FLOW_CONTROL: RegisterOffset = RegisterOffset::new(0x0070);
pub const MAC_RX_FLOW_CONTROL: RegisterOffset = RegisterOffset::new(0x0090);
pub const MAC_INTERRUPT_STATUS: RegisterOffset = RegisterOffset::new(0x00b0);
pub const MAC_INTERRUPT_ENABLE: RegisterOffset = RegisterOffset::new(0x00b4);
pub const MAC_VERSION: RegisterOffset = RegisterOffset::new(0x0110);
pub const MAC_DEBUG: RegisterOffset = RegisterOffset::new(0x0114);
pub const MAC_HW_FEATURE1: RegisterOffset = RegisterOffset::new(0x0120);
pub const MAC_MDIO_ADDRESS: RegisterOffset = RegisterOffset::new(0x0200);
pub const MAC_MDIO_DATA: RegisterOffset = RegisterOffset::new(0x0204);
pub const MAC_ADDRESS0_HIGH: RegisterOffset = RegisterOffset::new(0x0300);
pub const MAC_ADDRESS0_LOW: RegisterOffset = RegisterOffset::new(0x0304);
pub const MAC_RX_QUEUE_CONTROL0: RegisterOffset = RegisterOffset::new(0x00a0);
pub const MTL_TX_QUEUE0_OPERATION_MODE: RegisterOffset = RegisterOffset::new(0x0d00);
pub const MTL_QUEUE0_INTERRUPT_CONTROL_STATUS: RegisterOffset = RegisterOffset::new(0x0d2c);
pub const MTL_RX_QUEUE0_OPERATION_MODE: RegisterOffset = RegisterOffset::new(0x0d30);
pub const MTL_RX_QUEUE0_MISSED_PACKET_OVERFLOW_COUNTER: RegisterOffset =
    RegisterOffset::new(0x0d34);
pub const MTL_RX_QUEUE0_DEBUG: RegisterOffset = RegisterOffset::new(0x0d38);
pub const DMA_MODE: RegisterOffset = RegisterOffset::new(0x1000);
pub const DMA_SYSTEM_BUS_MODE: RegisterOffset = RegisterOffset::new(0x1004);
pub const DMA_STATUS: RegisterOffset = RegisterOffset::new(0x1008);
pub const DMA_DEBUG_STATUS0: RegisterOffset = RegisterOffset::new(0x100c);
pub const DMA_AXI_BUS_MODE: RegisterOffset = RegisterOffset::new(0x1028);
pub const DMA_CHANNEL0_CONTROL: RegisterOffset = RegisterOffset::new(0x1100);
pub const DMA_CHANNEL0_TX_CONTROL: RegisterOffset = RegisterOffset::new(0x1104);
pub const DMA_CHANNEL0_RX_CONTROL: RegisterOffset = RegisterOffset::new(0x1108);
pub const DMA_CHANNEL0_TX_DESCRIPTOR_LIST_HIGH: RegisterOffset = RegisterOffset::new(0x1110);
pub const DMA_CHANNEL0_TX_DESCRIPTOR_LIST: RegisterOffset = RegisterOffset::new(0x1114);
pub const DMA_CHANNEL0_RX_DESCRIPTOR_LIST_HIGH: RegisterOffset = RegisterOffset::new(0x1118);
pub const DMA_CHANNEL0_RX_DESCRIPTOR_LIST: RegisterOffset = RegisterOffset::new(0x111c);
pub const DMA_CHANNEL0_TX_TAIL_POINTER: RegisterOffset = RegisterOffset::new(0x1120);
pub const DMA_CHANNEL0_RX_TAIL_POINTER: RegisterOffset = RegisterOffset::new(0x1128);
pub const DMA_CHANNEL0_TX_RING_LENGTH: RegisterOffset = RegisterOffset::new(0x112c);
pub const DMA_CHANNEL0_RX_RING_LENGTH: RegisterOffset = RegisterOffset::new(0x1130);
pub const DMA_CHANNEL0_INTERRUPT_ENABLE: RegisterOffset = RegisterOffset::new(0x1134);
pub const DMA_CHANNEL0_STATUS: RegisterOffset = RegisterOffset::new(0x1160);

/// Encodes a descriptor count for the DWMAC ring-length register.
pub fn encode_ring_length(entries: usize) -> Result<u32, RegisterValueError> {
    let encoded = entries
        .checked_sub(1)
        .and_then(|value| u16::try_from(value).ok())
        .ok_or(RegisterValueError::OutOfRange)?;
    Ok(u32::from(encoded))
}

/// Encodes a receive buffer capacity for the channel receive-control register.
pub fn encode_rx_buffer_size(capacity: usize) -> Result<u32, RegisterValueError> {
    let capacity = u32::try_from(capacity).map_err(|_| RegisterValueError::OutOfRange)?;
    if capacity == 0 {
        return Err(RegisterValueError::OutOfRange);
    }
    RX_BUFFER_SIZE.encode(capacity)
}

/// Selects the DMA interrupt summary bits for the advertised Synopsys revision.
pub const fn dma_interrupt_enable(version: u8) -> u32 {
    let summaries = if version >= DWMAC_4_10_VERSION {
        DMA_INTERRUPT_NORMAL_4_10 | DMA_INTERRUPT_ABNORMAL_4_10
    } else {
        DMA_INTERRUPT_NORMAL | DMA_INTERRUPT_ABNORMAL
    };
    summaries
        | DMA_INTERRUPT_FATAL_BUS
        | DMA_INTERRUPT_RX_PROCESS_STOPPED
        | DMA_INTERRUPT_RX_BUFFER_UNAVAILABLE
        | DMA_INTERRUPT_RX
        | DMA_INTERRUPT_TX
}

/// Reports whether the receive DMA stopped for lack of a published tail.
pub const fn dma_status_needs_rx_resume(status: u32) -> bool {
    status & DMA_INTERRUPT_RX_BUFFER_UNAVAILABLE != 0
}

/// Builds the fixed EIC7700 AXI policy and enables 64-bit DMA when required.
pub const fn dma_system_bus_mode(tx_high: u32, rx_high: u32) -> u32 {
    let extended = if tx_high != 0 || rx_high != 0 {
        DMA_EXTENDED_ADDRESS_MODE
    } else {
        0
    };
    DMA_ADDRESS_ALIGNED_BEATS
        | DMA_AXI_WRITE_LIMIT_2
        | DMA_AXI_READ_LIMIT_2
        | DMA_AXI_BURSTS_16_8_4
        | extended
}

/// Decodes the documented clear-on-read MTL queue-zero receive-loss counters.
pub const fn decode_mtl_rx_loss(register: u32) -> MtlRxLossSnapshot {
    MtlRxLossSnapshot {
        missed_packets: MTL_RX_MISSED_PACKET_COUNT.decode(register),
        missed_counter_overflow: register & (1 << 27) != 0,
        fifo_overflow_packets: MTL_RX_FIFO_OVERFLOW_PACKET_COUNT.decode(register),
        fifo_counter_overflow: register & (1 << 11) != 0,
    }
}

/// Decodes the read-only hardware state described by the EIC7700X GMAC TRM.
pub const fn decode_flow_control_debug(
    mac_debug: u32,
    mtl_rx_debug: u32,
    dma_debug: u32,
    mtl_queue_interrupt: u32,
) -> FlowControlDebugSnapshot {
    FlowControlDebugSnapshot {
        mac_tx_controller_state: MAC_DEBUG_TX_CONTROLLER_STATE.decode(mac_debug),
        mac_tx_engine_active: mac_debug & (1 << 16) != 0,
        mac_rx_fifo_state: MAC_DEBUG_RX_FIFO_STATE.decode(mac_debug),
        mac_rx_engine_active: mac_debug & 1 != 0,
        mtl_rx_queued_packets: MTL_RX_DEBUG_PACKETS.decode(mtl_rx_debug),
        mtl_rx_fill_level: MTL_RX_DEBUG_FILL_LEVEL.decode(mtl_rx_debug),
        mtl_rx_read_state: MTL_RX_DEBUG_READ_STATE.decode(mtl_rx_debug),
        mtl_rx_write_active: mtl_rx_debug & 1 != 0,
        dma_tx_state: DMA_DEBUG_TX_STATE0.decode(dma_debug),
        dma_rx_state: DMA_DEBUG_RX_STATE0.decode(dma_debug),
        mtl_rx_overflow: mtl_queue_interrupt & (1 << 16) != 0,
    }
}

/// Enables queue zero and assigns the complete hardware-advertised FIFO to it.
pub fn configure_queue_zero(
    mac_feature1: u32,
    current_mtl_tx: u32,
    current_mtl_rx: u32,
    current_mac_rx_queue: u32,
) -> Result<QueueZeroConfiguration, RegisterValueError> {
    let tx_queue_size = encode_fifo_queue_size(
        MAC_FEATURE1_TX_FIFO_SIZE.decode(mac_feature1),
        MTL_TX_QUEUE0_SIZE,
    )?;
    let rx_queue_size = encode_fifo_queue_size(
        MAC_FEATURE1_RX_FIFO_SIZE.decode(mac_feature1),
        MTL_RX_QUEUE0_SIZE,
    )?;
    let mtl_tx_operation_mode = MTL_TX_QUEUE0_ENABLE.replace(
        MTL_TX_QUEUE0_SIZE.replace(current_mtl_tx, tx_queue_size)?,
        MTL_TX_QUEUE_ENABLED,
    )? | MTL_TX_STORE_AND_FORWARD;
    let mtl_rx_operation_mode =
        MTL_RX_QUEUE0_SIZE.replace(current_mtl_rx, rx_queue_size)? | MTL_RX_STORE_AND_FORWARD;
    let mac_rx_queue_control0 =
        MAC_RX_QUEUE0_ENABLE.replace(current_mac_rx_queue, MAC_RX_QUEUE_ENABLED_DCB)?;
    Ok(QueueZeroConfiguration {
        mac_rx_queue_control0,
        mtl_tx_operation_mode,
        mtl_rx_operation_mode,
    })
}

/// Configures negotiated pause handling for queue zero.
///
/// The 4-KiB thresholds follow the EIC7700X TRM Part 4 definitions of
/// `MTL_RxQ0_Operation_Mode`, `MAC_Q0_Tx_Flow_Ctrl`, and `MAC_Rx_Flow_Ctrl`:
/// <https://github.com/eswincomputing/EIC7700X-SoC-Technical-Reference-Manual/releases/tag/v1.0.0-20250103>.
/// They match the DWMAC4 policy in Linux `dwmac4_dma_rx_chan_op_mode()`.
/// The FIFO capacity comes from the queue-size field read back from hardware;
/// callers must first compare it with the advertised feature register.
pub fn configure_flow_control(
    current_mtl_rx: u32,
    tx_pause: bool,
    rx_pause: bool,
) -> Result<FlowControlConfiguration, RegisterValueError> {
    let rx_fifo_bytes = MTL_RX_QUEUE0_SIZE
        .decode(current_mtl_rx)
        .checked_add(1)
        .and_then(|units| units.checked_mul(256))
        .ok_or(RegisterValueError::OutOfRange)?;
    let mut mtl_rx_operation_mode =
        MTL_RX_QUEUE0_FLOW_CONTROL_DEACTIVATION.replace(current_mtl_rx, 0)?;
    mtl_rx_operation_mode =
        MTL_RX_QUEUE0_FLOW_CONTROL_ACTIVATION.replace(mtl_rx_operation_mode, 0)?;
    mtl_rx_operation_mode &= !MTL_RX_ENHANCED_FLOW_CONTROL;

    if tx_pause && rx_fifo_bytes >= MINIMUM_FLOW_CONTROL_FIFO_BYTES {
        let (deactivation_threshold, activation_threshold) =
            if rx_fifo_bytes == MINIMUM_FLOW_CONTROL_FIFO_BYTES {
                (3, 1)
            } else {
                (7, 4)
            };
        mtl_rx_operation_mode = MTL_RX_QUEUE0_FLOW_CONTROL_DEACTIVATION
            .replace(mtl_rx_operation_mode, deactivation_threshold)?;
        mtl_rx_operation_mode = MTL_RX_QUEUE0_FLOW_CONTROL_ACTIVATION
            .replace(mtl_rx_operation_mode, activation_threshold)?;
        mtl_rx_operation_mode |= MTL_RX_ENHANCED_FLOW_CONTROL;
    }

    let mac_tx_flow_control_queue0 = if tx_pause {
        MAC_PAUSE_TIME | MAC_TX_FLOW_CONTROL_ENABLE
    } else {
        0
    };
    let mac_rx_flow_control = if rx_pause {
        MAC_RX_FLOW_CONTROL_ENABLE
    } else {
        0
    };
    Ok(FlowControlConfiguration {
        rx_fifo_bytes,
        mtl_rx_operation_mode,
        mac_tx_flow_control_queue0,
        mac_rx_flow_control,
    })
}

/// Checks that the queue-size readback agrees with the feature register.
pub fn validate_rx_fifo_readback(
    mac_feature1: u32,
    actual_mtl_rx: u32,
) -> Result<(), RegisterValueError> {
    let expected = encode_fifo_queue_size(
        MAC_FEATURE1_RX_FIFO_SIZE.decode(mac_feature1),
        MTL_RX_QUEUE0_SIZE,
    )?;
    if MTL_RX_QUEUE0_SIZE.decode(actual_mtl_rx) != expected {
        return Err(RegisterValueError::ReadbackMismatch);
    }
    Ok(())
}

/// Reports the access-restricted busy bit that must clear before a MAC flow write.
pub const fn mac_tx_flow_control_busy(register: u32) -> bool {
    register & MAC_TX_FLOW_CONTROL_BUSY != 0
}

/// Verifies all writable flow-control fields after programming the hardware.
pub fn validate_flow_control_readback(
    expected: FlowControlConfiguration,
    actual_mtl_rx: u32,
    actual_mac_tx: u32,
    actual_mac_rx: u32,
) -> Result<(), RegisterValueError> {
    const MTL_MASK: u32 = MTL_RX_QUEUE0_SIZE.mask
        | MTL_RX_QUEUE0_FLOW_CONTROL_DEACTIVATION.mask
        | MTL_RX_QUEUE0_FLOW_CONTROL_ACTIVATION.mask
        | MTL_RX_ENHANCED_FLOW_CONTROL
        | MTL_RX_STORE_AND_FORWARD;
    const MAC_TX_MASK: u32 = 0xffff_00f2;
    if actual_mtl_rx & MTL_MASK != expected.mtl_rx_operation_mode & MTL_MASK
        || actual_mac_tx & MAC_TX_MASK != expected.mac_tx_flow_control_queue0 & MAC_TX_MASK
        || actual_mac_rx & MAC_RX_FLOW_CONTROL_ENABLE
            != expected.mac_rx_flow_control & MAC_RX_FLOW_CONTROL_ENABLE
        || mac_tx_flow_control_busy(actual_mac_tx)
    {
        return Err(RegisterValueError::ReadbackMismatch);
    }
    Ok(())
}

fn encode_fifo_queue_size(
    feature_encoding: u32,
    queue_size: RegisterField,
) -> Result<u32, RegisterValueError> {
    let fifo_bytes = FIFO_SIZE_UNIT_BYTES
        .checked_shl(feature_encoding)
        .ok_or(RegisterValueError::OutOfRange)?;
    let encoded = fifo_bytes
        .checked_div(256)
        .and_then(|units| units.checked_sub(1))
        .ok_or(RegisterValueError::OutOfRange)?;
    queue_size
        .encode(encoded)
        .map(|value| queue_size.decode(value))
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn queue_zero_register_offsets_match_the_dwmac4_layout() {
        assert_eq!(MAC_CONFIGURATION.offset(), 0x0000);
        assert_eq!(MAC_VERSION.offset(), 0x0110);
        assert_eq!(MAC_MDIO_ADDRESS.offset(), 0x0200);
        assert_eq!(DMA_MODE.offset(), 0x1000);
        assert_eq!(DMA_CHANNEL0_TX_CONTROL.offset(), 0x1104);
        assert_eq!(DMA_CHANNEL0_RX_CONTROL.offset(), 0x1108);
        assert_eq!(DMA_CHANNEL0_TX_DESCRIPTOR_LIST.offset(), 0x1114);
        assert_eq!(DMA_CHANNEL0_RX_DESCRIPTOR_LIST.offset(), 0x111c);
        assert_eq!(DMA_CHANNEL0_STATUS.offset(), 0x1160);
    }

    #[ktest]
    fn checked_field_rejects_values_outside_its_mask() {
        let field = RegisterField::new(0x0000_ff00, 8);
        assert_eq!(field.encode(0xff).unwrap(), 0x0000_ff00);
        assert_eq!(field.encode(0x100), Err(RegisterValueError::OutOfRange));
    }

    #[ktest]
    fn queue_lengths_and_rx_buffer_size_use_hardware_encoding() {
        assert_eq!(encode_ring_length(64).unwrap(), 63);
        assert_eq!(encode_ring_length(0), Err(RegisterValueError::OutOfRange));
        assert_eq!(encode_rx_buffer_size(2048).unwrap(), 2048 << 1);
        assert_eq!(
            encode_rx_buffer_size(1 << 14),
            Err(RegisterValueError::OutOfRange)
        );
    }

    #[ktest]
    fn queue_zero_configuration_enables_mac_and_mtl_data_paths() {
        let feature1 = (7 << 6) | 7;
        let configured = configure_queue_zero(feature1, 0, 0, 0).unwrap();

        assert_eq!(MAC_RX_QUEUE_CONTROL0.offset(), 0x00a0);
        assert_eq!(MAC_HW_FEATURE1.offset(), 0x0120);
        assert_eq!(MTL_TX_QUEUE0_OPERATION_MODE.offset(), 0x0d00);
        assert_eq!(MTL_RX_QUEUE0_OPERATION_MODE.offset(), 0x0d30);
        assert_eq!(configured.mac_rx_queue_control0, 2);
        assert_eq!(configured.mtl_tx_operation_mode, (63 << 16) | (2 << 2) | 2);
        assert_eq!(configured.mtl_rx_operation_mode, (63 << 20) | (1 << 5));
    }

    #[ktest]
    fn queue_zero_configuration_preserves_unrelated_register_fields() {
        let feature1 = (7 << 6) | 7;
        let configured =
            configure_queue_zero(feature1, 0xa000_0055, 0x4000_00c0, 0xffff_fffd).unwrap();

        assert_eq!(configured.mtl_tx_operation_mode, 0xa03f_005b);
        assert_eq!(configured.mtl_rx_operation_mode, 0x43f0_00e0);
        assert_eq!(configured.mac_rx_queue_control0, 0xffff_fffe);
    }

    #[ktest]
    fn interrupt_summary_bits_follow_the_synopsys_revision() {
        assert_eq!(dma_interrupt_enable(0x40), 0x0001_91c1);
        assert_eq!(dma_interrupt_enable(0x41), 0x0000_d1c1);
        assert_eq!(dma_interrupt_enable(0x51), 0x0000_d1c1);
        assert_eq!(dma_interrupt_enable(0x52), 0x0000_d1c1);
    }

    #[ktest]
    fn extended_dma_addresses_enable_the_high_address_registers() {
        assert_eq!(dma_system_bus_mode(0, 0), 0x0202_100e);
        assert_eq!(dma_system_bus_mode(1, 0), 0x0202_180e);
        assert_eq!(dma_system_bus_mode(0, 1), 0x0202_180e);
    }

    #[ktest]
    fn receive_buffer_unavailable_requires_a_tail_pointer_kick() {
        assert!(!dma_status_needs_rx_resume(0));
        assert!(dma_status_needs_rx_resume(1 << 7));
        assert!(dma_status_needs_rx_resume((1 << 14) | (1 << 7)));
        assert!(!dma_status_needs_rx_resume((1 << 10) | (1 << 2)));
    }
}
