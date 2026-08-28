use std::{
    collections::{HashMap, HashSet, VecDeque},
    env,
    process::ExitCode,
};

const USAGE: &str = "usage: dwmac-rx-model --protocol current|bounded --ring-size 2|3|4 --json";
const MAX_STATES: usize = 100_000;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum Owner {
    Dma,
    CpuComplete,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum EndPhase {
    None,
    ClearStatus,
    Rearm,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum Protocol {
    Current,
    Bounded,
}

impl Protocol {
    const fn name(self) -> &'static str {
        match self {
            Self::Current => "current",
            Self::Bounded => "bounded",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
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
    const ALL: [Self; 10] = [
        Self::DmaComplete,
        Self::DeliverIrq,
        Self::StartRxPoll,
        Self::PollConsume,
        Self::PollFinishEmpty,
        Self::PollFinishBudget,
        Self::ClearStatus,
        Self::Rearm,
        Self::ServiceTx,
        Self::ServiceTimer,
    ];

    const fn name(self) -> &'static str {
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

    fn rank(self) -> usize {
        Self::ALL
            .iter()
            .position(|candidate| *candidate == self)
            .unwrap()
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
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

fn initial_state(ring_size: usize, _protocol: Protocol) -> State {
    State {
        owners: vec![Owner::Dma; ring_size],
        dma_cursor: 0,
        rx_head: 0,
        tail: 0,
        irq_masked: false,
        irq_asserted: false,
        rx_pending: false,
        tx_pending: false,
        timer_pending: true,
        polling: false,
        budget_left: 0,
        end_phase: EndPhase::None,
    }
}

fn successors(state: &State, _protocol: Protocol) -> Vec<(Action, State)> {
    let mut result = Vec::new();

    if state.owners[state.dma_cursor] == Owner::Dma {
        let mut next = state.clone();
        next.owners[next.dma_cursor] = Owner::CpuComplete;
        next.dma_cursor = (next.dma_cursor + 1) % next.owners.len();
        next.irq_asserted = true;
        result.push((Action::DmaComplete, next));
    }
    if state.irq_asserted
        && !state.irq_masked
        && !state.polling
        && state.end_phase == EndPhase::None
    {
        let mut next = state.clone();
        next.irq_masked = true;
        next.rx_pending = true;
        next.tx_pending = true;
        result.push((Action::DeliverIrq, next));
    }
    if state.rx_pending && !state.polling && state.end_phase == EndPhase::None {
        let mut next = state.clone();
        next.rx_pending = false;
        next.polling = true;
        result.push((Action::StartRxPoll, next));
    }
    if state.polling && state.owners[state.rx_head] == Owner::CpuComplete {
        let mut next = state.clone();
        next.owners[next.rx_head] = Owner::Dma;
        next.rx_head = (next.rx_head + 1) % next.owners.len();
        next.tail = next.rx_head;
        result.push((Action::PollConsume, next));
    }
    if state.polling && state.owners[state.rx_head] == Owner::Dma {
        let mut next = state.clone();
        next.polling = false;
        next.end_phase = EndPhase::ClearStatus;
        result.push((Action::PollFinishEmpty, next));
    }
    if state.end_phase == EndPhase::ClearStatus {
        let mut next = state.clone();
        next.irq_asserted = false;
        next.end_phase = EndPhase::Rearm;
        result.push((Action::ClearStatus, next));
    }
    if state.end_phase == EndPhase::Rearm {
        let mut next = state.clone();
        next.irq_masked = false;
        next.end_phase = EndPhase::None;
        result.push((Action::Rearm, next));
    }
    if !state.polling && state.end_phase == EndPhase::None && state.tx_pending {
        let mut next = state.clone();
        next.tx_pending = false;
        result.push((Action::ServiceTx, next));
    }
    if !state.polling && state.end_phase == EndPhase::None && state.timer_pending {
        let mut next = state.clone();
        next.timer_pending = false;
        result.push((Action::ServiceTimer, next));
    }

    result.sort_by_key(|(action, _)| action.rank());
    result
}

fn validate_state(state: &State) -> Result<(), &'static str> {
    if !(2..=4).contains(&state.owners.len()) {
        return Err("ring-size-out-of-range");
    }
    if state.dma_cursor >= state.owners.len()
        || state.rx_head >= state.owners.len()
        || state.tail >= state.owners.len()
    {
        return Err("ring-index-out-of-range");
    }
    if state.polling && state.end_phase != EndPhase::None {
        return Err("poll-and-end-phase-overlap");
    }
    if state.end_phase != EndPhase::None && !state.irq_masked {
        return Err("poll-end-with-unmasked-irq");
    }
    Ok(())
}

fn reachable_graph(
    initial: State,
    protocol: Protocol,
) -> Result<HashMap<State, Vec<(Action, State)>>, &'static str> {
    let mut graph = HashMap::new();
    let mut queue = VecDeque::from([initial]);
    while let Some(state) = queue.pop_front() {
        if graph.contains_key(&state) {
            continue;
        }
        validate_state(&state)?;
        let edges = successors(&state, protocol);
        for (_, successor) in &edges {
            validate_state(successor)?;
            if !graph.contains_key(successor) {
                queue.push_back(successor.clone());
            }
        }
        graph.insert(state, edges);
        if graph.len() > MAX_STATES {
            return Err("model-state-cap-exceeded");
        }
    }
    Ok(graph)
}

fn shortest_prefixes(
    graph: &HashMap<State, Vec<(Action, State)>>,
    initial: &State,
) -> HashMap<State, Vec<Action>> {
    let mut paths = HashMap::from([(initial.clone(), Vec::new())]);
    let mut queue = VecDeque::from([initial.clone()]);
    while let Some(state) = queue.pop_front() {
        let prefix = paths.get(&state).unwrap().clone();
        for (action, successor) in graph.get(&state).unwrap() {
            if paths.contains_key(successor) {
                continue;
            }
            let mut successor_prefix = prefix.clone();
            successor_prefix.push(*action);
            paths.insert(successor.clone(), successor_prefix);
            queue.push_back(successor.clone());
        }
    }
    paths
}

fn shortest_progress_cycle(
    graph: &HashMap<State, Vec<(Action, State)>>,
    start: &State,
) -> Option<Vec<Action>> {
    type SearchNode = (State, Vec<Action>, bool, bool);
    let mut queue: VecDeque<SearchNode> =
        VecDeque::from([(start.clone(), Vec::new(), false, false)]);
    let mut seen = HashSet::from([(start.clone(), false, false)]);
    while let Some((state, path, saw_dma, saw_consume)) = queue.pop_front() {
        for (action, successor) in graph.get(&state).unwrap() {
            if matches!(action, Action::ServiceTimer | Action::ServiceTx) {
                continue;
            }
            let next_saw_dma = saw_dma || *action == Action::DmaComplete;
            let next_saw_consume = saw_consume || *action == Action::PollConsume;
            let mut next_path = path.clone();
            next_path.push(*action);
            if successor == start && next_saw_dma && next_saw_consume {
                return Some(next_path);
            }
            let key = (successor.clone(), next_saw_dma, next_saw_consume);
            if seen.insert(key) {
                queue.push_back((successor.clone(), next_path, next_saw_dma, next_saw_consume));
            }
        }
    }
    None
}

fn action_path_key(path: &[Action]) -> Vec<usize> {
    path.iter().map(|action| action.rank()).collect()
}

fn shortest_starvation_lasso(
    graph: &HashMap<State, Vec<(Action, State)>>,
    initial: &State,
) -> Option<(Vec<Action>, Vec<Action>)> {
    let prefixes = shortest_prefixes(graph, initial);
    let mut candidates = Vec::new();
    let mut states: Vec<&State> = graph.keys().collect();
    states.sort();
    for state in states {
        if !(state.polling && state.timer_pending && state.tx_pending) {
            continue;
        }
        if let Some(cycle) = shortest_progress_cycle(graph, state) {
            let prefix = prefixes.get(state).unwrap().clone();
            candidates.push((prefix, cycle));
        }
    }
    candidates.into_iter().min_by_key(|(prefix, cycle)| {
        (
            prefix.len() + cycle.len(),
            action_path_key(prefix),
            action_path_key(cycle),
        )
    })
}

fn action_array(actions: &[Action]) -> String {
    let names: Vec<String> = actions
        .iter()
        .map(|action| format!("\"{}\"", action.name()))
        .collect();
    format!("[{}]", names.join(","))
}

fn print_counterexample(report: &Report) {
    println!(
        "{{\"protocol\":\"{}\",\"verdict\":\"counterexample\",\"property\":\"bounded-rx-poll\",\"explored_states\":{},\"prefix\":{},\"cycle\":{}}}",
        report.protocol.name(),
        report.explored_states,
        action_array(&report.prefix),
        action_array(&report.cycle),
    );
}

fn run(protocol: Protocol, ring_size: usize) -> Result<Report, &'static str> {
    let initial = initial_state(ring_size, protocol);
    let graph = reachable_graph(initial.clone(), protocol)?;
    let (prefix, cycle) =
        shortest_starvation_lasso(&graph, &initial).ok_or("counterexample-not-found")?;
    Ok(Report {
        protocol,
        explored_states: graph.len(),
        prefix,
        cycle,
    })
}

fn main() -> ExitCode {
    let (protocol, ring_size) = match parse_args() {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    match run(protocol, ring_size) {
        Ok(report) => {
            print_counterexample(&report);
            ExitCode::from(1)
        }
        Err(error) => {
            eprintln!("model error: {error}");
            ExitCode::from(3)
        }
    }
}
