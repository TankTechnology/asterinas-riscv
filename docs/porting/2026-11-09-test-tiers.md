# Test tiers and gate wiring, 2026-11-09

The host test suite used to run every module in one target. The unit tests,
the orchestration tests that spawn QEMU-ish subprocesses, and the contract
tests that read the Makefile all shared one exit code and one runtime. This
page records the split and the rule that keeps it from growing back.

## Tiers

| Tier | Command | What it covers | Size |
| --- | --- | --- | --- |
| unit | `make test_riscv_host` | pure logic, parsers, contracts, argv builders; no host process | 63 modules / 698 tests, ~2 s |
| integration | `make test_riscv_host_integration` | tests that spawn host processes (git, bash, compilers, fake QEMU drivers) | 59 modules / 1,740 tests, ~39 s |
| all | `make test_riscv_host_all` | both tiers; release and full validation | 122 modules / 2,438 tests |

Membership lives in `tests/tiers.json`. A module is in the integration tier
if its tests spawn a host process or if its recorded runtime is at least one
second. The runner filters the module list **and** the baseline before
checking, so each tier is a valid run on its own:

```bash
rt test                      # unit tier
rt test --tier integration
rt test --tier all
rt test --list [--tier ...]
```

## Gate scenarios

Integration gates are no longer spelled as one `rt` command per milestone.
`rt` exposes one command group with a scenario registry:

```bash
rt gate rootfs
rt gate desktop-m5
rt gate desktop-drm
rt gate debug-console
rt gate browser-web
rt gate daily-use
rt gate desktop-m6
rt gate desktop-m7
rt gate graphics-physical
```

The registry lives in `tools/riscv/rt` (`GATES`), and the Makefile targets
call `rt gate <name>`. A gate implementation keeps its own arguments, so the
scenario name is the only interface the build file needs to know.

## Wiring contract

`tests/test_wiring.py` is the only test module allowed to read the Makefile.
It checks:

- every `rt <command>` in the Makefile resolves to a command in `rt`;
- every `rt gate <name>` resolves to a registry entry;
- every command and gate maps to an existing module or script;
- every `tools.riscv.tests.*` reference in the Makefile exists as a module
  (and class references are `unittest.TestCase` subclasses);
- the set of modules that still assert on raw Makefile text does not grow.

## Policy for new tests

1. Put a unit test in the unit tier by default. If it needs a host process,
   add the module to `tests/tiers.json` under `integration`.
2. Do not read the Makefile, another source file, or a build script to make
   an assertion. Test the function, the contract or the argv builder.
3. A new gate is one entry in `GATES`, one module implementing the scenario,
   and one scenario line in the Makefile. It does not get its own test runner
   or its own top-level command.
4. Keep test modules under roughly 1,000 lines. The two historical giants
   (`test_qemu_uboot_booti`, `test_debian_rootfs`) are the remaining split
   candidates.

## Remaining work

- Split the two >7,000-line test modules by contract.
- Convert the 12 modules frozen by `MAKEFILE_READERS` to behaviour tests and
  delete their names from the allowlist.
- Merge per-milestone gate classes that are no longer shared libraries into
  the scenario registry; the shared classifiers (desktop M3/M4, network M5,
  megrez markers) must move to a common module first.
