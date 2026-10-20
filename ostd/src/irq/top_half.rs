// SPDX-License-Identifier: MPL-2.0

//! The top half of interrupt handling.

use core::{fmt::Debug, ops::Deref};

use id_alloc::IdAlloc;
use spin::Once;

use crate::{
    Error,
    arch::{
        irq::{HwIrqLine, IRQ_NUM_MAX, IRQ_NUM_MIN, IrqRemapping},
        trap::TrapFrame,
    },
    prelude::*,
    sync::{RwLock, SpinLock, WriteIrqDisabled},
};

/// A type alias for the IRQ callback function.
pub type IrqCallbackFunction = dyn Fn(&TrapFrame) + Sync + Send + 'static;

/// An Interrupt ReQuest (IRQ) line.
///
/// Users can use [`alloc`] or [`alloc_specific`] to allocate a (specific) IRQ line.
///
/// The IRQ number is guaranteed to be an external IRQ number and users can use [`on_active`] to
/// safely register callback functions on this IRQ line. When the IRQ line is dropped, all the
/// registered callbacks will be unregistered automatically.
///
/// [`alloc`]: Self::alloc
/// [`alloc_specific`]: Self::alloc_specific
/// [`on_active`]: Self::on_active
#[must_use]
#[derive(Debug)]
pub struct IrqLine {
    inner: Arc<InnerHandle>,
    callbacks: Vec<CallbackHandle>,
    phased_callback: Option<PhasedCallbackHandle>,
}

impl IrqLine {
    /// Allocates an available IRQ line.
    pub fn alloc() -> Result<Self> {
        get_or_init_allocator()
            .lock()
            .alloc()
            .map(|id| Self::new(id as u8))
            .ok_or(Error::NotEnoughResources)
    }

    /// Allocates a specific IRQ line.
    pub fn alloc_specific(irq_num: u8) -> Result<Self> {
        get_or_init_allocator()
            .lock()
            .alloc_specific((irq_num - IRQ_NUM_MIN) as usize)
            .map(|id| Self::new(id as u8))
            .ok_or(Error::NotEnoughResources)
    }

    fn new(index: u8) -> Self {
        let inner = InnerHandle { index };
        inner.remapping.init(index + IRQ_NUM_MIN);

        Self {
            inner: Arc::new(inner),
            callbacks: Vec::new(),
            phased_callback: None,
        }
    }

    /// Gets the IRQ number.
    pub fn num(&self) -> u8 {
        self.inner.index + IRQ_NUM_MIN
    }

    /// Registers a callback that will be invoked when the IRQ is active.
    ///
    /// For each IRQ line, multiple callbacks may be registered.
    pub fn on_active<F>(&mut self, callback: F)
    where
        F: Fn(&TrapFrame) + Sync + Send + 'static,
    {
        self.register_callback(Box::new(callback));
    }

    /// Registers exclusive work before and after the hardware IRQ is acknowledged.
    ///
    /// Only one phased callback may be registered on an IRQ line at a time.
    #[cfg(any(target_arch = "riscv64", ktest))]
    pub(crate) fn on_active_with_post_ack<B, A>(
        &mut self,
        before_ack: B,
        after_ack: A,
    ) -> Result<()>
    where
        B: Fn(&TrapFrame) + Sync + Send + 'static,
        A: Fn(&TrapFrame) + Sync + Send + 'static,
    {
        let callback = Arc::new(PhasedCallback {
            before_ack: Box::new(before_ack),
            after_ack: Box::new(after_ack),
        });
        let mut slot = self.inner.phased_callback.write();
        if slot.is_some() {
            return Err(Error::NotEnoughResources);
        }
        *slot = Some(callback.clone());
        self.phased_callback = Some(PhasedCallbackHandle {
            irq_index: self.inner.index,
            callback,
        });
        Ok(())
    }

    fn register_callback(&mut self, callback: Box<IrqCallbackFunction>) {
        let callback_handle = {
            let callback_addr = core::ptr::from_ref(&*callback).addr();

            let mut callbacks = self.inner.callbacks.write();
            callbacks.push(callback);

            CallbackHandle {
                irq_index: self.inner.index,
                callback_addr,
            }
        };

        self.callbacks.push(callback_handle);
    }

    /// Unregisters callbacks owned by this IRQ-line handle.
    ///
    /// This waits for ordinary callbacks through their registry lock and for a
    /// phased callback through its IRQ-side ownership snapshot.
    #[cfg(any(target_arch = "riscv64", ktest))]
    pub(crate) fn clear_callbacks(&mut self) {
        self.callbacks.clear();
        self.phased_callback.take();
    }

    /// Checks if there are no registered callbacks.
    pub fn is_empty(&self) -> bool {
        self.callbacks.is_empty() && self.phased_callback.is_none()
    }

    /// Checks whether this handle exclusively owns an unused IRQ line.
    #[cfg(any(target_arch = "riscv64", ktest))]
    pub(crate) fn is_dedicated_and_empty(&self) -> bool {
        if Arc::strong_count(&self.inner) != 1
            || !self.callbacks.is_empty()
            || self.phased_callback.is_some()
        {
            return false;
        }

        let ordinary_callbacks_empty = self.inner.callbacks.read().is_empty();
        let phased_callback_empty = self.inner.phased_callback.read().is_none();
        ordinary_callbacks_empty && phased_callback_empty
    }

    #[cfg(all(ktest, target_arch = "riscv64"))]
    pub(crate) fn has_claim_time_phased_snapshot(&self) -> bool {
        self.phased_callback
            .as_ref()
            .is_some_and(|handle| Arc::strong_count(&handle.callback) > 2)
    }

    /// Gets the remapping index of the IRQ line.
    ///
    /// This method will return `None` if interrupt remapping is disabled or
    /// not supported by the architecture.
    pub fn remapping_index(&self) -> Option<u16> {
        self.inner.remapping.remapping_index()
    }
}

impl Clone for IrqLine {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            callbacks: Vec::new(),
            phased_callback: None,
        }
    }
}

struct Inner {
    callbacks: RwLock<Vec<Box<IrqCallbackFunction>>, WriteIrqDisabled>,
    phased_callback: RwLock<Option<Arc<PhasedCallback>>, WriteIrqDisabled>,
    remapping: IrqRemapping,
}

struct PhasedCallback {
    before_ack: Box<IrqCallbackFunction>,
    after_ack: Box<IrqCallbackFunction>,
}

/// An opaque claim-time ownership snapshot of an IRQ's phased callback.
pub(crate) struct PhasedCallbackSnapshot(Option<Arc<PhasedCallback>>);

impl PhasedCallbackSnapshot {
    pub(crate) const fn empty() -> Self {
        Self(None)
    }
}

#[cfg(target_arch = "riscv64")]
pub(crate) fn snapshot_phased_callback(irq_num: u8) -> PhasedCallbackSnapshot {
    let irq_index = (irq_num - IRQ_NUM_MIN) as usize;
    let callback = {
        let slot = INNERS[irq_index].phased_callback.read();
        slot.clone()
    };
    PhasedCallbackSnapshot(callback)
}

impl Inner {
    const fn new() -> Self {
        Self {
            callbacks: RwLock::new(Vec::new()),
            phased_callback: RwLock::new(None),
            remapping: IrqRemapping::new(),
        }
    }
}

const NUMBER_OF_IRQS: usize = (IRQ_NUM_MAX - IRQ_NUM_MIN) as usize + 1;

static INNERS: [Inner; NUMBER_OF_IRQS] = [const { Inner::new() }; NUMBER_OF_IRQS];
static ALLOCATOR: Once<SpinLock<IdAlloc>> = Once::new();

fn get_or_init_allocator() -> &'static SpinLock<IdAlloc> {
    ALLOCATOR.call_once(|| SpinLock::new(IdAlloc::with_capacity(NUMBER_OF_IRQS)))
}

/// A handle for an allocated IRQ line.
///
/// When the handle is dropped, the IRQ line will be released automatically.
#[must_use]
#[derive(Debug)]
struct InnerHandle {
    index: u8,
}

impl Deref for InnerHandle {
    type Target = Inner;

    fn deref(&self) -> &Self::Target {
        &INNERS[self.index as usize]
    }
}

impl Drop for InnerHandle {
    fn drop(&mut self) {
        ALLOCATOR.get().unwrap().lock().free(self.index as usize);
    }
}

/// A handle for a registered callback on an IRQ line.
///
/// When the handle is dropped, the callback will be unregistered automatically.
#[must_use]
#[derive(Debug)]
struct CallbackHandle {
    irq_index: u8,
    callback_addr: usize,
}

impl Drop for CallbackHandle {
    fn drop(&mut self) {
        let mut callbacks = INNERS[self.irq_index as usize].callbacks.write();

        let pos = callbacks
            .iter()
            .position(|element| core::ptr::from_ref(&**element).addr() == self.callback_addr);
        let _ = callbacks.swap_remove(pos.unwrap());
    }
}

#[must_use]
struct PhasedCallbackHandle {
    irq_index: u8,
    callback: Arc<PhasedCallback>,
}

impl Debug for PhasedCallbackHandle {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("PhasedCallbackHandle")
            .field("irq_index", &self.irq_index)
            .finish_non_exhaustive()
    }
}

impl Drop for PhasedCallbackHandle {
    fn drop(&mut self) {
        let removed = {
            let mut slot = INNERS[self.irq_index as usize].phased_callback.write();
            let is_owner = slot
                .as_ref()
                .is_some_and(|callback| Arc::ptr_eq(callback, &self.callback));
            debug_assert!(is_owner, "phased callback ownership changed unexpectedly");
            if is_owner { slot.take() } else { None }
        };
        drop(removed);

        // An IRQ-side Arc snapshot is released only after its post-ack callback.
        while has_in_flight_snapshot(&self.callback) {
            core::hint::spin_loop();
        }
    }
}

fn has_in_flight_snapshot(callback: &Arc<PhasedCallback>) -> bool {
    Arc::strong_count(callback) != 1
}

pub(super) fn process(trap_frame: &TrapFrame, hw_irq_line: &HwIrqLine) {
    let inner = &INNERS[(hw_irq_line.irq_num() - IRQ_NUM_MIN) as usize];
    let phased_callback = hw_irq_line.phased_callback_snapshot().0.as_deref();
    run_callback_phases(
        || {
            {
                let callbacks = inner.callbacks.read();
                for callback in &*callbacks {
                    callback(trap_frame);
                }
            }
            if let Some(callback) = &phased_callback {
                (callback.before_ack)(trap_frame);
            }
        },
        || hw_irq_line.ack(),
        || {
            if let Some(callback) = &phased_callback {
                (callback.after_ack)(trap_frame);
            }
        },
    );
}

fn run_callback_phases(before_ack: impl FnOnce(), ack: impl FnOnce(), after_ack: impl FnOnce()) {
    before_ack();
    ack();
    after_ack();
}

#[cfg(ktest)]
mod test {
    use super::*;

    const IRQ_NUM: u8 = 64;
    const IRQ_INDEX: usize = (IRQ_NUM - IRQ_NUM_MIN) as usize;

    #[ktest]
    fn alloc_and_free_irq() {
        let irq_line = IrqLine::alloc_specific(IRQ_NUM).unwrap();
        assert!(IrqLine::alloc_specific(IRQ_NUM).is_err());

        let irq_line_cloned = irq_line.clone();
        assert!(IrqLine::alloc_specific(IRQ_NUM).is_err());

        drop(irq_line);
        assert!(IrqLine::alloc_specific(IRQ_NUM).is_err());

        drop(irq_line_cloned);
        assert!(IrqLine::alloc_specific(IRQ_NUM).is_ok());
    }

    #[ktest]
    fn register_and_unregister_callback() {
        let mut irq_line = IrqLine::alloc_specific(IRQ_NUM).unwrap();
        let mut irq_line_cloned = irq_line.clone();

        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 0);

        irq_line.on_active(|_| {});
        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 1);

        irq_line_cloned.on_active(|_| {});
        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 2);

        irq_line_cloned.on_active(|_| {});
        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 3);

        drop(irq_line);
        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 2);

        drop(irq_line_cloned);
        assert_eq!(INNERS[IRQ_INDEX].callbacks.read().len(), 0);
    }

    #[ktest]
    fn clear_callbacks_unregisters_callbacks_immediately() {
        let mut irq_line = IrqLine::alloc().unwrap();
        let irq_index = (irq_line.num() - IRQ_NUM_MIN) as usize;
        irq_line.on_active(|_| {});
        irq_line.on_active(|_| {});
        irq_line.on_active_with_post_ack(|_| {}, |_| {}).unwrap();
        assert_eq!(INNERS[irq_index].callbacks.read().len(), 2);
        assert!(INNERS[irq_index].phased_callback.read().is_some());

        irq_line.clear_callbacks();

        assert!(irq_line.is_empty());
        assert_eq!(INNERS[irq_index].callbacks.read().len(), 0);
        assert!(INNERS[irq_index].phased_callback.read().is_none());
    }

    #[ktest]
    fn dedicated_empty_line_rejects_aliases_and_callbacks() {
        let mut irq_line = IrqLine::alloc().unwrap();
        assert!(irq_line.is_dedicated_and_empty());

        let cloned_irq_line = irq_line.clone();
        assert!(!irq_line.is_dedicated_and_empty());
        drop(cloned_irq_line);
        assert!(irq_line.is_dedicated_and_empty());

        irq_line.on_active(|_| {});
        assert!(!irq_line.is_dedicated_and_empty());
        irq_line.clear_callbacks();
        assert!(irq_line.is_dedicated_and_empty());
    }

    #[ktest]
    fn phased_callbacks_mask_and_fence_before_ack_then_wake() {
        let stage = core::cell::Cell::new(0);
        run_callback_phases(
            || {
                assert_eq!(stage.get(), 0);
                stage.set(1); // Mask source priority.
                stage.set(2); // Fence the priority write.
            },
            || {
                assert_eq!(stage.get(), 2);
                stage.set(3); // Write the PLIC completion ID.
                stage.set(4); // Fence the completion write.
            },
            || {
                assert_eq!(stage.get(), 4);
                stage.set(5); // Wake only after visible completion.
            },
        );
        assert_eq!(stage.get(), 5);
    }

    #[ktest]
    fn claim_snapshot_keeps_phased_callback_in_flight_after_slot_clear() {
        let callback = Arc::new(PhasedCallback {
            before_ack: Box::new(|_| {}),
            after_ack: Box::new(|_| {}),
        });
        let snapshot = PhasedCallbackSnapshot(Some(callback.clone()));

        assert!(has_in_flight_snapshot(&callback));
        drop(snapshot);
        assert!(!has_in_flight_snapshot(&callback));
    }

    #[ktest]
    fn phased_registration_uses_one_owned_callback_entry() {
        let mut irq_line = IrqLine::alloc().unwrap();
        let mut cloned_irq_line = irq_line.clone();
        let irq_index = (irq_line.num() - IRQ_NUM_MIN) as usize;

        irq_line.on_active_with_post_ack(|_| {}, |_| {}).unwrap();

        assert!(INNERS[irq_index].phased_callback.read().is_some());
        assert!(
            cloned_irq_line
                .on_active_with_post_ack(|_| {}, |_| {})
                .is_err()
        );

        irq_line.clear_callbacks();
        cloned_irq_line
            .on_active_with_post_ack(|_| {}, |_| {})
            .unwrap();
        drop(cloned_irq_line);
        assert!(INNERS[irq_index].phased_callback.read().is_none());
    }
}
