# RISC-V Svade CI Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minimal four-hart Sv48 and Sv39 forced-Svade boot coverage to the existing RISC-V GitHub Actions workflow.

**Architecture:** Let the existing QEMU argument generator accept an optional RISC-V CPU model through an environment variable while preserving its current default exactly. Add two integration-test matrix entries that reuse the existing action and Makefile, selecting forced Svade and the existing Sv39 Cargo feature where required.

**Tech Stack:** GitHub Actions YAML, POSIX-compatible Bash, GNU Make, OSDK, QEMU RISC-V `virt`.

---

## Scope and review gate

Work only in:

```text
/home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c3
```

Branch:

```text
codex/riscv-svade-c3
```

The branch is stacked on PR2 commit `b86f120a3`. Modify only:

- `.github/workflows/test_riscv.yml`;
- `tools/qemu_args.sh`.

The design and plan files are local notes and must not be staged. Do not stage,
commit, push, or create a PR before user review.

### Task 1: Make the existing RISC-V QEMU CPU selectable

**Files:**

- Modify: `tools/qemu_args.sh`

- [ ] **Step 1: Run the failing override check**

```bash
forced_cpu='rv64,svpbmt=true,zkr=true,svadu=false,svade=true'
actual=$(
  RISCV_QEMU_CPU="$forced_cpu" \
    NETDEV=none CONSOLE=serial tools/qemu_args.sh riscv 2>/dev/null
)
case "$actual" in
  *"-cpu $forced_cpu"*) exit 0 ;;
  *) printf '%s\n' "$actual"; exit 1 ;;
esac
```

Expected: exit 1 because the baseline still emits
`-cpu rv64,svpbmt=true,zkr=true`.

- [ ] **Step 2: Implement the minimal environment input**

Document `RISCV_QEMU_CPU` beside the existing variables:

```bash
#  - RISCV_QEMU_CPU: RISC-V QEMU CPU model and extensions;
```

Set the default beside the other defaults:

```bash
RISCV_QEMU_CPU=${RISCV_QEMU_CPU:-"rv64,svpbmt=true,zkr=true"}
```

Use it only in the RISC-V branch:

```bash
-cpu $RISCV_QEMU_CPU \
```

- [ ] **Step 3: Run default and override checks**

```bash
default_args=$(NETDEV=none CONSOLE=serial tools/qemu_args.sh riscv 2>/dev/null)
forced_cpu='rv64,svpbmt=true,zkr=true,svadu=false,svade=true'
forced_args=$(
  RISCV_QEMU_CPU="$forced_cpu" \
    NETDEV=none CONSOLE=serial tools/qemu_args.sh riscv 2>/dev/null
)

case "$default_args" in
  *"-cpu rv64,svpbmt=true,zkr=true"*) ;;
  *) exit 1 ;;
esac
case "$forced_args" in
  *"-cpu $forced_cpu"*) ;;
  *) exit 1 ;;
esac
```

Expected: exit 0.

- [ ] **Step 4: Check shell syntax**

```bash
bash -n tools/qemu_args.sh
```

Expected: exit 0.

### Task 2: Add the two persistent boot lanes

**Files:**

- Modify: `.github/workflows/test_riscv.yml`

- [ ] **Step 1: Run the failing workflow-coverage check**

```bash
ruby -ryaml -e '
  workflow = YAML.load_file(".github/workflows/test_riscv.yml")
  rows = workflow.fetch("jobs").fetch("integration-test")
    .fetch("strategy").fetch("matrix").fetch("include")
  svade = rows.select { |row| row["riscv_qemu_cpu"].to_s.include?("svade=true") }
  abort "expected two Svade rows" unless svade.size == 2
'
```

Expected: nonzero with `expected two Svade rows`.

- [ ] **Step 2: Add Sv48 and Sv39 matrix rows**

Add these debug boot rows beside `boot-debug-smp4`:

```yaml
          - test_id: 'boot-debug-sv48-svade-smp4'
            release: false
            smp: 4
            riscv_qemu_cpu: 'rv64,svpbmt=true,zkr=true,svadu=false,svade=true'

          - test_id: 'boot-debug-sv39-svade-smp4'
            release: false
            smp: 4
            features: 'riscv_sv39_mode'
            riscv_qemu_cpu: 'rv64,svpbmt=true,zkr=true,sv48=false,svadu=false,svade=true'
```

Export the optional matrix values on the existing integration-test action
step:

```yaml
        env:
          FEATURES: ${{ matrix.features }}
          RISCV_QEMU_CPU: ${{ matrix.riscv_qemu_cpu }}
```

Forward the existing matrix `smp` field through the composite action:

```yaml
        with:
          smp: ${{ matrix.smp }}
```

- [ ] **Step 3: Run the complete workflow-coverage check**

```bash
ruby -ryaml -e '
  workflow = YAML.load_file(".github/workflows/test_riscv.yml")
  job = workflow.fetch("jobs").fetch("integration-test")
  rows = job.fetch("strategy").fetch("matrix").fetch("include")
  svade = rows.select { |row| row["riscv_qemu_cpu"].to_s.include?("svade=true") }
  abort "expected two Svade rows" unless svade.size == 2
  abort "Svade lanes must be debug" unless svade.all? { |row| row["release"] == false }
  abort "all Svade rows must use four harts" unless svade.all? { |row| row["smp"] == 4 }
  sv39 = svade.select { |row| row["features"] == "riscv_sv39_mode" }
  abort "expected one Sv39 row" unless sv39.size == 1
  abort "Sv39 must disable Sv48" unless sv39[0]["riscv_qemu_cpu"].include?("sv48=false")
  sv48 = svade.reject { |row| row.key?("features") }
  abort "expected one default Sv48 row" unless sv48.size == 1
  step = job.fetch("steps").find { |item| item["uses"] == "./.github/actions/test" }
  env = step.fetch("env")
  abort "FEATURES not forwarded" unless env["FEATURES"] == "${{ matrix.features }}"
  abort "RISCV_QEMU_CPU not forwarded" unless env["RISCV_QEMU_CPU"] == "${{ matrix.riscv_qemu_cpu }}"
  abort "SMP not forwarded" unless step.fetch("with")["smp"] == "${{ matrix.smp }}"
'
```

Expected: exit 0.

### Task 3: Validate the workflow-equivalent runtime paths

**Files:** No additional files.

- [ ] **Step 1: Run the Sv48 forced-Svade boot lane**

Inside the project container:

```bash
RISCV_QEMU_CPU='rv64,svpbmt=true,zkr=true,svadu=false,svade=true' \
  make run_kernel \
    AUTO_TEST=boot \
    TARGET_ARCH=riscv64 \
    SMP=4
```

Expected: exit 0 and `qemu.log` contains `Successfully booted.`.

- [ ] **Step 2: Run the Sv39 forced-Svade boot lane**

Inside the project container:

```bash
RISCV_QEMU_CPU='rv64,svpbmt=true,zkr=true,sv48=false,svadu=false,svade=true' \
  FEATURES=riscv_sv39_mode \
  make run_kernel \
    AUTO_TEST=boot \
    TARGET_ARCH=riscv64 \
    SMP=4
```

Expected: exit 0 and `qemu.log` contains `Successfully booted.`.

- [ ] **Step 3: Run static validation**

```bash
bash -n tools/qemu_args.sh
make check TARGET_ARCH=riscv64
git diff --check
git status --short
```

Expected: all checks exit 0. Git status lists only the two intended tracked
files plus the two untracked local design/plan files.

- [ ] **Step 4: Review the PR3-only diff**

Run the Asterinas persona review against `b86f120a3`. Verify maintainability,
RISC-V hardware semantics, workflow compatibility, and CI cost. Fix any major
finding and rerun the affected checks.

- [ ] **Step 5: Present the review package**

Provide the user:

- the two-file diff and line count;
- RED/GREEN evidence;
- both workflow-equivalent boot results;
- static-check and persona-review results;
- current status without staging or committing.
