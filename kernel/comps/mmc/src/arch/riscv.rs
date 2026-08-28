// SPDX-License-Identifier: MPL-2.0

use core::{hint::spin_loop, ops::Range};

use fdt::{Fdt, node::FdtNode};
use ostd::{arch::boot::DEVICE_TREE, io::IoMem, mm::io::VmIoOnce};

use crate::{
    card::{Card, HostController, Response},
    sdhci::{
        Command, HostError, Register, ResponseType, decode_command_failure, decode_data_failure,
        eic7700_core_clock_config,
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
    interrupt: u32,
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
    bus_width: Option<usize>,
    clock_frequency: Option<u32>,
    max_frequency: Option<u32>,
    no_mmc: bool,
    clock_resource_valid: bool,
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
    const fn invalid_field(self) -> Option<&'static str> {
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
    Ok(PlatformConfig {
        mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
        clock_mmio_range: CLOCK_MMIO_START + CLOCK_CORE_OFFSET
            ..CLOCK_MMIO_START + CLOCK_CORE_OFFSET + CLOCK_CORE_SIZE,
        interrupt: INTERRUPT,
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
    let mut host = MmioHost { mmio, clock_mmio };
    host.validate_handoff().map_err(ProbeError::Host)?;
    ostd::info!(
        "[mmc] controller {:#x} irq={} bounded-pio",
        config.mmio_range.start,
        config.interrupt
    );
    let card = Card::discover(&mut host).map_err(ProbeError::Host)?;
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
    Ok(Some((host, card)))
}

/// Safe MMIO-backed SDHCI polling host.
pub(super) struct MmioHost {
    mmio: IoMem,
    clock_mmio: IoMem,
}

impl MmioHost {
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

    fn wait_interrupt(&self, wanted: u32) -> Result<(), HostError> {
        for _ in 0..POLL_BUDGET {
            let status = self.read32(Register::InterruptStatus.offset())?;
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
            if status & wanted != 0 {
                self.write32(Register::InterruptStatus.offset(), wanted)?;
                return Ok(());
            }
            spin_loop();
        }
        Err(HostError::Timeout)
    }

    fn wait_command_complete(&self, index: u8) -> Result<(), HostError> {
        for _ in 0..POLL_BUDGET {
            let status = self.read32(Register::InterruptStatus.offset())?;
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
            if status & INTERRUPT_COMMAND_COMPLETE != 0 {
                self.write32(
                    Register::InterruptStatus.offset(),
                    INTERRUPT_COMMAND_COMPLETE,
                )?;
                return Ok(());
            }
            spin_loop();
        }
        Err(HostError::Timeout)
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

    fn command(&mut self, command: Command) -> Result<Response, HostError> {
        if !command.has_valid_block_count() {
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
            self.write16(Register::BlockSize.offset(), 512)?;
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

    fn reset_data_line(&mut self) {
        let _ = self.write8(Register::SoftwareReset.offset(), RESET_DATA);
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
            bus_width: Some(BUS_WIDTH),
            clock_frequency: Some(208_000_000),
            max_frequency: Some(208_000_000),
            no_mmc: true,
            clock_resource_valid: true,
        }
    }

    #[ktest]
    fn accepts_only_frozen_megrez_sd_resources() {
        assert_eq!(
            validate(valid_fields()).unwrap(),
            PlatformConfig {
                mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
                clock_mmio_range: CLOCK_MMIO_START + CLOCK_CORE_OFFSET
                    ..CLOCK_MMIO_START + CLOCK_CORE_OFFSET + CLOCK_CORE_SIZE,
                interrupt: INTERRUPT,
                bus_width: BUS_WIDTH,
                clock_frequency: 208_000_000,
                max_frequency: 208_000_000,
            }
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
                interrupt: INTERRUPT,
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
}
