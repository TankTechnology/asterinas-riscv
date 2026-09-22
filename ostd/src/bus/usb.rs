// SPDX-License-Identifier: MPL-2.0

//! Polling USB host support for firmware-configured RISC-V controllers.

mod report_queue;

use alloc::boxed::Box;
use core::{
    mem::{self, ManuallyDrop},
    pin::pin,
    ptr::NonNull,
    task::{Context, Poll, Waker},
    time::Duration,
};

use crab_usb::{Device, DeviceInfo, Endpoint, EventHandler, KernelOp, USBHost};
use usb_if::{
    descriptor::{ConfigurationDescriptor, EndpointType},
    endpoint::{RequestId, TransferCompletion, TransferRequest},
    host::ControlSetup,
    transfer::{Direction, Recipient, Request, RequestType},
};

use self::report_queue::{BootKeyboardReportQueue, BootReportQueue, ReportEndpoint};
use crate::{
    arch,
    io::IoMem,
    mm::{
        CachePolicy, HasSize, VmIoOnce,
        dma::{DmaWindow, UsbKernelOp},
    },
    task::Task,
};

const HOST_OPERATION_TIMEOUT: Duration = Duration::from_secs(5);
const KEYBOARD_DISCOVERY_TIMEOUT: Duration = Duration::from_secs(30);
const BOOT_KEYBOARD_REPORT_LEN: usize = 8;
/// The boot mouse report: buttons, X displacement, Y displacement.
const BOOT_MOUSE_REPORT_LEN: usize = 3;
/// The mouse transfer buffer. A parsed layout is refused above this length, so
/// the buffer always covers the report the device describes.
const MOUSE_REPORT_LEN: usize = 8;
const XHCI_MIN_CAPLENGTH: usize = 0x20;
const XHCI_CAPABILITY_ACCESSORS_LEN: usize = 0x24;
const XHCI_OPERATIONAL_PORT_REGISTERS_OFFSET: usize = 0x400;
const XHCI_PORT_REGISTER_SET_LEN: usize = 0x10;
const XHCI_RUNTIME_INTERRUPTER_REGISTERS_OFFSET: usize = 0x20;
const XHCI_INTERRUPTER_REGISTER_SET_LEN: usize = 0x20;
const XHCI_EXTENDED_CAPABILITY_HEADER_LEN: usize = size_of::<u32>();

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DriveError {
    Timeout,
}

/// A failure while starting or polling a USB boot keyboard.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsbKeyboardError {
    /// The MMIO mapping does not cover the xHCI register layout advertised by the controller.
    InvalidMmio,
    /// CrabUSB could not create the xHCI host.
    HostCreate,
    /// xHCI initialization failed.
    HostInit,
    /// Enabling or disabling xHCI interrupts failed.
    Interrupt,
    /// A controller operation exceeded its deadline.
    Timeout(UsbKeyboardStage),
    /// USB device enumeration failed.
    Enumeration,
    /// No boot-protocol USB keyboard or mouse appeared before the discovery deadline.
    KeyboardNotFound,
    /// CrabUSB could not open the selected keyboard.
    DeviceOpen,
    /// The boot-keyboard interface could not be claimed.
    ClaimInterface,
    /// The device rejected HID boot protocol.
    SetBootProtocol,
    /// The interrupt-IN endpoint could not be opened.
    EndpointOpen,
    /// Submitting or completing the interrupt-IN transfer failed.
    Transfer,
    /// A completed boot-keyboard report did not contain exactly eight bytes.
    InvalidReportLength,
}

/// The USB keyboard startup stage that owns a bounded controller operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsbKeyboardStage {
    /// Initialize and start the xHCI host controller.
    HostInit,
    /// Discover and enumerate attached USB devices.
    Enumeration,
    /// Open the selected keyboard device.
    DeviceOpen,
    /// Claim the keyboard's HID interface.
    ClaimInterface,
    /// Select HID boot protocol for the device.
    SetBootProtocol,
}

/// Identity of a selected USB HID boot device.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UsbDeviceInfo {
    /// USB vendor identifier.
    pub vendor_id: u16,
    /// USB product identifier.
    pub product_id: u16,
}

/// Identity of the optional boot keyboard and mouse owned by one xHCI host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UsbHidInfo {
    /// The optional boot keyboard discovered on this controller.
    pub keyboard: Option<UsbDeviceInfo>,
    /// The optional boot mouse discovered beside the keyboard.
    pub mouse: Option<UsbDeviceInfo>,
    /// The layout the mouse reports in, once its descriptor has been parsed.
    pub mouse_layout: Option<MouseReportLayout>,
}

/// One completed HID boot report from the shared xHCI host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsbHidReport {
    /// An exact eight-byte boot-keyboard report.
    Keyboard([u8; BOOT_KEYBOARD_REPORT_LEN]),
    /// A mouse report of at most `MOUSE_REPORT_LEN` bytes in a fixed buffer.
    Mouse {
        /// Report bytes; bytes after `actual_length` are zero.
        bytes: [u8; MOUSE_REPORT_LEN],
        /// Number of bytes completed by the interrupt transfer.
        actual_length: usize,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BootHidInterface {
    kind: BootHidKind,
    number: u8,
    alternate: u8,
    endpoint: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BootHidKind {
    Keyboard,
    Mouse,
}

/// The `wValue` of a HID `SET_PROTOCOL` request selecting the boot report.
const SET_BOOT_PROTOCOL: u16 = 0;
/// The `wValue` selecting the format the device's report descriptor defines.
const SET_REPORT_PROTOCOL: u16 = 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DiscoveryDecision {
    Continue,
    Complete,
    Missing,
}

fn discovery_decision(
    keyboard_found: bool,
    mouse_found: bool,
    deadline_expired: bool,
) -> DiscoveryDecision {
    if keyboard_found && mouse_found {
        DiscoveryDecision::Complete
    } else if !deadline_expired {
        DiscoveryDecision::Continue
    } else if keyboard_found || mouse_found {
        DiscoveryDecision::Complete
    } else {
        DiscoveryDecision::Missing
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BootHidInterfaceError {
    Ambiguous,
    InvalidEndpoint,
}

fn classify_boot_interface(class: u8, subclass: u8, protocol: u8) -> Option<BootHidKind> {
    if (class, subclass) != (0x03, 0x01) {
        return None;
    }
    match protocol {
        0x01 => Some(BootHidKind::Keyboard),
        0x02 => Some(BootHidKind::Mouse),
        _ => None,
    }
}

fn find_boot_hid_interface(
    configurations: &[ConfigurationDescriptor],
) -> Result<Option<BootHidInterface>, BootHidInterfaceError> {
    let Some(configuration) = configurations.first() else {
        return Ok(None);
    };
    let mut selected = None;
    for alternate in configuration
        .interfaces
        .iter()
        .flat_map(|interface| &interface.alt_settings)
    {
        let Some(kind) =
            classify_boot_interface(alternate.class, alternate.subclass, alternate.protocol)
        else {
            continue;
        };
        if selected.is_some() {
            return Err(BootHidInterfaceError::Ambiguous);
        }
        let mut interrupt_in = alternate.endpoints.iter().filter(|endpoint| {
            endpoint.transfer_type == EndpointType::Interrupt && endpoint.direction == Direction::In
        });
        let endpoint = interrupt_in
            .next()
            .filter(|endpoint| {
                endpoint.address & 0x0f != 0
                    && endpoint.max_packet_size
                        >= match kind {
                            BootHidKind::Keyboard => BOOT_KEYBOARD_REPORT_LEN as u16,
                            BootHidKind::Mouse => BOOT_MOUSE_REPORT_LEN as u16,
                        }
            })
            .ok_or(BootHidInterfaceError::InvalidEndpoint)?;
        if interrupt_in.next().is_some() {
            return Err(BootHidInterfaceError::InvalidEndpoint);
        }
        selected = Some(BootHidInterface {
            kind,
            number: alternate.interface_number,
            alternate: alternate.alternate_setting,
            endpoint: endpoint.address,
        });
    }
    Ok(selected)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum XhciMmioError {
    MmioRead,
    InvalidMappingProperties,
    InvalidRegisterLayout,
    InvalidExtendedCapability,
    UnsupportedExtendedCapability,
}

struct XhciHost {
    // Fields drop in declaration order: CrabUSB releases its register accessors before the MMIO
    // mapping, then its callback adapter. Failed active hosts are abandoned as one complete value.
    host: USBHost,
    _mmio: IoMem,
    kernel_op: Box<UsbKernelOp>,
}

impl XhciHost {
    fn new(mmio: IoMem, dma_window: DmaWindow) -> Result<Self, UsbKeyboardError> {
        if let Err(error) = validate_xhci_mmio(&mmio) {
            crate::warn!("xHCI MMIO validation failed: {:?}", error);
            return Err(UsbKeyboardError::InvalidMmio);
        }
        let kernel_op = new_usb_kernel_op(dma_window);
        // SAFETY: `kernel_op` has a stable heap address and is moved into `XhciHost` without moving
        // its allocation. Field order drops `host` before `kernel_op`, while active failed hosts
        // are forgotten whole. If construction fails, CrabUSB returns no host retaining the
        // callback and `kernel_op` is reclaimed normally.
        let kernel_op_static = unsafe { extend_kernel_op_lifetime(kernel_op.as_ref()) };

        // SAFETY: `validate_xhci_mmio` checked every fixed and controller-derived register range
        // that CrabUSB 0.9.10 constructs or dereferences, as well as unique ownership and UC
        // mapping. The xHCI capability registers are read-only after reset, and `_mmio` keeps the
        // validated mapping alive until after `host`.
        let host = unsafe { new_xhci_host_unchecked(mmio.as_non_null_ptr(), kernel_op_static) }?;
        Ok(Self {
            host,
            _mmio: mmio,
            kernel_op,
        })
    }
}

/// Extends a boxed callback adapter's reference for CrabUSB's host lifetime.
///
/// # Safety
///
/// The adapter must have a stable address and outlive every CrabUSB value that receives the
/// returned reference.
unsafe fn extend_kernel_op_lifetime(kernel_op: &UsbKernelOp) -> &'static UsbKernelOp {
    // SAFETY: The caller upholds the allocation's stability and lifetime.
    unsafe { &*(kernel_op as *const UsbKernelOp) }
}

/// Creates a CrabUSB host from a raw xHCI register base.
///
/// # Safety
///
/// `mmio_base` must remain uniquely owned, uncacheable, and mapped for the returned host's
/// lifetime. No other accessor may touch its registers. The mapping must cover every fixed
/// register, every region described by `CAPLENGTH`, `HCSPARAMS1`, `DBOFF`, and `RTSOFF`, and every
/// entry and linked-list hop described by `HCCPARAMS1.XECP`.
unsafe fn new_xhci_host_unchecked(
    mmio_base: NonNull<u8>,
    kernel_op: &'static dyn KernelOp,
) -> Result<USBHost, UsbKeyboardError> {
    USBHost::new_xhci(mmio_base, kernel_op).map_err(|_| UsbKeyboardError::HostCreate)
}

fn validate_xhci_mmio(mmio: &IoMem) -> Result<(), XhciMmioError> {
    validate_xhci_mapping_properties(mmio.cache_policy(), mmio.is_unique())?;
    if !(mmio.as_non_null_ptr().as_ptr() as usize).is_multiple_of(size_of::<u64>()) {
        return Err(XhciMmioError::InvalidRegisterLayout);
    }

    validate_xhci_mmio_with(mmio.size(), |offset| {
        mmio.read_once::<u32>(offset)
            .map_err(|_| XhciMmioError::MmioRead)
    })
}

fn validate_xhci_mapping_properties(
    cache_policy: CachePolicy,
    is_unique: bool,
) -> Result<(), XhciMmioError> {
    if cache_policy != CachePolicy::Uncacheable || !is_unique {
        return Err(XhciMmioError::InvalidMappingProperties);
    }
    Ok(())
}

fn validate_xhci_mmio_with(
    mmio_size: usize,
    mut read: impl FnMut(usize) -> Result<u32, XhciMmioError>,
) -> Result<(), XhciMmioError> {
    if !region_fits(0, XHCI_CAPABILITY_ACCESSORS_LEN, mmio_size) {
        return Err(XhciMmioError::InvalidRegisterLayout);
    }

    let caplength_hciversion = read(0x00)?;
    let hcsparams1 = read(0x04)?;
    let hccparams1 = read(0x10)?;
    let doorbell_offset = read(0x14)? as usize;
    let runtime_offset = read(0x18)? as usize;

    let operational_offset = (caplength_hciversion & 0xff) as usize;
    let version = (caplength_hciversion >> 16) as u16;
    let max_slots = (hcsparams1 & 0xff) as usize;
    let max_interrupters = ((hcsparams1 >> 8) & 0x7ff) as usize;
    let max_ports = (hcsparams1 >> 24) as usize;

    let Some(port_registers_len) = max_ports
        .checked_mul(XHCI_PORT_REGISTER_SET_LEN)
        .and_then(|length| XHCI_OPERATIONAL_PORT_REGISTERS_OFFSET.checked_add(length))
    else {
        return Err(XhciMmioError::InvalidRegisterLayout);
    };
    let Some(doorbell_registers_len) = max_slots
        .checked_add(1)
        .and_then(|count| count.checked_mul(size_of::<u32>()))
    else {
        return Err(XhciMmioError::InvalidRegisterLayout);
    };
    let Some(interrupter_registers_len) = max_interrupters
        .checked_mul(XHCI_INTERRUPTER_REGISTER_SET_LEN)
        .and_then(|length| XHCI_RUNTIME_INTERRUPTER_REGISTERS_OFFSET.checked_add(length))
    else {
        return Err(XhciMmioError::InvalidRegisterLayout);
    };

    if operational_offset < XHCI_MIN_CAPLENGTH
        || !operational_offset.is_multiple_of(size_of::<u64>())
        || !(0x0090..=0x0120).contains(&version)
        || max_slots == 0
        || max_interrupters == 0
        || max_ports == 0
        || !region_fits(operational_offset, port_registers_len, mmio_size)
        || doorbell_offset < operational_offset
        || !doorbell_offset.is_multiple_of(size_of::<u32>())
        || !region_fits(doorbell_offset, doorbell_registers_len, mmio_size)
        || runtime_offset < operational_offset
        || !runtime_offset.is_multiple_of(XHCI_INTERRUPTER_REGISTER_SET_LEN)
        || !region_fits(runtime_offset, interrupter_registers_len, mmio_size)
        || regions_overlap(
            operational_offset,
            port_registers_len,
            runtime_offset,
            interrupter_registers_len,
        )
        || regions_overlap(
            operational_offset,
            port_registers_len,
            doorbell_offset,
            doorbell_registers_len,
        )
        || regions_overlap(
            runtime_offset,
            interrupter_registers_len,
            doorbell_offset,
            doorbell_registers_len,
        )
    {
        return Err(XhciMmioError::InvalidRegisterLayout);
    }

    let extended_capability_offset =
        ((hccparams1 >> 16) as usize).checked_mul(XHCI_EXTENDED_CAPABILITY_HEADER_LEN);
    let Some(mut offset) = extended_capability_offset.filter(|offset| *offset != 0) else {
        return Ok(());
    };
    if offset < XHCI_MIN_CAPLENGTH {
        return Err(XhciMmioError::InvalidExtendedCapability);
    }

    loop {
        if !region_fits(offset, XHCI_EXTENDED_CAPABILITY_HEADER_LEN, mmio_size) {
            return Err(XhciMmioError::InvalidExtendedCapability);
        }
        let header = read(offset)?;
        let capability_len = extended_capability_len(offset, header, mmio_size, &mut read)?;
        if !region_fits(offset, capability_len, mmio_size)
            || regions_overlap(
                offset,
                capability_len,
                operational_offset,
                port_registers_len,
            )
            || regions_overlap(
                offset,
                capability_len,
                runtime_offset,
                interrupter_registers_len,
            )
            || regions_overlap(
                offset,
                capability_len,
                doorbell_offset,
                doorbell_registers_len,
            )
        {
            return Err(XhciMmioError::InvalidExtendedCapability);
        }

        let next_dwords = ((header >> 8) & 0xff) as usize;
        if next_dwords == 0 {
            return Ok(());
        }
        let next_offset = next_dwords
            .checked_mul(XHCI_EXTENDED_CAPABILITY_HEADER_LEN)
            .and_then(|delta| offset.checked_add(delta))
            .ok_or(XhciMmioError::InvalidExtendedCapability)?;
        let capability_end = offset
            .checked_add(capability_len)
            .ok_or(XhciMmioError::InvalidExtendedCapability)?;
        if next_offset < capability_end
            || !region_fits(next_offset, XHCI_EXTENDED_CAPABILITY_HEADER_LEN, mmio_size)
        {
            return Err(XhciMmioError::InvalidExtendedCapability);
        }
        offset = next_offset;
    }
}

fn extended_capability_len(
    offset: usize,
    header: u32,
    mmio_size: usize,
    read: &mut impl FnMut(usize) -> Result<u32, XhciMmioError>,
) -> Result<usize, XhciMmioError> {
    let capability_id = (header & 0xff) as u8;
    let length = match capability_id {
        1 | 3 => 8,
        2 => {
            if !region_fits(offset, 0x10, mmio_size) {
                return Err(XhciMmioError::InvalidExtendedCapability);
            }
            let protocol_speed_id_count = (read(offset + 0x08)? >> 28) as usize;
            protocol_speed_id_count
                .checked_mul(size_of::<u32>())
                .and_then(|length| 0x10usize.checked_add(length))
                .ok_or(XhciMmioError::InvalidExtendedCapability)?
        }
        // CrabUSB 0.9.10 uses xhci 0.9.2's `repr(C)` MSI accessor, whose Rust layout does not
        // match all legal PCI MSI capability layouts and can panic or access beyond the
        // capability. Reject MSI until the dependency models these registers safely.
        5 => return Err(XhciMmioError::UnsupportedExtendedCapability),
        6 => {
            if !region_fits(offset, 8, mmio_size) {
                return Err(XhciMmioError::InvalidExtendedCapability);
            }
            let local_memory_kib = read(offset + 0x04)? as usize;
            local_memory_kib
                .checked_mul(1024)
                .and_then(|length| 8usize.checked_add(length))
                .ok_or(XhciMmioError::InvalidExtendedCapability)?
        }
        10 => {
            if !offset.is_multiple_of(size_of::<u64>()) {
                return Err(XhciMmioError::InvalidExtendedCapability);
            }
            0x40
        }
        17 => 0x0c,
        _ => XHCI_EXTENDED_CAPABILITY_HEADER_LEN,
    };
    Ok(length)
}

fn region_fits(offset: usize, length: usize, mmio_size: usize) -> bool {
    offset
        .checked_add(length)
        .is_some_and(|end| end <= mmio_size)
}

fn regions_overlap(
    first_offset: usize,
    first_length: usize,
    second_offset: usize,
    second_length: usize,
) -> bool {
    first_offset < second_offset.saturating_add(second_length)
        && second_offset < first_offset.saturating_add(first_length)
}

fn new_usb_kernel_op(dma_window: DmaWindow) -> Box<UsbKernelOp> {
    Box::new(UsbKernelOp::new(dma_window))
}

struct Deadline {
    start: u64,
    ticks: u64,
}

impl Deadline {
    fn after(duration: Duration) -> Self {
        let ticks = duration
            .as_nanos()
            .saturating_mul(arch::tsc_freq() as u128)
            .div_ceil(1_000_000_000)
            .min(u64::MAX as u128) as u64;
        Self {
            start: arch::read_tsc(),
            ticks,
        }
    }

    fn expired(&self) -> bool {
        arch::read_tsc().wrapping_sub(self.start) >= self.ticks
    }
}

fn drive_with<F>(
    future: F,
    mut pump_events: impl FnMut(),
    mut deadline_expired: impl FnMut() -> bool,
    mut wait: impl FnMut(),
) -> Result<F::Output, DriveError>
where
    F: Future,
{
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    loop {
        match future.as_mut().poll(&mut context) {
            Poll::Ready(output) => return Ok(output),
            Poll::Pending => {
                pump_events();
                if deadline_expired() {
                    return Err(DriveError::Timeout);
                }
                wait();
            }
        }
    }
}

fn drive<F>(future: F, events: &EventHandler) -> Result<F::Output, DriveError>
where
    F: Future,
{
    let deadline = Deadline::after(HOST_OPERATION_TIMEOUT);
    drive_with(
        future,
        || {
            events.handle_event();
        },
        || deadline.expired(),
        Task::yield_now,
    )
}

fn timeout_at(stage: UsbKeyboardStage, kernel_op: &UsbKernelOp) -> UsbKeyboardError {
    kernel_op.log_dma_snapshot();
    UsbKeyboardError::Timeout(stage)
}

fn abandon_host(xhci: XhciHost, events: EventHandler) {
    // The controller may still own DMA rings. CrabUSB has no shutdown API, so leaking this
    // one failed host is safer than freeing memory that the controller can still access.
    mem::forget((xhci, events));
}

/// A polling USB HID boot host backed by one CrabUSB xHCI controller.
pub struct PollingUsbHidHost {
    // CrabUSB has no controller shutdown API. Keep DMA-visible state alive even if the polling
    // worker exits after a transfer error.
    inner: ManuallyDrop<PollingUsbHidHostInner>,
}

struct PollingUsbHidHostInner {
    _xhci: XhciHost,
    events: EventHandler,
    keyboard: Option<BootKeyboardSession>,
    mouse: Option<BootMouseSession>,
}

struct BootKeyboardSession {
    _device: Device,
    endpoint: Endpoint,
    reports: BootKeyboardReportQueue,
    info: UsbDeviceInfo,
}

struct BootMouseSession {
    _device: Device,
    endpoint: Endpoint,
    reports: BootReportQueue<MOUSE_REPORT_LEN>,
    info: UsbDeviceInfo,
    layout: Option<MouseReportLayout>,
    /// Failed transfers in a row since the last delivered report.
    consecutive_failures: u32,
}

/// How many consecutive failed transfers retire a mouse.
///
/// The report queue re-arms itself after every failed transfer, so a device
/// that recovers starts delivering reports again on its own and the run resets
/// to zero. This threshold is only for a device that has genuinely gone away,
/// where retrying forever would ask a dead endpoint for the rest of the boot.
const MOUSE_FAILURE_RETIREMENT_THRESHOLD: u32 = 32;

struct OpenedHidDevice {
    device: Device,
    endpoint: Endpoint,
    info: UsbDeviceInfo,
    mouse_layout: Option<MouseReportLayout>,
}

impl<const N: usize> ReportEndpoint<N> for Endpoint {
    fn submit_report(&mut self, report: &mut [u8; N]) -> Result<RequestId, UsbKeyboardError> {
        self.submit(TransferRequest::interrupt_in(report))
            .map_err(|_| UsbKeyboardError::Transfer)
    }

    fn poll_report_request(
        &mut self,
        request: RequestId,
        context: &mut Context<'_>,
    ) -> Poll<Result<TransferCompletion, UsbKeyboardError>> {
        match self.poll_request(request, context) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Ok(completion)) => Poll::Ready(Ok(completion)),
            Poll::Ready(Err(_)) => Poll::Ready(Err(UsbKeyboardError::Transfer)),
        }
    }
}

/// The largest HID report descriptor reported here, covering the boot devices.
const HID_REPORT_DESCRIPTOR_LEN: usize = 128;

/// The number of usages one HID main item may carry before parsing gives up.
const HID_LOCAL_USAGE_CAPACITY: usize = 8;

const HID_ITEM_LONG: u8 = 0xfe;
const HID_ITEM_SIZE_MASK: u8 = 0x03;
const HID_ITEM_TYPE_MASK: u8 = 0x03;
const HID_ITEM_TYPE_MAIN: u8 = 0x00;
const HID_ITEM_TYPE_GLOBAL: u8 = 0x01;
const HID_ITEM_TYPE_LOCAL: u8 = 0x02;
const HID_ITEM_TAG_MASK: u8 = 0xf0;
const HID_GLOBAL_USAGE_PAGE: u8 = 0x00;
const HID_GLOBAL_REPORT_ID: u8 = 0x80;
const HID_GLOBAL_REPORT_SIZE: u8 = 0x70;
const HID_GLOBAL_REPORT_COUNT: u8 = 0x90;
const HID_LOCAL_USAGE: u8 = 0x00;
const HID_MAIN_INPUT: u8 = 0x80;
const HID_USAGE_PAGE_GENERIC_DESKTOP: u16 = 0x01;
const HID_USAGE_PAGE_BUTTON: u16 = 0x09;
const HID_USAGE_X: u16 = 0x30;
const HID_USAGE_Y: u16 = 0x31;
const HID_USAGE_WHEEL: u16 = 0x38;

/// Where every field the mouse decoder reads sits in one report.
///
/// The offsets come from the device's own report descriptor, so a device that
/// describes twelve-bit axes, a wheel, or neither is read exactly as it
/// describes itself. No field position is inferred from the report length.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MouseReportLayout {
    /// Bit offset of the button mask.
    pub buttons_offset: u8,
    /// Width of the whole button mask, which is every button bit together.
    pub buttons_bits: u8,
    /// Bit offset of the horizontal displacement.
    pub x_offset: u8,
    /// Width of the horizontal displacement.
    pub x_bits: u8,
    /// Bit offset of the vertical displacement.
    pub y_offset: u8,
    /// Width of the vertical displacement.
    pub y_bits: u8,
    /// Bit offset of the wheel, when the device describes one.
    pub wheel_offset: Option<u8>,
    /// Width of the wheel.
    pub wheel_bits: u8,
    /// Total report length in bytes, which the transfer buffer must cover.
    pub report_bytes: u8,
}

/// Reads one little-endian HID short item value of at most four bytes.
fn item_value(bytes: &[u8]) -> u32 {
    bytes.iter().enumerate().fold(0u32, |value, (shift, byte)| {
        value | (u32::from(*byte) << (8 * shift))
    })
}

/// Derives a mouse layout from one HID report descriptor.
///
/// Returns `None` for any descriptor this parser does not fully understand,
/// including one that uses report IDs. Its caller then leaves the device in the
/// boot protocol rather than reading a report whose shape is unknown.
fn parse_mouse_report_descriptor(descriptor: &[u8]) -> Option<MouseReportLayout> {
    let mut layout = MouseReportLayout {
        buttons_offset: 0,
        buttons_bits: 0,
        x_offset: 0,
        x_bits: 0,
        y_offset: 0,
        y_bits: 0,
        wheel_offset: None,
        wheel_bits: 0,
        report_bytes: 0,
    };
    let mut offset_bits: u16 = 0;
    let mut usage_page: u16 = 0;
    let mut report_size: u16 = 0;
    let mut report_count: u16 = 0;
    let mut usages = [0u16; HID_LOCAL_USAGE_CAPACITY];
    let mut usage_count = 0usize;
    let mut index = 0usize;

    while index < descriptor.len() {
        let prefix = descriptor[index];
        index += 1;
        if prefix == HID_ITEM_LONG {
            let size = usize::from(*descriptor.get(index)?);
            index = index.checked_add(2 + size)?;
            continue;
        }
        let size = match prefix & HID_ITEM_SIZE_MASK {
            0 => 0,
            1 => 1,
            2 => 2,
            _ => 4,
        };
        let value = item_value(descriptor.get(index..index.checked_add(size)?)?);
        index += size;

        match (prefix >> 2) & HID_ITEM_TYPE_MASK {
            HID_ITEM_TYPE_GLOBAL => match prefix & HID_ITEM_TAG_MASK {
                HID_GLOBAL_USAGE_PAGE => usage_page = value as u16,
                // A report ID prefixes every report with a byte this parser
                // does not model, so the layout would be wrong.
                HID_GLOBAL_REPORT_ID => return None,
                HID_GLOBAL_REPORT_SIZE => report_size = u16::try_from(value).ok()?,
                HID_GLOBAL_REPORT_COUNT => report_count = u16::try_from(value).ok()?,
                _ => {}
            },
            HID_ITEM_TYPE_LOCAL => {
                if prefix & HID_ITEM_TAG_MASK == HID_LOCAL_USAGE
                    && usage_count < HID_LOCAL_USAGE_CAPACITY
                {
                    usages[usage_count] = value as u16;
                    usage_count += 1;
                }
            }
            HID_ITEM_TYPE_MAIN => {
                if prefix & HID_ITEM_TAG_MASK == HID_MAIN_INPUT {
                    let bits = report_size.checked_mul(report_count)?;
                    // Bit 0 of an input item's data is clear for a data field
                    // and set for the constant padding between them.
                    if value & 0x01 == 0 {
                        for field in 0..usize::from(report_count) {
                            let start = offset_bits + report_size * field as u16;
                            let usage = usages.get(field).copied().filter(|_| field < usage_count);
                            match (usage_page, usage) {
                                // The button bits form one mask of
                                // `report_count` one-bit fields, not one field.
                                (HID_USAGE_PAGE_BUTTON, _) if field == 0 => {
                                    layout.buttons_offset = start as u8;
                                    layout.buttons_bits = bits as u8;
                                }
                                (HID_USAGE_PAGE_GENERIC_DESKTOP, Some(HID_USAGE_X)) => {
                                    layout.x_offset = start as u8;
                                    layout.x_bits = report_size as u8;
                                }
                                (HID_USAGE_PAGE_GENERIC_DESKTOP, Some(HID_USAGE_Y)) => {
                                    layout.y_offset = start as u8;
                                    layout.y_bits = report_size as u8;
                                }
                                (HID_USAGE_PAGE_GENERIC_DESKTOP, Some(HID_USAGE_WHEEL)) => {
                                    layout.wheel_offset = Some(start as u8);
                                    layout.wheel_bits = report_size as u8;
                                }
                                _ => {}
                            }
                        }
                    }
                    offset_bits = offset_bits.checked_add(bits)?;
                }
                // Every main item clears the local items.
                usage_count = 0;
            }
            _ => {}
        }
        if offset_bits > 64 {
            return None;
        }
    }

    if offset_bits == 0 || offset_bits % 8 != 0 || offset_bits > 64 {
        return None;
    }
    if layout.buttons_bits == 0 || layout.x_bits == 0 || layout.y_bits == 0 {
        return None;
    }
    layout.report_bytes = (offset_bits / 8) as u8;
    Some(layout)
}

/// Reads one interface's HID report descriptor and derives its mouse layout.
///
/// A boot device still describes the report it sends under the report protocol,
/// which is where a mouse wheel lives; the boot report the decoder otherwise
/// reads has no such field. The request is advisory: a device that refuses it
/// still opens, and its caller then keeps the boot protocol.
fn fetch_mouse_layout(
    device: &mut Device,
    events: &EventHandler,
    interface: BootHidInterface,
) -> Option<MouseReportLayout> {
    let setup = ControlSetup {
        request_type: RequestType::Standard,
        recipient: Recipient::Interface,
        request: Request::GetDescriptor,
        value: 0x2200,
        index: u16::from(interface.number),
    };
    let mut descriptor = [0u8; HID_REPORT_DESCRIPTOR_LEN];
    let length = match drive(device.control_in(setup, &mut descriptor), events) {
        Ok(Ok(length)) => length.min(descriptor.len()),
        Ok(Err(error)) => {
            crate::info!(
                "USB HID {:?} on interface {} report descriptor refused: {:?}",
                interface.kind,
                interface.number,
                error,
            );
            return None;
        }
        Err(DriveError::Timeout) => {
            crate::warn!(
                "USB HID {:?} on interface {} report descriptor request timed out",
                interface.kind,
                interface.number,
            );
            return None;
        }
    };

    match parse_mouse_report_descriptor(&descriptor[..length]) {
        Some(layout) => {
            crate::info!(
                "USB HID {:?} on interface {} report layout: {:?}",
                interface.kind,
                interface.number,
                layout,
            );
            Some(layout)
        }
        None => {
            crate::info!(
                "USB HID {:?} on interface {} report descriptor: {:02x?}",
                interface.kind,
                interface.number,
                &descriptor[..length],
            );
            None
        }
    }
}

/// Asks one HID interface to report in `protocol`, or reports why it refused.
///
/// A refusal is not necessarily fatal: the boot protocol is the one report every
/// HID boot device implements, so callers can fall back to it.
fn set_hid_protocol(
    xhci: &XhciHost,
    device: &mut Device,
    events: &EventHandler,
    interface: BootHidInterface,
    protocol: u16,
) -> Result<(), UsbKeyboardError> {
    let setup = ControlSetup {
        request_type: RequestType::Class,
        recipient: Recipient::Interface,
        request: Request::Other(0x0b),
        value: protocol,
        index: u16::from(interface.number),
    };
    match drive(device.control_out(setup, &[]), events) {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(_)) => Err(UsbKeyboardError::SetBootProtocol),
        Err(DriveError::Timeout) => Err(timeout_at(
            UsbKeyboardStage::SetBootProtocol,
            xhci.kernel_op.as_ref(),
        )),
    }
}

fn open_hid_device(
    xhci: &mut XhciHost,
    events: &EventHandler,
    device_info: &DeviceInfo,
    interface: BootHidInterface,
) -> Result<OpenedHidDevice, UsbKeyboardError> {
    let info = UsbDeviceInfo {
        vendor_id: device_info.vendor_id(),
        product_id: device_info.product_id(),
    };
    let mut device = match drive(xhci.host.open_device(device_info), events) {
        Ok(Ok(device)) => device,
        Ok(Err(_)) => return Err(UsbKeyboardError::DeviceOpen),
        Err(DriveError::Timeout) => {
            return Err(timeout_at(
                UsbKeyboardStage::DeviceOpen,
                xhci.kernel_op.as_ref(),
            ));
        }
    };

    match drive(
        device.claim_interface(interface.number, interface.alternate),
        events,
    ) {
        Ok(Ok(())) => {}
        Ok(Err(_)) => {
            mem::forget(device);
            return Err(UsbKeyboardError::ClaimInterface);
        }
        Err(DriveError::Timeout) => {
            let error = timeout_at(UsbKeyboardStage::ClaimInterface, xhci.kernel_op.as_ref());
            mem::forget(device);
            return Err(error);
        }
    }

    let mut mouse_layout = fetch_mouse_layout(&mut device, events, interface);
    // The boot report has no wheel field, so a mouse only reaches its wheel in
    // the report protocol, and only once its descriptor says how to read it.
    let protocol = match (interface.kind, mouse_layout) {
        (BootHidKind::Mouse, Some(_)) => SET_REPORT_PROTOCOL,
        _ => SET_BOOT_PROTOCOL,
    };

    match set_hid_protocol(xhci, &mut device, events, interface, protocol) {
        Ok(()) => crate::info!(
            "USB HID {:?} on interface {} selected protocol wValue={}",
            interface.kind,
            interface.number,
            protocol,
        ),
        // Only a mouse asks for the report protocol, and it asks solely to
        // reach its wheel. A device that stalls or times out on that one
        // control transfer is still a mouse the user can move and click with,
        // so keep it and stay in the boot report instead of refusing the whole
        // device. Refusing it is what left the keyboard working and the mouse
        // gone: the keyboard never asks for the report protocol, so it never
        // took this path.
        Err(error) if protocol == SET_REPORT_PROTOCOL => {
            crate::warn!(
                "USB HID {:?} on interface {} refused the report protocol ({:?}); \
                 keeping the boot report, so this mouse reports no wheel",
                interface.kind,
                interface.number,
                error,
            );
            if let Err(fallback) =
                set_hid_protocol(xhci, &mut device, events, interface, SET_BOOT_PROTOCOL)
            {
                mem::forget(device);
                return Err(fallback);
            }
            // The boot report is not the report the descriptor described, so
            // the layout derived from that descriptor no longer applies.
            mouse_layout = None;
        }
        Err(error) => {
            mem::forget(device);
            return Err(error);
        }
    }

    let endpoint = match device.endpoint(interface.endpoint) {
        Ok(endpoint) => endpoint,
        Err(_) => {
            mem::forget(device);
            return Err(UsbKeyboardError::EndpointOpen);
        }
    };
    Ok(OpenedHidDevice {
        device,
        endpoint,
        info,
        mouse_layout: match interface.kind {
            BootHidKind::Mouse => mouse_layout,
            BootHidKind::Keyboard => None,
        },
    })
}

impl PollingUsbHidHost {
    /// Starts one firmware-configured xHCI controller and discovers boot HID devices.
    ///
    /// A controller may contribute a keyboard, a mouse, or both. This permits boards whose
    /// physical USB sockets are wired to separate xHCI controllers while preserving the shared
    /// keyboard-and-mouse fast path used by QEMU.
    pub fn open(mmio: IoMem, dma_window: DmaWindow) -> Result<Self, UsbKeyboardError> {
        let mut xhci = XhciHost::new(mmio, dma_window)?;
        let events = xhci.host.create_event_handler();

        match drive(xhci.host.init(), &events) {
            Ok(Ok(())) => {}
            Ok(Err(_)) => {
                abandon_host(xhci, events);
                return Err(UsbKeyboardError::HostInit);
            }
            Err(DriveError::Timeout) => {
                let error = timeout_at(UsbKeyboardStage::HostInit, xhci.kernel_op.as_ref());
                abandon_host(xhci, events);
                return Err(error);
            }
        }

        // CrabUSB enables the global xHCI interrupt at the end of initialization. Keep it
        // disabled until the kernel has installed the platform interrupt mapping.
        if xhci.host.disable_irq().is_err() {
            abandon_host(xhci, events);
            return Err(UsbKeyboardError::HostInit);
        }

        let discovery_deadline = Deadline::after(KEYBOARD_DISCOVERY_TIMEOUT);
        let mut keyboard = None;
        let mut mouse = None;
        loop {
            let devices = match drive(xhci.host.probe_devices(), &events) {
                Ok(Ok(devices)) => devices,
                Ok(Err(_)) => {
                    abandon_host(xhci, events);
                    return Err(UsbKeyboardError::Enumeration);
                }
                Err(DriveError::Timeout) => {
                    let error = timeout_at(UsbKeyboardStage::Enumeration, xhci.kernel_op.as_ref());
                    abandon_host(xhci, events);
                    return Err(error);
                }
            };

            for device in devices {
                let device_id = device.id();
                let interface = match find_boot_hid_interface(device.configurations()) {
                    Ok(Some(interface)) => interface,
                    Ok(None) => continue,
                    Err(_) => {
                        abandon_host(xhci, events);
                        return Err(UsbKeyboardError::Enumeration);
                    }
                };
                let slot = match interface.kind {
                    BootHidKind::Keyboard => &mut keyboard,
                    BootHidKind::Mouse => &mut mouse,
                };
                if slot
                    .as_ref()
                    .is_some_and(|(selected_id, _, _)| *selected_id != device_id)
                {
                    abandon_host(xhci, events);
                    return Err(UsbKeyboardError::Enumeration);
                }
                if slot.is_none()
                    && let Some(device_info) = device.into_device_info()
                {
                    *slot = Some((device_id, device_info, interface));
                }
            }
            match discovery_decision(
                keyboard.is_some(),
                mouse.is_some(),
                discovery_deadline.expired(),
            ) {
                DiscoveryDecision::Continue => Task::yield_now(),
                DiscoveryDecision::Complete => break,
                DiscoveryDecision::Missing => {
                    abandon_host(xhci, events);
                    return Err(UsbKeyboardError::KeyboardNotFound);
                }
            }
        }

        let keyboard_session = keyboard.and_then(|(_, keyboard_info, keyboard_interface)| {
            let opened_keyboard =
                match open_hid_device(&mut xhci, &events, &keyboard_info, keyboard_interface) {
                    Ok(opened) => opened,
                    Err(error) => {
                        crate::warn!("USB boot keyboard unavailable: {:?}", error);
                        return None;
                    }
                };
            let mut session = BootKeyboardSession {
                _device: opened_keyboard.device,
                endpoint: opened_keyboard.endpoint,
                reports: BootKeyboardReportQueue::empty(),
                info: opened_keyboard.info,
            };
            if let Err(error) = session.reports.fill(&mut session.endpoint) {
                crate::warn!("USB boot keyboard report queue unavailable: {:?}", error);
                mem::forget(session);
                return None;
            }
            Some(session)
        });

        let mouse_session = mouse.and_then(|(_, mouse_info, mouse_interface)| {
            let opened = match open_hid_device(&mut xhci, &events, &mouse_info, mouse_interface) {
                Ok(opened) => opened,
                Err(error) => {
                    crate::warn!("USB boot mouse unavailable: {:?}", error);
                    return None;
                }
            };
            let mut session = BootMouseSession {
                _device: opened.device,
                endpoint: opened.endpoint,
                reports: BootReportQueue::empty(),
                info: opened.info,
                layout: opened.mouse_layout,
                consecutive_failures: 0,
            };
            if let Err(error) = session.reports.fill(&mut session.endpoint) {
                crate::warn!("USB boot mouse report queue unavailable: {:?}", error);
                mem::forget(session);
                return None;
            }
            Some(session)
        });

        if keyboard_session.is_none() && mouse_session.is_none() {
            abandon_host(xhci, events);
            return Err(UsbKeyboardError::DeviceOpen);
        }

        Ok(Self {
            inner: ManuallyDrop::new(PollingUsbHidHostInner {
                _xhci: xhci,
                events,
                keyboard: keyboard_session,
                mouse: mouse_session,
            }),
        })
    }

    /// Returns the selected keyboard and optional mouse identities.
    pub fn info(&self) -> UsbHidInfo {
        UsbHidInfo {
            keyboard: self.inner.keyboard.as_ref().map(|keyboard| keyboard.info),
            mouse: self.inner.mouse.as_ref().map(|mouse| mouse.info),
            mouse_layout: self.inner.mouse.as_ref().and_then(|mouse| mouse.layout),
        }
    }

    /// Enables the xHCI global interrupt after the platform IRQ handler is installed.
    pub fn enable_irq(&mut self) -> Result<(), UsbKeyboardError> {
        self.inner
            ._xhci
            .host
            .enable_irq()
            .map_err(|_| UsbKeyboardError::Interrupt)
    }

    /// Disables the xHCI global interrupt before the platform IRQ handler is removed.
    pub fn disable_irq(&mut self) -> Result<(), UsbKeyboardError> {
        self.inner
            ._xhci
            .host
            .disable_irq()
            .map_err(|_| UsbKeyboardError::Interrupt)
    }

    /// Pumps xHCI and returns one completed keyboard or mouse report, if available.
    pub fn poll_report(&mut self) -> Result<Option<UsbHidReport>, UsbKeyboardError> {
        let inner = &mut *self.inner;
        inner.events.handle_event();
        let mut context = Context::from_waker(Waker::noop());
        if let Some(keyboard) = &mut inner.keyboard
            && let Some(report) = keyboard
                .reports
                .poll(&mut keyboard.endpoint, &mut context)?
        {
            return Ok(Some(UsbHidReport::Keyboard(report)));
        }
        let Some(mouse) = &mut inner.mouse else {
            return Ok(None);
        };
        match mouse.reports.poll(&mut mouse.endpoint, &mut context) {
            Ok(report) => {
                mouse.consecutive_failures = 0;
                Ok(report.map(|report| UsbHidReport::Mouse {
                    bytes: *report.bytes(),
                    actual_length: report.actual_length(),
                }))
            }
            Err(error) => {
                mouse.consecutive_failures += 1;
                let failures = mouse.consecutive_failures;
                if failures < MOUSE_FAILURE_RETIREMENT_THRESHOLD {
                    // The queue re-armed the slot, so the next poll asks the
                    // device again. Keeping the session is the point: removing
                    // it here is what left a working keyboard next to a cursor
                    // that could never come back until the next boot.
                    crate::warn!(
                        "USB boot mouse transfer failed, retrying ({}/{}): {:?}",
                        failures,
                        MOUSE_FAILURE_RETIREMENT_THRESHOLD,
                        error,
                    );
                    return Ok(None);
                }
                crate::warn!(
                    "USB boot mouse retired after {} consecutive transfer failures: {:?}",
                    failures,
                    error,
                );
                let failed_mouse = inner.mouse.take().unwrap();
                mem::forget(failed_mouse);
                Ok(None)
            }
        }
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::vec;
    use core::{cell::Cell, future::poll_fn, task::Poll};

    use usb_if::{
        descriptor::{
            ConfigurationDescriptor, EndpointDescriptor, EndpointType, InterfaceDescriptor,
            InterfaceDescriptors,
        },
        transfer::Direction,
    };

    use super::{
        BootHidInterfaceError, BootHidKind, DriveError, MouseReportLayout, UsbKeyboardError,
        UsbKeyboardStage, XHCI_CAPABILITY_ACCESSORS_LEN, XHCI_MIN_CAPLENGTH, XhciMmioError,
        classify_boot_interface, drive_with, find_boot_hid_interface, new_usb_kernel_op,
        parse_mouse_report_descriptor, validate_xhci_mapping_properties, validate_xhci_mmio_with,
    };
    use crate::{
        mm::{CachePolicy, dma::DmaWindow},
        prelude::ktest,
    };

    const VALID_CAPLENGTH_HCIVERSION: u32 = 0x0110_0020;
    const VALID_HCSPARAMS1: u32 = 0x0100_0101;
    const VALID_DOORBELL_OFFSET: u32 = 0x0480;
    const VALID_RUNTIME_OFFSET: u32 = 0x0440;

    fn validate_layout(
        mmio_size: usize,
        caplength_hciversion: u32,
        hcsparams1: u32,
        hccparams1: u32,
        doorbell_offset: u32,
        runtime_offset: u32,
        extended_registers: &[(usize, u32)],
    ) -> Result<(), XhciMmioError> {
        validate_xhci_mmio_with(mmio_size, |offset| {
            Ok(match offset {
                0x00 => caplength_hciversion,
                0x04 => hcsparams1,
                0x10 => hccparams1,
                0x14 => doorbell_offset,
                0x18 => runtime_offset,
                _ => extended_registers
                    .iter()
                    .find_map(|(register_offset, value)| {
                        (*register_offset == offset).then_some(*value)
                    })
                    .unwrap_or_else(|| panic!("unexpected xHCI register read at {offset:#x}")),
            })
        })
    }

    fn validate_standard_layout(
        mmio_size: usize,
        hccparams1: u32,
        extended_registers: &[(usize, u32)],
    ) -> Result<(), XhciMmioError> {
        validate_layout(
            mmio_size,
            VALID_CAPLENGTH_HCIVERSION,
            VALID_HCSPARAMS1,
            hccparams1,
            VALID_DOORBELL_OFFSET,
            VALID_RUNTIME_OFFSET,
            extended_registers,
        )
    }

    #[ktest]
    fn accepts_valid_fixed_layout_without_extended_capabilities() {
        assert_eq!(validate_standard_layout(0x500, 1, &[]), Ok(()));
        assert_eq!(XHCI_MIN_CAPLENGTH, 0x20);
        assert_eq!(XHCI_CAPABILITY_ACCESSORS_LEN, 0x24);
    }

    #[ktest]
    fn accepts_extended_capability_ending_at_mmio_end() {
        let offset = 0x4f8;
        let hccparams1 = ((offset / size_of::<u32>()) as u32) << 16 | 1;

        assert_eq!(
            validate_standard_layout(0x500, hccparams1, &[(offset, 1)]),
            Ok(())
        );
    }

    #[ktest]
    fn accepts_qemu_supported_protocols_before_operational_registers() {
        let qemu_caplength_hciversion = 0x0100_0040;
        let qemu_hcsparams1 = 0x0800_1040;
        let qemu_hccparams1 = 0x0008_7001;
        let supported_protocols = [
            (0x20, 0x0200_0402),
            (0x28, 0x0000_0405),
            (0x30, 0x0300_0002),
            (0x38, 0x0000_0401),
        ];

        assert_eq!(
            validate_layout(
                0x4000,
                qemu_caplength_hciversion,
                qemu_hcsparams1,
                qemu_hccparams1,
                0x2000,
                0x1000,
                &supported_protocols,
            ),
            Ok(())
        );
    }

    #[ktest]
    fn rejects_extended_capability_overlapping_base_capability_registers() {
        let offset = XHCI_MIN_CAPLENGTH - size_of::<u32>();
        let hccparams1 = ((offset / size_of::<u32>()) as u32) << 16 | 1;

        assert_eq!(
            validate_standard_layout(0x500, hccparams1, &[(offset, 1)]),
            Err(XhciMmioError::InvalidExtendedCapability)
        );
    }

    #[ktest]
    fn rejects_extended_capability_overlapping_operational_registers() {
        let offset = XHCI_MIN_CAPLENGTH;
        let hccparams1 = ((offset / size_of::<u32>()) as u32) << 16 | 1;

        assert_eq!(
            validate_standard_layout(0x500, hccparams1, &[(offset, 1)]),
            Err(XhciMmioError::InvalidExtendedCapability)
        );
    }

    #[ktest]
    fn rejects_overlapping_fixed_controller_regions() {
        let cases = [
            (0x0420, VALID_DOORBELL_OFFSET),
            (VALID_RUNTIME_OFFSET, 0x0460),
        ];

        for (runtime_offset, doorbell_offset) in cases {
            assert_eq!(
                validate_layout(
                    0x500,
                    VALID_CAPLENGTH_HCIVERSION,
                    VALID_HCSPARAMS1,
                    1,
                    doorbell_offset,
                    runtime_offset,
                    &[],
                ),
                Err(XhciMmioError::InvalidRegisterLayout)
            );
        }
    }

    #[ktest]
    fn rejects_extended_capability_next_pointer_outside_mmio() {
        let result = validate_standard_layout(0x4f0, 0x0040_0001, &[(0x100, 0x0000_ff01)]);

        assert_eq!(result, Err(XhciMmioError::InvalidExtendedCapability));
    }

    #[ktest]
    fn rejects_all_msi_capabilities_until_dependency_is_fixed() {
        let cases = [
            (0x4f4, 0x0000_0005),
            (0x4f0, 0x0080_0005),
            (0x4ec, 0x0100_0005),
            (0x4e8, 0x0180_0005),
            // A 64-bit MSI capability remains valid at a 4-mod-8 dword address.
            (0x4ec, 0x0080_0005),
        ];

        for (offset, header) in cases {
            let hccparams1 = ((offset / size_of::<u32>()) as u32) << 16 | 1;
            assert_eq!(
                validate_standard_layout(0x500, hccparams1, &[(offset, header)]),
                Err(XhciMmioError::UnsupportedExtendedCapability),
                "MSI capability at {offset:#x} with header {header:#x}",
            );
        }
    }

    #[ktest]
    fn accepts_supported_local_memory_and_unknown_capability_boundaries() {
        let cases: &[(usize, &[(usize, u32)])] = &[
            (0x4e8, &[(0x4e8, 2), (0x4f0, 0x2000_0000)]),
            (0x4f8, &[(0x4f8, 6), (0x4fc, 0)]),
            (0x4fc, &[(0x4fc, 0xff)]),
        ];

        for (offset, registers) in cases {
            let hccparams1 = ((*offset / size_of::<u32>()) as u32) << 16 | 1;
            assert_eq!(
                validate_standard_layout(0x500, hccparams1, registers),
                Ok(()),
                "extended capability boundary at {offset:#x}",
            );
        }
    }

    #[ktest]
    fn rejects_out_of_bounds_controller_regions() {
        let cases = [
            (
                "capability accessors",
                0x23,
                VALID_CAPLENGTH_HCIVERSION,
                VALID_HCSPARAMS1,
                1,
                0x20,
                0x40,
            ),
            (
                "port registers",
                0x42f,
                VALID_CAPLENGTH_HCIVERSION,
                VALID_HCSPARAMS1,
                1,
                0x100,
                0x200,
            ),
            (
                "doorbells",
                0x500,
                VALID_CAPLENGTH_HCIVERSION,
                VALID_HCSPARAMS1,
                1,
                0x4fc,
                VALID_RUNTIME_OFFSET,
            ),
            (
                "runtime interrupters",
                0x500,
                VALID_CAPLENGTH_HCIVERSION,
                VALID_HCSPARAMS1,
                1,
                VALID_DOORBELL_OFFSET,
                0x4e0,
            ),
            (
                "extended capability head",
                0x500,
                VALID_CAPLENGTH_HCIVERSION,
                VALID_HCSPARAMS1,
                0x0140_0001,
                VALID_DOORBELL_OFFSET,
                VALID_RUNTIME_OFFSET,
            ),
        ];

        for (name, size, caplength, hcsparams1, hccparams1, doorbell, runtime) in cases {
            assert!(
                validate_layout(
                    size,
                    caplength,
                    hcsparams1,
                    hccparams1,
                    doorbell,
                    runtime,
                    &[],
                )
                .is_err(),
                "accepted out-of-bounds {name}",
            );
        }
    }

    #[ktest]
    fn requires_unique_uncacheable_mmio() {
        assert_eq!(
            validate_xhci_mapping_properties(CachePolicy::Uncacheable, true),
            Ok(())
        );
        assert_eq!(
            validate_xhci_mapping_properties(CachePolicy::Uncacheable, false),
            Err(XhciMmioError::InvalidMappingProperties)
        );
        assert_eq!(
            validate_xhci_mapping_properties(CachePolicy::Writeback, true),
            Err(XhciMmioError::InvalidMappingProperties)
        );
    }

    #[ktest]
    fn different_dma_windows_get_independent_kernel_adapters() {
        let first_window = DmaWindow::new(0x2000, 0x1000, 0x1000).unwrap();
        let second_window = DmaWindow::new(0x5000, 0x1000, 0x1000).unwrap();

        let first = new_usb_kernel_op(first_window);
        let second = new_usb_kernel_op(second_window);

        assert!(!core::ptr::eq(&*first, &*second));
        assert_eq!(
            first.translate_for_test(0x1800..0x1801).unwrap().start,
            0x2800
        );
        assert_eq!(
            second.translate_for_test(0x1800..0x1801).unwrap().start,
            0x5800
        );
    }

    #[ktest]
    fn completes_a_future_while_pumping_controller_events() {
        let mut future_polls = 0;
        let mut event_pumps = 0;
        let output = drive_with(
            poll_fn(|_| {
                future_polls += 1;
                if future_polls == 3 {
                    Poll::Ready(7)
                } else {
                    Poll::Pending
                }
            }),
            || event_pumps += 1,
            || false,
            || {},
        )
        .unwrap();

        assert_eq!(output, 7);
        assert_eq!(event_pumps, 2);
    }

    #[ktest]
    fn stops_pumping_when_the_deadline_expires() {
        let event_pumps = Cell::new(0);
        let result = drive_with(
            poll_fn(|_| Poll::<()>::Pending),
            || event_pumps.set(event_pumps.get() + 1),
            || event_pumps.get() == 2,
            || {},
        );

        assert_eq!(result, Err(DriveError::Timeout));
        assert_eq!(event_pumps.get(), 2);
    }

    #[ktest]
    fn timeout_error_identifies_the_controller_stage() {
        assert_ne!(
            UsbKeyboardError::Timeout(UsbKeyboardStage::HostInit),
            UsbKeyboardError::Timeout(UsbKeyboardStage::Enumeration)
        );
    }

    #[ktest]
    fn classifies_only_hid_boot_keyboard_and_mouse_interfaces() {
        assert_eq!(
            classify_boot_interface(0x03, 0x01, 0x01),
            Some(BootHidKind::Keyboard)
        );
        assert_eq!(
            classify_boot_interface(0x03, 0x01, 0x02),
            Some(BootHidKind::Mouse)
        );
        for fields in [(0x03, 0x00, 0x02), (0x03, 0x01, 0x00), (0xff, 0x01, 0x02)] {
            assert_eq!(classify_boot_interface(fields.0, fields.1, fields.2), None);
        }
    }

    /// The report descriptor the attached Megrez mouse returned.
    const MEGREZ_MOUSE_DESCRIPTOR: [u8; 64] = [
        0x05, 0x01, 0x09, 0x02, 0xa1, 0x01, 0x09, 0x01, 0xa1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29,
        0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02, 0x95, 0x01, 0x75, 0x05,
        0x81, 0x01, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x00, 0xf8, 0x26, 0xff, 0x07, 0x75,
        0x0c, 0x95, 0x02, 0x81, 0x06, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7f, 0x75, 0x08, 0x95, 0x01,
        0x81, 0x06, 0xc0, 0xc0,
    ];

    #[ktest]
    fn derives_every_mouse_field_offset_from_the_descriptor() {
        // The device describes three button bits, five constant padding bits,
        // two twelve-bit displacements and an eight-bit wheel, so X starts at
        // bit 8, Y at bit 20, the wheel at bit 32 and the report is five bytes.
        assert_eq!(
            parse_mouse_report_descriptor(&MEGREZ_MOUSE_DESCRIPTOR),
            Some(MouseReportLayout {
                buttons_offset: 0,
                buttons_bits: 3,
                x_offset: 8,
                x_bits: 12,
                y_offset: 20,
                y_bits: 12,
                wheel_offset: Some(32),
                wheel_bits: 8,
                report_bytes: 5,
            })
        );
    }

    #[ktest]
    fn refuses_descriptors_it_cannot_read_in_full() {
        // A report ID prefixes every report with a byte the layout omits.
        let mut with_report_id = [0u8; 8];
        with_report_id[..4].copy_from_slice(&[0x85, 0x01, 0x09, 0x01]);
        assert_eq!(parse_mouse_report_descriptor(&with_report_id), None);
        // A truncated descriptor never reaches the axes.
        assert_eq!(
            parse_mouse_report_descriptor(&MEGREZ_MOUSE_DESCRIPTOR[..20]),
            None
        );
        assert_eq!(parse_mouse_report_descriptor(&[]), None);
    }

    #[ktest]
    fn completes_discovery_with_either_hid_kind_at_the_deadline() {
        use super::DiscoveryDecision::{Complete, Continue, Missing};

        assert_eq!(super::discovery_decision(false, false, false), Continue);
        assert_eq!(super::discovery_decision(true, false, false), Continue);
        assert_eq!(super::discovery_decision(false, true, false), Continue);
        assert_eq!(super::discovery_decision(true, true, false), Complete);
        assert_eq!(super::discovery_decision(true, false, true), Complete);
        assert_eq!(super::discovery_decision(false, true, true), Complete);
        assert_eq!(super::discovery_decision(false, false, true), Missing);
    }

    fn boot_configuration(protocol: u8, max_packet_size: u16) -> ConfigurationDescriptor {
        ConfigurationDescriptor {
            num_interfaces: 1,
            configuration_value: 1,
            attributes: 0x80,
            max_power: 0,
            string_index: None,
            string: None,
            interfaces: vec![InterfaceDescriptors {
                interface_number: 0,
                alt_settings: vec![InterfaceDescriptor {
                    interface_number: 0,
                    alternate_setting: 0,
                    class: 0x03,
                    subclass: 0x01,
                    protocol,
                    string_index: None,
                    string: None,
                    num_endpoints: 1,
                    endpoints: vec![EndpointDescriptor {
                        address: 0x81,
                        max_packet_size,
                        transfer_type: EndpointType::Interrupt,
                        direction: Direction::In,
                        packets_per_microframe: 1,
                        interval: 10,
                    }],
                }],
            }],
            raw: vec![],
        }
    }

    #[ktest]
    fn selects_one_exact_interrupt_in_endpoint_for_each_boot_kind() {
        let keyboard = boot_configuration(0x01, 8);
        let mouse = boot_configuration(0x02, 4);

        assert_eq!(
            find_boot_hid_interface(&[keyboard]).unwrap().unwrap().kind,
            BootHidKind::Keyboard
        );
        assert_eq!(
            find_boot_hid_interface(&[mouse]).unwrap().unwrap().kind,
            BootHidKind::Mouse
        );
    }

    #[ktest]
    fn rejects_short_mouse_packets_and_ambiguous_interrupt_endpoints() {
        let short_mouse = boot_configuration(0x02, 2);
        assert_eq!(
            find_boot_hid_interface(&[short_mouse]),
            Err(BootHidInterfaceError::InvalidEndpoint)
        );

        let mut ambiguous = boot_configuration(0x02, 4);
        let duplicate = ambiguous.interfaces[0].alt_settings[0].endpoints[0].clone();
        ambiguous.interfaces[0].alt_settings[0]
            .endpoints
            .push(duplicate);
        assert_eq!(
            find_boot_hid_interface(&[ambiguous]),
            Err(BootHidInterfaceError::InvalidEndpoint)
        );
    }
}
