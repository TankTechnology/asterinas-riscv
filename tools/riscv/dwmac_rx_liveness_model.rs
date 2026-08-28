#![allow(dead_code)]

use std::{env, process::ExitCode};

const USAGE: &str = "usage: dwmac-rx-model --protocol current|bounded --ring-size 2|3|4 --json";

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Owner {
    Dma,
    CpuComplete,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum EndPhase {
    None,
    ClearStatus,
    Rearm,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Protocol {
    Current,
    Bounded,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Action {
    DmaComplete,
    DeliverIrq,
    StartRxPoll,
    PollConsume,
    PollFinishEmpty,
    PollFinishBudget,
    ClearStatus,
    Rearm,
    ServiceTx,
    ServiceTimer,
}

impl Action {
    fn name(self) -> &'static str {
        match self {
            Self::DmaComplete => "dma-complete",
            Self::DeliverIrq => "deliver-irq",
            Self::StartRxPoll => "start-rx-poll",
            Self::PollConsume => "poll-consume",
            Self::PollFinishEmpty => "poll-finish-empty",
            Self::PollFinishBudget => "poll-finish-budget",
            Self::ClearStatus => "clear-status",
            Self::Rearm => "rearm",
            Self::ServiceTx => "service-tx",
            Self::ServiceTimer => "service-timer",
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct State {
    owners: Vec<Owner>,
    dma_cursor: usize,
    rx_head: usize,
    tail: usize,
    irq_masked: bool,
    irq_asserted: bool,
    rx_pending: bool,
    tx_pending: bool,
    timer_pending: bool,
    polling: bool,
    budget_left: u8,
    end_phase: EndPhase,
}

#[derive(Debug)]
struct Report {
    protocol: Protocol,
    explored_states: usize,
    prefix: Vec<Action>,
    cycle: Vec<Action>,
}

fn parse_args() -> Result<(Protocol, usize), String> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    if arguments.len() != 5
        || arguments[0] != "--protocol"
        || arguments[2] != "--ring-size"
        || arguments[4] != "--json"
    {
        return Err(USAGE.into());
    }
    let protocol = match arguments[1].as_str() {
        "current" => Protocol::Current,
        "bounded" => Protocol::Bounded,
        _ => return Err(USAGE.into()),
    };
    let ring_size = match arguments[3].as_str() {
        "2" => 2,
        "3" => 3,
        "4" => 4,
        _ => return Err(USAGE.into()),
    };
    Ok((protocol, ring_size))
}

fn main() -> ExitCode {
    let (protocol, ring_size) = match parse_args() {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    let _ = (protocol, ring_size);
    eprintln!("model transition relation is not implemented");
    ExitCode::from(3)
}
