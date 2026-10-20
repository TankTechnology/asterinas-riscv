// SPDX-License-Identifier: MPL-2.0

use core::{array, hint::spin_loop, ops::Range, time::Duration};

use fdt::{Fdt, node::FdtNode};
use ostd::{
    arch::{boot::DEVICE_TREE, device::io_mem, irq::InterruptSourceInFdt},
    io::IoMem,
    mm::io::VmIoOnce,
};

pub(super) fn dma_write_barrier() {
    io_mem::fence();
}

pub(super) fn dma_read_barrier() {
    io_mem::fence();
}

use crate::{
    phy::{Deadline, LinkState, MdioBus, MdioError},
    regs::{
        DMA_MODE, DMA_STATUS, MAC_CONFIGURATION, MAC_HW_FEATURE1, MAC_INTERRUPT_STATUS,
        MAC_MDIO_ADDRESS, MAC_MDIO_DATA, MAC_VERSION, RegisterOffset,
    },
    select::{MonotonicClock, PortCandidate, SelectError, select_linked_port},
};

const COMPATIBLE: &[u8] = b"eswin,win2030-qos-eth\0";
const PORT_PATHS: [&str; 2] = ["/soc/ethernet@50400000", "/soc/ethernet@50410000"];
const CLOCK_NAMES: &[u8] = b"app\0stmmaceth\0tx\0";
const RESET_NAME: &[u8] = b"ethrst\0";
const PHY_MODE: &[u8] = b"rgmii-txid\0";

const HSP_START: usize = 0x5044_0000;
const HSP_SIZE: usize = 0x2000;
const HSP_WINDOW_START: usize = 0x100;
const HSP_WINDOW_END: usize = 0x220;
const SYSCRG_START: usize = 0x5182_8000;
const SYSCRG_SIZE: usize = 0x8_0000;
const SYS_CLOCK_START: usize = 0x148;
const SYS_CLOCK_END: usize = 0x160;
const SYS_RESET_OFFSET: usize = 0x41c;
const PINCTRL_START: usize = 0x5160_0080;
const PINCTRL_SIZE: usize = 0x1f_ff80;
const RGMII_WINDOW_START: usize = 0x290;
const RGMII_WINDOW_END: usize = 0x298;
const GPIO_START: usize = 0x5160_0000;
const GPIO_SIZE: usize = 0x80;
const GPIO_WINDOW_END: usize = 0x20;

const HSP_ACLK_ENABLE: u32 = 1 << 31;
const HSP_ACLK_DIVISOR: u32 = 2 << 4;
const HSP_CFG_ENABLE: u32 = (1 << 31) | (1 << 30);
const ETH_TX_CLOCK_SELECT: u32 = 1 << 16;
const ETH_PHY_RGMII_SELECT: u32 = 1;
const ETH_AXI_REQUEST: u32 = 1;
const TX_CLOCK_GATE: u32 = 1;
const TX_CLOCK_PARENT_MASK: u32 = 1 << 1;
const TX_CLOCK_DIVISOR_MASK: u32 = 0x7f << 4;
const EIC7700_DWMAC_VERSION: u8 = 0x52;
const MDIO_BUSY: u32 = 1;
const MDIO_WRITE: u32 = 1 << 2;
const MDIO_READ: u32 = 3 << 2;
const MDIO_CLOCK_150_250MHZ: u32 = 4 << 8;
const MDIO_REGISTER_SHIFT: u32 = 16;
const MDIO_PHY_SHIFT: u32 = 21;
const PHY_ID1: u8 = 2;
const PHY_ID2: u8 = 3;
const GMAC_PROBE_OFFSETS: [RegisterOffset; 6] = [
    MAC_CONFIGURATION,
    MAC_INTERRUPT_STATUS,
    MAC_VERSION,
    MAC_HW_FEATURE1,
    DMA_MODE,
    DMA_STATUS,
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PortFields {
    alias_index: u8,
    enabled: bool,
    compatible: bool,
    controller_id: Option<u32>,
    mmio: Option<(usize, usize)>,
    interrupt_parent: Option<u32>,
    interrupt: Option<u32>,
    dma_noncoherent: bool,
    iommu_absent: bool,
    dma_ranges_absent: bool,
    clock_names_valid: bool,
    clock_cells: Option<[u32; 6]>,
    reset_name_valid: bool,
    reset_cells: Option<[u32; 3]>,
    phy_mode_valid: bool,
    phy_address: Option<u8>,
    mac_address: Option<[u8; 6]>,
    hsp_cells: Option<[u32; 4]>,
    hsp_provider_valid: bool,
    syscrg_cells: Option<[u32; 3]>,
    syscrg_provider_valid: bool,
    delay_registers: Option<[u32; 3]>,
    delay_1000m: Option<[u32; 3]>,
    delay_100m: Option<[u32; 3]>,
    delay_10m: Option<[u32; 3]>,
    rgmii_select: Option<[u32; 3]>,
    rgmii_provider_valid: bool,
    reset_gpio: Option<[u32; 3]>,
    gpio_provider_valid: bool,
    axi_valid: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PlatformConfig {
    alias_index: u8,
    controller_id: u8,
    mmio_range: Range<usize>,
    interrupt: (u32, u32),
    clock_cells: [u32; 6],
    reset_cells: [u32; 3],
    phy_address: u8,
    mac_address: [u8; 6],
    hsp_phy_offset: usize,
    hsp_axi_offset: usize,
    delay_registers: [usize; 3],
    delay_values: [[u32; 3]; 3],
    tx_clock_offset: usize,
    reset_mask: u32,
    rgmii_offset: usize,
    gpio_data_offset: usize,
    gpio_direction_offset: usize,
    gpio_pin_mask: u32,
}

impl PlatformConfig {
    fn interrupt_source(&self) -> InterruptSourceInFdt {
        InterruptSourceInFdt {
            interrupt_parent: self.interrupt.0,
            interrupt: self.interrupt.1,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum PlatformError {
    InvalidDeviceTree,
    MmioUnavailable,
    RegisterAccess,
    ReadbackMismatch,
    UnsupportedController,
    InvalidPhy,
    Mdio(MdioError),
    Select(SelectError),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RegisterBank {
    Gpio,
    Rgmii,
    SysCrg,
    Hsp,
    Gmac(u8),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LinkSpeed {
    Mbps10,
    Mbps100,
    Mbps1000,
}

impl LinkSpeed {
    const fn from_mbps(speed_mbps: u16) -> Option<Self> {
        match speed_mbps {
            10 => Some(Self::Mbps10),
            100 => Some(Self::Mbps100),
            1000 => Some(Self::Mbps1000),
            _ => None,
        }
    }
}

trait PlatformRegisters {
    fn read(&mut self, bank: RegisterBank, offset: usize) -> Result<u32, PlatformError>;
    fn write(&mut self, bank: RegisterBank, offset: usize, value: u32)
    -> Result<(), PlatformError>;
    fn write_readback(
        &mut self,
        bank: RegisterBank,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError>;
    fn wait_reset_pulse(&mut self) -> Result<(), PlatformError>;
}

fn expected_fields(alias_index: u8) -> Option<PortFields> {
    let common = |alias_index| PortFields {
        alias_index,
        enabled: true,
        compatible: true,
        controller_id: Some(u32::from(alias_index)),
        mmio: Some((0, 0)),
        interrupt_parent: Some(16),
        interrupt: None,
        dma_noncoherent: true,
        iommu_absent: true,
        dma_ranges_absent: true,
        clock_names_valid: true,
        clock_cells: None,
        reset_name_valid: true,
        reset_cells: None,
        phy_mode_valid: true,
        phy_address: Some(0),
        mac_address: None,
        hsp_cells: None,
        hsp_provider_valid: true,
        syscrg_cells: Some([18, 0x148, 0x14c]),
        syscrg_provider_valid: true,
        delay_registers: None,
        delay_1000m: None,
        delay_100m: None,
        delay_10m: Some([0, 0, 0]),
        rgmii_select: None,
        rgmii_provider_valid: true,
        reset_gpio: None,
        gpio_provider_valid: true,
        axi_valid: true,
    };
    match alias_index {
        0 => Some(PortFields {
            mmio: Some((0x5040_0000, 0x1_0000)),
            interrupt: Some(61),
            clock_cells: Some([3, 550, 3, 551, 3, 552]),
            reset_cells: Some([20, 7, 0x0400_0000]),
            mac_address: Some([0x00, 0x48, 0x54, 0x71, 0x00, 0x47]),
            hsp_cells: Some([22, 0x1030, 0x100, 0x108]),
            delay_registers: Some([0x114, 0x118, 0x11c]),
            delay_1000m: Some([0x2323_2323, 0x800c_8023, 0x0c0c_0c0c]),
            delay_100m: Some([0x5050_5050, 0x803f_8050, 0x3f3f_3f3f]),
            rgmii_select: Some([26, 0x290, 3]),
            reset_gpio: Some([25, 30, 1]),
            ..common(0)
        }),
        1 => Some(PortFields {
            mmio: Some((0x5041_0000, 0x1_0000)),
            interrupt: Some(70),
            clock_cells: Some([3, 550, 3, 551, 3, 553]),
            reset_cells: Some([20, 7, 0x0200_0000]),
            mac_address: Some([0x00, 0x48, 0x54, 0x71, 0x00, 0x48]),
            hsp_cells: Some([22, 0x1034, 0x200, 0x208]),
            delay_registers: Some([0x214, 0x218, 0x21c]),
            delay_1000m: Some([0x2525_2525, 0x8026_8025, 0x2626_2626]),
            delay_100m: Some([0x4848_4848, 0x8058_8048, 0x5858_5858]),
            rgmii_select: Some([26, 0x294, 3]),
            reset_gpio: Some([29, 16, 1]),
            ..common(1)
        }),
        _ => None,
    }
}

fn validate_port(fields: PortFields) -> Result<PlatformConfig, PlatformError> {
    if Some(fields) != expected_fields(fields.alias_index) {
        return Err(PlatformError::InvalidDeviceTree);
    }
    let alias = usize::from(fields.alias_index);
    let (mmio_start, mmio_size) = fields.mmio.unwrap();
    let reset_gpio = fields.reset_gpio.unwrap();
    let gpio_port = match reset_gpio[0] {
        29 => 0,
        25 => 2,
        _ => return Err(PlatformError::InvalidDeviceTree),
    };
    let gpio_data_offset = gpio_port * 0x0c;
    Ok(PlatformConfig {
        alias_index: fields.alias_index,
        controller_id: fields.controller_id.unwrap() as u8,
        mmio_range: mmio_start..mmio_start + mmio_size,
        interrupt: (fields.interrupt_parent.unwrap(), fields.interrupt.unwrap()),
        clock_cells: fields.clock_cells.unwrap(),
        reset_cells: fields.reset_cells.unwrap(),
        phy_address: fields.phy_address.unwrap(),
        mac_address: fields.mac_address.unwrap(),
        hsp_phy_offset: fields.hsp_cells.unwrap()[2] as usize,
        hsp_axi_offset: fields.hsp_cells.unwrap()[3] as usize,
        delay_registers: fields.delay_registers.unwrap().map(|value| value as usize),
        delay_values: [
            fields.delay_10m.unwrap(),
            fields.delay_100m.unwrap(),
            fields.delay_1000m.unwrap(),
        ],
        tx_clock_offset: 0x158 + alias * 4,
        reset_mask: fields.reset_cells.unwrap()[2],
        rgmii_offset: fields.rgmii_select.unwrap()[1] as usize,
        gpio_data_offset,
        gpio_direction_offset: gpio_data_offset + 4,
        gpio_pin_mask: 1 << reset_gpio[1],
    })
}

pub(super) fn hardware_probe_requested() -> bool {
    aster_cmdline::HARDWARE_PROBE.get().is_some()
}

fn parse_gmac_probe(value: &str) -> Option<u8> {
    match value {
        "gmac-0" => Some(0),
        "gmac-1" => Some(1),
        _ => None,
    }
}

pub(super) fn hardware_probe_target() -> Option<u8> {
    aster_cmdline::HARDWARE_PROBE
        .get()
        .and_then(|value| parse_gmac_probe(value))
}

pub(super) fn gmac_phy_diagnostic_requested() -> bool {
    aster_cmdline::GMAC_DIAGNOSTIC
        .get()
        .is_some_and(|value| value == "phy")
}

pub(super) const fn probe_error_reason(error: PlatformError) -> &'static str {
    match error {
        PlatformError::InvalidDeviceTree => "invalid-device-tree",
        PlatformError::MmioUnavailable => "mmio-unavailable",
        PlatformError::RegisterAccess => "register-access",
        PlatformError::ReadbackMismatch => "readback-mismatch",
        PlatformError::UnsupportedController => "unsupported-controller",
        PlatformError::InvalidPhy => "invalid-phy",
        PlatformError::Mdio(_) => "mdio-error",
        PlatformError::Select(_) => "select-error",
    }
}

fn validate_probe_value(offset: RegisterOffset, value: u32) -> Result<(), PlatformError> {
    if value == u32::MAX {
        return Err(PlatformError::ReadbackMismatch);
    }
    if offset == MAC_VERSION && value & 0xff != u32::from(EIC7700_DWMAC_VERSION) {
        return Err(PlatformError::ReadbackMismatch);
    }
    Ok(())
}

fn validate_ports(fields: [PortFields; 2]) -> Result<[PlatformConfig; 2], PlatformError> {
    if fields[0].alias_index != 0 || fields[1].alias_index != 1 {
        return Err(PlatformError::InvalidDeviceTree);
    }
    Ok([validate_port(fields[0])?, validate_port(fields[1])?])
}

fn cells<const N: usize>(node: FdtNode<'_, '_>, name: &str) -> Option<[u32; N]> {
    let property = node.property(name)?;
    let (chunks, remainder) = property.value.as_chunks::<4>();
    if chunks.len() != N || !remainder.is_empty() {
        return None;
    }
    Some(array::from_fn(|index| u32::from_be_bytes(chunks[index])))
}

fn exact_text(node: FdtNode<'_, '_>, name: &str, expected: &[u8]) -> bool {
    node.property(name)
        .is_some_and(|property| property.value == expected)
}

fn provider_has_range(tree: &Fdt<'_>, phandle: u32, start: usize, size: usize) -> bool {
    tree.find_phandle(phandle)
        .and_then(|node| node.reg())
        .is_some_and(|mut regions| {
            let Some(region) = regions.next() else {
                return false;
            };
            regions.next().is_none()
                && region.starting_address as usize == start
                && region.size == Some(size)
        })
}

fn node_has_range(tree: &Fdt<'_>, path: &str, start: usize, size: usize) -> bool {
    tree.find_node(path)
        .and_then(|node| node.reg())
        .is_some_and(|mut regions| {
            let Some(region) = regions.next() else {
                return false;
            };
            regions.next().is_none()
                && region.starting_address as usize == start
                && region.size == Some(size)
        })
}

fn axi_is_exact(tree: &Fdt<'_>, node: FdtNode<'_, '_>) -> bool {
    let Some([phandle]) = cells::<1>(node, "snps,axi-config") else {
        return false;
    };
    let Some(axi) = tree.find_phandle(phandle) else {
        return false;
    };
    cells::<7>(axi, "snps,blen") == Some([0, 0, 0, 0, 16, 8, 4])
        && cells::<1>(axi, "snps,rd_osr_lmt") == Some([2])
        && cells::<1>(axi, "snps,wr_osr_lmt") == Some([2])
        && cells::<1>(axi, "snps,lpi_en") == Some([0])
}

fn mac_address(node: FdtNode<'_, '_>) -> Option<[u8; 6]> {
    for name in ["local-mac-address", "mac-address"] {
        let Some(property) = node.property(name) else {
            continue;
        };
        let Ok(address) = <[u8; 6]>::try_from(property.value) else {
            return None;
        };
        return Some(address);
    }
    None
}

fn fields_from_node(tree: &Fdt<'_>, alias_index: u8, node: FdtNode<'_, '_>) -> PortFields {
    let hsp_cells = cells::<4>(node, "eswin,hsp_sp_csr");
    let syscrg_cells = cells::<3>(node, "eswin,syscrg_csr");
    let rgmii_select = cells::<3>(node, "eswin,rgmiisel");
    let reset_gpio = cells::<3>(node, "rst-gpios");
    let mmio = node.reg().and_then(|mut regions| {
        let first = regions.next()?;
        if regions.next().is_some() {
            return None;
        }
        Some((first.starting_address as usize, first.size?))
    });
    PortFields {
        alias_index,
        enabled: status_enabled(node.property("status").map(|property| property.value)),
        compatible: exact_text(node, "compatible", COMPATIBLE),
        controller_id: cells::<1>(node, "id").map(|cells| cells[0]),
        mmio,
        interrupt_parent: cells::<1>(node, "interrupt-parent").map(|cells| cells[0]),
        interrupt: cells::<1>(node, "interrupts").map(|cells| cells[0]),
        dma_noncoherent: node.property("dma-noncoherent").is_some(),
        iommu_absent: node.property("iommus").is_none(),
        dma_ranges_absent: node.property("dma-ranges").is_none(),
        clock_names_valid: exact_text(node, "clock-names", CLOCK_NAMES),
        clock_cells: cells(node, "clocks"),
        reset_name_valid: exact_text(node, "reset-names", RESET_NAME),
        reset_cells: cells(node, "resets"),
        phy_mode_valid: exact_text(node, "phy-mode", PHY_MODE),
        phy_address: cells::<1>(node, "eswin,phyaddr")
            .map(|cells| u8::try_from(cells[0]).ok())
            .unwrap_or(Some(0)),
        mac_address: mac_address(node),
        hsp_cells,
        hsp_provider_valid: hsp_cells
            .is_some_and(|cells| provider_has_range(tree, cells[0], HSP_START, HSP_SIZE)),
        syscrg_cells,
        syscrg_provider_valid: syscrg_cells
            .is_some_and(|cells| provider_has_range(tree, cells[0], SYSCRG_START, SYSCRG_SIZE)),
        delay_registers: cells(node, "eswin,dly_hsp_reg"),
        delay_1000m: cells(node, "dly-param-1000m"),
        delay_100m: cells(node, "dly-param-100m"),
        delay_10m: cells(node, "dly-param-10m"),
        rgmii_select,
        rgmii_provider_valid: rgmii_select
            .is_some_and(|cells| provider_has_range(tree, cells[0], PINCTRL_START, PINCTRL_SIZE)),
        reset_gpio,
        gpio_provider_valid: reset_gpio.is_some_and(|gpio_cells| {
            let expected_port = match gpio_cells[0] {
                29 => 0,
                25 => 2,
                _ => return false,
            };
            node_has_range(tree, "/soc/gpio@51600000", GPIO_START, GPIO_SIZE)
                && tree
                    .find_phandle(gpio_cells[0])
                    .is_some_and(|provider| cells::<1>(provider, "reg") == Some([expected_port]))
        }),
        axi_valid: axi_is_exact(tree, node),
    }
}

/// Interpret the Devicetree status property according to the DT binding:
/// absent, `"ok"`, and `"okay"` mean enabled; every other value disables the
/// node.  Keeping this as a small pure function makes the discovery gate
/// testable without touching MMIO.
fn status_enabled(status: Option<&[u8]>) -> bool {
    match status {
        None => true,
        Some(value) => value == b"ok\0" || value == b"okay\0",
    }
}

fn validate_discovered_ports(
    fields: [PortFields; 2],
) -> Result<Option<[PlatformConfig; 2]>, PlatformError> {
    // The normal datapath requires a coherent pair of EIC7700 ports.  If the
    // firmware gates either node, skip the whole component before acquiring
    // clocks, reset GPIOs, or GMAC MMIO.  A disabled node is a policy choice,
    // not a malformed device tree.
    if fields.iter().any(|field| !field.enabled) {
        return Ok(None);
    }
    validate_ports(fields).map(Some)
}

fn ancestors_status_enabled(tree: &Fdt<'_>, path: &str) -> bool {
    if !path.starts_with('/') {
        return false;
    }
    let mut current = path;
    while let Some((parent, _)) = current.rsplit_once('/') {
        if parent.is_empty() {
            return true;
        }
        let Some(node) = tree.find_node(parent) else {
            return false;
        };
        if !status_enabled(node.property("status").map(|property| property.value)) {
            return false;
        }
        current = parent;
    }
    true
}

fn discover_configs() -> Result<Option<[PlatformConfig; 2]>, PlatformError> {
    let tree = DEVICE_TREE.get().ok_or(PlatformError::InvalidDeviceTree)?;
    discover_configs_from_tree(tree)
}

fn discover_configs_from_tree(
    tree: &Fdt<'_>,
) -> Result<Option<[PlatformConfig; 2]>, PlatformError> {
    let compatible_count = tree
        .all_nodes()
        .filter(|node| exact_text(*node, "compatible", COMPATIBLE))
        .count();
    if compatible_count == 0 {
        return Ok(None);
    }
    if compatible_count != 2 {
        return Err(PlatformError::InvalidDeviceTree);
    }
    let node0 = tree
        .find_node(PORT_PATHS[0])
        .ok_or(PlatformError::InvalidDeviceTree)?;
    let node1 = tree
        .find_node(PORT_PATHS[1])
        .ok_or(PlatformError::InvalidDeviceTree)?;
    if PORT_PATHS
        .iter()
        .any(|path| !ancestors_status_enabled(tree, path))
    {
        return Ok(None);
    }
    let fields = [
        fields_from_node(tree, 0, node0),
        fields_from_node(tree, 1, node1),
    ];
    validate_discovered_ports(fields)
}

fn update_bits(
    registers: &mut dyn PlatformRegisters,
    bank: RegisterBank,
    offset: usize,
    clear: u32,
    set: u32,
) -> Result<(), PlatformError> {
    let old = registers.read(bank, offset)?;
    registers.write_readback(bank, offset, (old & !clear) | set)
}

fn clock_divisor(speed: LinkSpeed) -> u32 {
    match speed {
        LinkSpeed::Mbps1000 => 2,
        LinkSpeed::Mbps100 => 10,
        LinkSpeed::Mbps10 => 100,
    }
}

fn program_platform(
    registers: &mut dyn PlatformRegisters,
    config: &PlatformConfig,
    speed: LinkSpeed,
) -> Result<(), PlatformError> {
    update_bits(
        registers,
        RegisterBank::Gpio,
        config.gpio_data_offset,
        0,
        config.gpio_pin_mask,
    )?;
    update_bits(
        registers,
        RegisterBank::Gpio,
        config.gpio_direction_offset,
        0,
        config.gpio_pin_mask,
    )?;
    registers.write_readback(RegisterBank::Rgmii, config.rgmii_offset, 3)?;
    update_bits(
        registers,
        RegisterBank::SysCrg,
        0x148,
        0,
        HSP_ACLK_ENABLE | HSP_ACLK_DIVISOR,
    )?;
    registers.write_readback(RegisterBank::SysCrg, 0x14c, HSP_CFG_ENABLE)?;
    update_bits(
        registers,
        RegisterBank::Hsp,
        config.hsp_phy_offset,
        0,
        ETH_TX_CLOCK_SELECT | ETH_PHY_RGMII_SELECT,
    )?;
    registers.write_readback(RegisterBank::Hsp, config.hsp_axi_offset, ETH_AXI_REQUEST)?;

    let old_clock = registers.read(RegisterBank::SysCrg, config.tx_clock_offset)?;
    registers.write_readback(
        RegisterBank::SysCrg,
        config.tx_clock_offset,
        old_clock & !TX_CLOCK_GATE,
    )?;
    let new_clock = (old_clock & !(TX_CLOCK_PARENT_MASK | TX_CLOCK_DIVISOR_MASK | TX_CLOCK_GATE))
        | (clock_divisor(speed) << 4)
        | TX_CLOCK_GATE;
    registers.write_readback(RegisterBank::SysCrg, config.tx_clock_offset, new_clock)?;

    let delay_index = match speed {
        LinkSpeed::Mbps10 => 0,
        LinkSpeed::Mbps100 => 1,
        LinkSpeed::Mbps1000 => 2,
    };
    for index in 0..3 {
        registers.write_readback(
            RegisterBank::Hsp,
            config.delay_registers[index],
            config.delay_values[delay_index][index],
        )?;
    }

    let old_reset = registers.read(RegisterBank::SysCrg, SYS_RESET_OFFSET)?;
    let asserted = old_reset & !config.reset_mask;
    registers.write_readback(RegisterBank::SysCrg, SYS_RESET_OFFSET, asserted)?;
    registers.wait_reset_pulse()?;
    registers.write_readback(
        RegisterBank::SysCrg,
        SYS_RESET_OFFSET,
        asserted | config.reset_mask,
    )
}

struct MmioRegisters {
    gmac: [IoMem; 2],
    sys_clock: IoMem,
    sys_reset: IoMem,
    hsp: IoMem,
    rgmii: IoMem,
    gpio: IoMem,
}

impl MmioRegisters {
    fn acquire(configs: &[PlatformConfig; 2]) -> Result<Self, PlatformError> {
        let acquire = |range| IoMem::acquire(range).map_err(|_| PlatformError::MmioUnavailable);
        Ok(Self {
            gmac: [
                acquire(configs[0].mmio_range.clone())?,
                acquire(configs[1].mmio_range.clone())?,
            ],
            sys_clock: acquire(SYSCRG_START + SYS_CLOCK_START..SYSCRG_START + SYS_CLOCK_END)?,
            sys_reset: acquire(
                SYSCRG_START + SYS_RESET_OFFSET..SYSCRG_START + SYS_RESET_OFFSET + 4,
            )?,
            hsp: acquire(HSP_START + HSP_WINDOW_START..HSP_START + HSP_WINDOW_END)?,
            rgmii: acquire(PINCTRL_START + RGMII_WINDOW_START..PINCTRL_START + RGMII_WINDOW_END)?,
            gpio: acquire(GPIO_START..GPIO_START + GPIO_WINDOW_END)?,
        })
    }

    fn resolve(&self, bank: RegisterBank, offset: usize) -> Result<(&IoMem, usize), PlatformError> {
        match bank {
            RegisterBank::Gpio if offset < GPIO_WINDOW_END => Ok((&self.gpio, offset)),
            RegisterBank::Rgmii if (RGMII_WINDOW_START..RGMII_WINDOW_END).contains(&offset) => {
                Ok((&self.rgmii, offset - RGMII_WINDOW_START))
            }
            RegisterBank::SysCrg if (SYS_CLOCK_START..SYS_CLOCK_END).contains(&offset) => {
                Ok((&self.sys_clock, offset - SYS_CLOCK_START))
            }
            RegisterBank::SysCrg if offset == SYS_RESET_OFFSET => Ok((&self.sys_reset, 0)),
            RegisterBank::Hsp if (HSP_WINDOW_START..HSP_WINDOW_END).contains(&offset) => {
                Ok((&self.hsp, offset - HSP_WINDOW_START))
            }
            RegisterBank::Gmac(index) if usize::from(index) < self.gmac.len() => {
                Ok((&self.gmac[usize::from(index)], offset))
            }
            _ => Err(PlatformError::RegisterAccess),
        }
    }
}

impl PlatformRegisters for MmioRegisters {
    fn read(&mut self, bank: RegisterBank, offset: usize) -> Result<u32, PlatformError> {
        let (mmio, relative) = self.resolve(bank, offset)?;
        mmio.read_once(relative)
            .map_err(|_| PlatformError::RegisterAccess)
    }

    fn write(
        &mut self,
        bank: RegisterBank,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError> {
        let (mmio, relative) = self.resolve(bank, offset)?;
        mmio.write_once(relative, &value)
            .map_err(|_| PlatformError::RegisterAccess)
    }

    fn write_readback(
        &mut self,
        bank: RegisterBank,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError> {
        self.write(bank, offset, value)?;
        if self.read(bank, offset)? != value {
            return Err(PlatformError::ReadbackMismatch);
        }
        Ok(())
    }

    fn wait_reset_pulse(&mut self) -> Result<(), PlatformError> {
        let deadline = aster_time::read_monotonic_time()
            .checked_add(Duration::from_micros(15))
            .ok_or(PlatformError::RegisterAccess)?;
        while aster_time::read_monotonic_time() < deadline {
            spin_loop();
        }
        Ok(())
    }
}

fn validate_controllers(registers: &mut dyn PlatformRegisters) -> Result<[u8; 2], PlatformError> {
    let mut versions = [0; 2];
    for alias in [0, 1] {
        let version =
            registers.read(RegisterBank::Gmac(alias), MAC_VERSION.offset() as usize)? as u8;
        if version != EIC7700_DWMAC_VERSION {
            return Err(PlatformError::UnsupportedController);
        }
        versions[usize::from(alias)] = version;
    }
    Ok(versions)
}

fn now_nanoseconds() -> u64 {
    u64::try_from(aster_time::read_monotonic_time().as_nanos()).unwrap_or(u64::MAX)
}

fn wait_mdio_idle(mmio: &IoMem, deadline: Deadline) -> Result<(), MdioError> {
    loop {
        let address: u32 = mmio
            .read_once(MAC_MDIO_ADDRESS.offset() as usize)
            .map_err(|_| MdioError::BusFault)?;
        if address & MDIO_BUSY == 0 {
            return Ok(());
        }
        if now_nanoseconds() >= deadline.as_nanoseconds() {
            return Err(MdioError::TimedOut);
        }
        spin_loop();
    }
}

struct PortMdio<'a> {
    mmio: &'a IoMem,
}

impl MdioBus for PortMdio<'_> {
    fn read(
        &mut self,
        phy_address: u8,
        register: u8,
        deadline: Deadline,
    ) -> Result<u16, MdioError> {
        if phy_address > 31 || register > 31 {
            return Err(MdioError::InvalidAddress);
        }
        wait_mdio_idle(self.mmio, deadline)?;
        self.mmio
            .write_once(MAC_MDIO_DATA.offset() as usize, &0u32)
            .map_err(|_| MdioError::BusFault)?;
        let command = (u32::from(phy_address) << MDIO_PHY_SHIFT)
            | (u32::from(register) << MDIO_REGISTER_SHIFT)
            | MDIO_CLOCK_150_250MHZ
            | MDIO_READ
            | MDIO_BUSY;
        self.mmio
            .write_once(MAC_MDIO_ADDRESS.offset() as usize, &command)
            .map_err(|_| MdioError::BusFault)?;
        wait_mdio_idle(self.mmio, deadline)?;
        self.mmio
            .read_once::<u32>(MAC_MDIO_DATA.offset() as usize)
            .map(|value| value as u16)
            .map_err(|_| MdioError::BusFault)
    }

    fn write(
        &mut self,
        phy_address: u8,
        register: u8,
        value: u16,
        deadline: Deadline,
    ) -> Result<(), MdioError> {
        if phy_address > 31 || register > 31 {
            return Err(MdioError::InvalidAddress);
        }
        wait_mdio_idle(self.mmio, deadline)?;
        self.mmio
            .write_once(MAC_MDIO_DATA.offset() as usize, &u32::from(value))
            .map_err(|_| MdioError::BusFault)?;
        let command = (u32::from(phy_address) << MDIO_PHY_SHIFT)
            | (u32::from(register) << MDIO_REGISTER_SHIFT)
            | MDIO_CLOCK_150_250MHZ
            | MDIO_WRITE
            | MDIO_BUSY;
        self.mmio
            .write_once(MAC_MDIO_ADDRESS.offset() as usize, &command)
            .map_err(|_| MdioError::BusFault)?;
        wait_mdio_idle(self.mmio, deadline)
    }
}

struct PlatformClock;

impl MonotonicClock for PlatformClock {
    fn now_nanoseconds(&self) -> u64 {
        now_nanoseconds()
    }

    fn wait_until(&mut self, target_nanoseconds: u64) {
        while now_nanoseconds() < target_nanoseconds {
            spin_loop();
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub(super) struct SelectedPortInfo {
    pub alias_index: u8,
    pub version: u8,
    pub interrupt_source: InterruptSourceInFdt,
    pub mac_address: [u8; 6],
    pub link_state: LinkState,
}

pub(super) struct MegrezPlatform {
    configs: [PlatformConfig; 2],
    registers: MmioRegisters,
    versions: [u8; 2],
}

impl MegrezPlatform {
    pub fn select_linked(&mut self) -> Result<SelectedPortInfo, PlatformError> {
        let [gmac0, gmac1] = &self.registers.gmac;
        let mut mdio0 = PortMdio { mmio: gmac0 };
        let mut mdio1 = PortMdio { mmio: gmac1 };
        let mut candidates = [
            PortCandidate::new(0, self.configs[0].phy_address, &mut mdio0),
            PortCandidate::new(1, self.configs[1].phy_address, &mut mdio1),
        ];
        let selected =
            select_linked_port(&mut candidates, &mut PlatformClock, Duration::from_secs(3))
                .map_err(PlatformError::Select)?;
        let alias = usize::from(selected.alias_index());
        let speed = LinkSpeed::from_mbps(selected.link_state().speed_mbps())
            .ok_or(PlatformError::InvalidPhy)?;
        program_platform(&mut self.registers, &self.configs[alias], speed)?;
        Ok(SelectedPortInfo {
            alias_index: selected.alias_index(),
            version: self.versions[alias],
            interrupt_source: self.configs[alias].interrupt_source(),
            mac_address: self.configs[alias].mac_address,
            link_state: selected.link_state(),
        })
    }

    pub fn read_gmac(&self, alias_index: u8, offset: usize) -> Result<u32, PlatformError> {
        self.registers
            .gmac
            .get(usize::from(alias_index))
            .ok_or(PlatformError::RegisterAccess)?
            .read_once(offset)
            .map_err(|_| PlatformError::RegisterAccess)
    }

    pub fn write_gmac(
        &self,
        alias_index: u8,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError> {
        self.registers
            .gmac
            .get(usize::from(alias_index))
            .ok_or(PlatformError::RegisterAccess)?
            .write_once(offset, &value)
            .map_err(|_| PlatformError::RegisterAccess)
    }
}

pub(super) fn prepare() -> Result<Option<MegrezPlatform>, PlatformError> {
    let Some(configs) = discover_configs()? else {
        return Ok(None);
    };
    let mut registers = MmioRegisters::acquire(&configs)?;
    let initial_speed = LinkSpeed::from_mbps(1000).unwrap();
    for config in &configs {
        program_platform(&mut registers, config, initial_speed)?;
    }
    let versions = validate_controllers(&mut registers)?;
    for config in &configs {
        let interrupt_source = config.interrupt_source();
        let deadline = Deadline::from_nanoseconds(
            now_nanoseconds()
                .checked_add(10_000_000)
                .ok_or(PlatformError::Mdio(MdioError::TimedOut))?,
        );
        let mut mdio = PortMdio {
            mmio: &registers.gmac[usize::from(config.alias_index)],
        };
        let id1 = mdio
            .read(config.phy_address, PHY_ID1, deadline)
            .map_err(PlatformError::Mdio)?;
        let id2 = mdio
            .read(config.phy_address, PHY_ID2, deadline)
            .map_err(PlatformError::Mdio)?;
        if matches!((id1, id2), (0, 0) | (u16::MAX, u16::MAX)) {
            return Err(PlatformError::InvalidPhy);
        }
        ostd::info!(
            "prepared GMAC{} mmio={:#x?} irq={}:{} mac={:02x?} phy={:04x}:{:04x}",
            config.controller_id,
            config.mmio_range,
            interrupt_source.interrupt_parent,
            interrupt_source.interrupt,
            config.mac_address,
            id1,
            id2,
        );
    }
    let platform = MegrezPlatform {
        configs,
        registers,
        versions,
    };
    debug_assert_eq!(platform.configs.len(), platform.registers.gmac.len());
    Ok(Some(platform))
}

/// Reuse the existing platform sequence without entering the PHY/MDIO/DMA
/// path used by normal network registration.
fn probe_platform(
    registers: &mut dyn PlatformRegisters,
    config: &PlatformConfig,
) -> Result<(), PlatformError> {
    program_platform(registers, config, LinkSpeed::Mbps1000)
}

fn probe_registers(
    registers: &mut dyn PlatformRegisters,
    config: &PlatformConfig,
) -> Result<(), PlatformError> {
    let device = config.alias_index;
    for offset in GMAC_PROBE_OFFSETS {
        ostd::early_println!(
            "ASTERINAS_PROBE_ATTEMPT device=gmac-{} offset={:#06x}",
            device,
            offset.offset(),
        );
        let value = registers.read(RegisterBank::Gmac(device), offset.offset() as usize)?;
        ostd::early_println!(
            "ASTERINAS_PROBE_READ device=gmac-{} offset={:#06x} value={:#010x}",
            device,
            offset.offset(),
            value,
        );
        validate_probe_value(offset, value)?;
    }
    Ok(())
}

/// Counts every operation performed by the bounded hardware probe.
///
/// The probe is allowed to write platform glue registers, but it must not
/// write a GMAC register or enter the PHY/MDIO/DMA datapath.  Keeping the
/// audit at the trait boundary makes hidden accesses visible in the same
/// transcript as the fixed register reads.
struct ProbeAudit<'a> {
    inner: &'a mut dyn PlatformRegisters,
    reads: usize,
    writes: usize,
    gmac_reads: usize,
    gmac_writes: usize,
    md_io: usize,
    dma: usize,
    tx: usize,
    rx: usize,
    irq: usize,
}

impl<'a> ProbeAudit<'a> {
    fn new(inner: &'a mut dyn PlatformRegisters) -> Self {
        Self {
            inner,
            reads: 0,
            writes: 0,
            gmac_reads: 0,
            gmac_writes: 0,
            md_io: 0,
            dma: 0,
            tx: 0,
            rx: 0,
            irq: 0,
        }
    }

    fn record_read(&mut self, bank: RegisterBank, _offset: usize) {
        self.reads += 1;
        if matches!(bank, RegisterBank::Gmac(_)) {
            self.gmac_reads += 1;
        }
    }

    fn record_write(&mut self, bank: RegisterBank, offset: usize) {
        self.writes += 1;
        let RegisterBank::Gmac(_) = bank else {
            return;
        };
        self.gmac_writes += 1;
        if matches!(offset, 0x0200 | 0x0204) {
            self.md_io += 1;
        } else if (0x1000..0x1200).contains(&offset) {
            self.dma += 1;
            if matches!(offset, 0x1104 | 0x1110 | 0x1114 | 0x1120 | 0x112c) {
                self.tx += 1;
            }
            if matches!(offset, 0x1108 | 0x1118 | 0x111c | 0x1128 | 0x1130) {
                self.rx += 1;
            }
        }
    }

    fn emit(&self, device: u8) {
        ostd::early_println!(
            "ASTERINAS_PROBE_ACCESS device=gmac-{} reads={} writes={} \
             gmac_reads={} gmac_writes={} mdio={} dma={} tx={} rx={} irq={}",
            device,
            self.reads,
            self.writes,
            self.gmac_reads,
            self.gmac_writes,
            self.md_io,
            self.dma,
            self.tx,
            self.rx,
            self.irq,
        );
    }
}

impl PlatformRegisters for ProbeAudit<'_> {
    fn read(&mut self, bank: RegisterBank, offset: usize) -> Result<u32, PlatformError> {
        self.record_read(bank, offset);
        self.inner.read(bank, offset)
    }

    fn write(
        &mut self,
        bank: RegisterBank,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError> {
        self.record_write(bank, offset);
        if matches!(bank, RegisterBank::Gmac(_)) {
            return Err(PlatformError::RegisterAccess);
        }
        self.inner.write(bank, offset, value)
    }

    fn write_readback(
        &mut self,
        bank: RegisterBank,
        offset: usize,
        value: u32,
    ) -> Result<(), PlatformError> {
        self.record_write(bank, offset);
        if matches!(bank, RegisterBank::Gmac(_)) {
            return Err(PlatformError::RegisterAccess);
        }
        self.record_read(bank, offset);
        self.inner.write_readback(bank, offset, value)
    }

    fn wait_reset_pulse(&mut self) -> Result<(), PlatformError> {
        self.inner.wait_reset_pulse()
    }
}

/// Probe one GMAC after enabling only its platform clock/reset/glue.
pub(super) fn probe_read_only(alias: u8) -> Result<(), PlatformError> {
    let device = match alias {
        0 | 1 => alias,
        _ => return Err(PlatformError::InvalidDeviceTree),
    };
    ostd::early_println!("ASTERINAS_PROBE_BEGIN device=gmac-{}", device);
    let Some(configs) = discover_configs()? else {
        return Err(PlatformError::InvalidDeviceTree);
    };
    let config = &configs[usize::from(device)];
    ostd::early_println!(
        "ASTERINAS_PROBE_RESOURCE device=gmac-{} base={:#010x} size={:#x} irq={}",
        device,
        config.mmio_range.start,
        config.mmio_range.end - config.mmio_range.start,
        config.interrupt.1,
    );
    ostd::early_println!(
        "ASTERINAS_PROBE_DT device=gmac-{} clocks={:?} resets={:?}",
        device,
        config.clock_cells,
        config.reset_cells,
    );
    ostd::early_println!(
        "ASTERINAS_PROBE_PHASE device=gmac-{} phase=mmio-map",
        device,
    );
    let mut registers = MmioRegisters::acquire(&configs)?;
    let mut audit = ProbeAudit::new(&mut registers);
    ostd::early_println!(
        "ASTERINAS_PROBE_PHASE device=gmac-{} phase=platform-init",
        device,
    );
    let result = (|| {
        probe_platform(&mut audit, config)?;
        ostd::early_println!(
            "ASTERINAS_PROBE_PHASE device=gmac-{} phase=mmio-read",
            device,
        );
        probe_registers(&mut audit, config)
    })();
    audit.emit(device);
    result?;
    ostd::early_println!("ASTERINAS_PROBE_PASS device=gmac-{}", device);
    ostd::early_println!("ASTERINAS_PROBE_DONE device=gmac-{} status=pass", device);
    Ok(())
}

/// Initialize the EIC7700 GMAC platform and report PHY state without starting
/// the network data path.  `prepare` only acquires MMIO, programs the required
/// clock/reset/glue registers, validates the MAC blocks, and performs bounded
/// Clause 22 reads.  We deliberately do not call `device::register`, so this
/// diagnostic cannot allocate DMA rings, enable interrupts, transmit packets,
/// or register a socket-facing network device.
pub(super) fn phy_diagnostic() -> Result<(), PlatformError> {
    let Some(mut platform) = prepare()? else {
        return Err(PlatformError::InvalidDeviceTree);
    };
    let selected = platform.select_linked()?;
    ostd::early_println!(
        "ASTERINAS_GMAC_PHY_READY alias={} version={:#04x} link=up speed={} duplex={} mac={:02x?}",
        selected.alias_index,
        selected.version,
        selected.link_state.speed_mbps(),
        selected.link_state.is_full_duplex(),
        selected.mac_address,
    );
    ostd::early_println!("ASTERINAS_GMAC_PHY_DONE status=pass");
    Ok(())
}

#[cfg(ktest)]
mod tests {
    extern crate alloc;

    use alloc::vec::Vec;

    use ostd::prelude::ktest;

    use super::*;

    fn frozen_port(alias_index: u8) -> PortFields {
        match alias_index {
            0 => PortFields {
                alias_index,
                enabled: true,
                compatible: true,
                controller_id: Some(0),
                mmio: Some((0x5040_0000, 0x1_0000)),
                interrupt_parent: Some(16),
                interrupt: Some(61),
                dma_noncoherent: true,
                iommu_absent: true,
                dma_ranges_absent: true,
                clock_names_valid: true,
                clock_cells: Some([3, 550, 3, 551, 3, 552]),
                reset_name_valid: true,
                reset_cells: Some([20, 7, 0x0400_0000]),
                phy_mode_valid: true,
                phy_address: Some(0),
                mac_address: Some([0x00, 0x48, 0x54, 0x71, 0x00, 0x47]),
                hsp_cells: Some([22, 0x1030, 0x100, 0x108]),
                hsp_provider_valid: true,
                syscrg_cells: Some([18, 0x148, 0x14c]),
                syscrg_provider_valid: true,
                delay_registers: Some([0x114, 0x118, 0x11c]),
                delay_1000m: Some([0x2323_2323, 0x800c_8023, 0x0c0c_0c0c]),
                delay_100m: Some([0x5050_5050, 0x803f_8050, 0x3f3f_3f3f]),
                delay_10m: Some([0, 0, 0]),
                rgmii_select: Some([26, 0x290, 3]),
                rgmii_provider_valid: true,
                reset_gpio: Some([25, 30, 1]),
                gpio_provider_valid: true,
                axi_valid: true,
            },
            1 => PortFields {
                alias_index,
                enabled: true,
                compatible: true,
                controller_id: Some(1),
                mmio: Some((0x5041_0000, 0x1_0000)),
                interrupt_parent: Some(16),
                interrupt: Some(70),
                dma_noncoherent: true,
                iommu_absent: true,
                dma_ranges_absent: true,
                clock_names_valid: true,
                clock_cells: Some([3, 550, 3, 551, 3, 553]),
                reset_name_valid: true,
                reset_cells: Some([20, 7, 0x0200_0000]),
                phy_mode_valid: true,
                phy_address: Some(0),
                mac_address: Some([0x00, 0x48, 0x54, 0x71, 0x00, 0x48]),
                hsp_cells: Some([22, 0x1034, 0x200, 0x208]),
                hsp_provider_valid: true,
                syscrg_cells: Some([18, 0x148, 0x14c]),
                syscrg_provider_valid: true,
                delay_registers: Some([0x214, 0x218, 0x21c]),
                delay_1000m: Some([0x2525_2525, 0x8026_8025, 0x2626_2626]),
                delay_100m: Some([0x4848_4848, 0x8058_8048, 0x5858_5858]),
                delay_10m: Some([0, 0, 0]),
                rgmii_select: Some([26, 0x294, 3]),
                rgmii_provider_valid: true,
                reset_gpio: Some([29, 16, 1]),
                gpio_provider_valid: true,
                axi_valid: true,
            },
            _ => unreachable!(),
        }
    }

    #[ktest]
    fn accepts_exact_frozen_megrez_ports() {
        let ports = validate_ports([frozen_port(0), frozen_port(1)]).unwrap();

        assert_eq!(ports[0].mmio_range, 0x5040_0000..0x5041_0000);
        assert_eq!(ports[1].mmio_range, 0x5041_0000..0x5042_0000);
        assert_eq!(ports[0].interrupt, (16, 61));
        assert_eq!(ports[1].interrupt, (16, 70));
        assert_eq!(ports[1].mac_address, [0x00, 0x48, 0x54, 0x71, 0x00, 0x48]);
    }

    #[ktest]
    fn follows_standard_devicetree_status_defaults() {
        assert!(status_enabled(None));
        assert!(status_enabled(Some(b"ok\0")));
        assert!(status_enabled(Some(b"okay\0")));
        assert!(!status_enabled(Some(b"disabled\0")));
        assert!(!status_enabled(Some(b"broken")));
    }

    #[ktest]
    fn skips_disabled_ports_before_mmio_validation() {
        let mut disabled = frozen_port(0);
        disabled.enabled = false;

        assert_eq!(
            validate_discovered_ports([disabled, frozen_port(1)]),
            Ok(None)
        );
    }

    #[ktest]
    fn discovery_skips_disabled_nodes_in_real_device_trees() {
        // These fixtures intentionally omit clocks, resets, PHYs, and IRQs.
        // Disabled nodes must be skipped before that resource validation.
        for bytes in [
            include_bytes!("fixtures/ports-disabled.dtb").as_slice(),
            include_bytes!("fixtures/port-disabled.dtb").as_slice(),
            include_bytes!("fixtures/soc-disabled.dtb").as_slice(),
        ] {
            let tree = Fdt::new(bytes).unwrap();
            assert_eq!(discover_configs_from_tree(&tree), Ok(None));
        }
    }

    #[ktest]
    fn discovery_validates_resources_when_nodes_are_enabled() {
        // Positive control for the status gate: the same incomplete resources
        // must be rejected when firmware permits both controllers.
        let tree = Fdt::new(include_bytes!("fixtures/ports-enabled.dtb")).unwrap();
        assert_eq!(
            discover_configs_from_tree(&tree),
            Err(PlatformError::InvalidDeviceTree)
        );
    }

    #[ktest]
    fn hardware_probe_selector_accepts_only_one_fixed_gmac_target() {
        assert_eq!(parse_gmac_probe("gmac-0"), Some(0));
        assert_eq!(parse_gmac_probe("gmac-1"), Some(1));
        assert_eq!(parse_gmac_probe("dwc3-0"), None);
        assert_eq!(parse_gmac_probe("gmac-0,gmac-1"), None);
    }

    #[ktest]
    fn hardware_probe_reads_only_the_fixed_register_whitelist() {
        assert_eq!(
            GMAC_PROBE_OFFSETS.map(RegisterOffset::offset),
            [0x0000, 0x00b0, 0x0110, 0x0120, 0x1000, 0x1008]
        );
    }

    #[ktest]
    fn hardware_probe_errors_have_stable_machine_reasons() {
        assert_eq!(
            probe_error_reason(PlatformError::RegisterAccess),
            "register-access"
        );
        assert_eq!(
            probe_error_reason(PlatformError::MmioUnavailable),
            "mmio-unavailable"
        );
        assert_eq!(
            probe_error_reason(PlatformError::InvalidDeviceTree),
            "invalid-device-tree"
        );
    }

    #[ktest]
    fn hardware_probe_rejects_gated_and_wrong_version_values() {
        assert_eq!(
            validate_probe_value(MAC_CONFIGURATION, u32::MAX),
            Err(PlatformError::ReadbackMismatch)
        );
        assert_eq!(validate_probe_value(MAC_VERSION, 0x1052), Ok(()));
        assert_eq!(
            validate_probe_value(MAC_VERSION, 0x1051),
            Err(PlatformError::ReadbackMismatch)
        );
    }

    #[ktest]
    fn rejects_missing_or_drifted_resources_without_fallbacks() {
        let mut cases = Vec::new();
        let mut missing_mac = frozen_port(0);
        missing_mac.mac_address = None;
        cases.push(missing_mac);
        let mut coherent = frozen_port(0);
        coherent.dma_noncoherent = false;
        cases.push(coherent);
        let mut translated_by_iommu = frozen_port(0);
        translated_by_iommu.iommu_absent = false;
        cases.push(translated_by_iommu);
        let mut translated_by_dma_ranges = frozen_port(1);
        translated_by_dma_ranges.dma_ranges_absent = false;
        cases.push(translated_by_dma_ranges);
        let mut wrong_irq = frozen_port(1);
        wrong_irq.interrupt = Some(61);
        cases.push(wrong_irq);
        let mut wrong_delay = frozen_port(1);
        wrong_delay.delay_1000m = Some([0; 3]);
        cases.push(wrong_delay);

        for fields in cases {
            assert_eq!(validate_port(fields), Err(PlatformError::InvalidDeviceTree));
        }
    }

    #[ktest]
    fn requires_documented_dwmac_5_20_on_both_ports() {
        let mut exact = VersionRegisters([0x52, 0x52]);
        assert_eq!(validate_controllers(&mut exact).unwrap(), [0x52, 0x52]);

        for versions in [[0x51, 0x52], [0x52, 0x53], [0x52, 0x51]] {
            let mut registers = VersionRegisters(versions);
            assert_eq!(
                validate_controllers(&mut registers),
                Err(PlatformError::UnsupportedController)
            );
        }
    }

    #[ktest]
    fn official_platform_sequence_is_exact_and_read_back() {
        let port = validate_port(frozen_port(1)).unwrap();
        let mut registers = FakeRegisters::default();

        program_platform(&mut registers, &port, LinkSpeed::Mbps1000).unwrap();

        assert_eq!(
            registers.writes,
            [
                (RegisterBank::Gpio, 0x00, 1 << 16),
                (RegisterBank::Gpio, 0x04, 1 << 16),
                (RegisterBank::Rgmii, 0x294, 3),
                (RegisterBank::SysCrg, 0x148, 0x8000_0020),
                (RegisterBank::SysCrg, 0x14c, 0xc000_0000),
                (RegisterBank::Hsp, 0x200, 0x0001_0001),
                (RegisterBank::Hsp, 0x208, 1),
                (RegisterBank::SysCrg, 0x15c, 0),
                (RegisterBank::SysCrg, 0x15c, 0x21),
                (RegisterBank::Hsp, 0x214, 0x2525_2525),
                (RegisterBank::Hsp, 0x218, 0x8026_8025),
                (RegisterBank::Hsp, 0x21c, 0x2626_2626),
                (RegisterBank::SysCrg, 0x41c, 0),
                (RegisterBank::SysCrg, 0x41c, 0x0200_0000),
            ]
        );
        assert_eq!(registers.readbacks, registers.writes);
    }

    #[ktest]
    fn hardware_probe_programs_selected_platform_before_gmac_reads() {
        let port = validate_port(frozen_port(1)).unwrap();
        let mut registers = ProbeRegisters::default();

        probe_platform(&mut registers, &port).unwrap();
        probe_registers(&mut registers, &port).unwrap();

        assert!(registers.platform_ready);
        assert_eq!(registers.gmac_reads, GMAC_PROBE_OFFSETS.len());
        assert!(
            registers
                .events
                .iter()
                .position(|event| *event == ProbeEvent::PlatformReady)
                .unwrap()
                < registers
                    .events
                    .iter()
                    .position(|event| *event == ProbeEvent::FirstGmacRead)
                    .unwrap()
        );
    }

    #[ktest]
    fn hardware_probe_audit_counts_accesses_and_forbids_datapath_writes() {
        let port = validate_port(frozen_port(1)).unwrap();
        let mut registers = ProbeRegisters::default();
        let mut audit = ProbeAudit::new(&mut registers);

        probe_platform(&mut audit, &port).unwrap();
        probe_registers(&mut audit, &port).unwrap();

        assert_eq!(audit.gmac_reads, GMAC_PROBE_OFFSETS.len());
        assert_eq!(audit.gmac_writes, 0);
        assert_eq!(audit.md_io, 0);
        assert_eq!(audit.dma, 0);
        assert_eq!(audit.tx, 0);
        assert_eq!(audit.rx, 0);
        assert_eq!(audit.irq, 0);
        assert!(audit.writes > 0);

        assert_eq!(
            audit.write(RegisterBank::Gmac(1), DMA_MODE.offset() as usize, 1),
            Err(PlatformError::RegisterAccess)
        );
        assert_eq!(audit.gmac_writes, 1);
        assert_eq!(audit.dma, 1);
    }

    #[derive(Default)]
    struct FakeRegisters {
        writes: Vec<(RegisterBank, usize, u32)>,
        readbacks: Vec<(RegisterBank, usize, u32)>,
    }

    struct VersionRegisters([u8; 2]);

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum ProbeEvent {
        PlatformReady,
        FirstGmacRead,
    }

    #[derive(Default)]
    struct ProbeRegisters {
        platform_ready: bool,
        gmac_reads: usize,
        events: Vec<ProbeEvent>,
        reset_asserted: bool,
    }

    impl PlatformRegisters for ProbeRegisters {
        fn read(&mut self, bank: RegisterBank, offset: usize) -> Result<u32, PlatformError> {
            match bank {
                RegisterBank::Gmac(1) => {
                    if !self.platform_ready {
                        return Err(PlatformError::RegisterAccess);
                    }
                    if self.gmac_reads == 0 {
                        self.events.push(ProbeEvent::FirstGmacRead);
                    }
                    self.gmac_reads += 1;
                    Ok(if offset == MAC_VERSION.offset() as usize {
                        u32::from(EIC7700_DWMAC_VERSION)
                    } else {
                        0
                    })
                }
                RegisterBank::SysCrg if offset == SYS_RESET_OFFSET => {
                    Ok(if self.reset_asserted { 0 } else { 0x0200_0000 })
                }
                _ => Ok(0),
            }
        }

        fn write(
            &mut self,
            bank: RegisterBank,
            offset: usize,
            value: u32,
        ) -> Result<(), PlatformError> {
            if bank == RegisterBank::SysCrg && offset == SYS_RESET_OFFSET {
                self.reset_asserted = value & 0x0200_0000 == 0;
                if !self.reset_asserted {
                    self.platform_ready = true;
                    self.events.push(ProbeEvent::PlatformReady);
                }
            }
            Ok(())
        }

        fn write_readback(
            &mut self,
            bank: RegisterBank,
            offset: usize,
            value: u32,
        ) -> Result<(), PlatformError> {
            self.write(bank, offset, value)
        }

        fn wait_reset_pulse(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
    }

    impl PlatformRegisters for VersionRegisters {
        fn read(&mut self, bank: RegisterBank, offset: usize) -> Result<u32, PlatformError> {
            if offset != MAC_VERSION.offset() as usize {
                return Err(PlatformError::RegisterAccess);
            }
            let RegisterBank::Gmac(alias) = bank else {
                return Err(PlatformError::RegisterAccess);
            };
            self.0
                .get(usize::from(alias))
                .copied()
                .map(u32::from)
                .ok_or(PlatformError::RegisterAccess)
        }

        fn write(
            &mut self,
            _bank: RegisterBank,
            _offset: usize,
            _value: u32,
        ) -> Result<(), PlatformError> {
            Err(PlatformError::RegisterAccess)
        }

        fn write_readback(
            &mut self,
            _bank: RegisterBank,
            _offset: usize,
            _value: u32,
        ) -> Result<(), PlatformError> {
            Err(PlatformError::RegisterAccess)
        }

        fn wait_reset_pulse(&mut self) -> Result<(), PlatformError> {
            Err(PlatformError::RegisterAccess)
        }
    }

    impl PlatformRegisters for FakeRegisters {
        fn read(&mut self, _bank: RegisterBank, _offset: usize) -> Result<u32, PlatformError> {
            Ok(0)
        }

        fn write_readback(
            &mut self,
            bank: RegisterBank,
            offset: usize,
            value: u32,
        ) -> Result<(), PlatformError> {
            self.writes.push((bank, offset, value));
            self.readbacks.push((bank, offset, value));
            Ok(())
        }

        fn write(
            &mut self,
            bank: RegisterBank,
            offset: usize,
            value: u32,
        ) -> Result<(), PlatformError> {
            self.writes.push((bank, offset, value));
            Ok(())
        }

        fn wait_reset_pulse(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
    }
}
