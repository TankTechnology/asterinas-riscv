// SPDX-License-Identifier: MPL-2.0

use alloc::format;

use aster_console::{
    AnyConsoleDevice, ConsoleSendError, ConsoleSendReadyError, SetOutputChangeCallbackError,
    TtyConsoleDevice,
};
use ostd::mm::Infallible;
use spin::Once;

use super::{Tty, TtyDriver};
use crate::{
    device::{
        DevtmpfsInodeMeta,
        registry::char,
        tty::{file::TtyFile, termio::CTermios},
    },
    fs::file::PerOpenFileOps,
    prelude::*,
};

/// The driver for serial devices.
#[derive(Clone)]
pub(super) struct SerialDriver {
    console: Arc<dyn AnyConsoleDevice>,
}

impl SerialDriver {
    const MINOR_ID_BASE: u32 = 64;

    fn tty_console(&self) -> &dyn TtyConsoleDevice {
        self.console
            .tty()
            .expect("a serial console must provide TTY output")
    }
}

impl TtyDriver for SerialDriver {
    // Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/linux/major.h#L18>.
    const DEVICE_MAJOR_ID: u32 = 4;
    // The serial driver owns only the console transport, which cannot retain a file description.
    const SCM_RIGHTS_PROVEN_LEAF: bool = true;

    fn devtmpfs_meta(&self, index: u32) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new(format!(
            "ttyS{}",
            index - Self::MINOR_ID_BASE
        )))
    }

    fn open(tty: Arc<Tty<Self>>) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(TtyFile::new(tty)))
    }

    fn push_output(&self, chs: &[u8]) -> Result<usize> {
        match self.tty_console().try_send_tty(chs) {
            Ok(len) => Ok(len),
            Err(ConsoleSendError::Busy) => {
                return_errno_with_message!(Errno::EAGAIN, "the serial transmitter is busy")
            }
            Err(ConsoleSendError::Io) => {
                return_errno_with_message!(Errno::EIO, "the serial transmitter failed")
            }
        }
    }

    fn echo_callback(&self) -> impl FnMut(&[u8]) + '_ {
        |chs| self.console.send_diagnostic_or_restart(chs)
    }

    fn poll_output_ready(&self) -> Result<bool> {
        match self.tty_console().poll_send_ready() {
            Ok(ready) => Ok(ready),
            Err(ConsoleSendReadyError::Io) => {
                return_errno_with_message!(Errno::EIO, "the serial transmitter failed")
            }
        }
    }

    fn notify_input(&self) {}

    fn on_termios_change(&self, _old_termios: &CTermios, _new_termios: &CTermios) {}
}

static SERIAL0: Once<Arc<Tty<SerialDriver>>> = Once::new();

/// Returns the `ttyS0` device.
///
/// Returns `None` if the device is not found nor initialized.
pub(super) fn serial0_device() -> Option<&'static Arc<Tty<SerialDriver>>> {
    SERIAL0.get()
}

pub(super) fn init_in_first_process() -> Result<()> {
    let devices = aster_console::all_devices();

    // Initialize the `ttyS0` device if the serial console is available.

    let serial_console = devices
        .iter()
        .find(|(name, _)| name.as_str() == aster_uart::CONSOLE_NAME)
        .map(|(_, device)| device.clone());

    if let Some(serial_console) = serial_console {
        if serial_console.tty().is_none() || serial_console.input().is_none() {
            return_errno_with_message!(
                Errno::EIO,
                "the registered serial console has incomplete capabilities"
            );
        }
        let driver = SerialDriver {
            console: serial_console.clone(),
        };
        let serial0 = Tty::new(SerialDriver::MINOR_ID_BASE, driver);

        SERIAL0.call_once(|| serial0.clone());
        char::register(serial0.clone())?;

        let output_tty = serial0.clone();
        let output_change_callback_fn = Box::leak(Box::new(move || {
            output_tty.notify_output_change();
        }));
        match serial_console
            .tty()
            .unwrap()
            .try_set_output_change_callback(output_change_callback_fn)
        {
            Ok(()) | Err(SetOutputChangeCallbackError::Unsupported) => {}
            Err(SetOutputChangeCallbackError::AlreadyInstalled) => {
                return_errno_with_message!(
                    Errno::EBUSY,
                    "the serial output callback is already installed"
                )
            }
        }
        serial_console
            .input()
            .unwrap()
            .register_receive_callback(Box::leak(Box::new(
                move |mut reader: VmReader<Infallible>| {
                    let mut chs = vec![0u8; reader.remain()];
                    reader.read(&mut VmWriter::from(chs.as_mut_slice()));
                    let _ = serial0.push_input(chs.as_slice());
                },
            )));
    }

    Ok(())
}

#[cfg(ktest)]
mod tests {
    use core::{
        fmt::Debug,
        sync::atomic::{AtomicBool, AtomicUsize, Ordering},
    };

    use aster_console::ConsoleOutputChangeCallback;
    use ostd::prelude::ktest;

    use super::*;
    use crate::{events::IoEvents, fs::file::StatusFlags, process::signal::Pollable};

    #[derive(Debug)]
    struct TestConsole {
        tty_send_calls: AtomicUsize,
        send_result: Result<usize, ConsoleSendError>,
        send_ready: Result<bool, ConsoleSendReadyError>,
        poll_send_ready_calls: AtomicUsize,
    }

    enum TestSendReadiness {
        Ready,
        Backpressured,
        Failed,
    }

    impl TestConsole {
        fn new(send_result: Result<usize, ConsoleSendError>, readiness: TestSendReadiness) -> Self {
            Self {
                tty_send_calls: AtomicUsize::new(0),
                send_result,
                send_ready: match readiness {
                    TestSendReadiness::Ready => Ok(true),
                    TestSendReadiness::Backpressured => Ok(false),
                    TestSendReadiness::Failed => Err(ConsoleSendReadyError::Io),
                },
                poll_send_ready_calls: AtomicUsize::new(0),
            }
        }
    }

    #[derive(Debug, Default)]
    struct LegacyConsole {
        send_calls: AtomicUsize,
    }

    struct CapacityChangingConsole {
        send_ready: AtomicBool,
        output_change_callback_fn: Once<&'static ConsoleOutputChangeCallback>,
    }

    impl Debug for CapacityChangingConsole {
        fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
            f.debug_struct("CapacityChangingConsole")
                .finish_non_exhaustive()
        }
    }

    impl AnyConsoleDevice for TestConsole {
        fn send_diagnostic_or_restart(&self, _buf: &[u8]) {}

        fn tty(&self) -> Option<&dyn TtyConsoleDevice> {
            Some(self)
        }
    }

    impl TtyConsoleDevice for TestConsole {
        fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
            self.poll_send_ready_calls.fetch_add(1, Ordering::Relaxed);
            self.send_ready
        }

        fn try_send_tty(&self, _buf: &[u8]) -> Result<usize, ConsoleSendError> {
            self.tty_send_calls.fetch_add(1, Ordering::Relaxed);
            self.send_result
        }
    }

    impl AnyConsoleDevice for LegacyConsole {
        fn send_diagnostic_or_restart(&self, _buf: &[u8]) {
            self.send_calls.fetch_add(1, Ordering::Relaxed);
        }

        fn tty(&self) -> Option<&dyn TtyConsoleDevice> {
            Some(self)
        }
    }

    impl TtyConsoleDevice for LegacyConsole {
        fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
            self.send_diagnostic_or_restart(buf);
            Ok(buf.len())
        }
    }

    impl AnyConsoleDevice for CapacityChangingConsole {
        fn send_diagnostic_or_restart(&self, _buf: &[u8]) {
            self.send_ready.store(false, Ordering::Relaxed);
            if let Some(callback_fn) = self.output_change_callback_fn.get() {
                callback_fn();
            }
        }

        fn tty(&self) -> Option<&dyn TtyConsoleDevice> {
            Some(self)
        }
    }

    impl TtyConsoleDevice for CapacityChangingConsole {
        fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
            Ok(self.send_ready.load(Ordering::Relaxed))
        }

        fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
            self.send_diagnostic_or_restart(buf);
            Ok(buf.len())
        }

        fn try_set_output_change_callback(
            &self,
            callback_fn: &'static ConsoleOutputChangeCallback,
        ) -> Result<(), SetOutputChangeCallbackError> {
            let mut installed = false;
            self.output_change_callback_fn.call_once(|| {
                installed = true;
                callback_fn
            });
            if installed {
                Ok(())
            } else {
                Err(SetOutputChangeCallbackError::AlreadyInstalled)
            }
        }
    }

    #[ktest]
    fn serial_output_uses_the_fallible_tty_path() {
        let console = Arc::new(TestConsole::new(Ok(1), TestSendReadiness::Ready));
        let driver = SerialDriver {
            console: console.clone(),
        };

        assert_eq!(driver.push_output(b"x").unwrap(), 1);
        assert_eq!(console.tty_send_calls.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn explicit_infallible_adapter_preserves_console_behavior() {
        let console = Arc::new(LegacyConsole::default());
        let driver = SerialDriver {
            console: console.clone(),
        };

        assert_eq!(driver.push_output(b"x").unwrap(), 1);
        assert_eq!(console.send_calls.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn serial_writability_follows_the_console() {
        let driver = SerialDriver {
            console: Arc::new(TestConsole::new(
                Err(ConsoleSendError::Busy),
                TestSendReadiness::Backpressured,
            )),
        };

        assert!(!driver.poll_output_ready().unwrap());
    }

    #[ktest]
    fn serial_eagain_is_not_reported_as_writable() {
        let console = Arc::new(TestConsole::new(
            Err(ConsoleSendError::Busy),
            TestSendReadiness::Backpressured,
        ));
        let tty = Tty::new(
            SerialDriver::MINOR_ID_BASE,
            SerialDriver {
                console: console.clone(),
            },
        );
        let mut reader = VmReader::from(b"x".as_slice()).to_fallible();

        let error = tty.write(&mut reader, StatusFlags::O_NONBLOCK).unwrap_err();
        assert_eq!(error.error(), Errno::EAGAIN);
        assert_eq!(console.poll_send_ready_calls.load(Ordering::Relaxed), 1);
        assert!(tty.poll(IoEvents::OUT, None).is_empty());
    }

    #[ktest]
    fn readiness_io_error_becomes_eio_and_is_not_writable() {
        let tty = Tty::new(
            SerialDriver::MINOR_ID_BASE,
            SerialDriver {
                console: Arc::new(TestConsole::new(
                    Err(ConsoleSendError::Busy),
                    TestSendReadiness::Failed,
                )),
            },
        );
        let mut reader = VmReader::from(b"x".as_slice()).to_fallible();

        let error = tty.write(&mut reader, StatusFlags::empty()).unwrap_err();
        assert_eq!(error.error(), Errno::EIO);
        assert_eq!(reader.remain(), 1);
        let events = tty.poll(IoEvents::OUT, None);
        assert!(!events.contains(IoEvents::OUT));
        assert!(events.contains(IoEvents::ERR));
    }

    #[ktest]
    fn diagnostic_output_invalidates_cached_writability() {
        let console = Arc::new(CapacityChangingConsole {
            send_ready: AtomicBool::new(true),
            output_change_callback_fn: Once::new(),
        });
        let tty = Tty::new(
            SerialDriver::MINOR_ID_BASE,
            SerialDriver {
                console: console.clone(),
            },
        );
        let output_tty = tty.clone();
        assert_eq!(
            console.try_set_output_change_callback(Box::leak(Box::new(move || {
                output_tty.notify_output_change();
            }))),
            Ok(())
        );

        assert_eq!(tty.poll(IoEvents::OUT, None), IoEvents::OUT);
        console.send_diagnostic_or_restart(b"log");

        assert!(tty.poll(IoEvents::OUT, None).is_empty());
    }

    #[ktest]
    fn serial_io_error_becomes_eio_without_advancing_input() {
        let tty = Tty::new(
            SerialDriver::MINOR_ID_BASE,
            SerialDriver {
                console: Arc::new(TestConsole::new(
                    Err(ConsoleSendError::Io),
                    TestSendReadiness::Ready,
                )),
            },
        );
        let mut reader = VmReader::from(b"x".as_slice()).to_fallible();

        let error = tty.write(&mut reader, StatusFlags::O_NONBLOCK).unwrap_err();
        assert_eq!(error.error(), Errno::EIO);
        assert_eq!(reader.remain(), 1);
    }

    #[ktest]
    fn serial_busy_error_becomes_eagain() {
        let driver = SerialDriver {
            console: Arc::new(TestConsole::new(
                Err(ConsoleSendError::Busy),
                TestSendReadiness::Backpressured,
            )),
        };

        let error = driver.push_output(b"x").unwrap_err();
        assert_eq!(error.error(), Errno::EAGAIN);
    }
}
