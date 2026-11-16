// SPDX-License-Identifier: MPL-2.0

use alloc::vec::Vec;
use core::ops::Range;

use aster_input::input_dev::RegisteredInputDevice;
use fdt::node::FdtNode;
use ostd::{
    arch::{
        boot::DEVICE_TREE,
        irq::{self as arch_irq, InterruptSourceInFdt},
    },
    bus::usb::{PollingUsbHidHost, UsbHidReport, UsbKeyboardError},
    io::IoMem,
    irq::IrqLine,
    mm::{HasSize, dma::DmaWindow, io::VmIoOnce},
    sync::Waiter,
};

use crate::{
    keyboard::{HidBootKeyboard, register as register_keyboard},
    mouse::{HidBootMouse, register as register_mouse},
};

mod capability;
mod pci;

const EIC7700_DWC3_MMIO_SIZE: usize = 0x1_0000;
const EIC7700_USB0_MMIO_START: usize = 0x5048_0000;
const EIC7700_USB1_MMIO_START: usize = 0x5049_0000;
const EIC7700_DRAM_START: usize = 0x8000_0000;
const EIC7700_DRAM_SIZE: usize = 0x4_0000_0000;
const PAGE_SIZE: usize = 0x1000;
const USB_HOST_SELECTOR: &str = "asterinas,usb-host";
const XHCI_CAPLENGTH: usize = 0x00;
const XHCI_HCSPARAMS1: usize = 0x04;
const XHCI_PORTSC_1: usize = 0x400;
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

/// Every DTB-selected DWC3 controller that validated, in DTB order.
///
/// Each entry gets its own MMIO mapping, interrupt line and XHCI host: the
/// controllers are independent hardware, so the board's second controller can
/// enumerate its own devices.
static HOST_CONFIGS: spin::Mutex<Vec<Dwc3HostConfig>> = spin::Mutex::new(Vec::new());

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
    // Register the PCI xHCI driver so PCI-host machines (QEMU virt, x86)
    // can enumerate the controller. The DWC3 MMIO path below is selected
    // only when the DTB chooses it via /chosen/asterinas,usb-host.
    pci::init();
    let device_tree = DEVICE_TREE.get().unwrap();
    let selector = device_tree
        .find_node("/chosen")
        .and_then(|chosen| chosen.property(USB_HOST_SELECTOR));
    let Some(selector) = selector else {
        return;
    };
    let Some(selectors) = selector.as_str() else {
        ostd::warn!("invalid '/chosen/{}' property", USB_HOST_SELECTOR);
        return;
    };

    // The property is a NUL-separated list. Each path that resolves and
    // validates becomes a host in its own right; on Megrez both DWC3
    // controllers are named, on QEMU virt none are.
    let mut configs = Vec::new();
    for selector in selectors.split('\0').filter(|path| !path.is_empty()) {
        let Some(node) = resolve_selected(Some(selector), |path| device_tree.find_node(path))
        else {
            ostd::warn!(
                "failed to resolve USB host selected by '/chosen/{}' (value {:?})",
                USB_HOST_SELECTOR,
                selector
            );
            continue;
        };
        let config = match config_from_node(node) {
            Ok(config) => config,
            Err(error) => {
                ostd::warn!("rejected selected USB host {:?}: {:?}", selector, error);
                continue;
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

        // The capability probe runs in the host worker, not here: an MMIO
        // range is never released when its `IoMem` drops, so a range probed
        // here could not be acquired again by the worker that needs it.
        configs.push(config);
    }

    ostd::info!("DWC3 USB host controllers selected: {}", configs.len());
    *HOST_CONFIGS.lock() = configs;
}

/// Returns how many USB host workers `run_host` can start.
///
/// A PCI-discovered xHCI host and the DTB-selected DWC3 hosts are exclusive:
/// the first controller the platform exposes is the one brought up, as before.
pub(super) fn host_count() -> usize {
    if pci::has_host() {
        1
    } else {
        HOST_CONFIGS.lock().len()
    }
}

/// Starts one USB host worker.
///
/// Each worker owns its controller's MMIO mapping, interrupt line and XHCI
/// host, so several controllers can run at once.
pub(super) fn run_host(index: usize) {
    if let Some(host) = pci::take_host_config() {
        ostd::info!("Using PCI xHCI host for USB HID");
        run_hid_host(host.mmio, host.dma_window, host.interrupt_source);
        return;
    }

    let config = {
        let configs = HOST_CONFIGS.lock();
        match configs.get(index) {
            Some(config) => config.clone(),
            None => return,
        }
    };

    let mmio = match IoMem::acquire(config.mmio_range.clone()) {
        Ok(mmio) => mmio,
        Err(_) => {
            ostd::warn!("failed to retain xHCI MMIO range for host {index}");
            return;
        }
    };
    match capability::probe(&mmio) {
        Ok(capabilities) => ostd::info!(
            "Host {index} xHCI {:#06x}: slots={}, ports={}, interrupters={}, AC64={}, CSZ64={}",
            capabilities.version,
            capabilities.max_slots,
            capabilities.max_ports,
            capabilities.max_interrupters,
            capabilities.addresses_64bit,
            capabilities.contexts_64byte,
        ),
        Err(error) => {
            ostd::warn!("xHCI capability probe failed for host {index}: {:?}", error);
            return;
        }
    }
    if prepare_dwc3_host(&mmio).is_err() {
        ostd::warn!("failed to select the DWC3 host role for host {index}");
        return;
    }

    ostd::info!(
        "Starting USB host {index}: mmio={:#x?}, interrupt={}:{}",
        config.mmio_range,
        config.interrupt_parent,
        config.interrupt,
    );
    run_hid_host(
        mmio,
        config.dma_window,
        InterruptSourceInFdt {
            interrupt_parent: config.interrupt_parent,
            arguments: [config.interrupt],
        },
    );
}

/// Logs the xHCI root-hub PORTSC registers (one per port) for bring-up.
fn log_port_status(mmio: &IoMem) {
    // Read the whole 32-bit capability word; bit 0-7 is CAPLENGTH.
    let Ok(cap_word) = mmio.read_once::<u32>(XHCI_CAPLENGTH) else {
        ostd::warn!("failed to read xHCI capability");
        return;
    };
    let operational = (cap_word & 0xFF) as usize;
    // MaxPorts is bits 31:24 of HCSPARAMS1 (offset 0x04), matching the
    // xHCI spec (Linux's HCS_MAX_PORTS) and QEMU's qemu-xhci.
    let Ok(hcsparams1) = mmio.read_once::<u32>(XHCI_HCSPARAMS1) else {
        ostd::warn!("failed to read xHCI HCSPARAMS1");
        return;
    };
    let max_ports = (hcsparams1 >> 24) & 0xFF;
    for i in 0..max_ports {
        let offset = operational + XHCI_PORTSC_1 + (i as usize) * 4;
        match mmio.read_once::<u32>(offset) {
            Ok(portsc) => {
                ostd::info!(
                    "xHCI PORTSC{}: connected={}, enabled={}, reset={}, speed={}",
                    i + 1,
                    portsc & 0x1,
                    (portsc >> 1) & 0x1,
                    (portsc >> 4) & 0x1,
                    (portsc >> 10) & 0xF,
                );
            }
            Err(error) => {
                ostd::warn!("xHCI PORTSC{} read failed at {offset:#x}: {error:?}", i + 1);
            }
        }
    }
}

/// Runs one xHCI host: opens its HID devices, maps its interrupt and pumps
/// completed reports until the host fails.
///
/// Both the DWC3 MMIO hosts (from `/chosen/asterinas,usb-host`) and the PCI
/// xHCI adapter call this. The controller's mapping, event handler, decoder
/// state and input devices all live in this frame, so several controllers run
/// side by side without sharing state. No polling happens while idle: the
/// interrupt wakes the loop.
pub(crate) fn run_hid_host(
    mmio: IoMem,
    dma_window: DmaWindow,
    interrupt_source: InterruptSourceInFdt,
) {
    ostd::info!(
        "Starting interrupt-driven xHCI host: mmio={:#x}, irq={}:{}",
        mmio.size(),
        interrupt_source.interrupt_parent,
        interrupt_source.arguments[0],
    );

    // Diagnostics: report the root-hub port status so a missing device
    // connection or stalled reset is visible before the discovery loop.
    log_port_status(&mmio);

    // The xHCI host requires an exclusive mapping, so it takes ownership.
    let mut host = match PollingUsbHidHost::open(mmio, dma_window) {
        Ok(host) => host,
        // A controller with nothing plugged in is not an error: the board's
        // second controller may have no boot HID device.
        Err(UsbKeyboardError::KeyboardNotFound) => {
            ostd::info!("xHCI host has no boot HID device");
            return;
        }
        Err(error) => {
            ostd::warn!("xHCI host startup failed: {:?}", error);
            return;
        }
    };

    let hid_info = host.info();
    let mut keyboard_decoder = hid_info.keyboard.map(|_| HidBootKeyboard::new());
    let mut keyboard_device: Option<RegisteredInputDevice> = None;
    let mut mouse_decoder = hid_info.mouse_layout.map(HidBootMouse::new);
    let mut mouse_device: Option<RegisteredInputDevice> = None;

    // The interrupt handler only wakes this thread; all report processing
    // runs here, in task context (device registration, evdev dispatch and tty
    // input all require it).
    let (waiter, waker) = Waiter::new_pair();
    let mut irq_line = match IrqLine::alloc() {
        Ok(line) => line,
        Err(_) => {
            ostd::warn!("failed to allocate USB IRQ line");
            return;
        }
    };
    irq_line.on_active(move |_| {
        waker.wake_up();
    });

    let irq_chip = match arch_irq::IRQ_CHIP.get() {
        Some(chip) => chip,
        None => {
            ostd::warn!("IRQ chip unavailable for USB");
            return;
        }
    };
    // The mapping must stay alive for as long as the handler is installed, so
    // this thread owns it.
    let _irq_line = match irq_chip.map_fdt_pin_to(interrupt_source, irq_line) {
        Ok(mapped) => mapped,
        Err(_) => {
            ostd::warn!("failed to map USB interrupt to PLIC");
            return;
        }
    };

    // The host disabled its global interrupt until the handler was installed.
    if host.enable_irq().is_err() {
        ostd::warn!("failed to enable the xHCI interrupt");
        return;
    }

    ostd::info!(
        "USB host loop started: keyboard={}, mouse={}",
        hid_info.keyboard.is_some(),
        hid_info.mouse.is_some(),
    );

    loop {
        loop {
            let report = match host.poll_report() {
                Ok(Some(report)) => report,
                // No report ready; the interrupt will schedule us again.
                Ok(None) => break,
                Err(error) => {
                    ostd::warn!("USB transfer stopped: {:?}", error);
                    return;
                }
            };
            match report {
                UsbHidReport::Keyboard(report) => {
                    let Some(decoder) = keyboard_decoder.as_mut() else {
                        continue;
                    };
                    let events = decoder.decode(report);
                    ostd::debug!(
                        "USB boot keyboard report {:02x?}: {} events",
                        report,
                        events.len()
                    );
                    if events.is_empty() {
                        continue;
                    }
                    let device = keyboard_device.get_or_insert_with(|| {
                        let info = hid_info
                            .keyboard
                            .expect("a keyboard report implies a boot keyboard");
                        ostd::info!(
                            "USB boot keyboard registered: {:04x}:{:04x}",
                            info.vendor_id,
                            info.product_id,
                        );
                        register_keyboard(info.vendor_id, info.product_id)
                    });
                    device.submit_events(&events);
                }
                UsbHidReport::Mouse {
                    bytes,
                    actual_length,
                } => {
                    let Some(decoder) = mouse_decoder.as_mut() else {
                        continue;
                    };
                    let events = match decoder.decode(bytes, actual_length) {
                        Ok(events) => events,
                        Err(error) => {
                            ostd::debug!("discarding a malformed mouse report: {:?}", error);
                            continue;
                        }
                    };
                    if events.is_empty() {
                        continue;
                    }
                    let device = mouse_device.get_or_insert_with(|| {
                        let info = hid_info.mouse.expect("a mouse report implies a boot mouse");
                        let layout = hid_info
                            .mouse_layout
                            .expect("a decoded report implies a known layout");
                        ostd::info!(
                            "USB boot mouse registered: {:04x}:{:04x}",
                            info.vendor_id,
                            info.product_id,
                        );
                        register_mouse(info.vendor_id, info.product_id, layout)
                    });
                    device.submit_events(&events);
                }
            }
        }
        waiter.wait();
    }
}

/// Runs the first USB host worker.
///
/// Kept for platforms whose kernel calls the older entry point.
pub(super) fn run_polling() {
    run_host(0);
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
