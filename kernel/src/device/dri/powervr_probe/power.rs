// SPDX-License-Identifier: MPL-2.0

//! Opt-in, reversible PowerVR ID read after an exact Megrez CRG gate.

use core::{hint::spin_loop, time::Duration};

use ostd::{io::IoMem, mm::VmIoOnce};

use super::{
    CRG_BASE, CRG_GATE_BIT, CrgSnapshot, GPU_ACLK_OFFSET, GPU_CFG_OFFSET, GPU_GRAY_OFFSET,
    GPU_REG_START, GPU_RESET_OFFSET, inspect_gpu_crg_dt, print_gpu_crg_snapshot,
};

const EXPECTED_INITIAL: CrgSnapshot = CrgSnapshot {
    aclk: 0x20,
    cfg: 0,
    gray: 0,
    reset: 0,
};
const EXPECTED_GPU_ID: u64 = 0x001e_0003_0198_0065;
const GPU_ID_OFFSET: usize = 0x20;

trait PowerIo {
    fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str>;
    fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str>;
    fn write_reset(&mut self, value: u32) -> Result<(), &'static str>;
    fn delay_reset_pulse(&mut self) -> Result<(), &'static str>;
    fn read_gpu_id(&mut self) -> Result<u64, &'static str>;
}

fn write_clock_checked(
    io: &mut impl PowerIo,
    index: usize,
    value: u32,
) -> Result<(), &'static str> {
    io.write_clock(index, value)?;
    let observed = io.snapshot()?;
    let actual = match index {
        0 => observed.aclk,
        1 => observed.cfg,
        2 => observed.gray,
        _ => return Err("invalid_clock_index"),
    };
    if actual != value {
        return Err("clock_readback_mismatch");
    }
    Ok(())
}

fn write_reset_checked(io: &mut impl PowerIo, value: u32) -> Result<(), &'static str> {
    io.write_reset(value)?;
    if io.snapshot()?.reset != value {
        return Err("reset_readback_mismatch");
    }
    Ok(())
}

fn restore_crg(io: &mut impl PowerIo, initial: CrgSnapshot) -> Result<(), &'static str> {
    // Assert reset before gating clocks, matching the RockOS device deinit.
    // Attempt all writes even if one fails so a partial restore is visible.
    let mut restored = write_reset_checked(io, initial.reset).is_ok();
    for (index, value) in [(2, initial.gray), (1, initial.cfg), (0, initial.aclk)] {
        restored &= write_clock_checked(io, index, value).is_ok();
    }
    if !restored || io.snapshot()? != initial {
        return Err("crg_restore_failed");
    }
    Ok(())
}

fn run_powered_id(io: &mut impl PowerIo) -> Result<u64, &'static str> {
    let initial = io.snapshot()?;
    if initial != EXPECTED_INITIAL {
        return Err("unexpected_initial_crg");
    }

    let attempt = (|| {
        for (index, value) in [
            initial.aclk | CRG_GATE_BIT,
            initial.cfg | CRG_GATE_BIT,
            initial.gray | CRG_GATE_BIT,
        ]
        .into_iter()
        .enumerate()
        {
            write_clock_checked(io, index, value)?;
        }
        let mut reset = initial.reset;
        for mask in [1, 2, 4, 8, 16] {
            io.delay_reset_pulse()?;
            reset |= mask;
            write_reset_checked(io, reset)?;
        }
        if io.snapshot()?
            != (CrgSnapshot {
                aclk: initial.aclk | CRG_GATE_BIT,
                cfg: initial.cfg | CRG_GATE_BIT,
                gray: initial.gray | CRG_GATE_BIT,
                reset: 0x1f,
            })
        {
            return Err("powered_crg_readback_mismatch");
        }
        io.read_gpu_id()
    })();

    restore_crg(io, initial)?;
    let id = attempt?;
    if id != EXPECTED_GPU_ID {
        return Err("unexpected_gpu_id");
    }
    Ok(id)
}

struct HardwarePowerIo {
    clocks: IoMem,
    reset: IoMem,
}

impl HardwarePowerIo {
    fn new() -> Result<Self, &'static str> {
        Ok(Self {
            clocks: IoMem::acquire(CRG_BASE + GPU_ACLK_OFFSET..CRG_BASE + GPU_GRAY_OFFSET + 4)
                .map_err(|_| "clock_registers_unavailable")?,
            reset: IoMem::acquire(CRG_BASE + GPU_RESET_OFFSET..CRG_BASE + GPU_RESET_OFFSET + 4)
                .map_err(|_| "reset_register_unavailable")?,
        })
    }
}

impl PowerIo for HardwarePowerIo {
    fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str> {
        Ok(CrgSnapshot {
            aclk: self.clocks.read_once(0).map_err(|_| "clock_read_failed")?,
            cfg: self
                .clocks
                .read_once(GPU_CFG_OFFSET - GPU_ACLK_OFFSET)
                .map_err(|_| "clock_read_failed")?,
            gray: self
                .clocks
                .read_once(GPU_GRAY_OFFSET - GPU_ACLK_OFFSET)
                .map_err(|_| "clock_read_failed")?,
            reset: self.reset.read_once(0).map_err(|_| "reset_read_failed")?,
        })
    }

    fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str> {
        let offset = match index {
            0..=2 => index * 4,
            _ => return Err("invalid_clock_index"),
        };
        self.clocks
            .write_once(offset, &value)
            .map_err(|_| "clock_write_failed")
    }

    fn write_reset(&mut self, value: u32) -> Result<(), &'static str> {
        self.reset
            .write_once(0, &value)
            .map_err(|_| "reset_write_failed")
    }

    fn delay_reset_pulse(&mut self) -> Result<(), &'static str> {
        let deadline = aster_time::read_monotonic_time()
            .checked_add(Duration::from_micros(15))
            .ok_or("reset_delay_overflow")?;
        while aster_time::read_monotonic_time() < deadline {
            spin_loop();
        }
        Ok(())
    }

    fn read_gpu_id(&mut self) -> Result<u64, &'static str> {
        let gpu = IoMem::acquire(
            GPU_REG_START + GPU_ID_OFFSET..GPU_REG_START + GPU_ID_OFFSET + size_of::<u64>(),
        )
        .map_err(|_| "gpu_id_register_unavailable")?;
        let id = gpu.read_once::<u64>(0).map_err(|_| "gpu_id_read_failed")?;
        aster_logger::println!("ASTERINAS_GPU_POWERED_ID raw={:#018x}", id);
        Ok(id)
    }
}

pub(super) fn probe_on_request(emit_crg_snapshot: bool) {
    if let Err(reason) = inspect_gpu_crg_dt() {
        if emit_crg_snapshot {
            aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
        }
        aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
        return;
    }
    let mut io = match HardwarePowerIo::new() {
        Ok(io) => io,
        Err(reason) => {
            if emit_crg_snapshot {
                aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
            }
            aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
            return;
        }
    };
    if emit_crg_snapshot {
        match io.snapshot() {
            Ok(snapshot) => print_gpu_crg_snapshot(snapshot),
            Err(reason) => {
                aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
                aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
                return;
            }
        }
    }
    aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=starting");
    match run_powered_id(&mut io) {
        Ok(_) => aster_logger::println!(
            "ASTERINAS_GPU_POWERED_ID status=validated bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable"
        ),
        Err(reason) => {
            aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason)
        }
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::{vec, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;

    #[derive(Debug, Eq, PartialEq)]
    enum Action {
        Clock(usize, u32),
        Reset(u32),
        Delay,
        ReadId,
    }

    struct FakePowerIo {
        state: CrgSnapshot,
        id: u64,
        actions: Vec<Action>,
        reject_reset: Option<u32>,
    }

    impl FakePowerIo {
        fn new(id: u64) -> Self {
            Self {
                state: CrgSnapshot {
                    aclk: 0x20,
                    cfg: 0,
                    gray: 0,
                    reset: 0,
                },
                id,
                actions: Vec::new(),
                reject_reset: None,
            }
        }
    }

    impl PowerIo for FakePowerIo {
        fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str> {
            Ok(self.state)
        }

        fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str> {
            match index {
                0 => self.state.aclk = value,
                1 => self.state.cfg = value,
                2 => self.state.gray = value,
                _ => return Err("invalid_clock_index"),
            }
            self.actions.push(Action::Clock(index, value));
            Ok(())
        }

        fn write_reset(&mut self, value: u32) -> Result<(), &'static str> {
            if self.reject_reset == Some(value) {
                return Err("reset_write_failed");
            }
            self.state.reset = value;
            self.actions.push(Action::Reset(value));
            Ok(())
        }

        fn delay_reset_pulse(&mut self) -> Result<(), &'static str> {
            self.actions.push(Action::Delay);
            Ok(())
        }

        fn read_gpu_id(&mut self) -> Result<u64, &'static str> {
            self.actions.push(Action::ReadId);
            Ok(self.id)
        }
    }

    #[ktest]
    fn powered_id_sequence_restores_original_crg() {
        let mut io = FakePowerIo::new(0x001e_0003_0198_0065);
        assert_eq!(run_powered_id(&mut io), Ok(0x001e_0003_0198_0065));
        assert_eq!(io.state.aclk, 0x20);
        assert_eq!(io.state.cfg, 0);
        assert_eq!(io.state.gray, 0);
        assert_eq!(io.state.reset, 0);
        assert_eq!(
            io.actions,
            vec![
                Action::Clock(0, 0x8000_0020),
                Action::Clock(1, 0x8000_0000),
                Action::Clock(2, 0x8000_0000),
                Action::Delay,
                Action::Reset(1),
                Action::Delay,
                Action::Reset(3),
                Action::Delay,
                Action::Reset(7),
                Action::Delay,
                Action::Reset(15),
                Action::Delay,
                Action::Reset(31),
                Action::ReadId,
                Action::Reset(0),
                Action::Clock(2, 0),
                Action::Clock(1, 0),
                Action::Clock(0, 0x20),
            ]
        );
    }

    #[ktest]
    fn powered_id_mismatch_and_initial_drift_cannot_leave_gpu_enabled() {
        let mut io = FakePowerIo::new(0);
        assert_eq!(run_powered_id(&mut io), Err("unexpected_gpu_id"));
        assert_eq!(io.state.aclk, 0x20);
        assert_eq!(io.state.cfg, 0);
        assert_eq!(io.state.gray, 0);
        assert_eq!(io.state.reset, 0);
        assert_eq!(io.actions.last(), Some(&Action::Clock(0, 0x20)));

        let mut io = FakePowerIo::new(0x001e_0003_0198_0065);
        io.state.reset = 1;
        assert_eq!(run_powered_id(&mut io), Err("unexpected_initial_crg"));
        assert!(io.actions.is_empty());
    }

    #[ktest]
    fn powered_id_reset_write_failure_restores_all_registers() {
        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        io.reject_reset = Some(7);
        assert_eq!(run_powered_id(&mut io), Err("reset_write_failed"));
        assert_eq!(io.state, EXPECTED_INITIAL);
        assert!(!io.actions.contains(&Action::ReadId));
        assert_eq!(io.actions.last(), Some(&Action::Clock(0, 0x20)));
    }
}
