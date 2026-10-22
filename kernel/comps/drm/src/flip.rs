// SPDX-License-Identifier: MPL-2.0

//! Deferred page-flip completion.
//!
//! A legacy page flip is asynchronous: the ioctl validates the request, queues
//! it on the CRTC, and returns. The next completed scanout (the 60 Hz software
//! vblank tick, or a modeset that ticks the same counter) marks the flip due
//! and wakes a worker thread, because the transfer and the state publication
//! sleep and therefore cannot run in the timer softirq.

use alloc::{
    sync::Arc,
    vec::Vec,
};
use core::sync::atomic::{AtomicBool, Ordering};

use aster_core::prelude::*;
use ostd::warn;
use ostd::sync::{SpinLock, Waker};

use crate::{
    device::DrmDevice,
    event::{DRM_EVENT_FLIP_COMPLETE, DrmEventVblank},
    file::DrmEventQueue,
    ioctl::kms::{ScanoutSource, commit_modeset},
    kms::{
        display_mode::DrmDisplayMode,
        objects::{crtc::DrmCrtc, framebuffer::DrmFramebuffer},
    },
    utils::DrmRect,
};

/// One page flip waiting for the next completed scanout.
pub(crate) struct PendingFlip {
    device: Arc<dyn DrmDevice>,
    events: Arc<DrmEventQueue>,
    crtc_id: u32,
    connector_ids: Vec<u32>,
    display_mode: DrmDisplayMode,
    framebuffer: Arc<DrmFramebuffer>,
    source_rect: DrmRect,
    user_data: u64,
    event_requested: bool,
}

impl core::fmt::Debug for PendingFlip {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("PendingFlip")
            .field("crtc_id", &self.crtc_id)
            .field("user_data", &self.user_data)
            .finish_non_exhaustive()
    }
}

impl PendingFlip {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        device: Arc<dyn DrmDevice>,
        events: Arc<DrmEventQueue>,
        crtc_id: u32,
        connector_ids: Vec<u32>,
        display_mode: DrmDisplayMode,
        framebuffer: Arc<DrmFramebuffer>,
        source_rect: DrmRect,
        user_data: u64,
        event_requested: bool,
    ) -> Self {
        Self {
            device,
            events,
            crtc_id,
            connector_ids,
            display_mode,
            framebuffer,
            source_rect,
            user_data,
            event_requested,
        }
    }

    /// Applies the flip and queues its completion event.
    fn complete(self, crtc: &Arc<DrmCrtc>) {
        let result = commit_modeset(
            &*self.device,
            self.crtc_id,
            &self.connector_ids,
            Some(self.display_mode),
            ScanoutSource::Framebuffer(self.framebuffer),
            self.source_rect,
            false,
        );

        match result {
            Ok(_) if self.event_requested => {
                self.events.push(DrmEventVblank::new(
                    DRM_EVENT_FLIP_COMPLETE,
                    self.user_data,
                    aster_core::monotonic_now(),
                    crtc.vblank_sequence(),
                    self.crtc_id,
                ));
            }
            Ok(_) => {}
            Err(error) => {
                warn!("drm: page flip completion failed: {:?}", error);
            }
        }
    }
}

/// Applies page flips once the vblank tick marks them due.
///
/// One worker per DRM device. It holds the device's CRTCs directly, so the
/// wake-up path (which runs in the timer softirq) never takes the KMS object
/// store lock.
pub(crate) struct FlipWorker {
    crtcs: Vec<Arc<DrmCrtc>>,
    dirty: AtomicBool,
    wakers: SpinLock<Vec<Arc<Waker>>>,
}

impl core::fmt::Debug for FlipWorker {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("FlipWorker")
            .field("crtcs", &self.crtcs.len())
            .finish_non_exhaustive()
    }
}

impl FlipWorker {
    /// Starts the device's flip worker and wires it into its CRTCs.
    pub(crate) fn start(device: &Arc<dyn DrmDevice>) -> Option<Arc<Self>> {
        let kms_ops = device.as_kms_ops()?;
        let crtcs: Vec<Arc<DrmCrtc>> = kms_ops
            .mode_config()
            .object_store()
            .lock()
            .crtcs()
            .cloned()
            .collect();
        if crtcs.is_empty() {
            return None;
        }

        let worker = Arc::new(Self {
            crtcs,
            dirty: AtomicBool::new(false),
            wakers: SpinLock::new(Vec::new()),
        });
        for crtc in &worker.crtcs {
            crtc.set_flip_worker(Arc::downgrade(&worker));
        }

        let thread_worker = worker.clone();
        aster_core::spawn_kernel_thread("drm-flip", move || thread_worker.run());

        Some(worker)
    }

    fn run(&self) {
        loop {
            let _ = aster_core::wait_until_or_timeout(
                || self.dirty.swap(false, Ordering::AcqRel),
                |waker| self.wakers.lock().push(waker),
                None,
            );

            for crtc in &self.crtcs {
                if let Some(flip) = crtc.take_due_flip() {
                    flip.complete(crtc);
                }
            }
        }
    }

    /// Marks flips due and wakes the worker. IRQ-safe.
    pub(crate) fn notify(&self) {
        self.dirty.store(true, Ordering::Release);

        let wakers = core::mem::take(&mut *self.wakers.disable_irq().lock());
        for waker in wakers {
            waker.wake_up();
        }
    }
}

/// Keeps the workers of registered devices alive for the life of the kernel.
static WORKERS: SpinLock<Vec<Arc<FlipWorker>>> = SpinLock::new(Vec::new());

/// Starts and retains the flip worker of one device.
pub(crate) fn start_worker(device: &Arc<dyn DrmDevice>) {
    if let Some(worker) = FlipWorker::start(device) {
        WORKERS.disable_irq().lock().push(worker);
    }
}
