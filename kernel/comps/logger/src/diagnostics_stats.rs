// SPDX-License-Identifier: MPL-2.0

//! Caller-owned, allocation-free duration aggregation. No shared state or locks.

#[derive(Clone, Copy, Debug)]
pub(super) struct DurationStats {
    pub count: u64,
    pub total_ticks: u64,
    pub max_ticks: u64,
    pub invalid: u64,
}

impl DurationStats {
    pub const fn new() -> Self {
        Self {
            count: 0,
            total_ticks: 0,
            max_ticks: 0,
            invalid: 0,
        }
    }

    pub fn observe(&mut self, start: Option<u64>, end: Option<u64>) {
        let sample = start
            .zip(end)
            .and_then(|(start, end)| end.checked_sub(start));
        let update = sample.and_then(|ticks| {
            Some((
                self.count.checked_add(1)?,
                self.total_ticks.checked_add(ticks)?,
                ticks,
            ))
        });
        if let Some((count, total, ticks)) = update {
            self.count = count;
            self.total_ticks = total;
            self.max_ticks = self.max_ticks.max(ticks);
        } else {
            self.invalid = self.invalid.saturating_add(1);
        }
    }
}
