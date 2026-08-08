// SPDX-License-Identifier: MPL-2.0

//! Polling USB host support for firmware-configured RISC-V controllers.

mod report_queue;

use core::{
    mem::{self, ManuallyDrop},
    pin::pin,
    task::{Context, Poll, Waker},
    time::Duration,
};

use crab_usb::{Device, Endpoint, EventHandler, USBHost};
use spin::Once;
use usb_if::{
    descriptor::{ConfigurationDescriptor, EndpointType},
    endpoint::{RequestId, TransferCompletion, TransferRequest},
    host::ControlSetup,
    transfer::{Direction, Recipient, Request, RequestType},
};

use self::report_queue::{BootKeyboardReportQueue, ReportEndpoint};
use crate::{
    arch,
    io::IoMem,
    mm::dma::{DmaWindow, UsbKernelOp},
    task::Task,
};

const HOST_OPERATION_TIMEOUT: Duration = Duration::from_secs(5);
const KEYBOARD_DISCOVERY_TIMEOUT: Duration = Duration::from_secs(30);
const BOOT_KEYBOARD_REPORT_LEN: usize = 8;

static USB_KERNEL_OP: Once<UsbKernelOp> = Once::new();

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DriveError {
    Timeout,
}

/// A failure while starting or polling a USB boot keyboard.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsbKeyboardError {
    /// CrabUSB could not create the xHCI host.
    HostCreate,
    /// xHCI initialization failed.
    HostInit,
    /// A controller operation exceeded its deadline.
    Timeout(UsbKeyboardStage),
    /// USB device enumeration failed.
    Enumeration,
    /// No boot-protocol USB keyboard appeared before the discovery deadline.
    KeyboardNotFound,
    /// CrabUSB could not open the selected keyboard.
    DeviceOpen,
    /// The boot-keyboard interface could not be claimed.
    ClaimInterface,
    /// The keyboard rejected HID boot protocol.
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
    /// Select HID boot protocol for the keyboard.
    SetBootProtocol,
}

/// Identity of the selected USB boot keyboard.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UsbKeyboardInfo {
    /// USB vendor identifier.
    pub vendor_id: u16,
    /// USB product identifier.
    pub product_id: u16,
}

#[derive(Clone, Copy, Debug)]
struct BootKeyboardInterface {
    number: u8,
    alternate: u8,
    endpoint: u8,
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

fn timeout_at(stage: UsbKeyboardStage) -> UsbKeyboardError {
    if let Some(kernel_op) = USB_KERNEL_OP.get() {
        kernel_op.log_dma_snapshot();
    }
    UsbKeyboardError::Timeout(stage)
}

fn find_boot_keyboard(configurations: &[ConfigurationDescriptor]) -> Option<BootKeyboardInterface> {
    let configuration = configurations.first()?;
    configuration.interfaces.iter().find_map(|interface| {
        interface.alt_settings.iter().find_map(|alternate| {
            if (alternate.class, alternate.subclass, alternate.protocol) != (0x03, 0x01, 0x01) {
                return None;
            }
            alternate.endpoints.iter().find_map(|endpoint| {
                (endpoint.transfer_type == EndpointType::Interrupt
                    && endpoint.direction == Direction::In)
                    .then_some(BootKeyboardInterface {
                        number: alternate.interface_number,
                        alternate: alternate.alternate_setting,
                        endpoint: endpoint.address,
                    })
            })
        })
    })
}

fn abandon_host(mmio: IoMem, host: USBHost, events: EventHandler) {
    // The controller may still own DMA rings. CrabUSB has no shutdown API, so leaking this
    // one failed host is safer than freeing memory that the controller can still access.
    mem::forget((mmio, host, events));
}

fn abandon_open_device(mmio: IoMem, host: USBHost, events: EventHandler, device: Device) {
    // The opened device can also own endpoint rings that remain visible to the controller.
    mem::forget((mmio, host, events, device));
}

/// A polling USB HID boot keyboard backed by CrabUSB's xHCI driver.
///
/// # Safety
///
/// `Send` and `Sync` are sound because the keyboard is only ever accessed
/// through a `Mutex` from a single worker task, the xHCI MMIO is managed by
/// OSTD's `IoMem`, and CrabUSB's event ring is internally synchronized. The
/// underlying controller is a single logical device; there is no per-thread
/// state to race.
pub struct PollingUsbKeyboard {
    // CrabUSB has no controller shutdown API. Keep DMA-visible state alive even if the polling
    // worker exits after a transfer error.
    inner: ManuallyDrop<PollingUsbKeyboardInner>,
}

struct PollingUsbKeyboardInner {
    _mmio: IoMem,
    _host: USBHost,
    events: EventHandler,
    _device: Device,
    endpoint: Endpoint,
    reports: BootKeyboardReportQueue,
    info: UsbKeyboardInfo,
}

impl ReportEndpoint for Endpoint {
    fn submit_report(
        &mut self,
        report: &mut [u8; BOOT_KEYBOARD_REPORT_LEN],
    ) -> Result<RequestId, UsbKeyboardError> {
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

// SAFETY: See the `PollingUsbKeyboard` docs: exclusive worker-task access
// through a `Mutex`, OSTD-managed MMIO, and internally synchronized event
// ring make the type safe to share.
unsafe impl Send for PollingUsbKeyboard {}
unsafe impl Sync for PollingUsbKeyboard {}

impl PollingUsbKeyboard {
    /// Starts the firmware-configured xHCI controller and discovers one boot keyboard.
    pub fn open(mmio: IoMem, dma_window: DmaWindow) -> Result<Self, UsbKeyboardError> {
        let kernel_op = USB_KERNEL_OP.call_once(|| UsbKernelOp::new(dma_window));
        let mut host = USBHost::new_xhci(mmio.as_non_null_ptr(), kernel_op)
            .map_err(|_| UsbKeyboardError::HostCreate)?;
        let events = host.create_event_handler();

        match drive(host.init(), &events) {
            Ok(Ok(())) => {}
            Ok(Err(_)) => {
                abandon_host(mmio, host, events);
                return Err(UsbKeyboardError::HostInit);
            }
            Err(DriveError::Timeout) => {
                abandon_host(mmio, host, events);
                return Err(timeout_at(UsbKeyboardStage::HostInit));
            }
        }

        // Enable the xHCI global interrupt (USBCMD.INTE). Without it the
        // controller never asserts the event-ring interrupt, so port/reset
        // completion events are never delivered and device enumeration
        // stalls even though the keyboard is connected.
        host.enable_irq().map_err(|_| UsbKeyboardError::HostInit)?;

        let discovery_deadline = Deadline::after(KEYBOARD_DISCOVERY_TIMEOUT);
        let (device_info, interface) = loop {
            let devices = match drive(host.probe_devices(), &events) {
                Ok(Ok(devices)) => devices,
                Ok(Err(_)) => {
                    abandon_host(mmio, host, events);
                    return Err(UsbKeyboardError::Enumeration);
                }
                Err(DriveError::Timeout) => {
                    abandon_host(mmio, host, events);
                    return Err(timeout_at(UsbKeyboardStage::Enumeration));
                }
            };

            let keyboard = devices.into_iter().find_map(|device| {
                let interface = find_boot_keyboard(device.configurations())?;
                Some((device.into_device_info()?, interface))
            });
            if let Some(keyboard) = keyboard {
                break keyboard;
            }
            if discovery_deadline.expired() {
                abandon_host(mmio, host, events);
                return Err(UsbKeyboardError::KeyboardNotFound);
            }
            Task::yield_now();
        };

        let info = UsbKeyboardInfo {
            vendor_id: device_info.vendor_id(),
            product_id: device_info.product_id(),
        };
        let mut device = match drive(host.open_device(&device_info), &events) {
            Ok(Ok(device)) => device,
            Ok(Err(_)) => {
                abandon_host(mmio, host, events);
                return Err(UsbKeyboardError::DeviceOpen);
            }
            Err(DriveError::Timeout) => {
                abandon_host(mmio, host, events);
                return Err(timeout_at(UsbKeyboardStage::DeviceOpen));
            }
        };

        match drive(
            device.claim_interface(interface.number, interface.alternate),
            &events,
        ) {
            Ok(Ok(())) => {}
            Ok(Err(_)) => {
                abandon_open_device(mmio, host, events, device);
                return Err(UsbKeyboardError::ClaimInterface);
            }
            Err(DriveError::Timeout) => {
                abandon_open_device(mmio, host, events, device);
                return Err(timeout_at(UsbKeyboardStage::ClaimInterface));
            }
        }

        let set_protocol = ControlSetup {
            request_type: RequestType::Class,
            recipient: Recipient::Interface,
            request: Request::Other(0x0b),
            value: 0,
            index: u16::from(interface.number),
        };
        match drive(device.control_out(set_protocol, &[]), &events) {
            Ok(Ok(_)) => {}
            Ok(Err(_)) => {
                abandon_open_device(mmio, host, events, device);
                return Err(UsbKeyboardError::SetBootProtocol);
            }
            Err(DriveError::Timeout) => {
                abandon_open_device(mmio, host, events, device);
                return Err(timeout_at(UsbKeyboardStage::SetBootProtocol));
            }
        }

        let endpoint = match device.endpoint(interface.endpoint) {
            Ok(endpoint) => endpoint,
            Err(_) => {
                abandon_open_device(mmio, host, events, device);
                return Err(UsbKeyboardError::EndpointOpen);
            }
        };

        let mut keyboard = Self {
            inner: ManuallyDrop::new(PollingUsbKeyboardInner {
                _mmio: mmio,
                _host: host,
                events,
                _device: device,
                endpoint,
                reports: BootKeyboardReportQueue::empty(),
                info,
            }),
        };
        // Submit only after ManuallyDrop protects the complete DMA-visible ownership graph.
        let inner = &mut *keyboard.inner;
        inner.reports.fill(&mut inner.endpoint)?;
        Ok(keyboard)
    }

    /// Returns the selected keyboard's USB identity.
    pub fn info(&self) -> UsbKeyboardInfo {
        self.inner.info
    }

    /// Pumps xHCI and returns one completed eight-byte HID boot report, if available.
    pub fn poll_report(
        &mut self,
    ) -> Result<Option<[u8; BOOT_KEYBOARD_REPORT_LEN]>, UsbKeyboardError> {
        let inner = &mut *self.inner;
        inner.events.handle_event();
        let mut context = Context::from_waker(Waker::noop());
        inner.reports.poll(&mut inner.endpoint, &mut context)
    }

    /// Drives the xHCI event ring from an interrupt context.
    ///
    /// Returns `true` when a transfer activity event was seen, in which case
    /// the caller should schedule the deferred keyboard task to read the
    /// completed report. This is safe from interrupt context because
    /// [`EventHandler::handle_event`] only reads the event ring.
    pub fn handle_event_irq(&self) -> bool {
        matches!(
            self.inner.events.handle_event(),
            crab_usb::Event::TransferActivity { count } if count > 0
        )
    }
}

#[cfg(ktest)]
mod tests {
    use core::{cell::Cell, future::poll_fn, task::Poll};

    use super::{DriveError, UsbKeyboardError, UsbKeyboardStage, drive_with};
    use crate::prelude::ktest;

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
}
