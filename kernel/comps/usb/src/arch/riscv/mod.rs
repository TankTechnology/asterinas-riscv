// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use fdt::node::FdtNode;
use ostd::{
    arch::boot::DEVICE_TREE,
    bus::usb::PollingUsbKeyboard,
    io::IoMem,
    mm::{HasSize, dma::DmaWindow, io::VmIoOnce},
    task::Task,
};
use spin::Once;

use crate::keyboard::{HidBootKeyboard, register};

mod capability;

const EIC7700_DWC3_MMIO_SIZE: usize = 0x1_0000;
const EIC7700_USB0_MMIO_START: usize = 0x5048_0000;
const EIC7700_USB1_MMIO_START: usize = 0x5049_0000;
const EIC7700_DRAM_START: usize = 0x8000_0000;
const EIC7700_DRAM_SIZE: usize = 0x4_0000_0000;
const PAGE_SIZE: usize = 0x1000;
const USB_HOST_SELECTOR: &str = "asterinas,usb-host";
const XHCI_CAPLENGTH: usize = 0x00;
const XHCI_RTSOFF: usize = 0x18;
const XHCI_USBCMD: usize = 0x00;
const XHCI_USBSTS: usize = 0x04;
const XHCI_CRCR: usize = 0x18;
const XHCI_DCBAAP: usize = 0x30;
const XHCI_CONFIG: usize = 0x38;
const XHCI_PORTSC_1: usize = 0x400;
const XHCI_INTERRUPTER_0: usize = 0x20;
const XHCI_IMAN: usize = 0x00;
const XHCI_ERSTSZ: usize = 0x08;
const XHCI_ERSTBA: usize = 0x10;
const XHCI_ERDP: usize = 0x18;
const DWC3_GCTL: usize = 0xc110;
const DWC3_GCTL_PRTCAPDIR_MASK: u32 = 0x3 << 12;
const DWC3_GCTL_PRTCAP_HOST: u32 = 0x1 << 12;

#[derive(Clone, Debug, Eq, PartialEq)]
struct Dwc3HostConfig {
    mmio_range: Range<usize>,
    interrupt_parent: u32,
    interrupt: u32,
    dma_window: DmaWindow,
}

#[derive(Debug)]
struct HostResources {
    config: Dwc3HostConfig,
    mmio: IoMem,
}

static HOST_RESOURCES: Once<HostResources> = Once::new();

fn dwc3_host_gctl(gctl: u32) -> u32 {
    (gctl & !DWC3_GCTL_PRTCAPDIR_MASK) | DWC3_GCTL_PRTCAP_HOST
}

fn prepare_dwc3_host(mmio: &IoMem) -> Result<(), ()> {
    let gctl = mmio.read_once::<u32>(DWC3_GCTL).map_err(|_| ())?;
    let host_gctl = dwc3_host_gctl(gctl);
    if host_gctl != gctl {
        mmio.write_once(DWC3_GCTL, &host_gctl).map_err(|_| ())?;
    }

    let observed = mmio.read_once::<u32>(DWC3_GCTL).map_err(|_| ())?;
    if observed & DWC3_GCTL_PRTCAPDIR_MASK != DWC3_GCTL_PRTCAP_HOST {
        return Err(());
    }
    Ok(())
}

fn log_xhci_snapshot(mmio: &IoMem) {
    let Ok(cap_length) = mmio.read_once::<u8>(XHCI_CAPLENGTH) else {
        ostd::warn!("failed to read xHCI diagnostic registers");
        return;
    };
    let Ok(runtime_offset) = mmio.read_once::<u32>(XHCI_RTSOFF) else {
        ostd::warn!("failed to read xHCI runtime offset");
        return;
    };
    let operational = usize::from(cap_length);
    let interrupter = (runtime_offset as usize & !0x1f) + XHCI_INTERRUPTER_0;
    let read_u32 = |offset| mmio.read_once::<u32>(offset).ok();
    let read_u64 = |offset| mmio.read_once::<u64>(offset).ok();

    ostd::warn!(
        "xHCI timeout snapshot: GCTL={:x?}, USBCMD={:x?}, USBSTS={:x?}, CRCR={:x?}, DCBAAP={:x?}, CONFIG={:x?}, PORTSC1={:x?}, IMAN={:x?}, ERSTSZ={:x?}, ERSTBA={:x?}, ERDP={:x?}",
        read_u32(DWC3_GCTL),
        read_u32(operational + XHCI_USBCMD),
        read_u32(operational + XHCI_USBSTS),
        read_u64(operational + XHCI_CRCR),
        read_u64(operational + XHCI_DCBAAP),
        read_u32(operational + XHCI_CONFIG),
        read_u32(operational + XHCI_PORTSC_1),
        read_u32(interrupter + XHCI_IMAN),
        read_u32(interrupter + XHCI_ERSTSZ),
        read_u64(interrupter + XHCI_ERSTBA),
        read_u64(interrupter + XHCI_ERDP),
    );
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ConfigError {
    Disabled,
    UnsupportedCompatible,
    NotHost,
    InvalidMmio,
    InvalidInterrupt,
    CoherentDma,
    InvalidDmaWindow,
}

#[derive(Clone, Copy)]
struct ConfigFields<'a> {
    status: Option<&'a str>,
    is_dwc3: bool,
    dr_mode: Option<&'a str>,
    mmio: Option<(usize, usize)>,
    interrupt_parent: Option<u32>,
    interrupt: Option<u32>,
    dma_noncoherent: bool,
    dma_ranges: Option<&'a [u8]>,
}

fn parse_dma_window(bytes: &[u8]) -> Option<DmaWindow> {
    if bytes.len() != 3 * size_of::<u64>() {
        return None;
    }
    let read_u64 = |offset| u64::from_be_bytes(bytes[offset..offset + 8].try_into().unwrap());
    let device_start = read_u64(0);
    let cpu_start = read_u64(8);
    let size = read_u64(16);

    let device_start = usize::try_from(device_start).ok()?;
    let cpu_start = usize::try_from(cpu_start).ok()?;
    let size = usize::try_from(size).ok()?;
    if !device_start.is_multiple_of(PAGE_SIZE)
        || !cpu_start.is_multiple_of(PAGE_SIZE)
        || !size.is_multiple_of(PAGE_SIZE)
    {
        return None;
    }

    DmaWindow::new(device_start, cpu_start, size)
}

fn effective_dma_window(mmio_start: usize, advertised: DmaWindow) -> Option<DmaWindow> {
    let is_eic7700_xhci = matches!(
        mmio_start,
        EIC7700_USB0_MMIO_START | EIC7700_USB1_MMIO_START
    );
    let is_eic7700_parent_window =
        advertised == DmaWindow::new(0, 0xc000_0000, 0x200_0000_0000).unwrap();
    if is_eic7700_xhci && is_eic7700_parent_window {
        // The EIC7700 U-Boot xHCI handoff programs direct DRAM addresses in
        // DCBAAP and related registers. Preserve that working device view.
        DmaWindow::new(EIC7700_DRAM_START, EIC7700_DRAM_START, EIC7700_DRAM_SIZE)
    } else {
        Some(advertised)
    }
}

fn validate_config(fields: ConfigFields<'_>) -> Result<Dwc3HostConfig, ConfigError> {
    if !matches!(fields.status, None | Some("ok" | "okay")) {
        return Err(ConfigError::Disabled);
    }
    if !fields.is_dwc3 {
        return Err(ConfigError::UnsupportedCompatible);
    }
    if fields.dr_mode != Some("host") {
        return Err(ConfigError::NotHost);
    }

    let (mmio_start, mmio_size) = fields.mmio.ok_or(ConfigError::InvalidMmio)?;
    if mmio_size != EIC7700_DWC3_MMIO_SIZE || !mmio_start.is_multiple_of(PAGE_SIZE) {
        return Err(ConfigError::InvalidMmio);
    }
    let mmio_end = mmio_start
        .checked_add(mmio_size)
        .ok_or(ConfigError::InvalidMmio)?;

    let interrupt_parent = fields
        .interrupt_parent
        .filter(|value| *value != 0)
        .ok_or(ConfigError::InvalidInterrupt)?;
    let interrupt = fields
        .interrupt
        .filter(|value| *value != 0)
        .ok_or(ConfigError::InvalidInterrupt)?;

    if !fields.dma_noncoherent {
        return Err(ConfigError::CoherentDma);
    }
    let advertised_dma_window = fields
        .dma_ranges
        .and_then(parse_dma_window)
        .ok_or(ConfigError::InvalidDmaWindow)?;
    let dma_window = effective_dma_window(mmio_start, advertised_dma_window)
        .ok_or(ConfigError::InvalidDmaWindow)?;

    Ok(Dwc3HostConfig {
        mmio_range: mmio_start..mmio_end,
        interrupt_parent,
        interrupt,
        dma_window,
    })
}

fn resolve_selected<T>(
    selector: Option<&str>,
    resolve: impl FnOnce(&str) -> Option<T>,
) -> Option<T> {
    selector
        .filter(|selector| !selector.is_empty())
        .and_then(resolve)
}

fn config_from_node(node: FdtNode<'_, '_>) -> Result<Dwc3HostConfig, ConfigError> {
    let status = match node.property("status") {
        Some(property) => Some(property.as_str().ok_or(ConfigError::Disabled)?),
        None => None,
    };
    let is_dwc3 = node
        .compatible()
        .is_some_and(|compatibles| compatibles.all().any(|value| value == "snps,dwc3"));
    let dr_mode = node
        .property("dr_mode")
        .or_else(|| node.property("dr-mode"))
        .and_then(|property| property.as_str());
    let mmio = node.reg().and_then(|mut regions| {
        let region = regions.next()?;
        Some((region.starting_address as usize, region.size?))
    });
    let interrupt_parent = node
        .property("interrupt-parent")
        .and_then(|property| property.as_usize())
        .and_then(|value| value.try_into().ok());
    let interrupt = node
        .interrupts()
        .and_then(|mut interrupts| interrupts.next())
        .and_then(|value| value.try_into().ok());
    let dma_noncoherent = node.property("dma-noncoherent").is_some();
    let dma_ranges = node.property("dma-ranges");

    validate_config(ConfigFields {
        status,
        is_dwc3,
        dr_mode,
        mmio,
        interrupt_parent,
        interrupt,
        dma_noncoherent,
        dma_ranges: dma_ranges.map(|property| property.value),
    })
}

pub(super) fn init() {
    let device_tree = DEVICE_TREE.get().unwrap();
    let selector = device_tree
        .find_node("/chosen")
        .and_then(|chosen| chosen.property(USB_HOST_SELECTOR));
    let Some(selector) = selector else {
        return;
    };
    let Some(selector) = selector.as_str() else {
        ostd::warn!("invalid '/chosen/{}' property", USB_HOST_SELECTOR);
        return;
    };
    let Some(node) = resolve_selected(Some(selector), |path| device_tree.find_node(path)) else {
        ostd::warn!(
            "failed to resolve USB host selected by '/chosen/{}'",
            USB_HOST_SELECTOR
        );
        return;
    };
    let config = match config_from_node(node) {
        Ok(config) => config,
        Err(error) => {
            ostd::warn!("rejected selected USB host: {:?}", error);
            return;
        }
    };

    ostd::info!(
        "Selected DWC3 USB host: mmio={:#x?}, interrupt={}:{}, DMA={:#x}+{:#x}->{:#x}",
        config.mmio_range,
        config.interrupt_parent,
        config.interrupt,
        config.dma_window.device_start(),
        config.dma_window.size(),
        config.dma_window.cpu_start(),
    );

    let mmio = match IoMem::acquire(config.mmio_range.clone()) {
        Ok(mmio) => mmio,
        Err(_) => {
            ostd::warn!("failed to retain selected xHCI MMIO range");
            return;
        }
    };

    let capabilities = match capability::probe(&mmio) {
        Ok(capabilities) => capabilities,
        Err(error) => {
            ostd::warn!("xHCI capability probe failed: {:?}", error);
            return;
        }
    };
    ostd::info!(
        "Detected xHCI {:#06x}: slots={}, ports={}, interrupters={}, AC64={}, CSZ64={}",
        capabilities.version,
        capabilities.max_slots,
        capabilities.max_ports,
        capabilities.max_interrupters,
        capabilities.addresses_64bit,
        capabilities.contexts_64byte,
    );

    HOST_RESOURCES.call_once(|| HostResources { config, mmio });
}

pub fn run_polling() {
    let Some(resources) = HOST_RESOURCES.get() else {
        return;
    };
    if prepare_dwc3_host(&resources.mmio).is_err() {
        ostd::warn!("failed to select the DWC3 host role");
        return;
    }
    ostd::info!(
        "Starting polling xHCI host: mmio={:#x?}, bytes={:#x}",
        resources.config.mmio_range,
        resources.mmio.size(),
    );

    let mut keyboard =
        match PollingUsbKeyboard::open(resources.mmio.clone(), resources.config.dma_window) {
            Ok(keyboard) => keyboard,
            Err(error) => {
                log_xhci_snapshot(&resources.mmio);
                ostd::warn!("polling xHCI keyboard startup failed: {:?}", error);
                return;
            }
        };
    let info = keyboard.info();
    let registered = register(info.vendor_id, info.product_id);
    ostd::info!(
        "USB boot keyboard registered: {:04x}:{:04x}, handlers={}",
        info.vendor_id,
        info.product_id,
        registered.count_handlers(),
    );

    let mut decoder = HidBootKeyboard::new();
    loop {
        match keyboard.poll_report() {
            Ok(Some(report)) => {
                let events = decoder.decode(report);
                if !events.is_empty() {
                    registered.submit_events(&events);
                }
            }
            Ok(None) => Task::yield_now(),
            Err(error) => {
                ostd::warn!("USB boot keyboard polling stopped: {:?}", error);
                return;
            }
        }
    }
}

#[cfg(ktest)]
mod tests {
    use core::cell::Cell;

    use ostd::prelude::*;

    use super::*;

    const MEGREZ_DMA_RANGES: [u8; 24] = [
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // Device 0.
        0x00, 0x00, 0x00, 0x00, 0xc0, 0x00, 0x00, 0x00, // CPU 0xc0000000.
        0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, // Size 2 TiB.
    ];

    fn valid_fields() -> ConfigFields<'static> {
        ConfigFields {
            status: Some("okay"),
            is_dwc3: true,
            dr_mode: Some("host"),
            mmio: Some((0x5049_0000, EIC7700_DWC3_MMIO_SIZE)),
            interrupt_parent: Some(7),
            interrupt: Some(86),
            dma_noncoherent: true,
            dma_ranges: Some(&MEGREZ_DMA_RANGES),
        }
    }

    #[ktest]
    fn parses_megrez_noncoherent_dma_window() {
        assert_eq!(
            parse_dma_window(&MEGREZ_DMA_RANGES),
            DmaWindow::new(0, 0xc000_0000, 0x200_0000_0000)
        );
    }

    #[ktest]
    fn validates_selected_megrez_host_controller() {
        let config = validate_config(valid_fields()).unwrap();

        assert_eq!(config.mmio_range, 0x5049_0000..0x504a_0000);
        assert_eq!(config.interrupt_parent, 7);
        assert_eq!(config.interrupt, 86);
        assert_eq!(
            config.dma_window,
            DmaWindow::new(0x8000_0000, 0x8000_0000, 0x4_0000_0000).unwrap()
        );
    }

    #[ktest]
    fn rejects_disabled_device_mode_and_implicit_coherency() {
        let mut fields = valid_fields();
        fields.status = Some("disabled");
        assert_eq!(validate_config(fields), Err(ConfigError::Disabled));

        fields = valid_fields();
        fields.dr_mode = Some("peripheral");
        assert_eq!(validate_config(fields), Err(ConfigError::NotHost));

        fields = valid_fields();
        fields.dma_noncoherent = false;
        assert_eq!(validate_config(fields), Err(ConfigError::CoherentDma));
    }

    #[ktest]
    fn rejects_unsupported_controller_and_invalid_interrupts() {
        let mut fields = valid_fields();
        fields.is_dwc3 = false;
        assert_eq!(
            validate_config(fields),
            Err(ConfigError::UnsupportedCompatible)
        );

        fields = valid_fields();
        fields.interrupt_parent = Some(0);
        assert_eq!(validate_config(fields), Err(ConfigError::InvalidInterrupt));

        fields = valid_fields();
        fields.interrupt = None;
        assert_eq!(validate_config(fields), Err(ConfigError::InvalidInterrupt));
    }

    #[ktest]
    fn rejects_malformed_or_overflowing_ranges() {
        assert_eq!(parse_dma_window(&MEGREZ_DMA_RANGES[..20]), None);

        let mut overflowing = MEGREZ_DMA_RANGES;
        overflowing[8..16].copy_from_slice(&u64::MAX.to_be_bytes());
        assert_eq!(parse_dma_window(&overflowing), None);

        let mut fields = valid_fields();
        fields.mmio = Some((usize::MAX - 0x7fff, EIC7700_DWC3_MMIO_SIZE));
        assert_eq!(validate_config(fields), Err(ConfigError::InvalidMmio));
    }

    #[ktest]
    fn resolves_only_the_explicitly_selected_controller() {
        let calls = Cell::new(0);
        let selected = resolve_selected(Some("/soc/usb1/dwc3"), |path| {
            calls.set(calls.get() + 1);
            (path == "/soc/usb1/dwc3").then_some(0x5049_0000)
        });

        assert_eq!(selected, Some(0x5049_0000));
        assert_eq!(calls.get(), 1);
        assert_eq!(resolve_selected(None, |_| Some(0x5048_0000)), None);
        assert_eq!(resolve_selected(Some(""), |_| Some(0x5048_0000)), None);
    }

    #[ktest]
    fn selects_dwc3_host_role_without_changing_other_controls() {
        let firmware_device_mode = 0x0019_2004;

        assert_eq!(dwc3_host_gctl(firmware_device_mode), 0x0019_1004);
        assert_eq!(
            dwc3_host_gctl(firmware_device_mode) & !DWC3_GCTL_PRTCAPDIR_MASK,
            firmware_device_mode & !DWC3_GCTL_PRTCAPDIR_MASK
        );
    }
}
