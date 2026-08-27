// SPDX-License-Identifier: MPL-2.0

//! Concurrent virtio-gpu control queue submission and completion.
//!
//! Each submitted descriptor token owns a separate [`ControlRequest`].
//! This lets unrelated control commands remain in flight together while the
//! IRQ handler dispatches used-ring entries to the matching waiter.
//! The queue spinlock protects the virtqueue and token map, while per-request
//! state protects completion delivery.
//! Device notification and waiter wakeups happen after releasing the queue lock.

use alloc::{sync::Arc, vec::Vec};
use core::hint::spin_loop;

use aster_util::mem_obj_slice::Slice;
use ostd::{
    mm::dma::DmaStream,
    sync::{LocalIrqDisabled, SpinLock, WaitQueue},
    task::Task,
};

use super::{GpuCommandCompletion, VirtioGpuCtrlHdr};
use crate::queue::{AddBufsError, PopUsedError, VirtQueue, VirtQueueNotifier};

pub(super) struct ControlQueue {
    inner: SpinLock<ControlQueueInner, LocalIrqDisabled>,
    notifier: VirtQueueNotifier,
    descriptors_available: WaitQueue,
    descriptor_capacity: usize,
}

struct ControlQueueInner {
    queue: VirtQueue,
    irq_wait_enabled: bool,
    pending: TokenMap<Arc<ControlRequest>>,
}

struct TokenMap<T> {
    slots: Vec<Option<T>>,
}

struct ControlRequest {
    completion: SpinLock<Option<Result<u32, PopUsedError>>, LocalIrqDisabled>,
    listener: SpinLock<Option<Arc<dyn GpuCommandCompletion>>, LocalIrqDisabled>,
    // Every descriptor's memory must remain alive until the device returns
    // the token, even if completion is delayed.
    _dma_bufs: Vec<Arc<DmaStream>>,
}

#[must_use = "a submitted control request must be completed"]
pub(super) struct ControlTicket {
    queue: Arc<ControlQueue>,
    request: Arc<ControlRequest>,
    token: u16,
}

enum DispatchResult {
    NotReady,
    InvalidToken {
        token: u32,
        queue_size: usize,
    },
    Completed {
        request: Arc<ControlRequest>,
        result: Result<u32, PopUsedError>,
    },
}

impl ControlQueue {
    pub(super) fn new(queue: VirtQueue) -> Arc<Self> {
        let notifier = queue.notifier();
        let descriptor_capacity = queue.available_desc();
        Arc::new(Self {
            inner: SpinLock::new(ControlQueueInner {
                queue,
                irq_wait_enabled: false,
                pending: TokenMap::new(descriptor_capacity),
            }),
            notifier,
            descriptors_available: WaitQueue::new(),
            descriptor_capacity,
        })
    }

    pub(super) fn enable_irq_wait(&self) {
        self.inner.lock().irq_wait_enabled = true;
    }

    pub(super) fn handle_irq(&self) {
        self.poll_completions();
    }

    /// Reclaims every completion currently visible in the used ring.
    ///
    /// Virtio permits interrupt suppression and coalescing. Synchronous
    /// callers also invoke this path so progress does not depend on receiving
    /// one interrupt for every used-ring update.
    fn poll_completions(&self) {
        // A device may coalesce several used-ring updates into one interrupt.
        // Bound the work by the queue size so a malformed device cannot keep
        // the IRQ handler spinning forever.
        for _ in 0..self.descriptor_capacity {
            match self.dispatch_one() {
                DispatchResult::NotReady => return,
                DispatchResult::InvalidToken { token, queue_size } => {
                    ostd::error!(
                        "invalid virtio-gpu control token: {} (queue size: {})",
                        token,
                        queue_size,
                    );
                }
                DispatchResult::Completed { request, result } => {
                    if let Err(error) = result {
                        ostd::error!("invalid virtio-gpu control completion: {:?}", error);
                    }
                    request.complete(result);
                    self.descriptors_available.wake_one();
                }
            }
        }
    }

    pub(super) fn submit_dma_bufs(
        self: &Arc<Self>,
        inputs: &[&Slice<Arc<DmaStream>>],
        outputs: &[&Slice<Arc<DmaStream>>],
        listener: Option<Arc<dyn GpuCommandCompletion>>,
    ) -> ControlTicket {
        let dma_bufs = inputs
            .iter()
            .chain(outputs)
            .map(|slice| slice.mem_obj().clone())
            .collect();
        let request = Arc::new(ControlRequest {
            completion: SpinLock::new(None),
            listener: SpinLock::new(listener),
            _dma_bufs: dma_bufs,
        });
        let mut pending_request = Some(request.clone());

        let (token, should_notify) = self.descriptors_available.wait_until(|| {
            let mut inner = self.inner.lock();
            let token = match inner.queue.add_dma_bufs(inputs, outputs) {
                Ok(token) => token,
                Err(AddBufsError::BufferTooSmall) => return None,
                Err(AddBufsError::InvalidArgs) => panic!("invalid control queue buffers"),
            };
            inner.pending.insert(
                token,
                pending_request
                    .take()
                    .expect("control request submitted more than once"),
            );
            Some((token, inner.queue.should_notify()))
        });

        if should_notify {
            self.notifier.notify();
        }
        ControlTicket {
            queue: self.clone(),
            request,
            token,
        }
    }

    fn dispatch_one(&self) -> DispatchResult {
        let mut inner = self.inner.lock();
        let completion = inner
            .queue
            .pop_used_once_with_min_bytes(size_of::<VirtioGpuCtrlHdr>());
        let (token, result) = match completion {
            Err(PopUsedError::NotReady) => return DispatchResult::NotReady,
            Err(PopUsedError::InvalidToken { token, queue_size }) => {
                return DispatchResult::InvalidToken { token, queue_size };
            }
            Err(error @ PopUsedError::InvalidLength { token, .. }) => (token, Err(error)),
            Ok((token, used_len)) => (token, Ok(used_len)),
        };
        let request = inner
            .pending
            .remove(token)
            .expect("completed control request has no token owner");
        DispatchResult::Completed { request, result }
    }
}

impl<T> TokenMap<T> {
    fn new(capacity: usize) -> Self {
        Self {
            slots: core::iter::repeat_with(|| None).take(capacity).collect(),
        }
    }

    fn insert(&mut self, token: u16, value: T) {
        let slot = self
            .slots
            .get_mut(token as usize)
            .expect("control descriptor token is out of range");
        if slot.is_some() {
            panic!("control descriptor token is already pending");
        }
        *slot = Some(value);
    }

    fn remove(&mut self, token: u16) -> Option<T> {
        self.slots.get_mut(token as usize)?.take()
    }
}

impl ControlRequest {
    fn complete(&self, result: Result<u32, PopUsedError>) {
        let old_completion = self.completion.lock().replace(result);
        debug_assert!(old_completion.is_none());
        let listener = self.listener.lock().take();
        if let Some(listener) = listener {
            listener.complete();
        }
    }

    fn take_completion(&self) -> Option<Result<u32, PopUsedError>> {
        self.completion.lock().take()
    }
}

impl ControlTicket {
    pub(super) fn wait_for_used(self) -> Result<(u16, u32), PopUsedError> {
        let irq_wait_enabled = self.queue.inner.lock().irq_wait_enabled;
        let result = loop {
            if let Some(completion) = self.request.take_completion() {
                break completion;
            }
            self.queue.poll_completions();
            if irq_wait_enabled {
                Task::yield_now();
            } else {
                spin_loop();
            }
        }?;
        Ok((self.token, result))
    }

    pub(super) fn poll_completion(&self) {
        self.queue.poll_completions();
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::TokenMap;

    #[ktest]
    fn token_map_routes_out_of_order_completions() {
        let mut pending = TokenMap::new(8);
        pending.insert(2, "first");
        pending.insert(6, "second");

        assert_eq!(pending.remove(6), Some("second"));
        assert_eq!(pending.remove(2), Some("first"));
    }

    #[ktest]
    fn token_map_reuses_a_completed_token() {
        let mut pending = TokenMap::new(2);
        pending.insert(0, 10);
        assert_eq!(pending.remove(0), Some(10));

        pending.insert(0, 20);
        assert_eq!(pending.remove(0), Some(20));
    }
}
