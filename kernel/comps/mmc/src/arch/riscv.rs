// SPDX-License-Identifier: MPL-2.0

use alloc::sync::Arc;
use core::{
    hint::spin_loop,
    ops::Range,
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

use fdt::{Fdt, node::FdtNode};
use ostd::{
    arch::{
        boot::DEVICE_TREE,
        irq::{DeferredMappedIrqLine, IRQ_CHIP, InterruptSourceInFdt},
    },
    cpu::{CpuId, CpuSet},
    io::IoMem,
    irq::IrqLine,
    mm::{
        HasPaddr, HasSize, PAGE_SIZE,
        dma::{DmaCoherent, DmaWindow},
        io::{VmIo, VmIoOnce},
    },
    sync::WaitQueue,
    timer::{self, Jiffies, TIMER_FREQ},
};

use crate::{
    MMC_BOUNDED_PIO, MMC_DEFAULT_SPEED,
    card::{Card, HostController, Response},
    sdhci::{
        Command, DataDirection, HostError, Register, ResponseType, SDMA_BOUNDARY_BYTES,
        SdmaInterrupt, SdmaTransfer, classify_sdma_interrupt, decode_command_failure,
        decode_data_failure, eic7700_core_clock_config, next_sdma_boundary,
        sd_high_speed_host_control, sdma_host_control, sdma_v4_control, split_sdma_address,
        supports_sdma,
    },
};

const COMPATIBLE: &str = "eswin,sdhci-sdio";
const MMIO_START: usize = 0x5046_0000;
const MMIO_SIZE: usize = 0x1_0000;
const INTERRUPT: u32 = 81;
const BUS_WIDTH: usize = 4;
const CLOCK_FREQUENCY: u32 = 208_000_000;
const CLOCK_MMIO_START: usize = 0x5182_8000;
const CLOCK_MMIO_SIZE: usize = 0x8_0000;
const CLOCK_CORE_OFFSET: usize = 0x164;
const CLOCK_CORE_SIZE: usize = size_of::<u32>();
const CLOCK_AHB_OFFSET: usize = 0x148;
const CLOCK_CONFIG_OFFSET: usize = 0x14c;
const CLOCK_CORE_ENABLE: u32 = 1 << 16;
const CLOCK_CORE_DIVISOR_MASK: u32 = 0x0fff << 4;
const CLOCK_CORE_SELECT_416MHZ: u32 = 1;
const POLL_BUDGET: usize = 1_000_000;
/// How long a sleeping wait spins before it sleeps.
///
/// A woken task takes about 7 ms to be scheduled again on this board, so a
/// wait only sleeps when the expected device time clearly exceeds that; it
/// spins first to catch completions that arrive early.
const IRQ_SPIN_DURATION: Duration = Duration::from_millis(1);
/// Writes at least this large are expected to outlast the wake latency and
/// may sleep; smaller writes and all reads keep the polling path.
const IRQ_SLEEP_MIN_BYTES: usize = 64 * 1024;
/// Upper bound for one status wait in interrupt mode.
const IRQ_WAIT_TIMEOUT: Duration = Duration::from_secs(1);
/// One-shot timer cadence used to re-check a sleeping wait's deadline.
const IRQ_SLEEP_TICK: Duration = Duration::from_millis(1);

/// Waiters that sleep on SDHCI interrupt delivery or the one-shot timer.
static MMC_WAITERS: WaitQueue = WaitQueue::new();
const DMA_DEVICE_START: usize = 0x2000_0000;
const DMA_CPU_START: usize = 0xc000_0000;
const DMA_WINDOW_SIZE: usize = 0x4000_0000;
const SMMU_MMIO_START: usize = 0x50c0_0000;
const SMMU_MMIO_SIZE: usize = 0x10_0000;
const SD0_STREAM_ID: u32 = 16;

const BLOCK_COUNT: usize = 0x06;
const RESPONSE1: usize = 0x14;
const RESPONSE2: usize = 0x18;
const RESPONSE3: usize = 0x1c;
const HOST_CONTROL: usize = 0x28;
const INTERRUPT_ENABLE: usize = 0x34;
const SIGNAL_ENABLE: usize = 0x38;
const HOST_VERSION: usize = 0xfe;

const PRESENT_COMMAND_INHIBIT: u32 = 1 << 0;
const PRESENT_DATA_INHIBIT: u32 = 1 << 1;
const INTERRUPT_COMMAND_COMPLETE: u32 = 1 << 0;
const INTERRUPT_TRANSFER_COMPLETE: u32 = 1 << 1;
const INTERRUPT_DMA: u32 = 1 << 3;
const INTERRUPT_BUFFER_WRITE_READY: u32 = 1 << 4;
const INTERRUPT_BUFFER_READ_READY: u32 = 1 << 5;
const INTERRUPT_ERROR: u32 = 1 << 15;
const CLOCK_INTERNAL_ENABLE: u16 = 1 << 0;
const CLOCK_INTERNAL_STABLE: u16 = 1 << 1;
const CLOCK_CARD_ENABLE: u16 = 1 << 2;
const RESET_COMMAND: u8 = 1 << 1;
const RESET_DATA: u8 = 1 << 2;

#[derive(Clone, Debug, Eq, PartialEq)]
struct PlatformConfig {
    mmio_range: Range<usize>,
    clock_mmio_range: Range<usize>,
    dma_window: DmaWindow,
    interrupt: u32,
    interrupt_parent: u32,
    bus_width: usize,
    clock_frequency: u32,
    max_frequency: u32,
}

#[derive(Clone, Copy)]
struct ConfigFields {
    enabled: bool,
    compatible: bool,
    mmio: Option<(usize, usize)>,
    interrupt: Option<u32>,
    interrupt_parent: Option<u32>,
    bus_width: Option<usize>,
    clock_frequency: Option<u32>,
    max_frequency: Option<u32>,
    no_mmc: bool,
    clock_resource_valid: bool,
    iommu_stream_valid: bool,
    dma_window: Option<DmaWindow>,
    dma_noncoherent: bool,
}

fn valid_clock_resource(cells: &[u32], range: Option<(usize, usize)>) -> bool {
    cells.len() == 4
        && cells[1..]
            == [
                CLOCK_CORE_OFFSET as u32,
                CLOCK_AHB_OFFSET as u32,
                CLOCK_CONFIG_OFFSET as u32,
            ]
        && range == Some((CLOCK_MMIO_START, CLOCK_MMIO_SIZE))
}

impl ConfigFields {
    fn invalid_field(self) -> Option<&'static str> {
        if !self.enabled {
            return Some("status");
        }
        if !self.compatible {
            return Some("compatible");
        }
        if !matches!(self.mmio, Some((MMIO_START, MMIO_SIZE))) {
            return Some("reg");
        }
        if !matches!(self.interrupt, Some(INTERRUPT)) {
            return Some("interrupts");
        }
        if self.interrupt_parent.is_none() {
            return Some("interrupt-parent");
        }
        if !matches!(self.bus_width, Some(BUS_WIDTH)) {
            return Some("bus-width");
        }
        if !matches!(self.clock_frequency, Some(CLOCK_FREQUENCY)) {
            return Some("clock-frequency");
        }
        if !self.no_mmc {
            return Some("no-mmc");
        }
        if !self.clock_resource_valid {
            return Some("eswin,syscrg_csr");
        }
        if !self.iommu_stream_valid {
            return Some("iommus");
        }
        if self.dma_window != DmaWindow::new(DMA_DEVICE_START, DMA_CPU_START, DMA_WINDOW_SIZE) {
            return Some("dma-ranges");
        }
        if !self.dma_noncoherent {
            return Some("dma-noncoherent");
        }
        match self.max_frequency {
            Some(frequency) if frequency > 0 && frequency <= 208_000_000 => None,
            _ => Some("max-frequency"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ProbeError {
    InvalidDeviceTree,
    MmioUnavailable,
    Host(HostError),
}

impl ProbeError {
    pub(super) const fn stage(self) -> &'static str {
        match self {
            Self::InvalidDeviceTree => "device-tree",
            Self::MmioUnavailable => "mmio-acquire",
            Self::Host(_) => "host-handoff-or-card",
        }
    }
}

fn validate(fields: ConfigFields) -> Result<PlatformConfig, ProbeError> {
    if fields.invalid_field().is_some() {
        return Err(ProbeError::InvalidDeviceTree);
    }
    let max_frequency = fields.max_frequency.unwrap();
    // The Linux DT describes an IOVA window backed by SMMUv3 stream 16.
    // Asterinas does not configure a RISC-V IOMMU yet, so preserve U-Boot's
    // direct-DMA handoff instead of applying the unmapped IOVA offset.
    let dma_window = DmaWindow::new(DMA_CPU_START, DMA_CPU_START, DMA_WINDOW_SIZE)
        .ok_or(ProbeError::InvalidDeviceTree)?;
    Ok(PlatformConfig {
        mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
        clock_mmio_range: CLOCK_MMIO_START + CLOCK_CORE_OFFSET
            ..CLOCK_MMIO_START + CLOCK_CORE_OFFSET + CLOCK_CORE_SIZE,
        dma_window,
        interrupt: INTERRUPT,
        interrupt_parent: fields.interrupt_parent.unwrap(),
        bus_width: BUS_WIDTH,
        clock_frequency: fields.clock_frequency.unwrap(),
        max_frequency,
    })
}

fn select_config(
    fields: impl IntoIterator<Item = ConfigFields>,
) -> Result<Option<PlatformConfig>, ProbeError> {
    let mut saw_compatible = false;
    let mut selected = None;
    for fields in fields {
        saw_compatible = true;
        if fields.invalid_field().is_some() {
            continue;
        }
        let config = validate(fields)?;
        if selected.replace(config).is_some() {
            return Err(ProbeError::InvalidDeviceTree);
        }
    }
    match (saw_compatible, selected) {
        (_, Some(config)) => Ok(Some(config)),
        (true, None) => Err(ProbeError::InvalidDeviceTree),
        (false, None) => Ok(None),
    }
}

fn clock_resource_from_node(tree: &Fdt<'_>, node: FdtNode<'_, '_>) -> bool {
    let Some(property) = node.property("eswin,syscrg_csr") else {
        return false;
    };
    let (chunks, remainder) = property.value.as_chunks::<4>();
    if chunks.len() != 4 || !remainder.is_empty() {
        return false;
    }
    let cells = [
        u32::from_be_bytes(chunks[0]),
        u32::from_be_bytes(chunks[1]),
        u32::from_be_bytes(chunks[2]),
        u32::from_be_bytes(chunks[3]),
    ];
    let range = tree.find_phandle(cells[0]).and_then(|node| {
        let mut regions = node.reg()?;
        let first = regions.next()?;
        if regions.next().is_some() {
            return None;
        }
        Some((first.starting_address as usize, first.size?))
    });
    valid_clock_resource(&cells, range)
}

fn dma_window_from_cells(cells: &[u32]) -> Option<DmaWindow> {
    let [
        device_high,
        device_low,
        cpu_high,
        cpu_low,
        size_high,
        size_low,
    ] = cells
    else {
        return None;
    };
    let combine = |high: u32, low: u32| ((high as u64) << 32) | low as u64;
    DmaWindow::new(
        combine(*device_high, *device_low).try_into().ok()?,
        combine(*cpu_high, *cpu_low).try_into().ok()?,
        combine(*size_high, *size_low).try_into().ok()?,
    )
}

fn dma_window_from_node(node: FdtNode<'_, '_>) -> Option<DmaWindow> {
    let property = node.property("dma-ranges")?;
    let (chunks, remainder) = property.value.as_chunks::<4>();
    if chunks.len() != 6 || !remainder.is_empty() {
        return None;
    }
    let cells = [
        u32::from_be_bytes(chunks[0]),
        u32::from_be_bytes(chunks[1]),
        u32::from_be_bytes(chunks[2]),
        u32::from_be_bytes(chunks[3]),
        u32::from_be_bytes(chunks[4]),
        u32::from_be_bytes(chunks[5]),
    ];
    dma_window_from_cells(&cells)
}

fn valid_iommu_stream(
    cells: &[u32],
    provider_compatible: bool,
    provider_mmio: Option<(usize, usize)>,
    iommu_cells: Option<usize>,
) -> bool {
    matches!(cells, [_, SD0_STREAM_ID])
        && provider_compatible
        && provider_mmio == Some((SMMU_MMIO_START, SMMU_MMIO_SIZE))
        && iommu_cells == Some(1)
}

fn iommu_stream_from_node(tree: &Fdt<'_>, node: FdtNode<'_, '_>) -> bool {
    let Some(property) = node.property("iommus") else {
        return false;
    };
    let (chunks, remainder) = property.value.as_chunks::<4>();
    if chunks.len() != 2 || !remainder.is_empty() {
        return false;
    }
    let cells = [u32::from_be_bytes(chunks[0]), u32::from_be_bytes(chunks[1])];
    let Some(provider) = tree.find_phandle(cells[0]) else {
        return false;
    };
    let provider_compatible = provider
        .compatible()
        .is_some_and(|values| values.all().any(|value| value == "arm,smmu-v3"));
    let provider_mmio = provider.reg().and_then(|mut regions| {
        let first = regions.next()?;
        if regions.next().is_some() {
            return None;
        }
        Some((first.starting_address as usize, first.size?))
    });
    let iommu_cells = provider
        .property("#iommu-cells")
        .and_then(|property| property.as_usize());
    valid_iommu_stream(&cells, provider_compatible, provider_mmio, iommu_cells)
}

fn u32_cell(node: FdtNode<'_, '_>, name: &str) -> Option<u32> {
    let property = node.property(name)?;
    let bytes: [u8; 4] = property.value.try_into().ok()?;
    Some(u32::from_be_bytes(bytes))
}

fn fields_from_node(tree: &Fdt<'_>, node: FdtNode<'_, '_>) -> ConfigFields {
    let enabled = match node.property("status") {
        None => true,
        Some(status) => matches!(status.as_str(), Some("ok" | "okay")),
    };
    let compatible = node
        .compatible()
        .is_some_and(|values| values.all().any(|value| value == COMPATIBLE));
    let mmio = node.reg().and_then(|mut regions| {
        let first = regions.next()?;
        if regions.next().is_some() {
            return None;
        }
        Some((first.starting_address as usize, first.size?))
    });
    let interrupt = node
        .interrupts()
        .and_then(|mut interrupts| interrupts.next())
        .and_then(|value| value.try_into().ok());
    ConfigFields {
        enabled,
        compatible,
        mmio,
        interrupt,
        interrupt_parent: u32_cell(node, "interrupt-parent"),
        bus_width: node
            .property("bus-width")
            .and_then(|value| value.as_usize()),
        clock_frequency: node
            .property("clock-frequency")
            .and_then(|value| value.as_usize())
            .and_then(|value| value.try_into().ok()),
        max_frequency: node
            .property("max-frequency")
            .and_then(|value| value.as_usize())
            .and_then(|value| value.try_into().ok()),
        no_mmc: node.property("no-mmc").is_some(),
        clock_resource_valid: clock_resource_from_node(tree, node),
        iommu_stream_valid: iommu_stream_from_node(tree, node),
        dma_window: dma_window_from_node(node),
        dma_noncoherent: node.property("dma-noncoherent").is_some(),
    }
}

pub(super) fn probe() -> Result<Option<(MmioHost, Card)>, ProbeError> {
    let tree = DEVICE_TREE.get().unwrap();
    let nodes = tree.all_nodes().filter(|node| {
        node.compatible()
            .is_some_and(|values| values.all().any(|value| value == COMPATIBLE))
    });
    let Some(config) = select_config(nodes.map(|node| fields_from_node(tree, node)))? else {
        return Ok(None);
    };
    let mmio =
        IoMem::acquire(config.mmio_range.clone()).map_err(|_| ProbeError::MmioUnavailable)?;
    let clock_mmio =
        IoMem::acquire(config.clock_mmio_range.clone()).map_err(|_| ProbeError::MmioUnavailable)?;
    let mut host = MmioHost {
        mmio,
        clock_mmio,
        sdma: None,
        irq: None,
        data_sleep_allowed: false,
    };
    host.validate_handoff().map_err(ProbeError::Host)?;
    if sdma_allowed(MMC_BOUNDED_PIO.load(Ordering::Relaxed)) {
        match host.enable_sdma(config.dma_window) {
            Ok(()) => ostd::info!(
                "[mmc] controller {:#x} irq={} sdma boundary={}",
                config.mmio_range.start,
                config.interrupt,
                SDMA_BOUNDARY_BYTES
            ),
            Err(error) => ostd::warn!(
                "[mmc] controller {:#x} irq={} bounded-pio-fallback reason={:?}",
                config.mmio_range.start,
                config.interrupt,
                error
            ),
        }
    } else {
        ostd::warn!(
            "[mmc] controller {:#x} irq={} bounded-pio-forced",
            config.mmio_range.start,
            config.interrupt
        );
    }
    let card = Card::discover_with_policy(
        &mut host,
        !high_speed_allowed(MMC_DEFAULT_SPEED.load(Ordering::Relaxed)),
    )
    .map_err(ProbeError::Host)?;
    let mut sector0 = [0u8; 512];
    card.read_sector(&mut host, 0, &mut sector0)
        .map_err(ProbeError::Host)?;
    ostd::info!(
        "[mmc] SDHC rca={} sectors={} sector0={:02x}{:02x}",
        card.rca(),
        card.nr_sectors(),
        sector0[510],
        sector0[511]
    );
    ostd::info!(
        "[mmc] timing={} clock={}",
        card.speed_selection().label(),
        card.data_clock_hz()
    );
    // Card discovery runs on the polling path (no current task may be
    // available during early component initialization). Runtime transfers can
    // sleep, so enable interrupts only once discovery has finished.
    if crate::MMC_INTERRUPTS.load(Ordering::Relaxed) {
        match host.enable_interrupts(config.interrupt_parent, config.interrupt) {
            Ok(()) => ostd::info!(
                "[mmc] interrupts enabled on source {} (parent {})",
                config.interrupt,
                config.interrupt_parent
            ),
            Err(error) => ostd::warn!("[mmc] interrupts unavailable: {:?}", error),
        }
    }
    Ok(Some((host, card)))
}

const fn sdma_allowed(force_bounded_pio: bool) -> bool {
    !force_bounded_pio
}

const fn high_speed_allowed(force_default_speed: bool) -> bool {
    !force_default_speed
}

/// Converts a wall-clock duration into timer ticks, at least one.
fn duration_ticks(duration: Duration) -> u64 {
    let ticks = duration.as_nanos() * u128::from(TIMER_FREQ) / 1_000_000_000;
    u64::try_from(ticks).unwrap_or(u64::MAX).max(1)
}

/// Safe MMIO-backed SDHCI host.
pub(super) struct MmioHost {
    mmio: IoMem,
    clock_mmio: IoMem,
    sdma: Option<SdmaBuffer>,
    irq: Option<IrqCompletion>,
    /// Whether the current data transfer may sleep instead of polling.
    data_sleep_allowed: bool,
}

/// Interrupt delivery for the SDHCI controller.
///
/// The handler runs with the PLIC source masked: it counts the delivery and
/// wakes any sleeper, while the waiter reads and clears the controller status
/// itself. Waiting code re-arms the source before each sleep, so a
/// level-triggered line cannot storm and a completion is never lost behind a
/// source that a previous wake left masked.
struct IrqCompletion {
    line: DeferredMappedIrqLine,
    /// Incremented by the handler so a waiter can tell a real wakeup from a
    /// spurious return of the wait queue.
    deliveries: Arc<AtomicU64>,
    /// CPUs on which the one-shot timer waker has been installed.
    installed_on: CpuSet,
}

/// Why a sleeping wait stopped.
enum WaitWake {
    /// The wanted interrupt bits are visible.
    Ready(u32),
    /// An interrupt arrived but the wanted bits are not set yet.
    Interrupted,
    /// The wait timed out.
    TimedOut,
}

struct SdmaBuffer {
    memory: DmaCoherent,
    device_start: usize,
}

impl MmioHost {
    /// Registers the masked interrupt handler and enables interrupt signaling.
    fn enable_interrupts(&mut self, parent: u32, interrupt: u32) -> Result<(), HostError> {
        // `validate_handoff` leaves the normal/error signal enables at zero,
        // which keeps the line deasserted for the polling path. Interrupt mode
        // has a masked handler, so it can enable signaling again.
        self.write32(INTERRUPT_ENABLE, u32::MAX)?;
        self.write32(SIGNAL_ENABLE, u32::MAX)?;
        let irq_line = IrqLine::alloc().map_err(|_| HostError::Unsupported)?;
        let chip = IRQ_CHIP.get().ok_or(HostError::Unsupported)?;
        let mut line = chip
            .map_fdt_pin_to_masked(
                InterruptSourceInFdt {
                    interrupt_parent: parent,
                    interrupt,
                },
                irq_line,
            )
            .map_err(|_| HostError::Unsupported)?;
        let deliveries = Arc::new(AtomicU64::new(0));
        let counter = deliveries.clone();
        line.on_active_and_mask(move |_| {
            counter.fetch_add(1, Ordering::Relaxed);
            MMC_WAITERS.wake_all();
        })
        .map_err(|_| HostError::Unsupported)?;
        self.irq = Some(IrqCompletion {
            line,
            deliveries,
            installed_on: CpuSet::new_empty(),
        });
        Ok(())
    }

    /// Re-arms the source and schedules a timer wakeup on the current CPU.
    fn arm_irq(&mut self) -> bool {
        let Some(irq) = &mut self.irq else {
            return false;
        };
        if irq.line.rearm().is_err() {
            return false;
        }
        let cpu = CpuId::current_racy();
        if !irq.installed_on.contains(cpu) {
            irq.installed_on.add(cpu);
            timer::register_high_resolution_callback_on_cpu(|| {
                MMC_WAITERS.wake_all();
            });
        }
        timer::request_interrupt_after(IRQ_SLEEP_TICK);
        true
    }

    /// Waits until `wanted` or an error bit appears in InterruptStatus.
    ///
    /// With `may_sleep` set, the wait spins for a short while and then sleeps
    /// until the handler or the one-shot timer wakes it. The timer bounds a
    /// lost interrupt to a timeout; the source is re-armed before every sleep
    /// because the handler masks it on each delivery.
    fn wait_status(&mut self, wanted: u32, may_sleep: bool) -> Result<u32, HostError> {
        let mask = wanted | INTERRUPT_ERROR;
        let Some(frequency) = (self.irq.is_some() && may_sleep).then(|| ostd::arch::tsc_freq())
        else {
            return self.poll_status(mask, POLL_BUDGET);
        };
        if frequency == 0 {
            return self.poll_status(mask, POLL_BUDGET);
        }

        let spin_ticks = frequency * IRQ_SPIN_DURATION.as_micros() as u64 / 1_000_000;
        let spin_start = ostd::arch::read_tsc();
        loop {
            let status = self.read32(Register::InterruptStatus.offset())?;
            if status & mask != 0 {
                return Ok(status);
            }
            if ostd::arch::read_tsc().wrapping_sub(spin_start) >= spin_ticks {
                break;
            }
            spin_loop();
        }

        let deadline = Jiffies::elapsed().as_u64() + duration_ticks(IRQ_WAIT_TIMEOUT);
        loop {
            let observed = self
                .irq
                .as_ref()
                .ok_or(HostError::Unsupported)?
                .deliveries
                .load(Ordering::Relaxed);
            if !self.arm_irq() {
                return Err(HostError::Unsupported);
            }
            let host = &*self;
            let wake = MMC_WAITERS.wait_until(|| {
                if Jiffies::elapsed().as_u64() >= deadline {
                    return Some(WaitWake::TimedOut);
                }
                match host.read32(Register::InterruptStatus.offset()) {
                    Ok(status) if status & mask != 0 => return Some(WaitWake::Ready(status)),
                    _ => {}
                }
                let delivered = host
                    .irq
                    .as_ref()
                    .is_some_and(|irq| irq.deliveries.load(Ordering::Relaxed) != observed);
                delivered.then_some(WaitWake::Interrupted)
            });
            match wake {
                WaitWake::Ready(status) => return Ok(status),
                WaitWake::Interrupted => continue,
                WaitWake::TimedOut => return Err(HostError::Timeout),
            }
        }
    }

    fn poll_status(&self, mask: u32, budget: usize) -> Result<u32, HostError> {
        for _ in 0..budget {
            let status = self.read32(Register::InterruptStatus.offset())?;
            if status & mask != 0 {
                return Ok(status);
            }
            spin_loop();
        }
        Err(HostError::Timeout)
    }

    fn enable_sdma(&mut self, window: DmaWindow) -> Result<(), HostError> {
        let capabilities = self.read32(Register::Capabilities.offset())?;
        if !supports_sdma(capabilities) {
            return Err(HostError::Unsupported);
        }
        let host_control2 = sdma_v4_control(self.read16(Register::HostControl2.offset())?)?;
        self.write16(Register::HostControl2.offset(), host_control2)?;
        let cpu_end = window
            .cpu_start()
            .checked_add(window.size())
            .ok_or(HostError::Unsupported)?;
        let memory = DmaCoherent::alloc_in(
            SDMA_BOUNDARY_BYTES / PAGE_SIZE,
            false,
            window.cpu_start()..cpu_end,
        )
        .and_then(DmaCoherent::into_uncached)
        .map_err(|_| HostError::Unsupported)?;
        let physical_end = memory
            .paddr()
            .checked_add(memory.size())
            .ok_or(HostError::Unsupported)?;
        let device_range = window
            .translate(memory.paddr()..physical_end)
            .ok_or(HostError::Unsupported)?;
        if device_range.len() != SDMA_BOUNDARY_BYTES
            || !device_range.start.is_multiple_of(SDMA_BOUNDARY_BYTES)
        {
            return Err(HostError::Unsupported);
        }
        ostd::info!(
            "[mmc] SDMA buffer cpu={:#x} device={:#x} bytes={}",
            memory.paddr(),
            device_range.start,
            memory.size()
        );
        self.sdma = Some(SdmaBuffer {
            memory,
            device_start: device_range.start,
        });
        Ok(())
    }

    fn validate_handoff(&self) -> Result<(), HostError> {
        let version = self.read16(HOST_VERSION)?;
        if version == 0 || version == u16::MAX {
            return Err(HostError::Unsupported);
        }
        let clock = self.read16(Register::ClockControl.offset())?;
        if clock & (CLOCK_INTERNAL_ENABLE | CLOCK_INTERNAL_STABLE | CLOCK_CARD_ENABLE)
            != CLOCK_INTERNAL_ENABLE | CLOCK_INTERNAL_STABLE | CLOCK_CARD_ENABLE
        {
            return Err(HostError::Unsupported);
        }
        self.write32(INTERRUPT_ENABLE, u32::MAX)?;
        self.write32(SIGNAL_ENABLE, 0)?;
        Ok(())
    }

    fn wait_clear(&self, offset: usize, mask: u32) -> Result<(), HostError> {
        for _ in 0..POLL_BUDGET {
            if self.read32(offset)? & mask == 0 {
                return Ok(());
            }
            spin_loop();
        }
        Err(HostError::Timeout)
    }

    fn wait_interrupt(&mut self, wanted: u32) -> Result<(), HostError> {
        let status = self.wait_status(wanted, false)?;
        if status & INTERRUPT_ERROR != 0 {
            self.write32(Register::InterruptStatus.offset(), status)?;
            let failure =
                decode_data_failure(wanted, status).unwrap_or(crate::sdhci::DataFailure {
                    wanted,
                    status,
                    error: HostError::Unsupported,
                });
            ostd::error!(
                "[mmc] data wait {:#x} failed: status={:#x} error={:?}",
                failure.wanted,
                failure.status,
                failure.error
            );
            return Err(failure.error);
        }
        self.write32(Register::InterruptStatus.offset(), wanted)?;
        Ok(())
    }

    fn wait_command_complete(&mut self, index: u8) -> Result<(), HostError> {
        // Commands finish in microseconds; sleeping would only add the wake
        // latency, so command completion always polls.
        let status = self.wait_status(INTERRUPT_COMMAND_COMPLETE, false)?;
        if status & INTERRUPT_ERROR != 0 {
            self.write32(Register::InterruptStatus.offset(), status)?;
            let failure =
                decode_command_failure(index, status).unwrap_or(crate::sdhci::CommandFailure {
                    index,
                    status,
                    error: HostError::Unsupported,
                });
            ostd::error!(
                "[mmc] CMD{} failed: status={:#x} error={:?}",
                failure.index,
                failure.status,
                failure.error
            );
            return Err(failure.error);
        }
        self.write32(
            Register::InterruptStatus.offset(),
            INTERRUPT_COMMAND_COMPLETE,
        )?;
        Ok(())
    }

    fn submit_sdma(&mut self, command: Command, length: usize) -> Result<(), HostError> {
        let device_start = self
            .sdma
            .as_ref()
            .ok_or(HostError::Unsupported)?
            .device_start;
        let device_end = device_start
            .checked_add(length)
            .ok_or(HostError::Unsupported)?;
        let transfer = SdmaTransfer::new(command, device_start..device_end)?;
        // Large writes program the card for tens of milliseconds; sleeping
        // through that frees the CPU. Everything else keeps polling.
        self.data_sleep_allowed = self.irq.is_some()
            && command.data == Some(DataDirection::Write)
            && length >= IRQ_SLEEP_MIN_BYTES;
        self.wait_clear(
            Register::PresentState.offset(),
            PRESENT_COMMAND_INHIBIT | PRESENT_DATA_INHIBIT,
        )?;
        self.write32(Register::InterruptStatus.offset(), u32::MAX)?;
        self.write8(HOST_CONTROL, sdma_host_control(self.read8(HOST_CONTROL)?))?;
        self.write_sdma_address(transfer.system_address as u64)?;
        self.write16(Register::BlockSize.offset(), transfer.block_size)?;
        self.write16(BLOCK_COUNT, transfer.block_count)?;
        // RockOS U-Boot leaves this controller in SDHCI v4 mode, where 0x00
        // is the 32-bit block-count register rather than the legacy SDMA
        // system-address register. Mirror its compatibility write.
        self.write16(Register::SystemAddress.offset(), transfer.block_count)?;
        self.write16(Register::TransferMode.offset(), transfer.transfer_mode)?;
        self.write32(Register::Argument.offset(), command.argument)?;
        self.write16(Register::Command.offset(), command.command_bits())?;
        self.wait_command_complete(command.index)?;
        self.wait_sdma(transfer.system_address as u64)
    }

    fn wait_sdma(&mut self, mut system_address: u64) -> Result<(), HostError> {
        let may_sleep = self.data_sleep_allowed;
        loop {
            let status =
                self.wait_status(INTERRUPT_TRANSFER_COMPLETE | INTERRUPT_DMA, may_sleep)?;
            match classify_sdma_interrupt(status) {
                Ok(SdmaInterrupt::Pending) => continue,
                Ok(SdmaInterrupt::Complete) => {
                    self.write32(
                        Register::InterruptStatus.offset(),
                        status & (INTERRUPT_TRANSFER_COMPLETE | INTERRUPT_DMA),
                    )?;
                    return Ok(());
                }
                Ok(SdmaInterrupt::Boundary) => {
                    self.write32(Register::InterruptStatus.offset(), INTERRUPT_DMA)?;
                    system_address = next_sdma_boundary(system_address).ok_or(HostError::Dma)?;
                    self.write_sdma_address(system_address)?;
                }
                Err(error) => {
                    self.write32(Register::InterruptStatus.offset(), status)?;
                    ostd::error!(
                        "[mmc] SDMA failed: status={:#x} address={:#x} error={:?}",
                        status,
                        system_address,
                        error
                    );
                    return Err(error);
                }
            }
        }
    }

    fn write_sdma_address(&self, address: u64) -> Result<(), HostError> {
        let (low, high) = split_sdma_address(address);
        self.write32(Register::SdmaAddress.offset(), low)?;
        self.write32(Register::SdmaAddressHigh.offset(), high)
    }

    fn read8(&self, offset: usize) -> Result<u8, HostError> {
        self.mmio
            .read_once(offset)
            .map_err(|_| HostError::Unsupported)
    }

    fn read16(&self, offset: usize) -> Result<u16, HostError> {
        self.mmio
            .read_once(offset)
            .map_err(|_| HostError::Unsupported)
    }

    fn read32(&self, offset: usize) -> Result<u32, HostError> {
        self.mmio
            .read_once(offset)
            .map_err(|_| HostError::Unsupported)
    }

    fn write8(&self, offset: usize, value: u8) -> Result<(), HostError> {
        self.mmio
            .write_once(offset, &value)
            .map_err(|_| HostError::Unsupported)
    }

    fn write16(&self, offset: usize, value: u16) -> Result<(), HostError> {
        self.mmio
            .write_once(offset, &value)
            .map_err(|_| HostError::Unsupported)
    }

    fn write32(&self, offset: usize, value: u32) -> Result<(), HostError> {
        self.mmio
            .write_once(offset, &value)
            .map_err(|_| HostError::Unsupported)
    }

    fn read_core_clock(&self) -> Result<u32, HostError> {
        self.clock_mmio
            .read_once(0)
            .map_err(|_| HostError::Unsupported)
    }

    fn write_core_clock(&self, value: u32) -> Result<(), HostError> {
        self.clock_mmio
            .write_once(0, &value)
            .map_err(|_| HostError::Unsupported)
    }

    fn long_response(&self) -> Result<[u32; 4], HostError> {
        let response = |offset| -> Result<u32, HostError> {
            Ok((self.read32(offset)? << 8) | self.read8(offset - 1)? as u32)
        };
        Ok([
            response(RESPONSE3)?,
            response(RESPONSE2)?,
            response(RESPONSE1)?,
            self.read32(Register::Response0.offset())? << 8,
        ])
    }
}

impl HostController for MmioHost {
    fn reset(&mut self) -> Result<(), HostError> {
        self.write8(Register::SoftwareReset.offset(), RESET_COMMAND | RESET_DATA)?;
        for _ in 0..POLL_BUDGET {
            if self.read8(Register::SoftwareReset.offset())? & (RESET_COMMAND | RESET_DATA) == 0 {
                return Ok(());
            }
            spin_loop();
        }
        Err(HostError::Timeout)
    }

    fn set_clock(&mut self, hz: u32) -> Result<(), HostError> {
        let config = eic7700_core_clock_config(hz)?;
        let host_clock = self.read16(Register::ClockControl.offset())?;
        self.write16(
            Register::ClockControl.offset(),
            host_clock & !CLOCK_CARD_ENABLE,
        )?;

        let old_core = self.read_core_clock()?;
        self.write_core_clock(old_core & !CLOCK_CORE_ENABLE)?;
        for _ in 0..POLL_BUDGET {
            spin_loop();
        }
        let mut new_core = old_core & !(CLOCK_CORE_DIVISOR_MASK | CLOCK_CORE_SELECT_416MHZ);
        new_core |= (config.divisor as u32) << 4;
        if config.select_416mhz {
            new_core |= CLOCK_CORE_SELECT_416MHZ;
        }
        self.write_core_clock(new_core)?;
        for _ in 0..POLL_BUDGET / 10 {
            spin_loop();
        }
        self.write_core_clock(new_core | CLOCK_CORE_ENABLE)?;
        for _ in 0..POLL_BUDGET {
            spin_loop();
        }

        self.write16(
            Register::ClockControl.offset(),
            (host_clock & !CLOCK_CARD_ENABLE) | CLOCK_INTERNAL_ENABLE,
        )?;
        for _ in 0..POLL_BUDGET {
            if self.read16(Register::ClockControl.offset())? & CLOCK_INTERNAL_STABLE != 0 {
                self.write16(
                    Register::ClockControl.offset(),
                    host_clock | CLOCK_INTERNAL_ENABLE | CLOCK_CARD_ENABLE,
                )?;
                return Ok(());
            }
            spin_loop();
        }
        Err(HostError::Timeout)
    }

    fn set_timing(&mut self, timing: crate::card::CardTiming) -> Result<(), HostError> {
        let value = self.read8(HOST_CONTROL)?;
        let value =
            sd_high_speed_host_control(value, matches!(timing, crate::card::CardTiming::HighSpeed));
        self.write8(HOST_CONTROL, value)
    }

    fn command(&mut self, command: Command) -> Result<Response, HostError> {
        if !command.has_valid_data_shape() {
            return Err(HostError::Unsupported);
        }
        let inhibit = PRESENT_COMMAND_INHIBIT
            | if command.data.is_some() {
                PRESENT_DATA_INHIBIT
            } else {
                0
            };
        self.wait_clear(Register::PresentState.offset(), inhibit)?;
        self.write32(Register::InterruptStatus.offset(), u32::MAX)?;
        if command.data.is_some() {
            self.write16(Register::BlockSize.offset(), command.block_size() as u16)?;
            self.write16(BLOCK_COUNT, command.block_count() as u16)?;
            self.write16(
                Register::TransferMode.offset(),
                command.transfer_mode_bits(),
            )?;
        } else {
            self.write16(Register::TransferMode.offset(), 0)?;
        }
        self.write32(Register::Argument.offset(), command.argument)?;
        self.write16(Register::Command.offset(), command.command_bits())?;
        self.wait_command_complete(command.index)?;
        match command.response {
            ResponseType::None => Ok(Response::None),
            ResponseType::Long => Ok(Response::Long(self.long_response()?)),
            ResponseType::Short | ResponseType::ShortNoChecks | ResponseType::ShortBusy => {
                Ok(Response::Short(self.read32(Register::Response0.offset())?))
            }
        }
    }

    fn set_bus_width_4(&mut self) -> Result<(), HostError> {
        let value = self.read8(HOST_CONTROL)? | (1 << 1);
        self.write8(HOST_CONTROL, value)
    }

    fn wait_buffer_read_ready(&mut self) -> Result<(), HostError> {
        self.wait_interrupt(INTERRUPT_BUFFER_READ_READY)
    }

    fn read_data_word(&mut self) -> Result<u32, HostError> {
        self.read32(Register::BufferData.offset())
    }

    fn wait_buffer_write_ready(&mut self) -> Result<(), HostError> {
        self.wait_interrupt(INTERRUPT_BUFFER_WRITE_READY)
    }

    fn write_data_word(&mut self, value: u32) -> Result<(), HostError> {
        self.write32(Register::BufferData.offset(), value)
    }

    fn wait_transfer_complete(&mut self) -> Result<(), HostError> {
        self.wait_interrupt(INTERRUPT_TRANSFER_COMPLETE)
    }

    /// Recovers from a failed transfer instead of leaving the controller stuck.
    ///
    /// A multi-block transfer that fails on its auto CMD12 leaves two things
    /// behind: the data line is not idle, and the card never received the stop
    /// it is waiting for. Resetting only the data line clears the first and not
    /// the second, so on the Megrez board every later transfer failed too and
    /// the machine could not even read a binary to reboot. Reset command and
    /// data together, clear the latched status, and send the stop command the
    /// controller failed to deliver.
    fn reset_data_line(&mut self) {
        let reset = RESET_COMMAND | RESET_DATA;
        let _ = self.write8(Register::SoftwareReset.offset(), reset);
        for _ in 0..POLL_BUDGET {
            match self.read8(Register::SoftwareReset.offset()) {
                Ok(value) if value & reset == 0 => break,
                Ok(_) => spin_loop(),
                Err(_) => return,
            }
        }
        if let Ok(status) = self.read32(Register::InterruptStatus.offset()) {
            let _ = self.write32(Register::InterruptStatus.offset(), status);
        }
        // Best effort: the card may already be back in transfer state, in which
        // case CMD12 is answered with an error that nothing needs to act on.
        let _ = self.command(Command::stop_transmission());
        if let Ok(status) = self.read32(Register::InterruptStatus.offset()) {
            let _ = self.write32(Register::InterruptStatus.offset(), status);
        }
    }

    fn max_blocks_per_command(&self) -> usize {
        if self.sdma.is_some() {
            SDMA_BOUNDARY_BYTES / 512
        } else {
            u16::MAX as usize
        }
    }

    fn try_read_blocks(&mut self, command: Command, output: &mut [u8]) -> Result<bool, HostError> {
        if self.sdma.is_none() {
            return Ok(false);
        }
        if command.data != Some(DataDirection::Read) {
            return Err(HostError::Unsupported);
        }
        self.submit_sdma(command, output.len())?;
        self.sdma
            .as_ref()
            .unwrap()
            .memory
            .read_bytes(0, output)
            .map_err(|_| HostError::Dma)?;
        Ok(true)
    }

    fn try_write_blocks(&mut self, command: Command, data: &[u8]) -> Result<bool, HostError> {
        if self.sdma.is_none() {
            return Ok(false);
        }
        if command.data != Some(DataDirection::Write) {
            return Err(HostError::Unsupported);
        }
        self.sdma
            .as_ref()
            .unwrap()
            .memory
            .write_bytes(0, data)
            .map_err(|_| HostError::Dma)?;
        self.submit_sdma(command, data.len())?;
        Ok(true)
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn valid_fields() -> ConfigFields {
        ConfigFields {
            enabled: true,
            compatible: true,
            mmio: Some((MMIO_START, MMIO_SIZE)),
            interrupt: Some(INTERRUPT),
            interrupt_parent: Some(16),
            bus_width: Some(BUS_WIDTH),
            clock_frequency: Some(208_000_000),
            max_frequency: Some(208_000_000),
            no_mmc: true,
            clock_resource_valid: true,
            iommu_stream_valid: true,
            dma_window: DmaWindow::new(0x2000_0000, 0xc000_0000, 0x4000_0000),
            dma_noncoherent: true,
        }
    }

    #[ktest]
    fn uses_uboot_identity_dma_after_validating_linux_resources() {
        assert_eq!(
            validate(valid_fields()).unwrap(),
            PlatformConfig {
                mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
                clock_mmio_range: CLOCK_MMIO_START + CLOCK_CORE_OFFSET
                    ..CLOCK_MMIO_START + CLOCK_CORE_OFFSET + CLOCK_CORE_SIZE,
                dma_window: DmaWindow::new(0xc000_0000, 0xc000_0000, 0x4000_0000).unwrap(),
                interrupt: INTERRUPT,
                interrupt_parent: 16,
                bus_width: BUS_WIDTH,
                clock_frequency: 208_000_000,
                max_frequency: 208_000_000,
            }
        );
        assert_eq!(
            validate(valid_fields())
                .unwrap()
                .dma_window
                .translate(0xfff0_0000..0xfff8_0000),
            Some(0xfff0_0000..0xfff8_0000)
        );
    }

    #[ktest]
    fn rejects_emmc_and_resource_drift() {
        let mut fields = valid_fields();
        fields.mmio = Some((0x5045_0000, MMIO_SIZE));
        assert_eq!(validate(fields), Err(ProbeError::InvalidDeviceTree));

        for mutate in [
            |fields: &mut ConfigFields| fields.enabled = false,
            |fields: &mut ConfigFields| fields.compatible = false,
            |fields: &mut ConfigFields| fields.interrupt = Some(79),
            |fields: &mut ConfigFields| fields.bus_width = Some(8),
            |fields: &mut ConfigFields| fields.no_mmc = false,
        ] {
            let mut fields = valid_fields();
            mutate(&mut fields);
            assert_eq!(validate(fields), Err(ProbeError::InvalidDeviceTree));
        }
    }

    #[ktest]
    fn probe_errors_have_stable_diagnostic_stages() {
        assert_eq!(ProbeError::InvalidDeviceTree.stage(), "device-tree");
        assert_eq!(ProbeError::MmioUnavailable.stage(), "mmio-acquire");
        assert_eq!(
            ProbeError::Host(HostError::Timeout).stage(),
            "host-handoff-or-card"
        );
    }

    #[ktest]
    fn identifies_the_first_rejected_dtb_field() {
        assert_eq!(valid_fields().invalid_field(), None);

        let mut fields = valid_fields();
        fields.mmio = Some((MMIO_START, MMIO_SIZE / 2));
        assert_eq!(fields.invalid_field(), Some("reg"));

        let mut fields = valid_fields();
        fields.interrupt = None;
        assert_eq!(fields.invalid_field(), Some("interrupts"));

        let mut fields = valid_fields();
        fields.interrupt_parent = None;
        assert_eq!(fields.invalid_field(), Some("interrupt-parent"));

        let mut fields = valid_fields();
        fields.clock_frequency = None;
        assert_eq!(fields.invalid_field(), Some("clock-frequency"));

        let mut fields = valid_fields();
        fields.max_frequency = None;
        assert_eq!(fields.invalid_field(), Some("max-frequency"));
    }

    #[ktest]
    fn selects_one_fully_valid_node_among_compatible_controllers() {
        let mut other_sdio = valid_fields();
        other_sdio.mmio = Some((0x5047_0000, MMIO_SIZE));
        assert_eq!(
            select_config([other_sdio, valid_fields()]),
            Ok(Some(PlatformConfig {
                mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
                clock_mmio_range: CLOCK_MMIO_START + CLOCK_CORE_OFFSET
                    ..CLOCK_MMIO_START + CLOCK_CORE_OFFSET + CLOCK_CORE_SIZE,
                dma_window: DmaWindow::new(0xc000_0000, 0xc000_0000, 0x4000_0000).unwrap(),
                interrupt: INTERRUPT,
                interrupt_parent: 16,
                bus_width: BUS_WIDTH,
                clock_frequency: 208_000_000,
                max_frequency: 208_000_000,
            }))
        );
        assert_eq!(
            select_config([valid_fields(), valid_fields()]),
            Err(ProbeError::InvalidDeviceTree)
        );
    }

    #[ktest]
    fn accepts_only_the_megrez_sdhci_core_clock_resource() {
        assert!(valid_clock_resource(
            &[0x12, 0x164, 0x148, 0x14c],
            Some((0x5182_8000, 0x8_0000))
        ));
        assert!(!valid_clock_resource(
            &[0x12, 0x165, 0x148, 0x14c],
            Some((0x5182_8000, 0x8_0000))
        ));
        assert!(!valid_clock_resource(
            &[0x12, 0x164, 0x148, 0x14c],
            Some((0x5181_0000, 0x8_0000))
        ));
    }

    #[ktest]
    fn megrez_sdma_accepts_only_the_exact_noncoherent_dma_window() {
        assert_eq!(
            dma_window_from_cells(&[0, 0x2000_0000, 0, 0xc000_0000, 0, 0x4000_0000]),
            DmaWindow::new(0x2000_0000, 0xc000_0000, 0x4000_0000)
        );
        assert_eq!(
            dma_window_from_cells(&[0, 0x2000_0000, 0, 0xc000_0000, 0, 0]),
            None
        );
        assert_eq!(dma_window_from_cells(&[0, 1, 2]), None);

        let mut fields = valid_fields();
        fields.dma_noncoherent = false;
        assert_eq!(fields.invalid_field(), Some("dma-noncoherent"));

        let mut fields = valid_fields();
        fields.dma_window = DmaWindow::new(0, 0xc000_0000, 0x4000_0000);
        assert_eq!(fields.invalid_field(), Some("dma-ranges"));

        let mut fields = valid_fields();
        fields.iommu_stream_valid = false;
        assert_eq!(fields.invalid_field(), Some("iommus"));
    }

    #[ktest]
    fn bounded_pio_boot_policy_skips_sdma_without_changing_the_default() {
        assert!(!sdma_allowed(true));
        assert!(sdma_allowed(false));
    }

    #[ktest]
    fn default_speed_boot_policy_skips_promotion_without_changing_the_default() {
        assert!(!high_speed_allowed(true));
        assert!(high_speed_allowed(false));
    }

    #[ktest]
    fn accepts_only_the_linux_smmuv3_sd0_stream_contract() {
        assert!(valid_iommu_stream(
            &[0x15, 16],
            true,
            Some((0x50c0_0000, 0x10_0000)),
            Some(1)
        ));
        assert!(!valid_iommu_stream(
            &[0x15, 15],
            true,
            Some((0x50c0_0000, 0x10_0000)),
            Some(1)
        ));
        assert!(!valid_iommu_stream(
            &[0x15, 16],
            true,
            Some((0x50c0_0000, 0x10_0000)),
            Some(2)
        ));
        assert!(!valid_iommu_stream(
            &[0x15, 16, 0],
            true,
            Some((0x50c0_0000, 0x10_0000)),
            Some(1)
        ));
    }
}
