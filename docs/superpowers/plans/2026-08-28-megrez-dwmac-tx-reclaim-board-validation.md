# Megrez DWMAC TX Reclaim Board Validation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to execute this plan task by task. Do not start
> a second board boot in the same execution session.

**Goal:** Validate on one Milk-V Megrez boot that the uncached DWMAC descriptor
ring prevents lost TX completions, completes the full four-stage TCP probe, and
returns automatically to a fresh U-Boot prompt without a physical reset.

**Architecture:** Keep the experiment simulation-first and artifact-bound. A
fresh Sv39/SMP4 Asterinas image containing commit `131381300` is frozen with the
existing TCP probe and USB-disabled Megrez DTB. Host tests, a generic QEMU TCP
gate, and a QEMU software-reboot gate must all pass before a permit is issued.
The physical phase owns `/dev/ttyUSB0` once, starts the host responder before
mutating the board, transfers only stale RAM artifacts, sends exactly one
`booti`, then observes the guest terminal marker and automatic U-Boot recovery.

**Tech Stack:** safe Rust DWMAC driver, Python 3 `unittest`, cargo-osdk,
QEMU RISC-V generic Sv39/SMP4, U-Boot `booti`, Linux `TCP_INFO`, serial PTY,
existing `tools.riscv.megrez_debug` safety workflow.

---

## Scope and frozen decisions

- This is a **network correctness gate**, not a Debian desktop demonstration.
- Use only the `asterinas-riscv` worktree and its current commit; do not fetch,
  merge, push, or contact the upstream Asterinas repository.
- The board contract is Sv39, SMP=4, 2 GiB QEMU memory, and the current
  USB-disabled Megrez DTB. Do not substitute an Sv48 kernel or the old one-hart
  DTB.
- The guest address remains `10.100.19.200/21`; the responder remains
  `10.100.19.216:18080` on host interface `enp12s0`.
- The ordered bodies remain 16 KiB, 64 KiB, 1 MiB, and 16 MiB, with the
  `mod251` payload pattern and exact total `17,907,712` bytes.
- `asterinas.reboot_after=60` is the recovery mechanism. There is no 1,800
  second wait, no repeated boot loop, and no automatic `reset` command.
- The physical phase may issue exactly one `booti`. It must never issue
  `saveenv`, a second `booti`, or a blind serial reset.
- The runner may send one empty line only after a **post-terminal** U-Boot
  autoboot-countdown marker is observed, solely to stop recovered autoboot.
- On any mismatch, timeout, panic, missing recovery, or uncertain serial state,
  publish evidence, release the serial device, and stop. Do not improvise a
  second physical attempt.

## Acceptance contract

The run is a full PASS only if all of the following are true:

1. The plan, permit, current Git commit, kernel, initramfs, QEMU DTB, and Megrez
   DTB identities match immediately before the serial device is opened.
2. The transport log contains one and only one `booti` command and contains no
   `saveenv` or `reset` command.
3. Serial reaches `Enter riscv_boot` and contains no panic/oops/fatal marker.
4. At least one `ASTERINAS_GMAC_DATAPATH` record has `tx_reclaimed > 0`.
5. The TX ring does not remain at `tx_outstanding=64` with unchanged
   `tx_reclaimed` while later RX progress is observed.
6. The exact terminal marker is observed:

   ```text
   ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080 status=200 sizes=16384,65536,1048576,16777216 completed_bytes=17907712 pattern=mod251
   ```

7. `probe-tcp-info.json` is bound to the current plan and contains four ordered
   connections. Every connection records the exact accepted body size, no
   socket error, bounded samples, and an observation at least 60 ms after the
   final application send.
8. After the terminal marker, serial shows a fresh firmware/OpenSBI/U-Boot boot
   boundary and a recovered U-Boot prompt.
9. The final board result is `passed: true`, reason `board-pass`, and all four
   evidence files have stable SHA-256 identities.

A partial result may validate the descriptor fix without passing the complete
board gate. In particular, `tx_reclaimed > 0` followed by a later guest receive
failure falsifies the old cache-line ownership bug but identifies a new network
boundary. It must be reported as a partial result, not converted into PASS.

---

### Task 1: Seal the source and rerun only the relevant cheap gates

**Files:**
- Inspect: `kernel/comps/dwmac/src/queue.rs`
- Inspect: `tools/riscv/dwmac_tx_cacheline_model.rs`
- Inspect: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`
- Inspect: `tools/riscv/tests/test_megrez_debug.py`
- Inspect: `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`
- No source modifications expected

- [ ] **Step 1: Establish a clean immutable starting point**

```bash
cd /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-dwmac-high-info
git status --short
git rev-parse HEAD
git merge-base --is-ancestor 131381300 HEAD
git diff --check
```

Expected: empty status, a stable HEAD, ancestor check exit 0, and diff check
exit 0. Record the HEAD as `VALIDATION_COMMIT`. Any source edit after this point
invalidates all later QEMU and physical permits.

- [ ] **Step 2: Prove the cache-line failure and fixed invariant offline**

```bash
make test_riscv_dwmac_rx_model
PYTHONPATH="$PWD/tools/riscv:$PWD" \
  python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug \
  tools.riscv.tests.test_megrez_board_session -v
```

Expected: the DWMAC suite includes the packed-descriptor lost-completion
interleaving and the uncached-descriptor preservation case; all focused Megrez
tests pass. Do not rerun unrelated Debian, desktop, DRM, USB, or LTP suites.

- [ ] **Step 3: Run one offline RISC-V compile gate**

Use the already-present pinned image and read-only toolchain caches. Give the
container one explicit name and remove that exact container on every exit:

```bash
timeout 900 docker run --name codex-dwmac-tx-reclaim-check --rm \
  --network=none \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -v /home/ubuntu/.rustup:/root/.rustup:ro \
  -v /home/ubuntu/.cargo/registry:/root/.cargo/registry:ro \
  -v /home/ubuntu/.cargo/git:/root/.cargo/git:ro \
  -v /home/ubuntu/.cargo/bin/cargo-osdk:/root/.cargo/bin/cargo-osdk:ro \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'cargo osdk check --ktests -p aster-dwmac -p aster-network \
    -p aster-kernel --target riscv64imac-unknown-none-elf'
```

Expected: exit 0. Warnings outside the touched DWMAC scope are recorded but do
not trigger an unrelated cleanup pass.

**Stop condition:** Any failure here is handled offline. The board is not
touched.

### Task 2: Build and freeze one new Sv39/SMP4 kernel artifact

**Files:**
- Create ignored artifacts below
  `target/megrez-debug/dwmac-tx-reclaim-validation/artifacts/`
- Reuse the already validated TCP probe initramfs and USB-disabled Megrez DTB
- No tracked source modifications expected

- [ ] **Step 1: Create a new output root without overwriting old evidence**

```bash
VALIDATION_ROOT="$PWD/target/megrez-debug/dwmac-tx-reclaim-validation"
ARTIFACT_ROOT="$VALIDATION_ROOT/artifacts"
test ! -e "$VALIDATION_ROOT" || {
  echo 'validation output already exists; choose a new timestamped root' >&2
  exit 2
}
install -d -m 0755 "$ARTIFACT_ROOT"
```

Never reuse `target/megrez-debug/dwmac-high-info/plan.json` or either old board
run directory.

- [ ] **Step 2: Materialize the ignored OSDK initramfs prerequisite**

The root `OSDK.toml` always resolves
`test/initramfs/build/initramfs.cpio.gz`, even though this board run supplies a
separate probe initramfs to `booti`. Build the standard prerequisite inside the
pinned image, then dereference the container-private Nix out-link into a regular
workspace file before the container exits:

```bash
timeout 600 docker run --name codex-dwmac-tx-reclaim-initramfs --rm \
  --network=none \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'set -euo pipefail
    make initramfs TARGET_ARCH=riscv64 SMP=4
    cp -L test/initramfs/build/initramfs.cpio.gz /tmp/initramfs.cpio.gz
    unlink test/initramfs/build/initramfs.cpio.gz
    install -m 0644 /tmp/initramfs.cpio.gz \
      test/initramfs/build/initramfs.cpio.gz'
test -f test/initramfs/build/initramfs.cpio.gz
test ! -L test/initramfs/build/initramfs.cpio.gz
gzip -t test/initramfs/build/initramfs.cpio.gz
```

Expected: a non-empty regular gzip file. A dangling `/nix/store` symlink is not
accepted as a successful prerequisite.

- [ ] **Step 3: Build exactly Sv39/SMP4 in the pinned container**

```bash
timeout 1200 docker run --name codex-dwmac-tx-reclaim-build --rm \
  --network=none \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -v /home/ubuntu/.rustup:/root/.rustup:ro \
  -v /home/ubuntu/.cargo/registry:/root/.cargo/registry:ro \
  -v /home/ubuntu/.cargo/git:/root/.cargo/git:ro \
  -v /home/ubuntu/.cargo/bin/cargo-osdk:/root/.cargo/bin/cargo-osdk:ro \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'cd kernel && OSDK_TARGET_ARCH=riscv64 \
    cargo osdk build --scheme riscv --features riscv_sv39_mode'
```

Expected: exit 0 and a non-empty
`target/osdk/aster-kernel/aster-kernel-osdk-bin.Image`. Inspect the build log for
`riscv_sv39_mode`; a default/Sv48 build is not acceptable.

- [ ] **Step 4: Freeze all four payloads under new names**

```bash
install -m 0755 target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  "$ARTIFACT_ROOT/asterinas.booti"
install -m 0644 \
  target/megrez-debug/dwmac-high-info/artifacts/megrez-tcp-probe.cpio.gz \
  "$ARTIFACT_ROOT/megrez-tcp-probe.cpio.gz"
install -m 0644 \
  target/megrez-debug/dwmac-high-info/artifacts/megrez-no-usb.dtb \
  "$ARTIFACT_ROOT/megrez-no-usb.dtb"
install -m 0644 target/qemu-uboot/dwmac-high-info-fast/qemu-virt.dtb \
  "$ARTIFACT_ROOT/qemu-virt.dtb"
sha256sum "$ARTIFACT_ROOT"/* | tee "$ARTIFACT_ROOT/SHA256SUMS"
cksum "$ARTIFACT_ROOT"/* | tee "$ARTIFACT_ROOT/CKSUMS"
```

Verify the new kernel SHA-256 differs from the failed physical-run kernel
`001adf78ef91c469ee3926461ea47a905f6df230a03d6fedb1c78a8375074fca`.
The other three files may retain their old identities.

- [ ] **Step 5: Verify DTB and archive structure**

```bash
fdtget -l "$ARTIFACT_ROOT/qemu-virt.dtb" /cpus | \
  while read -r node; do
    fdtget -t s "$ARTIFACT_ROOT/qemu-virt.dtb" "/cpus/$node" status \
      2>/dev/null || true
  done
gzip -cd "$ARTIFACT_ROOT/megrez-tcp-probe.cpio.gz" | cpio --quiet -it
```

Expected: the QEMU DTB has exactly four enabled CPU nodes; the initramfs has
the already-reviewed probe layout and no QEMU static binary.

### Task 3: Issue fresh QEMU TCP and recovery evidence

**Files:**
- Create ignored fast evidence below
  `target/megrez-debug/dwmac-tx-reclaim-validation/qemu-fast/`
- Create ignored recovery evidence below
  `target/megrez-debug/dwmac-tx-reclaim-validation/qemu-recovery/`
- Create ignored `plan.json`, `recovery.json`, and `permit.json`

- [ ] **Step 1: Freeze the exact plan**

```bash
BOOTARGS='console=ttyS0 loglevel=info init=/init asterinas.net=eic7700-rj45,10.100.19.200/21 asterinas.reboot_after=60'
READY='ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080 status=200 sizes=16384,65536,1048576,16777216 completed_bytes=17907712 pattern=mod251'
python3 -m tools.riscv.megrez_debug plan \
  --profile tcp-probe \
  --kernel "$ARTIFACT_ROOT/asterinas.booti" \
  --initramfs "$ARTIFACT_ROOT/megrez-tcp-probe.cpio.gz" \
  --qemu-dtb "$ARTIFACT_ROOT/qemu-virt.dtb" \
  --megrez-dtb "$ARTIFACT_ROOT/megrez-no-usb.dtb" \
  --bootargs "$BOOTARGS" \
  --marker 'Enter riscv_boot' \
  --marker "$READY" \
  --reboot-after 60 \
  --output "$VALIDATION_ROOT/plan.json"
python3 -m tools.riscv.megrez_debug check "$VALIDATION_ROOT/plan.json"
sha256sum "$VALIDATION_ROOT/plan.json" | tee "$VALIDATION_ROOT/PLAN.SHA256"
```

- [ ] **Step 2: Run the fast generic-network simulation**

```bash
python3 -m tools.riscv.megrez_debug simulate \
  "$VALIDATION_ROOT/plan.json" \
  --tier fast \
  --output-directory "$VALIDATION_ROOT/qemu-fast" \
  --uboot-build-directory target/qemu-uboot/cache/u-boot-build
```

Expected: exact READY marker, `passed: true`, profile
`generic-sv39-smp4-tcp-probe`, device set `virtio-net-slirp`, CPU argument
`sv48=false`, SMP4, and no panic/oops/fatal marker. QEMU cannot validate the
Megrez DWMAC hardware path; this gate validates the kernel/probe/boot protocol.

- [ ] **Step 3: Run a separate 60-second software-reboot gate**

```bash
RECOVERY_ROOT="$VALIDATION_ROOT/qemu-recovery"
install -d -m 0755 "$RECOVERY_ROOT"
env \
  ASTERINAS_RISCV_BOOTI="$ARTIFACT_ROOT/asterinas.booti" \
  ASTERINAS_INITRAMFS="$ARTIFACT_ROOT/megrez-tcp-probe.cpio.gz" \
  QEMU_UBOOT_PROFILE=generic-sv39-smp4-software-reboot \
  QEMU_UBOOT_OUT_DIR="$RECOVERY_ROOT" \
  QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
python3 tools/riscv/qemu_uboot_booti.py run \
  --profile generic-sv39-smp4-software-reboot \
  --device-set virtio-net-slirp \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk "$RECOVERY_ROOT/boot.ext4" \
  --manifest "$RECOVERY_ROOT/artifacts.json" \
  --dtb-audit "$RECOVERY_ROOT/qemu-dtb-audit.json" \
  --serial-log "$RECOVERY_ROOT/serial.log" \
  --marker-event "$RECOVERY_ROOT/marker-event.txt" \
  --result "$RECOVERY_ROOT/qemu-result.json"
sha256sum "$ARTIFACT_ROOT/asterinas.booti" > "$RECOVERY_ROOT/SHA256SUMS"
python3 -m tools.riscv.megrez_debug recovery \
  "$VALIDATION_ROOT/plan.json" \
  --native-result "$RECOVERY_ROOT/qemu-result.json" \
  --serial-log "$RECOVERY_ROOT/serial.log" \
  --sha256sums "$RECOVERY_ROOT/SHA256SUMS" \
  --output "$VALIDATION_ROOT/recovery.json"
```

Expected: exact profile, fresh recovery boundary, no `-no-reboot`, and a
recovery record bound to the current kernel SHA-256.

- [ ] **Step 4: Issue and revalidate the physical permit**

```bash
python3 -m tools.riscv.megrez_debug preboard \
  "$VALIDATION_ROOT/plan.json" \
  --desktop-result "$VALIDATION_ROOT/qemu-fast/result.json" \
  --recovery-result "$VALIDATION_ROOT/recovery.json" \
  --output "$VALIDATION_ROOT/permit.json"
python3 -m tools.riscv.megrez_debug check "$VALIDATION_ROOT/plan.json"
git diff --check
test -z "$(git status --short)"
```

Expected: permit is bound to the exact plan, current Git commit, passing fast
simulation, and passing recovery evidence. If any tracked file changes after
this command, delete the permit and return to Task 1.

### Task 4: Perform the no-mutation host and serial preflight

**Files:**
- No source changes
- The board remains at U-Boot throughout this task

- [ ] **Step 1: Check exclusive resources without stealing an active session**

```bash
test -c /dev/ttyUSB0
! fuser /dev/ttyUSB0
! ss -ltnp 'sport = :18080' | grep -q LISTEN
ip -4 -brief address show dev enp12s0
```

Expected: no owner of `/dev/ttyUSB0`, port 18080 free, and host address
`10.100.19.216/21`. If the serial port is owned, identify and terminate only a
stale process created by this workflow; never kill an unknown interactive
session blindly.

- [ ] **Step 2: Run the board command in dry-run mode**

```bash
BOARD_ROOT="$VALIDATION_ROOT/board-run-$(date +%Y%m%d-%H%M%S)"
python3 -m tools.riscv.megrez_debug board \
  "$VALIDATION_ROOT/plan.json" /dev/ttyUSB0 \
  --simulation-result "$VALIDATION_ROOT/qemu-fast/result.json" \
  --recovery-result "$VALIDATION_ROOT/recovery.json" \
  --output-directory "$BOARD_ROOT" \
  --timeout 300 \
  --dry-run
```

Expected: the printed action list contains volatile RAM loads, CRC checks,
temporary `setenv`, and one `booti`; it contains no `saveenv`, `reset`, or
second `booti`. Dry-run must not open or mutate the board.

- [ ] **Step 3: Record the immediate go/no-go decision**

GO requires all of the following at the same time:

- board is visibly or serially at a U-Boot prompt;
- permit and plan checks are still green;
- exact serial/network resources are free;
- no source file changed after QEMU evidence;
- host responder can bind `10.100.19.216:18080`;
- the operator has not requested a pause.

Any failed item is NO-GO. Do not consume the single physical attempt.

### Task 5: Execute exactly one physical boot

**Files:**
- Create ignored evidence only in the `BOARD_ROOT` chosen by Task 4
- No source or Git changes during the run

- [ ] **Step 1: Start the integrated runner once**

Run the exact Task 4 command without `--dry-run`:

```bash
python3 -m tools.riscv.megrez_debug board \
  "$VALIDATION_ROOT/plan.json" /dev/ttyUSB0 \
  --simulation-result "$VALIDATION_ROOT/qemu-fast/result.json" \
  --recovery-result "$VALIDATION_ROOT/recovery.json" \
  --output-directory "$BOARD_ROOT" \
  --timeout 300
```

The 300-second value is a **hard upper bound**, not an intended wait. It covers
compressed XMODEM transfer, the four TCP stages, the fixed 60-second software
reboot, and recovered U-Boot countdown handling in one lifecycle. Poll live
serial and host evidence at intervals below 30 seconds; do not sit silently.

- [ ] **Step 2: Enforce mutation and recovery invariants during the run**

The runner must:

1. bind the responder before `booti`;
2. compare CRC32 for kernel/initramfs/DTB and transfer each mismatch at most
   once;
3. verify full U-Boot echoes and reject U-Boot/libfdt errors;
4. issue one `booti` only after all three load addresses pass CRC checks;
5. send no more commands while Asterinas is running;
6. treat PASS or FAIL as terminal and continue only passive serial observation;
7. stop a recovered U-Boot autoboot countdown with one empty line;
8. close the listener, serial handle, and evidence files on every exit.

If the software reboot is not observed, let the 300-second bound expire, close
everything, and report `recovery-not-observed`. Do not ask for a physical reset
inside this plan and do not launch another run.

- [ ] **Step 3: Verify the runner left no live resources**

```bash
! fuser /dev/ttyUSB0
! ss -ltnp 'sport = :18080' | grep -q LISTEN
pgrep -af 'megrez_debug|megrez_debug_probe' || true
```

Only the inspection command itself may appear. Any real residue is cleaned up
before evidence interpretation.

### Task 6: Classify the single run and close the milestone

**Files:**
- Inspect: `$BOARD_ROOT/serial.log`
- Inspect: `$BOARD_ROOT/transport.json`
- Inspect: `$BOARD_ROOT/probe-tcp-info.json`
- Inspect: `$BOARD_ROOT/result.json`
- Modify after classification:
  `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`

- [ ] **Step 1: Seal physical evidence before interpreting it**

```bash
test -s "$BOARD_ROOT/serial.log"
test -s "$BOARD_ROOT/transport.json"
test -s "$BOARD_ROOT/probe-tcp-info.json"
test -s "$BOARD_ROOT/result.json"
sha256sum "$BOARD_ROOT"/{serial.log,transport.json,probe-tcp-info.json,result.json} \
  | tee "$BOARD_ROOT/SHA256SUMS"
```

Do not edit these files after hashing. Any derived report goes into a separate
file.

- [ ] **Step 2: Extract the decisive counters once**

```bash
rg 'ASTERINAS_GMAC_DATAPATH|ASTERINAS_GMAC_TCP_PROBE_(READY|FAIL)|panic|oops|fatal|Firmware version|OpenSBI|U-Boot 2020.01' \
  "$BOARD_ROOT/serial.log" | tee "$BOARD_ROOT/classification.txt"
python3 -m json.tool "$BOARD_ROOT/result.json" >> "$BOARD_ROOT/classification.txt"
python3 -m json.tool "$BOARD_ROOT/probe-tcp-info.json" \
  >> "$BOARD_ROOT/classification.txt"
```

Check transport JSON structurally, not only with `rg`: exactly one `booti`, no
`saveenv`, no `reset`, and no command after `booti` other than the explicitly
classified recovered-autoboot stop.

- [ ] **Step 3: Select exactly one outcome**

| Evidence | Classification | Next action |
|---|---|---|
| `tx_reclaimed>0`, full READY, fresh U-Boot | `tx-reclaim-fixed/full-board-pass` | Accept the DWMAC fix; begin Debian/browser network integration in a new plan. |
| `tx_reclaimed>0`, no full READY | `tx-reclaim-fixed/later-network-stall` | Keep the DWMAC fix; use host ACK/guest byte boundary for offline analysis. No rerun. |
| submitted reaches 64, reclaimed stays 0, outstanding stays 64, TBU recurs | `tx-reclaim-still-stalled` | The coherence hypothesis is falsified or incomplete; inspect descriptor visibility/ownership offline. No rerun. |
| no meaningful TX submission | `pre-tx-control-path` | Inspect ARP/routing/link/control path offline. |
| full READY but no fresh U-Boot | `network-pass/recovery-fail` | Accept network evidence only; fix recovery workflow offline. |
| panic/oops/fatal before terminal | `kernel-fatal` | Preserve first fatal frame and analyze offline. |
| transfer/CRC/U-Boot setup failure before `booti` | `preboot-failure/no-boot-consumed` | Repair tooling/artifact issue; this is the only case where a later separately approved run may remain available. |

The first matching row is recorded with exact supporting lines and TCP sample
indices. Do not blend multiple speculative root causes.

- [ ] **Step 4: Update the evidence document and commit only the conclusion**

Add the new plan SHA, kernel SHA, four evidence hashes, one-boot count, recovery
status, decisive TX/RX counters, TCP_INFO boundary, and selected classification
to the existing evidence document. Run:

```bash
git diff --check
git status --short
git add docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md
git commit -m "docs(riscv): record Megrez TX reclaim validation"
```

Do not commit ignored binary artifacts. Do not push until the local milestone
summary has been checked against the sealed evidence hashes.

---

## Time budget and communication cadence

- Task 1, focused host/static gates: **10–20 minutes**.
- Task 2, cached RISC-V build and artifact freeze: **10–20 minutes**.
- Task 3, QEMU fast plus 60-second recovery gate: **5–10 minutes**.
- Task 4, preflight: **2–5 minutes**.
- Task 5, single physical lifecycle: normally **2–5 minutes**, hard bound
  **300 seconds**.
- Task 6, evidence classification and documentation: **10–20 minutes**.

Expected total when caches and U-Boot prompt are healthy: **40–75 minutes**.
No step should be silent for more than 60 seconds: long build/QEMU/serial phases
must report current phase, elapsed time, and whether output is still advancing.

## Plan self-review

- All problems reproducible in host models, compile checks, or QEMU are handled
  before opening the board serial device.
- The physical run answers one question with one boot: whether uncached DWMAC
  descriptors restore TX reclaim and permit full TCP completion.
- Artifact and permit identities prevent a rebuild or branch change from being
  mistaken for tested code.
- A 60-second software reboot is already observed on this board and removes the
  normal need for physical reset; the plan does not claim recovery from a dead
  timer/CPU/firmware path.
- The runner does not use Linux as a substitute guest: the booted kernel is
  Asterinas, and the host Linux machine is only a bounded TCP responder and
  evidence collector.
- The plan deliberately postpones desktop, browser, DRM, USB, hot-plug, and
  remote-branch work until this network milestone is closed.
