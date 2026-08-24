// SPDX-License-Identifier: MPL-2.0

use core::{hint::spin_loop, ops::Range};

use fdt::node::FdtNode;
use ostd::{arch::boot::DEVICE_TREE, io::IoMem, mm::io::VmIoOnce};

use crate::{
    card::{Card, HostController, Response},
    sdhci::{Command, DataDirection, HostError, Register, ResponseType, decode_interrupt_error},
};

const COMPATIBLE: &str = "eswin,sdhci-sdio";
const MMIO_START: usize = 0x5046_0000;
const MMIO_SIZE: usize = 0x1_0000;
const INTERRUPT: u32 = 81;
const BUS_WIDTH: usize = 4;
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
    interrupt: u32,
    bus_width: usize,
    max_frequency: u32,
}

#[derive(Clone, Copy)]
struct ConfigFields {
    enabled: bool,
    compatible: bool,
    mmio: Option<(usize, usize)>,
    interrupt: Option<u32>,
    bus_width: Option<usize>,
    max_frequency: Option<u32>,
    no_mmc: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ProbeError {
    InvalidDeviceTree,
    MmioUnavailable,
    Host(HostError),
}

fn validate(fields: ConfigFields) -> Result<PlatformConfig, ProbeError> {
    if !fields.enabled
        || !fields.compatible
        || fields.mmio != Some((MMIO_START, MMIO_SIZE))
        || fields.interrupt != Some(INTERRUPT)
        || fields.bus_width != Some(BUS_WIDTH)
        || !fields.no_mmc
    {
        return Err(ProbeError::InvalidDeviceTree);
    }
    let max_frequency = fields
        .max_frequency
        .filter(|frequency| *frequency > 0 && *frequency <= 208_000_000)
        .ok_or(ProbeError::InvalidDeviceTree)?;
    Ok(PlatformConfig {
        mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
        interrupt: INTERRUPT,
        bus_width: BUS_WIDTH,
        max_frequency,
    })
}

fn config_from_node(node: FdtNode<'_, '_>) -> Result<PlatformConfig, ProbeError> {
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
    validate(ConfigFields {
        enabled,
        compatible,
        mmio,
        interrupt,
        bus_width: node
            .property("bus-width")
            .and_then(|value| value.as_usize()),
        max_frequency: node
            .property("max-frequency")
            .and_then(|value| value.as_usize())
            .and_then(|value| value.try_into().ok()),
        no_mmc: node.property("no-mmc").is_some(),
    })
}

pub(super) fn probe() -> Result<Option<(MmioHost, Card)>, ProbeError> {
    let tree = DEVICE_TREE.get().unwrap();
    let mut nodes = tree.all_nodes().filter(|node| {
        node.compatible()
            .is_some_and(|values| values.all().any(|value| value == COMPATIBLE))
    });
    let Some(node) = nodes.next() else {
        return Ok(None);
    };
    if nodes.next().is_some() {
        return Err(ProbeError::InvalidDeviceTree);
    }
    let config = config_from_node(node)?;
    let mmio =
        IoMem::acquire(config.mmio_range.clone()).map_err(|_| ProbeError::MmioUnavailable)?;
    let mut host = MmioHost { mmio };
    host.validate_handoff().map_err(ProbeError::Host)?;
    ostd::info!(
        "[mmc] controller {:#x} irq={} read-only",
        config.mmio_range.start,
        config.interrupt
    );
    let card = Card::discover(&mut host).map_err(ProbeError::Host)?;
    ostd::info!(
        "[mmc] SDHC rca={} sectors={}",
        card.rca(),
        card.nr_sectors()
    );
    Ok(Some((host, card)))
}

/// Safe MMIO-backed SDHCI polling host.
pub(super) struct MmioHost {
    mmio: IoMem,
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
                return Err(decode_interrupt_error(status).unwrap_or(HostError::Unsupported));
            }
            if status & wanted != 0 {
                self.write32(Register::InterruptStatus.offset(), wanted)?;
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

    fn set_clock(&mut self, _hz: u32) -> Result<(), HostError> {
        let clock = self.read16(Register::ClockControl.offset())?;
        if clock & (CLOCK_INTERNAL_STABLE | CLOCK_CARD_ENABLE)
            != CLOCK_INTERNAL_STABLE | CLOCK_CARD_ENABLE
        {
            return Err(HostError::Unsupported);
        }
        Ok(())
    }

    fn command(&mut self, command: Command) -> Result<Response, HostError> {
        let inhibit = PRESENT_COMMAND_INHIBIT
            | if command.data.is_some() {
                PRESENT_DATA_INHIBIT
            } else {
                0
            };
        self.wait_clear(Register::PresentState.offset(), inhibit)?;
        self.write32(Register::InterruptStatus.offset(), u32::MAX)?;
        if command.data == Some(DataDirection::Read) {
            self.write16(Register::BlockSize.offset(), 512)?;
            self.write16(BLOCK_COUNT, 1)?;
            self.write16(Register::TransferMode.offset(), 1 << 4)?;
        } else {
            self.write16(Register::TransferMode.offset(), 0)?;
        }
        self.write32(Register::Argument.offset(), command.argument)?;
        self.write16(Register::Command.offset(), command.command_bits())?;
        self.wait_interrupt(INTERRUPT_COMMAND_COMPLETE)?;
        match command.response {
            ResponseType::None => Ok(Response::None),
            ResponseType::Long => Ok(Response::Long(self.long_response()?)),
            ResponseType::Short | ResponseType::ShortBusy => {
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
            max_frequency: Some(208_000_000),
            no_mmc: true,
        }
    }

    #[ktest]
    fn accepts_only_frozen_megrez_sd_resources() {
        assert_eq!(
            validate(valid_fields()).unwrap(),
            PlatformConfig {
                mmio_range: MMIO_START..MMIO_START + MMIO_SIZE,
                interrupt: INTERRUPT,
                bus_width: BUS_WIDTH,
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
}
