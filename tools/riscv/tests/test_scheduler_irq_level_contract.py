import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


class SchedulerIrqLevelContractTests(unittest.TestCase):
    def test_task_switch_does_not_reset_irq_level(self) -> None:
        source = (REPO / "ostd/src/task/processor.rs").read_text(encoding="utf-8")
        self.assertNotIn(
            "take_level_for_switch()",
            source,
            "IRQ level belongs to the interrupt stack and must not be reset by a task switch",
        )


if __name__ == "__main__":
    unittest.main()
