// SPDX-License-Identifier: MPL-2.0

//! Serialized virtio-gpu control queue submission and completion.
//!
//! [`ControlQueue::operation`] is a sleeping mutex that permits exactly one
//! in-flight request and protects any request buffer prepared by its guard's
//! owner. [`ControlQueue::inner`] protects the virtqueue and completion slot
//! from both task and IRQ context. The IRQ handler only takes `inner`, stores
//! one completion without allocating, releases the IRQ-disabled lock, and then
//! wakes the task. Device notification likewise happens after releasing
//! `inner`, using the separately cloned notifier.

use alloc::sync::Arc;
use core::hint::spin_loop;

use aster_util::mem_obj_slice::Slice;
use ostd::{
    mm::dma::DmaStream,
    sync::{LocalIrqDisabled, Mutex, MutexGuard, SpinLock, WaitQueue},
};

use super::VirtioGpuCtrlHdr;
use crate::queue::{PopUsedError, VirtQueue, VirtQueueNotifier};

pub(super) struct ControlQueue {
    operation: Mutex<()>,
    inner: SpinLock<ControlQueueInner, LocalIrqDisabled>,
    notifier: VirtQueueNotifier,
    completion_waiters: WaitQueue,
}

struct ControlQueueInner {
    queue: VirtQueue,
    irq_wait_enabled: bool,
    completed: Option<Result<(u16, u32), PopUsedError>>,
}

pub(super) struct ControlOperation<'a> {
    queue: &'a ControlQueue,
    _guard: MutexGuard<'a, ()>,
}

impl ControlQueue {
    pub(super) fn new(queue: VirtQueue) -> Arc<Self> {
        let notifier = queue.notifier();
        Arc::new(Self {
            operation: Mutex::new(()),
            inner: SpinLock::new(ControlQueueInner {
                queue,
                irq_wait_enabled: false,
                completed: None,
            }),
            notifier,
            completion_waiters: WaitQueue::new(),
        })
    }

    pub(super) fn lock(&self) -> ControlOperation<'_> {
        ControlOperation {
            queue: self,
            _guard: self.operation.lock(),
        }
    }

    pub(super) fn enable_irq_wait(&self) {
        // Serialize the mode switch with submissions. Otherwise a request
        // that chose polling could have its response consumed by the IRQ
        // handler after this flag changes and wait forever.
        let _operation = self.operation.lock();
        self.inner.lock().irq_wait_enabled = true;
    }

    pub(super) fn handle_irq(&self) {
        let completed = {
            let mut inner = self.inner.lock();
            if !inner.irq_wait_enabled || inner.completed.is_some() {
                return;
            }
            let completed = match inner
                .queue
                .pop_used_once_with_min_bytes(size_of::<VirtioGpuCtrlHdr>())
            {
                Err(PopUsedError::NotReady) => return,
                result => result,
            };
            inner.completed = Some(completed);
            completed
        };
        if let Err(error) = completed {
            ostd::error!("invalid virtio-gpu control completion: {:?}", error);
        }
        self.completion_waiters.wake_all();
    }
}

impl ControlOperation<'_> {
    pub(super) fn submit_dma_bufs(
        &self,
        inputs: &[&Slice<Arc<DmaStream>>],
        outputs: &[&Slice<Arc<DmaStream>>],
    ) {
        let should_notify = {
            let mut inner = self.queue.inner.lock();
            inner
                .queue
                .add_dma_bufs(inputs, outputs)
                .expect("add control queue buffers");
            inner.queue.should_notify()
        };
        if should_notify {
            self.queue.notifier.notify();
        }
    }

    pub(super) fn wait_for_used(&self, min_bytes: usize) -> Result<(u16, u32), PopUsedError> {
        let irq_wait_enabled = self.queue.inner.lock().irq_wait_enabled;
        if !irq_wait_enabled {
            loop {
                let result = self
                    .queue
                    .inner
                    .lock()
                    .queue
                    .pop_used_once_with_min_bytes(min_bytes);
                match result {
                    Err(PopUsedError::NotReady) => spin_loop(),
                    Err(error) => {
                        ostd::error!("invalid virtio-gpu control completion: {:?}", error);
                        return Err(error);
                    }
                    Ok(completed) => return Ok(completed),
                }
            }
        }

        self.queue.completion_waiters.wait_until(|| {
            let mut inner = self.queue.inner.lock();
            let completed = inner.completed.take()?;
            debug_assert!(
                completed
                    .as_ref()
                    .is_ok_and(|(_, len)| *len as usize >= min_bytes)
                    || completed.is_err()
            );
            Some(completed)
        })
    }
}
