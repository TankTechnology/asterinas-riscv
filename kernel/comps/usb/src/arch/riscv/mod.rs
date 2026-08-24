// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use aster_input::input_dev::RegisteredInputDevice;
use fdt::node::FdtNode;
use ostd::{
    arch::{
        boot::DEVICE_TREE,
        irq::{self as arch_irq, InterruptSourceInFdt},
    },
    bus::usb::{PollingUsbKeyboard, UsbKeyboardError},
    io::IoMem,
    irq::IrqLine,
    mm::{HasSize, dma::DmaWindow, io::VmIoOnce},
    sync::{Mutex, SpinLock, Waiter},
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

static HOST_RESOURCES: SpinLock<Option<HostResources>> = SpinLock::new(None);

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

    let mut resources = HOST_RESOURCES.lock();
    if resources.is_none() {
        *resources = Some(HostResources { config, mmio });
    }
}

static KEYBOARD: Once<Mutex<PollingUsbKeyboard>> = Once::new();

struct DeferredKeyboardState {
    decoder: HidBootKeyboard,
    registered: Option<RegisteredInputDevice>,
}

struct EnabledKeyboardIrq<'a> {
    keyboard: &'a Mutex<PollingUsbKeyboard>,
}

impl<'a> EnabledKeyboardIrq<'a> {
    fn new(keyboard: &'a Mutex<PollingUsbKeyboard>) -> Result<Self, UsbKeyboardError> {
        let enable_result = keyboard.lock().enable_irq();
        if let Err(error) = enable_result {
            if let Err(disable_error) = keyboard.lock().disable_irq() {
                ostd::warn!(
                    "failed to restore disabled xHCI interrupts after enable error: {:?}",
                    disable_error
                );
            }
            return Err(error);
        }
        Ok(Self { keyboard })
    }
}

impl Drop for EnabledKeyboardIrq<'_> {
    fn drop(&mut self) {
        if let Err(error) = self.keyboard.lock().disable_irq() {
            ostd::warn!("failed to disable xHCI interrupts: {:?}", error);
        }
    }
}

fn process_deferred_keyboard(
    keyboard: &Mutex<PollingUsbKeyboard>,
    state: &mut DeferredKeyboardState,
) -> bool {
    loop {
        let (report, info) = {
            let mut keyboard = keyboard.lock();
            match keyboard.poll_report() {
                Ok(Some(report)) => (report, keyboard.info()),
                Ok(None) => return true,
                Err(error) => {
                    ostd::warn!("USB boot keyboard transfer stopped: {:?}", error);
                    return false;
                }
            }
        };
        let events = state.decoder.decode(report);
        if !events.is_empty() {
            let device = state.registered.get_or_insert_with(|| {
                ostd::info!(
                    "USB boot keyboard registered: {:04x}:{:04x}",
                    info.vendor_id,
                    info.product_id,
                );
                register(info.vendor_id, info.product_id)
            });
            device.submit_events(&events);
        }
    }
}

/// Interrupt-driven USB boot keyboard loop.
///
/// The xHCI event ring interrupt (from the DTB `interrupt-parent`/`interrupt`
/// properties) drives the keyboard: the handler wakes this task, which drains
/// the event ring and emits evdev events. No polling loop runs while the
/// keyboard is idle.
pub fn run_polling() {
    let Some(resources) = HOST_RESOURCES.lock().take() else {
        return;
    };
    if prepare_dwc3_host(&resources.mmio).is_err() {
        ostd::warn!("failed to select the DWC3 host role");
        return;
    }
    ostd::info!(
        "Starting interrupt-driven xHCI host: mmio={:#x?}, bytes={:#x}, irq={}:{}",
        resources.config.mmio_range,
        resources.mmio.size(),
        resources.config.interrupt_parent,
        resources.config.interrupt,
    );

    let keyboard = match PollingUsbKeyboard::open(resources.mmio, resources.config.dma_window) {
        Ok(keyboard) => Mutex::new(keyboard),
        Err(error) => {
            ostd::warn!("xHCI keyboard startup failed: {:?}", error);
            return;
        }
    };
    KEYBOARD.call_once(|| keyboard);
    let keyboard = KEYBOARD.get().unwrap();

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
    let interrupt_source = InterruptSourceInFdt {
        interrupt_parent: resources.config.interrupt_parent,
        interrupt: resources.config.interrupt,
    };
    let mut mapped_irq = match irq_chip.map_fdt_pin_to_masked(interrupt_source, irq_line) {
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

    // Keep this guard declared after `mapped_irq`: reverse drop order disables
    // the xHCI INTE bit before the PLIC mapping is torn down.
    let _enabled_keyboard_irq = match EnabledKeyboardIrq::new(keyboard) {
        Ok(guard) => guard,
        Err(error) => {
            ostd::warn!("failed to enable xHCI interrupts: {:?}", error);
            return;
        }
    };

    ostd::info!("USB boot keyboard interrupt-driven loop started");

    let mut state = DeferredKeyboardState {
        decoder: HidBootKeyboard::new(),
        registered: None,
    };
    loop {
        if !process_deferred_keyboard(keyboard, &mut state) {
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
    fn selects_dwc3_host_role_without_changing_other_controls() {
        let firmware_device_mode = 0x0019_2004;

        assert_eq!(dwc3_host_gctl(firmware_device_mode), 0x0019_1004);
        assert_eq!(
            dwc3_host_gctl(firmware_device_mode) & !DWC3_GCTL_PRTCAPDIR_MASK,
            firmware_device_mode & !DWC3_GCTL_PRTCAPDIR_MASK
        );
    }
}
