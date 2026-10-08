// SPDX-License-Identifier: MPL-2.0

//! Console-device registration and common I/O contracts.
//!
//! [`AnyConsoleDevice`] provides diagnostic output and exposes optional TTY and input capabilities.
//! Output-change callbacks wake TTY readiness observers.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

use alloc::{collections::BTreeMap, fmt::Debug, string::String, sync::Arc, vec::Vec};
use core::any::Any;

use component::{ComponentInitError, init_component};
use ostd::{
    mm::{Infallible, VmReader},
    sync::{LocalIrqDisabled, SpinLock, SpinLockGuard},
};
use spin::Once;

pub type ConsoleReceiveCallback = dyn Fn(VmReader<Infallible>) + Send + Sync;
pub type ConsoleOutputChangeCallback = dyn Fn() + Send + Sync;

/// An error from an immediate TTY send attempt.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConsoleSendError {
    /// The device cannot currently accept the first byte.
    Busy,
    /// The device could not complete an immediate send because of an I/O error or timeout.
    Io,
}

/// An error from polling whether a console can send data.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConsoleSendReadyError {
    /// Device access failed.
    Io,
}

impl From<ConsoleSendReadyError> for ConsoleSendError {
    fn from(_error: ConsoleSendReadyError) -> Self {
        Self::Io
    }
}

/// An error from installing an output-change callback.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SetOutputChangeCallbackError {
    /// The console does not provide output-change notifications.
    Unsupported,
    /// A callback was already installed.
    AlreadyInstalled,
}

/// Immediate, fallible output used by a TTY driver.
pub trait TtyConsoleDevice: Send + Sync + Debug {
    /// Polls whether an immediate TTY send can claim the device now.
    ///
    /// If this returns `Ok(false)`, the device may arm a later send-readiness notification.
    fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        Ok(true)
    }

    /// Immediately attempts to send TTY data.
    ///
    /// `Ok(n)` means that exactly the first `n` bytes were accepted, where `n <= buf.len()`.
    /// An error may be returned only when no byte was accepted.
    /// This method returns [`ConsoleSendError::Busy`] if the first byte cannot be accepted.
    /// Blocking, retry, and signal handling belong to the generic TTY layer.
    /// Each device must explicitly state how it satisfies this delivery contract.
    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError>;

    /// Installs the callback invoked when output readiness may have changed.
    ///
    /// The callback may run synchronously from another console operation or in interrupt context.
    /// It must therefore never sleep.
    ///
    /// Returns an error if notifications are unsupported or a callback was already installed.
    fn try_set_output_change_callback(
        &self,
        _callback_fn: &'static ConsoleOutputChangeCallback,
    ) -> Result<(), SetOutputChangeCallbackError> {
        Err(SetOutputChangeCallbackError::Unsupported)
    }
}

/// Input delivered by a console device.
pub trait InputConsoleDevice: Send + Sync + Debug {
    /// Registers a callback that will be invoked when the console device receives data.
    ///
    /// The callback may be called in the interrupt context. Therefore, it should _never_ sleep.
    fn register_receive_callback(&self, callback_fn: &'static ConsoleReceiveCallback);
}

/// A registered console with diagnostic output and optional runtime capabilities.
pub trait AnyConsoleDevice: Send + Sync + Any + Debug {
    /// Sends diagnostic data, applying the device's fatal-error policy when delivery fails.
    ///
    /// A fatal UART error prints a panic backtrace and restarts the machine.
    fn send_diagnostic_or_restart(&self, buf: &[u8]);

    /// Returns the immediate TTY-output capability when the device provides one.
    fn tty(&self) -> Option<&dyn TtyConsoleDevice> {
        None
    }

    /// Returns the input capability when the device provides one.
    fn input(&self) -> Option<&dyn InputConsoleDevice> {
        None
    }
}

pub fn register_device(name: String, device: Arc<dyn AnyConsoleDevice>) {
    COMPONENT
        .get()
        .unwrap()
        .console_device_table
        .lock()
        .insert(name, device);
}

pub fn all_devices() -> Vec<(String, Arc<dyn AnyConsoleDevice>)> {
    let console_devices = COMPONENT.get().unwrap().console_device_table.lock();
    console_devices
        .iter()
        .map(|(name, device)| (name.clone(), device.clone()))
        .collect()
}

pub fn all_devices_lock<'a>()
-> SpinLockGuard<'a, BTreeMap<String, Arc<dyn AnyConsoleDevice>>, LocalIrqDisabled> {
    COMPONENT.get().unwrap().console_device_table.lock()
}

static COMPONENT: Once<Component> = Once::new();

#[init_component]
fn component_init() -> Result<(), ComponentInitError> {
    let component = Component::init()?;
    COMPONENT.call_once(|| component);
    Ok(())
}

#[derive(Debug)]
struct Component {
    console_device_table: SpinLock<BTreeMap<String, Arc<dyn AnyConsoleDevice>>, LocalIrqDisabled>,
}

impl Component {
    pub fn init() -> Result<Self, ComponentInitError> {
        Ok(Self {
            console_device_table: SpinLock::new(BTreeMap::new()),
        })
    }
}
