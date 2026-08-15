// SPDX-License-Identifier: MPL-2.0

//! Implements virtio-sound device instances (device ID 25).
//!
//! This MVP targets PCM playback: the control queue drives the PCM handshake
//! (`SET_PARAMS`/`PREPARE`/`START`), and the TX queue carries PCM frames to the
//! host audio backend. Capture (RX queue) and the event queue are left dormant
//! for now, mirroring the initial scope of the NixOS bring-up.

use alloc::{format, string::ToString, sync::Arc, vec::Vec};
use core::{
    hint::spin_loop,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

use aster_util::mem_obj_slice::Slice;
use ostd::{
    Error,
    mm::{
        Fallible, FallibleVmWrite, PAGE_SIZE, VmIo, VmReader, dma::DmaStream,
        io::util::HasVmReaderWriter,
    },
    sync::SpinLock,
};

use super::{
    config::{SoundFeatures, VirtioSoundConfig},
    D_OUTPUT, FMT_S16, RATE_48000, R_PCM_INFO, R_PCM_PREPARE, R_PCM_SET_PARAMS, R_PCM_START,
    S_OK, VQ_CONTROL, VQ_TX, VirtioSndHdr, VirtioSndPcmHdr, VirtioSndPcmInfo,
    VirtioSndPcmSetParams, VirtioSndPcmStatus, VirtioSndPcmXfer, VirtioSndQueryInfo,
};
use crate::{
    device::{VirtioDeviceError, sound::register_device},
    queue::VirtQueue,
    transport::DeviceTransport,
};

/// Number of descriptors per virtqueue.
const QUEUE_SIZE: u16 = 64;

/// Control-buffer layout: the request is written at the start of the page and
/// the response is read from a fixed offset, keeping the two areas disjoint.
const CTRL_REQ_OFFSET: usize = 0;
const CTRL_RESP_OFFSET: usize = 256;

/// TX-buffer layout (single page): `[xfer header][PCM data ... status]`.
const TX_XFER_OFFSET: usize = 0;
const TX_DATA_OFFSET: usize = size_of::<VirtioSndPcmXfer>();
const TX_STATUS_OFFSET: usize = PAGE_SIZE - size_of::<VirtioSndPcmStatus>();
const TX_DATA_CAP: usize = TX_STATUS_OFFSET - TX_DATA_OFFSET;

/// Hardware buffer / period sizes reported to the device (matches QEMU defaults).
const BUFFER_BYTES: u32 = 8192;
const PERIOD_BYTES: u32 = 2048;
const CHANNELS: u8 = 2;

/// Metadata for a single PCM stream, discovered via `PCM_INFO`.
#[derive(Clone, Copy, Debug)]
pub struct StreamInfo {
    pub direction: u8,
    pub features: u32,
    pub formats: u64,
    pub rates: u64,
    pub channels_min: u8,
    pub channels_max: u8,
}

/// A virtio-sound device.
pub struct SoundDevice {
    transport: SpinLock<DeviceTransport>,
    control_queue: SpinLock<VirtQueue>,
    tx_queue: SpinLock<VirtQueue>,
    control_buf: Arc<DmaStream>,
    tx_buf: Arc<DmaStream>,
    streams: Vec<StreamInfo>,
    /// The stream id used for playback (`u32::MAX` when no output stream exists).
    playback_stream: u32,
    playback_started: AtomicBool,
}

impl SoundDevice {
    pub(crate) fn negotiate_features(features: u64) -> u64 {
        // We do not drive control elements, so clear `VIRTIO_SND_F_CTLS`.
        features & !SoundFeatures::VIRTIO_SND_F_CTLS.bits()
    }

    pub(crate) fn init(mut device_transport: DeviceTransport) -> Result<(), VirtioDeviceError> {
        let config_manager = VirtioSoundConfig::new_manager(device_transport.as_ref());
        let config = config_manager.read_config();
        ostd::debug!("virtio_snd_config = {:?}", config);

        let mut control_queue = VirtQueue::new(VQ_CONTROL, QUEUE_SIZE, device_transport.as_mut())?;
        let tx_queue = VirtQueue::new(VQ_TX, QUEUE_SIZE, device_transport.as_mut())?;

        let control_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);
        let tx_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);

        // Mark the device ready before issuing the first control request.
        device_transport.finish_init();

        let streams = query_streams(&mut control_queue, &control_buf, config.streams)?;
        let playback_stream = streams
            .iter()
            .position(|s| s.direction == D_OUTPUT)
            .map(|i| i as u32)
            .unwrap_or(u32::MAX);
        ostd::debug!(
            "virtio_snd: {} streams, playback stream id = {}",
            streams.len(),
            if playback_stream == u32::MAX {
                "none".to_string()
            } else {
                format!("{}", playback_stream)
            }
        );

        let device = Arc::new(Self {
            transport: SpinLock::new(device_transport),
            control_queue: SpinLock::new(control_queue),
            tx_queue: SpinLock::new(tx_queue),
            control_buf,
            tx_buf,
            streams,
            playback_stream,
            playback_started: AtomicBool::new(false),
        });

        register_device(
            format!("virtio_snd.{}", SOUND_DEVICE_ID.fetch_add(1, Ordering::Relaxed)),
            device,
        );

        Ok(())
    }

    /// Returns the number of PCM streams exposed by the device.
    pub fn num_streams(&self) -> usize {
        self.streams.len()
    }

    /// Runs the playback handshake (`SET_PARAMS`/`PREPARE`/`START`) exactly once.
    pub fn prepare_playback(&self) -> Result<(), Error> {
        if self.playback_started.load(Ordering::Acquire) {
            return Ok(());
        }
        let stream_id = self.playback_stream;
        if stream_id == u32::MAX {
            return Err(Error::IoError);
        }

        self.set_params(stream_id).map_err(virtio_err)?;
        self.prepare(stream_id).map_err(virtio_err)?;
        self.start(stream_id).map_err(virtio_err)?;
        self.playback_started.store(true, Ordering::Release);
        Ok(())
    }

    /// Writes up to one TX message worth of PCM frames and waits for the device
    /// to consume them. Returns the number of bytes consumed from `reader`.
    pub fn play(&self, reader: &mut VmReader<'_, Fallible>) -> Result<usize, Error> {
        self.prepare_playback()?;
        let stream_id = self.playback_stream;

        // Copy PCM frames into the DMA buffer, bounded so the data never
        // overwrites the status area at the end of the page.
        let len = {
            let mut writer = self.tx_buf.writer().unwrap();
            writer.skip(TX_DATA_OFFSET).limit(TX_DATA_CAP);
            writer.write_fallible(reader).map_err(|(err, _)| err)?
        };
        if len == 0 {
            return Ok(0);
        }

        // Fill the xfer header and flush everything the device will read.
        let xfer_slice = Slice::new(self.tx_buf.clone(), TX_XFER_OFFSET..TX_DATA_OFFSET);
        xfer_slice
            .write_val(0, &VirtioSndPcmXfer { stream_id })
            .unwrap();
        self.tx_buf
            .sync_to_device(TX_XFER_OFFSET..TX_DATA_OFFSET + len)
            .unwrap();

        // Submit `[xfer header][PCM data]` as device-readable, `[status]` as
        // device-writable (matching Linux's 2-out + 1-in TX message layout).
        {
            let mut queue = self.tx_queue.lock();
            let xfer_slice = Slice::new(self.tx_buf.clone(), TX_XFER_OFFSET..TX_DATA_OFFSET);
            let data_slice = Slice::new(self.tx_buf.clone(), TX_DATA_OFFSET..TX_DATA_OFFSET + len);
            let status_slice = Slice::new(
                self.tx_buf.clone(),
                TX_STATUS_OFFSET..TX_STATUS_OFFSET + size_of::<VirtioSndPcmStatus>(),
            );
            queue
                .add_dma_bufs(&[&xfer_slice, &data_slice], &[&status_slice])
                .expect("add tx queue buffers");
            if queue.should_notify() {
                queue.notify();
            }
        }

        // Wait for the device to report the message as consumed.
        loop {
            let mut queue = self.tx_queue.lock();
            if queue
                .pop_used_with_min_bytes(size_of::<VirtioSndPcmStatus>())
                .is_ok()
            {
                break;
            }
            drop(queue);
            spin_loop();
        }

        let status_slice = Slice::new(
            self.tx_buf.clone(),
            TX_STATUS_OFFSET..TX_STATUS_OFFSET + size_of::<VirtioSndPcmStatus>(),
        );
        status_slice.sync_from_device().unwrap();
        let status: VirtioSndPcmStatus = status_slice.read_val(0).unwrap();
        if status.status != S_OK {
            return Err(Error::IoError);
        }

        Ok(len)
    }

    fn set_params(&self, stream_id: u32) -> Result<(), VirtioDeviceError> {
        let req = VirtioSndPcmSetParams {
            hdr: VirtioSndPcmHdr {
                hdr: VirtioSndHdr { code: R_PCM_SET_PARAMS },
                stream_id,
            },
            buffer_bytes: BUFFER_BYTES,
            period_bytes: PERIOD_BYTES,
            features: 0,
            channels: CHANNELS,
            format: FMT_S16,
            rate: RATE_48000,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &req, size_of::<u32>())?;
        check_ok(code)
    }

    fn prepare(&self, stream_id: u32) -> Result<(), VirtioDeviceError> {
        let req = pcm_hdr(R_PCM_PREPARE, stream_id);
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &req, size_of::<u32>())?;
        check_ok(code)
    }

    fn start(&self, stream_id: u32) -> Result<(), VirtioDeviceError> {
        let req = pcm_hdr(R_PCM_START, stream_id);
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &req, size_of::<u32>())?;
        check_ok(code)
    }
}

fn pcm_hdr(code: u32, stream_id: u32) -> VirtioSndPcmHdr {
    VirtioSndPcmHdr {
        hdr: VirtioSndHdr { code },
        stream_id,
    }
}

fn check_ok(code: u32) -> Result<(), VirtioDeviceError> {
    if code == S_OK {
        Ok(())
    } else {
        ostd::warn!("virtio-sound control request failed: code = {:#x}", code);
        Err(VirtioDeviceError::UnsupportedConfig)
    }
}

fn virtio_err(e: VirtioDeviceError) -> Error {
    match e {
        VirtioDeviceError::ResourceAlloc(e) => e,
        _ => Error::IoError,
    }
}

/// Sends a control request and waits for the device to complete it, returning
/// the response status code.
fn control_cmd<T: ostd_pod::Pod>(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req: &T,
    resp_len: usize,
) -> Result<u32, VirtioDeviceError> {
    let req_len = size_of::<T>();

    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.write_val(0, req).unwrap();
    req_slice.sync_to_device().unwrap();

    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    queue
        .add_dma_bufs(&[&req_slice], &[&resp_slice])
        .expect("add control queue buffers");
    if queue.should_notify() {
        queue.notify();
    }

    loop {
        if queue.pop_used_with_min_bytes(resp_len).is_ok() {
            break;
        }
        spin_loop();
    }

    resp_slice.sync_from_device().unwrap();
    Ok(resp_slice.read_val::<u32>(0).unwrap())
}

/// Queries the `PCM_INFO` of every stream.
fn query_streams(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    num_streams: u32,
) -> Result<Vec<StreamInfo>, VirtioDeviceError> {
    let count = num_streams as usize;
    if count == 0 {
        return Ok(Vec::new());
    }

    let info_size = size_of::<VirtioSndPcmInfo>();
    let resp_len = size_of::<u32>() + count * info_size;

    let req = VirtioSndQueryInfo {
        hdr: VirtioSndHdr { code: R_PCM_INFO },
        start_id: 0,
        count: count as u32,
        size: info_size as u32,
    };
    let code = control_cmd(queue, buf, &req, resp_len)?;
    if code != S_OK {
        ostd::warn!("virtio-sound PCM_INFO failed: code = {:#x}", code);
        return Err(VirtioDeviceError::UnsupportedConfig);
    }

    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    resp_slice.sync_from_device().unwrap();

    let mut streams = Vec::with_capacity(count);
    for i in 0..count {
        let info: VirtioSndPcmInfo =
            resp_slice.read_val(size_of::<u32>() + i * info_size).unwrap();
        streams.push(StreamInfo {
            direction: info.direction,
            features: info.features,
            formats: info.formats,
            rates: info.rates,
            channels_min: info.channels_min,
            channels_max: info.channels_max,
        });
    }
    Ok(streams)
}

static SOUND_DEVICE_ID: AtomicUsize = AtomicUsize::new(0);
