// SPDX-License-Identifier: MPL-2.0

use core::{
    ops::Range,
    str,
    sync::atomic::{AtomicBool, Ordering},
};

use aster_input::input_dev::RegisteredInputDevice;
use aster_pci::PciDeviceLocation;
use fdt::node::FdtNode;
use ostd::{
    arch::{
        boot::DEVICE_TREE,
        irq::{self as arch_irq, InterruptSourceInFdt},
    },
    bus::usb::{
        MouseReportLayout, PollingUsbHidHost, UsbDeviceInfo, UsbHidInfo, UsbHidReport,
        UsbKeyboardError, run_bulk_loopback_self_test,
    },
    io::IoMem,
    irq::IrqLine,
    mm::{HasSize, dma::DmaWindow, io::VmIoOnce},
    sync::{Mutex, SpinLock, Waiter},
};
use spin::Once;

use crate::{
    keyboard::{HidBootKeyboard, register as register_keyboard},
    mouse::{HidBootMouse, register as register_mouse},
};

mod capability;
mod pci;

const EIC7700_DWC3_MMIO_SIZE: usize = 0x1_0000;
const EIC7700_USB0_MMIO_START: usize = 0x5048_0000;
const EIC7700_USB1_MMIO_START: usize = 0x5049_0000;
const EIC7700_USB_WRAPPER_PATHS: [&str; 2] = ["/soc/usb0@50480000", "/soc/usb1@50490000"];
const EIC7700_DRAM_START: usize = 0x8000_0000;
const EIC7700_DRAM_SIZE: usize = 0x4_0000_0000;
const PAGE_SIZE: usize = 0x1000;
const USB_HOST_SELECTOR: &str = "asterinas,usb-host";
const DWC3_GCTL: usize = 0xc110;
const DWC3_GSNPSID: usize = 0xc120;
const DWC3_PROBE_OFFSETS: [usize; 3] = [capability::HCCPARAMS1, DWC3_GCTL, DWC3_GSNPSID];
const DWC3_GCTL_PRTCAPDIR_MASK: u32 = 0x3 << 12;
const DWC3_GCTL_PRTCAP_HOST: u32 = 0x1 << 12;
const BULK_TEST_PAYLOAD_SHA256: &str =
    "c30b5b3bb192d370a1f07db31d6a39d186b0fbd24caa5e3187e60f85be8573ff";

static USB_BULK_SELFTEST: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.usb_bulk_selftest", USB_BULK_SELFTEST);

#[derive(Clone, Debug, Eq, PartialEq)]
struct Dwc3HostConfig {
    mmio_range: Range<usize>,
    interrupt_parent: u32,
    interrupt: u32,
    dma_window: DmaWindow,
}

#[derive(Debug)]
struct HostResources {
    mmio: IoMem,
    dma_window: DmaWindow,
    interrupt_source: InterruptSourceInFdt,
}

const MAX_SELECTED_DWC3_HOSTS: usize = 2;

static DWC3_HOST_RESOURCES: SpinLock<[Option<HostResources>; MAX_SELECTED_DWC3_HOSTS]> =
    SpinLock::new([None, None]);

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

fn parse_hardware_probe(value: &str) -> Option<(usize, &'static str)> {
    match value {
        "dwc3-0" => Some((0, "dwc3-0")),
        "dwc3-1" => Some((1, "dwc3-1")),
        _ => None,
    }
}

fn hardware_probe_target() -> Option<(usize, &'static str)> {
    aster_cmdline::HARDWARE_PROBE
        .get()
        .and_then(|value| parse_hardware_probe(value))
}

fn hardware_probe_fail(device: &str, reason: &str) {
    ostd::early_println!("ASTERINAS_PROBE_FAIL device={} reason={}", device, reason);
    ostd::early_println!("ASTERINAS_PROBE_DONE device={} status=fail", device);
}

fn probe_read_only(index: usize, device: &'static str) {
    let path = match index {
        0 => "/soc/usb0@50480000/dwc3@50480000",
        1 => "/soc/usb1@50490000/dwc3@50490000",
        _ => unreachable!(),
    };
    ostd::early_println!("ASTERINAS_PROBE_BEGIN device={}", device);
    let Some(device_tree) = DEVICE_TREE.get() else {
        hardware_probe_fail(device, "missing-device-tree");
        return;
    };
    let Some(node) = device_tree.find_node(path) else {
        hardware_probe_fail(device, "missing-device-tree-node");
        return;
    };
    if !ancestors_status_enabled(device_tree, path) {
        hardware_probe_fail(device, "disabled-device-tree");
        return;
    }
    let config = match config_from_node(node) {
        Ok(config) => config,
        Err(_) => {
            hardware_probe_fail(device, "invalid-device-tree-resource");
            return;
        }
    };
    let expected_base = if index == 0 {
        EIC7700_USB0_MMIO_START
    } else {
        EIC7700_USB1_MMIO_START
    };
    if config.mmio_range != (expected_base..expected_base + EIC7700_DWC3_MMIO_SIZE) {
        hardware_probe_fail(device, "unexpected-mmio-resource");
        return;
    }
    ostd::early_println!(
        "ASTERINAS_PROBE_RESOURCE device={} base={:#010x} size={:#x} irq={}",
        device,
        expected_base,
        EIC7700_DWC3_MMIO_SIZE,
        config.interrupt,
    );
    let mmio = match IoMem::acquire(config.mmio_range) {
        Ok(mmio) => mmio,
        Err(_) => {
            hardware_probe_fail(device, "mmio-unavailable");
            return;
        }
    };
    for offset in DWC3_PROBE_OFFSETS {
        let value = match mmio.read_once::<u32>(offset) {
            Ok(value) => value,
            Err(_) => {
                hardware_probe_fail(device, "mmio-read-failed");
                return;
            }
        };
        ostd::early_println!(
            "ASTERINAS_PROBE_READ device={} offset={:#06x} value={:#010x}",
            device,
            offset,
            value,
        );
    }
    ostd::early_println!("ASTERINAS_PROBE_PASS device={}", device);
    ostd::early_println!("ASTERINAS_PROBE_DONE device={} status=pass", device);
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

fn selected_host_paths(value: &[u8]) -> Option<[Option<&str>; MAX_SELECTED_DWC3_HOSTS]> {
    let strings = value.strip_suffix(&[0])?;
    if strings.is_empty() {
        return None;
    }

    let mut paths = [None; MAX_SELECTED_DWC3_HOSTS];
    for (count, bytes) in strings.split(|byte| *byte == 0).enumerate() {
        if bytes.is_empty() || count == MAX_SELECTED_DWC3_HOSTS {
            return None;
        }
        let path = str::from_utf8(bytes).ok()?;
        // Do not let libfdt alias resolution turn a selector such as
        // `usb1` into a node below a disabled wrapper. The handoff contract
        // carries absolute node paths only.
        if !path.starts_with('/')
            || path.contains("//")
            || path
                .split('/')
                .any(|component| component == "." || component == "..")
        {
            return None;
        }
        paths[count] = Some(path);
    }
    Some(paths)
}

fn status_enabled(status: Option<&[u8]>) -> bool {
    match status {
        None => true,
        Some(value) => value == b"ok\0" || value == b"okay\0",
    }
}

/// A selected DWC3 child is not usable when an EIC7700 wrapper above it is
/// disabled.  U-Boot normally gates the wrapper node, while the child may not
/// carry its own `status` property.  Check every named ancestor before any
/// MMIO acquisition so a disabled wrapper cannot reach xHCI setup.
fn ancestors_status_enabled(tree: &fdt::Fdt<'_>, path: &str) -> bool {
    if !path.starts_with('/') {
        return false;
    }
    let mut current = path;
    while let Some((parent, _)) = current.rsplit_once('/') {
        if parent.is_empty() {
            return true;
        }
        let Some(node) = tree.find_node(parent) else {
            // A partial DT must not make a selected child look reachable.
            return false;
        };
        if !status_enabled(node.property("status").map(|property| property.value)) {
            return false;
        }
        current = parent;
    }
    true
}

fn eic7700_usb_wrappers_disabled(tree: &fdt::Fdt<'_>) -> bool {
    let wrappers = EIC7700_USB_WRAPPER_PATHS.map(|path| tree.find_node(path));
    match wrappers {
        // No EIC7700 wrapper nodes means a QEMU virt-style DT. Keep the PCI
        // fallback in that case.
        [None, None] => false,
        // A partial EIC7700 DT is unsafe to probe: fail closed before PCI/XHCI
        // registration instead of treating the missing wrapper as enabled.
        [None, Some(_)] | [Some(_), None] => true,
        [Some(first), Some(second)] => {
            if EIC7700_USB_WRAPPER_PATHS
                .iter()
                .any(|path| !ancestors_status_enabled(tree, path))
            {
                return true;
            }
            [first, second]
                .iter()
                .all(|node| !status_enabled(node.property("status").map(|property| property.value)))
        }
    }
}

fn config_from_node(node: FdtNode<'_, '_>) -> Result<Dwc3HostConfig, ConfigError> {
    // Apply the raw DT status contract before parsing any textual properties.
    // In particular, a non-NUL-terminated `okay` must not be accepted here
    // while the ancestor gate treats it as disabled.
    if !status_enabled(node.property("status").map(|property| property.value)) {
        return Err(ConfigError::Disabled);
    }
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
        status: None,
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
    if let Some((index, device)) = hardware_probe_target() {
        // Diagnostic mode deliberately bypasses PCI/xHCI setup.  The only
        // permitted accesses are the three fixed read-only registers above.
        probe_read_only(index, device);
        return;
    }
    if aster_cmdline::HARDWARE_PROBE.get().is_some() {
        // A GMAC probe owns this boot.  Do not enter PCI/xHCI setup merely
        // because this component does not own the selected device.
        return;
    }

    let device_tree = DEVICE_TREE.get().unwrap();
    if eic7700_usb_wrappers_disabled(device_tree) {
        // The physical EIC7700 minimal profile disables both wrapper nodes.
        // Return before registering the PCI xHCI driver so no PCI BAR or IRQ
        // side effect can bypass the DT gate. QEMU's virt machine has no
        // EIC7700 wrapper nodes and therefore retains its PCI fallback.
        ostd::info!("EIC7700 USB wrappers disabled; skipping USB host setup");
        return;
    }
    pci::init();

    let selector = device_tree
        .find_node("/chosen")
        .and_then(|chosen| chosen.property(USB_HOST_SELECTOR));
    let Some(selector) = selector else {
        return;
    };
    let Some(selectors) = selected_host_paths(selector.value) else {
        ostd::warn!("invalid '/chosen/{}' property", USB_HOST_SELECTOR);
        return;
    };
    let mut resources = DWC3_HOST_RESOURCES.lock();
    for (index, selector) in selectors.into_iter().flatten().enumerate() {
        let Some(node) = resolve_selected(Some(selector), |path| device_tree.find_node(path))
        else {
            ostd::warn!(
                "failed to resolve USB host {} selected by '/chosen/{}'",
                index,
                USB_HOST_SELECTOR
            );
            continue;
        };
        if !ancestors_status_enabled(device_tree, selector) {
            ostd::info!("skipping disabled USB host {} and its wrapper", selector);
            continue;
        }
        let config = match config_from_node(node) {
            Ok(config) => config,
            Err(error) => {
                ostd::warn!("rejected selected USB host {}: {:?}", index, error);
                continue;
            }
        };

        ostd::info!(
            "Selected DWC3 USB host {}: mmio={:#x?}, interrupt={}:{}, DMA={:#x}+{:#x}->{:#x}",
            index,
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
                ostd::warn!(
                    "failed to retain selected xHCI MMIO range for host {}",
                    index
                );
                continue;
            }
        };

        let capabilities = match capability::probe(&mmio) {
            Ok(capabilities) => capabilities,
            Err(error) => {
                ostd::warn!(
                    "xHCI capability probe failed for host {}: {:?}",
                    index,
                    error
                );
                continue;
            }
        };
        ostd::info!(
            "Detected xHCI {} {:#06x}: slots={}, ports={}, interrupters={}, AC64={}, CSZ64={}",
            index,
            capabilities.version,
            capabilities.max_slots,
            capabilities.max_ports,
            capabilities.max_interrupters,
            capabilities.addresses_64bit,
            capabilities.contexts_64byte,
        );

        resources[index] = Some(HostResources {
            mmio,
            dma_window: config.dma_window,
            interrupt_source: InterruptSourceInFdt {
                interrupt_parent: config.interrupt_parent,
                interrupt: config.interrupt,
            },
        });
    }
}

static HID_HOSTS: [Once<Mutex<PollingUsbHidHost>>; MAX_SELECTED_DWC3_HOSTS] =
    [Once::new(), Once::new()];

struct DeferredKeyboardState {
    decoder: HidBootKeyboard,
    registered: RegisteredInputDevice,
}

impl DeferredKeyboardState {
    fn new(info: UsbDeviceInfo) -> Self {
        ostd::info!(
            "USB boot keyboard registered: {:04x}:{:04x} bus=usb name=usb_boot_keyboard",
            info.vendor_id,
            info.product_id,
        );
        Self {
            decoder: HidBootKeyboard::new(),
            registered: register_keyboard(info.vendor_id, info.product_id),
        }
    }
}

struct DeferredMouseState {
    decoder: HidBootMouse,
    registered: RegisteredInputDevice,
}

impl DeferredMouseState {
    fn new(info: UsbDeviceInfo, layout: Option<MouseReportLayout>) -> Self {
        let layout = layout.unwrap_or(crate::mouse::BOOT_MOUSE_LAYOUT);
        ostd::info!(
            "USB boot mouse registered: {:04x}:{:04x} bus=usb name=usb_boot_mouse",
            info.vendor_id,
            info.product_id,
        );
        Self {
            decoder: HidBootMouse::new(layout),
            registered: register_mouse(info.vendor_id, info.product_id, layout),
        }
    }
}

/// Failed transfers in a row before the HID task gives up for good.
///
/// A failed transfer leaves the report queue re-armed, so a device that
/// recovers delivers its next report on its own and the run resets to zero.
/// This threshold is only for a controller that is really gone.
const HID_FAILURE_RETIREMENT_THRESHOLD: u32 = 32;

struct DeferredHidState {
    keyboard: Option<DeferredKeyboardState>,
    mouse: Option<DeferredMouseState>,
    /// Failed transfers in a row since the last delivered report.
    consecutive_failures: u32,
}

impl DeferredHidState {
    fn new(info: UsbHidInfo) -> Self {
        Self {
            keyboard: info.keyboard.map(DeferredKeyboardState::new),
            mouse: info
                .mouse
                .map(|mouse| DeferredMouseState::new(mouse, info.mouse_layout)),
            consecutive_failures: 0,
        }
    }
}

struct EnabledHidIrqs<'a> {
    host: &'a Mutex<PollingUsbHidHost>,
    pci_location: Option<PciDeviceLocation>,
}

impl<'a> EnabledHidIrqs<'a> {
    fn new(
        host: &'a Mutex<PollingUsbHidHost>,
        pci_location: Option<PciDeviceLocation>,
    ) -> Result<Self, UsbKeyboardError> {
        let enable_result = host.lock().enable_irq();
        if let Err(error) = enable_result {
            if let Err(disable_error) = host.lock().disable_irq() {
                ostd::warn!(
                    "failed to restore disabled xHCI interrupts after enable error: {:?}",
                    disable_error
                );
            }
            return Err(error);
        }
        if let Some(location) = pci_location {
            pci::set_intx_enabled(location, true);
        }
        Ok(Self { host, pci_location })
    }
}

impl Drop for EnabledHidIrqs<'_> {
    fn drop(&mut self) {
        if let Err(error) = self.host.lock().disable_irq() {
            ostd::warn!("failed to disable xHCI interrupts: {:?}", error);
        }
        if let Some(location) = self.pci_location {
            pci::set_intx_enabled(location, false);
        }
    }
}

fn process_deferred_hid(host: &Mutex<PollingUsbHidHost>, state: &mut DeferredHidState) -> bool {
    loop {
        let report = {
            let mut host = host.lock();
            match host.poll_report() {
                Ok(Some(report)) => {
                    state.consecutive_failures = 0;
                    report
                }
                Ok(None) => return true,
                Err(error) => {
                    state.consecutive_failures += 1;
                    if state.consecutive_failures < HID_FAILURE_RETIREMENT_THRESHOLD {
                        // Only the keyboard reports its transfer errors up to
                        // here, but the mouse lives in this same task. Ending
                        // the task over one of them takes the other device
                        // down too, and the report queue has already re-armed
                        // the failed transfer, so stay up and let the next
                        // interrupt try again.
                        ostd::warn!(
                            "USB HID transfer failed, retrying ({}/{}): {:?}",
                            state.consecutive_failures,
                            HID_FAILURE_RETIREMENT_THRESHOLD,
                            error,
                        );
                        return true;
                    }
                    ostd::warn!(
                        "USB HID retired after {} consecutive transfer failures: {:?}",
                        state.consecutive_failures,
                        error,
                    );
                    return false;
                }
            }
        };
        match report {
            UsbHidReport::Keyboard(report) => {
                let Some(keyboard) = &mut state.keyboard else {
                    continue;
                };
                let events = keyboard.decoder.decode(report);
                if !events.is_empty() {
                    keyboard.registered.submit_events(&events);
                }
            }
            UsbHidReport::Mouse {
                bytes,
                actual_length,
            } => {
                let Some(mouse) = &mut state.mouse else {
                    continue;
                };
                match mouse.decoder.decode(bytes, actual_length) {
                    Ok(events) if !events.is_empty() => mouse.registered.submit_events(&events),
                    Ok(_) => {}
                    Err(error) => ostd::warn!("invalid USB boot mouse report: {:?}", error),
                }
            }
        }
    }
}

/// Interrupt-driven USB HID boot keyboard and mouse loop.
///
/// The xHCI event ring interrupt (from the DTB `interrupt-parent`/`interrupt`
/// properties) drives HID input: the handler wakes this task, which drains
/// the event ring and emits evdev events. No polling loop runs while the
/// keyboard is idle.
pub fn run_polling() {
    if aster_cmdline::HARDWARE_PROBE.get().is_some() {
        return;
    }
    if let Some(host) = pci::take_selected_host() {
        let location = host.location;
        ostd::info!(
            "Starting PCI xHCI host: 0000:{:02x}:{:02x}.{}, bytes={:#x}, irq={}:{}",
            location.bus,
            location.device,
            location.function,
            host.mmio.size(),
            host.interrupt_source.interrupt_parent,
            host.interrupt_source.interrupt,
        );
        run_hid_interrupt_driven(
            HostResources {
                mmio: host.mmio,
                dma_window: host.dma_window,
                interrupt_source: host.interrupt_source,
            },
            Some(location),
            &HID_HOSTS[0],
        );
        return;
    }

    run_dwc3_hid_worker(0);
}

/// Runs the second firmware-selected DWC3/xHCI HID worker, when present.
pub fn run_polling_secondary() {
    if aster_cmdline::HARDWARE_PROBE.get().is_some() {
        return;
    }
    run_dwc3_hid_worker(1);
}

fn run_dwc3_hid_worker(index: usize) {
    let bulk_selftest = USB_BULK_SELFTEST.load(Ordering::Relaxed);
    if bulk_selftest && index != 0 {
        return;
    }
    let Some(resources) = DWC3_HOST_RESOURCES.lock()[index].take() else {
        return;
    };
    if prepare_dwc3_host(&resources.mmio).is_err() {
        ostd::warn!("failed to select DWC3 host {} role", index);
        return;
    }
    ostd::info!(
        "Starting DWC3 xHCI host {}: bytes={:#x}, irq={}:{}",
        index,
        resources.mmio.size(),
        resources.interrupt_source.interrupt_parent,
        resources.interrupt_source.interrupt,
    );
    if bulk_selftest {
        match run_bulk_loopback_self_test(resources.mmio, resources.dma_window) {
            Ok(result) => ostd::info!(
                "ASTERINAS_USB_BULK_SELFTEST result=pass controller={} payload_bytes={} \
                 requested_in_bytes={} sha256={} resets={} out_transfers={} in_transfers={} \
                 out_bytes={} in_bytes={}",
                index,
                result.payload_bytes,
                result.requested_in_bytes,
                BULK_TEST_PAYLOAD_SHA256,
                result.resets,
                result.out_transfers,
                result.in_transfers,
                result.out_bytes,
                result.in_bytes,
            ),
            Err(error) => ostd::error!(
                "ASTERINAS_USB_BULK_SELFTEST result=fail controller={} error={:?}",
                index,
                error,
            ),
        }
        return;
    }
    run_hid_interrupt_driven(resources, None, &HID_HOSTS[index]);
}

fn run_hid_interrupt_driven(
    resources: HostResources,
    pci_location: Option<PciDeviceLocation>,
    host_slot: &'static Once<Mutex<PollingUsbHidHost>>,
) {
    let host = match PollingUsbHidHost::open(resources.mmio, resources.dma_window) {
        Ok(host) => Mutex::new(host),
        Err(error) => {
            ostd::warn!("xHCI HID startup failed: {:?}", error);
            return;
        }
    };
    let mut state = DeferredHidState::new(host.lock().info());
    host_slot.call_once(|| host);
    let host = host_slot.get().unwrap();

    let (waiter, waker) = Waiter::new_pair();

    // Map the xHCI event-ring interrupt while both the PLIC source and the
    // controller IRQ are disabled.
    let irq_line = match IrqLine::alloc() {
        Ok(line) => line,
        Err(_) => {
            ostd::warn!("failed to allocate USB IRQ line");
            return;
        }
    };

    let irq_chip = match arch_irq::IRQ_CHIP.get() {
        Some(chip) => chip,
        None => {
            ostd::warn!("IRQ chip unavailable for USB");
            return;
        }
    };
    let mut mapped_irq = match irq_chip.map_fdt_pin_to_masked(resources.interrupt_source, irq_line)
    {
        Ok(mapped) => mapped,
        Err(_) => {
            ostd::warn!("failed to map USB interrupt to PLIC");
            return;
        }
    };
    if mapped_irq
        .on_active_and_mask(move |_| {
            waker.wake_up();
        })
        .is_err()
    {
        ostd::warn!("failed to register exclusive USB IRQ callback");
        return;
    }

    // Keep this composite guard after `mapped_irq`: its explicit Drop disables
    // controller INTE first and PCI INTx second, before the PLIC mapping drops.
    let _enabled_hid_irqs = match EnabledHidIrqs::new(host, pci_location) {
        Ok(guard) => guard,
        Err(error) => {
            ostd::warn!("failed to enable xHCI interrupts: {:?}", error);
            return;
        }
    };

    ostd::info!("USB HID interrupt-driven loop started");

    loop {
        if !process_deferred_hid(host, &mut state) {
            return;
        }
        if mapped_irq.rearm().is_err() {
            ostd::warn!("USB IRQ rearm rejected by mapping state");
            return;
        }
        waiter.wait();
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
    fn parses_one_or_two_explicit_host_selectors() {
        assert_eq!(
            selected_host_paths(b"/soc/usb0/dwc3\0"),
            Some([Some("/soc/usb0/dwc3"), None])
        );
        assert_eq!(
            selected_host_paths(b"/soc/usb0/dwc3\0/soc/usb1/dwc3\0"),
            Some([Some("/soc/usb0/dwc3"), Some("/soc/usb1/dwc3")])
        );
        assert_eq!(selected_host_paths(b"usb1\0"), None);
        assert_eq!(selected_host_paths(b"/soc/usb1/../dwc3\0"), None);
        assert_eq!(selected_host_paths(b""), None);
        assert_eq!(selected_host_paths(b"/soc/usb0/dwc3"), None);
        assert_eq!(selected_host_paths(b"/soc/usb0/dwc3\0\0"), None);
        assert_eq!(
            selected_host_paths(b"/soc/usb0/dwc3\0/soc/usb1/dwc3\0/third\0"),
            None
        );
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
    fn skips_disabled_wrappers_in_real_device_trees() {
        for bytes in [
            include_bytes!("fixtures/wrappers-disabled.dtb").as_slice(),
            include_bytes!("fixtures/soc-disabled.dtb").as_slice(),
        ] {
            let tree = fdt::Fdt::new(bytes).unwrap();
            assert!(eic7700_usb_wrappers_disabled(&tree));
            for path in [
                "/soc/usb0@50480000/dwc3@50480000",
                "/soc/usb1@50490000/dwc3@50490000",
            ] {
                assert!(tree.find_node(path).is_some());
                assert!(!ancestors_status_enabled(&tree, path));
            }
        }
    }

    #[ktest]
    fn rejects_partial_wrappers_but_retains_virt_pci_fallback() {
        let partial = fdt::Fdt::new(include_bytes!("fixtures/wrapper-missing.dtb")).unwrap();
        assert!(eic7700_usb_wrappers_disabled(&partial));
        let absent = fdt::Fdt::new(include_bytes!("fixtures/wrappers-absent.dtb")).unwrap();
        assert!(!eic7700_usb_wrappers_disabled(&absent));
    }

    #[ktest]
    fn resolves_enabled_children_and_rejects_disabled_children() {
        let tree = fdt::Fdt::new(include_bytes!("fixtures/wrappers-enabled.dtb")).unwrap();
        assert!(!eic7700_usb_wrappers_disabled(&tree));
        let path = "/soc/usb1@50490000/dwc3@50490000";
        assert!(ancestors_status_enabled(&tree, path));
        let config = config_from_node(tree.find_node(path).unwrap()).unwrap();
        assert_eq!(config.mmio_range, 0x5049_0000..0x504a_0000);
        assert_eq!(config.interrupt, 86);

        for path in ["/disabled-child", "/malformed-status-child"] {
            assert_eq!(
                config_from_node(tree.find_node(path).unwrap()),
                Err(ConfigError::Disabled)
            );
        }
    }

    #[ktest]
    fn rejects_alias_selector_that_resolves_below_disabled_wrapper() {
        let tree = fdt::Fdt::new(include_bytes!("fixtures/wrappers-disabled.dtb")).unwrap();
        let selector = tree
            .find_node("/chosen")
            .unwrap()
            .property(USB_HOST_SELECTOR)
            .unwrap();
        // libfdt can resolve the alias, so selector validation must reject it
        // before path-based ancestor checks can be bypassed.
        assert!(tree.find_node("usb1").is_some());
        assert_eq!(selected_host_paths(selector.value), None);
        assert!(!ancestors_status_enabled(&tree, "usb1"));
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

    #[ktest]
    fn hardware_probe_selector_accepts_only_one_fixed_dwc3_target() {
        assert_eq!(parse_hardware_probe("dwc3-0"), Some((0, "dwc3-0")));
        assert_eq!(parse_hardware_probe("dwc3-1"), Some((1, "dwc3-1")));
        assert_eq!(parse_hardware_probe("gmac-0"), None);
        assert_eq!(parse_hardware_probe("dwc3-0,dwc3-1"), None);
    }

    #[ktest]
    fn hardware_probe_reads_only_the_fixed_register_whitelist() {
        assert_eq!(DWC3_PROBE_OFFSETS, [0x10, 0xc110, 0xc120]);
    }

    #[ktest]
    fn usb_keyboard_and_mouse_are_registered_before_the_first_report() {
        component::init_all(
            component::InitStage::Bootstrap,
            component::parse_metadata!(),
        )
        .unwrap();
        let before = aster_input::count_devices();
        let state = DeferredHidState::new(UsbHidInfo {
            keyboard: Some(UsbDeviceInfo {
                vendor_id: 0x0627,
                product_id: 0x0001,
            }),
            mouse: Some(UsbDeviceInfo {
                vendor_id: 0x0627,
                product_id: 0x0002,
            }),
            mouse_layout: None,
        });

        assert_eq!(aster_input::count_devices(), before + 2);
        assert_eq!(
            state
                .keyboard
                .as_ref()
                .unwrap()
                .registered
                .device()
                .id()
                .bustype(),
            aster_input::input_dev::InputId::BUS_USB
        );
        assert_eq!(
            state
                .mouse
                .as_ref()
                .unwrap()
                .registered
                .device()
                .id()
                .bustype(),
            aster_input::input_dev::InputId::BUS_USB
        );
        drop(state);
        assert_eq!(aster_input::count_devices(), before);
    }
}
