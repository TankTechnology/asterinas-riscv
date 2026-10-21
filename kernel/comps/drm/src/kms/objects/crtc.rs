// SPDX-License-Identifier: MPL-2.0

use alloc::{sync::{Arc, Weak}, vec::Vec};
use core::{
    sync::atomic::{AtomicU32, Ordering},
    time::Duration,
};

use ostd::sync::{Mutex, Waker};

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
        *self.vblank.last_time.lock()
    }

    /// Records one completed scanout and wakes vblank waiters.
    ///
    /// The driver has no refresh interrupt: a scanout completes when a
    /// modeset or page flip transfers and flushes the framebuffer, so that is
    /// the event this counter advances on.
    pub(crate) fn tick_vblank(&self, now: Duration) -> u32 {
        let sequence = self.vblank.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        *self.vblank.last_time.lock() = Some(now);

        let wakers = core::mem::take(&mut *self.vblank.wakers.lock());
        for waker in wakers {
            waker.wake_up();
        }

        sequence
    }

    /// Registers a waker for the next completed scanout.
    pub(crate) fn register_vblank_waker(&self, waker: Arc<Waker>) {
        self.vblank.wakers.lock().push(waker);
    }
}

/// Software vblank state of one CRTC.
struct DrmVblankState {
    sequence: AtomicU32,
    last_time: Mutex<Option<Duration>>,
    wakers: Mutex<Vec<Arc<Waker>>>,
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
            last_time: Mutex::new(None),
            wakers: Mutex::new(Vec::new()),
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
