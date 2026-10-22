// SPDX-License-Identifier: MPL-2.0

use alloc::{
    sync::{Arc, Weak},
    vec::Vec,
};
use core::{
    sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
    time::Duration,
};

use aster_core::{IntervalTimer, prelude::*};
use ostd::sync::{Mutex, SpinLock, Waker};

/// The software vblank period: 60 Hz.
const VBLANK_INTERVAL: Duration = Duration::from_micros(16_667);

use crate::kms::{
    display_mode::DrmDisplayMode,
    objects::{KmsObjectId, KmsObjectIndex, plane::DrmPlane, property::DrmPropertyAttachments},
};

/// A display pipeline that scans out planes using a display mode.
///
/// A CRTC owns the runtime mode and activation state of the pipeline.
/// Its primary plane is mandatory,
/// while its cursor plane is optional;
/// both links are non-owning because the KMS object store owns the topology objects.
#[derive(Debug)]
pub(crate) struct DrmCrtc {
    id: KmsObjectId,
    index: KmsObjectIndex,
    config: DrmCrtcConfig,
    state: Mutex<DrmCrtcState>,
    properties: DrmPropertyAttachments,
    vblank: DrmVblankState,
    vblank_timer: Mutex<Option<IntervalTimer>>,
    /// A page flip waiting for the next completed scanout.
    pending_flip: SpinLock<Option<crate::flip::PendingFlip>>,
    /// Set when the vblank tick made the pending flip due.
    flip_due: AtomicBool,
    /// The device's flip worker, woken when a flip becomes due.
    ///
    /// IRQ-safe: the periodic tick runs from the timer softirq.
    flip_worker: SpinLock<Weak<crate::flip::FlipWorker>>,
}

impl DrmCrtc {
    pub(super) fn new(
        id: KmsObjectId,
        index: KmsObjectIndex,
        config: DrmCrtcConfig,
        properties: DrmPropertyAttachments,
    ) -> Self {
        Self {
            id,
            index,
            config,
            state: Mutex::new(DrmCrtcState::default()),
            properties,
            vblank: DrmVblankState::default(),
            vblank_timer: Mutex::new(None),
            pending_flip: SpinLock::new(None),
            flip_due: AtomicBool::new(false),
            flip_worker: SpinLock::new(Weak::new()),
        }
    }

    pub(crate) fn id(&self) -> KmsObjectId {
        self.id
    }

    pub(crate) fn index(&self) -> KmsObjectIndex {
        self.index
    }

    pub(crate) fn gamma_size(&self) -> u32 {
        self.config.gamma_size
    }

    pub(crate) fn state(&self) -> &Mutex<DrmCrtcState> {
        &self.state
    }

    pub(crate) fn update_state(&self, state: DrmCrtcState) {
        *self.state.lock() = state;
    }

    pub(crate) fn primary_plane(&self) -> &Weak<DrmPlane> {
        &self.config.primary_plane
    }

    pub(crate) fn properties(&self) -> &DrmPropertyAttachments {
        &self.properties
    }

    /// Returns the number of completed scanouts (vblanks) on this CRTC.
    pub(crate) fn vblank_sequence(&self) -> u32 {
        self.vblank.sequence.load(Ordering::Relaxed)
    }

    /// Returns the timestamp of the most recent completed scanout.
    pub(crate) fn vblank_time(&self) -> Option<Duration> {
        let micros = self.vblank.last_time_micros.load(Ordering::Relaxed);
        (micros != 0).then(|| Duration::from_micros(micros))
    }

    /// Starts the periodic software vblank tick, if it is not running.
    ///
    /// The timer callback runs with interrupts disabled and only touches
    /// atomics and the IRQ-safe waiter list.
    pub(crate) fn start_vblank_timer(self: &Arc<Self>) {
        let mut timer = self.vblank_timer.lock();
        if timer.is_some() {
            return;
        }

        let crtc = Arc::downgrade(self);
        *timer = Some(IntervalTimer::new(VBLANK_INTERVAL, move || {
            if let Some(crtc) = crtc.upgrade() {
                crtc.tick_vblank(aster_core::monotonic_now());
            }
        }));
    }

    /// Stops the periodic software vblank tick.
    pub(crate) fn stop_vblank_timer(&self) {
        *self.vblank_timer.lock() = None;
    }

    /// Records one completed scanout and wakes vblank waiters.
    ///
    /// The driver has no refresh interrupt: a scanout completes when a
    /// modeset or page flip transfers and flushes the framebuffer, so that is
    /// the event this counter advances on.
    pub(crate) fn tick_vblank(&self, now: Duration) -> u32 {
        let sequence = self.vblank.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        self.vblank
            .last_time_micros
            .store(now.as_micros().try_into().unwrap_or(u64::MAX), Ordering::Relaxed);

        let wakers = core::mem::take(&mut *self.vblank.wakers.disable_irq().lock());
        for waker in wakers {
            waker.wake_up();
        }

        // A pending page flip completes on the next scanout. Mark it due and
        // wake the worker, which does the (sleeping) transfer outside the
        // softirq.
        let flip_pending = self.pending_flip.disable_irq().lock().is_some();
        if flip_pending && !self.flip_due.swap(true, Ordering::AcqRel) {
            let worker = self.flip_worker.disable_irq().lock().upgrade();
            if let Some(worker) = worker {
                worker.notify();
            }
        }

        sequence
    }

    /// Queues a page flip to complete on the next scanout.
    ///
    /// Linux rejects a second flip while one is pending; so does this.
    pub(crate) fn queue_flip(&self, flip: crate::flip::PendingFlip) -> Result<()> {
        let mut pending = self.pending_flip.disable_irq().lock();
        if pending.is_some() {
            return_errno_with_message!(Errno::EBUSY, "a page flip is already pending on the CRTC");
        }
        *pending = Some(flip);

        Ok(())
    }

    /// Takes the pending flip if the vblank tick made it due.
    pub(crate) fn take_due_flip(&self) -> Option<crate::flip::PendingFlip> {
        if !self.flip_due.swap(false, Ordering::AcqRel) {
            return None;
        }

        self.pending_flip.disable_irq().lock().take()
    }

    /// Registers the device's flip worker to be woken by the vblank tick.
    pub(crate) fn set_flip_worker(&self, worker: Weak<crate::flip::FlipWorker>) {
        *self.flip_worker.disable_irq().lock() = worker;
    }

    /// Registers a waker for the next completed scanout.
    pub(crate) fn register_vblank_waker(&self, waker: Arc<Waker>) {
        self.vblank.wakers.disable_irq().lock().push(waker);
    }
}

/// Software vblank state of one CRTC.
struct DrmVblankState {
    sequence: AtomicU32,
    /// Microseconds since boot; zero means "no scanout yet".
    last_time_micros: AtomicU64,
    /// IRQ-safe because the periodic tick runs from the timer softirq.
    wakers: SpinLock<Vec<Arc<Waker>>>,
}

impl core::fmt::Debug for DrmVblankState {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("DrmVblankState")
            .field("sequence", &self.sequence.load(Ordering::Relaxed))
            .finish_non_exhaustive()
    }
}

impl Default for DrmVblankState {
    fn default() -> Self {
        Self {
            sequence: AtomicU32::new(0),
            last_time_micros: AtomicU64::new(0),
            wakers: SpinLock::new(Vec::new()),
        }
    }
}

/// Immutable configuration of a DRM CRTC.
#[derive(Debug)]
pub(super) struct DrmCrtcConfig {
    pub gamma_size: u32,
    pub primary_plane: Weak<DrmPlane>,
    #[expect(dead_code)]
    pub cursor_plane: Weak<DrmPlane>,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct DrmCrtcState {
    display_mode: Option<DrmDisplayMode>,
    is_enabled: bool,
    #[expect(dead_code)]
    is_active: bool,
}

impl DrmCrtcState {
    pub(crate) fn new(display_mode: DrmDisplayMode) -> Self {
        Self {
            display_mode: Some(display_mode),
            is_enabled: true,
            is_active: true,
        }
    }

    pub(crate) fn display_mode(&self) -> Option<DrmDisplayMode> {
        self.display_mode
    }

    pub(crate) fn is_enabled(&self) -> bool {
        self.is_enabled
    }
}
