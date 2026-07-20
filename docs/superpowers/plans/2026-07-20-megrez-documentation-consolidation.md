# Megrez Documentation Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Megrez bring-up documentation to one live status page, one command reference, and one append-only evidence ledger while freezing the long guide and HTML as historical snapshots.

**Architecture:** `docs/porting/README.md` owns live state, `tools/riscv/README.md` owns executable commands, and `docs/porting/evidence/` owns immutable run evidence. The long guide and HTML stay at their current paths with archive notices, and HTML tests stop duplicating live status text.

**Tech Stack:** Markdown, static HTML/Mermaid, Python `unittest`, Playwright browser regression, Git.

---

### Task 1: Encode the three-entry documentation contract

**Files:**
- Modify: `tools/riscv/tests/test_megrez_boot_flow_html.py`
- Modify: `tools/riscv/tests/test_megrez_preflight_repository.py`
- Test: `tools/riscv/tests/test_megrez_boot_flow_html.py`

- [ ] **Step 1: Replace live-status duplication with repository-role assertions**

Add paths for the guide, evidence index, and latest evidence page:

```python
GUIDE_PATH = REPO_ROOT / "docs/porting/megrez-asterinas-boot-guide.md"
EVIDENCE_INDEX_PATH = REPO_ROOT / "docs/porting/evidence/megrez-history-index.md"
LATEST_EVIDENCE_PATH = (
    REPO_ROOT / "docs/porting/evidence/2026-07-20-megrez-pid1-recovery.md"
)
```

Load them in `setUpClass`, then add these contract tests:

```python
cls.guide = GUIDE_PATH.read_text() if GUIDE_PATH.is_file() else ""
cls.evidence_index = (
    EVIDENCE_INDEX_PATH.read_text() if EVIDENCE_INDEX_PATH.is_file() else ""
)
cls.latest_evidence = (
    LATEST_EVIDENCE_PATH.read_text() if LATEST_EVIDENCE_PATH.is_file() else ""
)

def test_readme_is_the_only_live_status_entry(self) -> None:
    for marker in (
        "唯一实时状态入口",
        "最后真机边界",
        "第一缺失边界",
        "当前单变量假设",
        "下一次 QEMU 门禁",
        "下一次真机门禁",
    ):
        self.assertIn(marker, self.readme)
    self.assertIn("../../tools/riscv/README.md", self.readme)
    self.assertIn("evidence/megrez-history-index.md", self.readme)

def test_archived_explainers_redirect_to_live_status(self) -> None:
    self.assertIn("历史快照", self.guide)
    self.assertIn("README.md", self.guide)
    self.assertIn("历史快照", self.html)
    self.assertIn("docs/porting/README.md", self.html)

def test_evidence_index_is_append_only_not_live_status(self) -> None:
    self.assertIn("3ef99e6bd", self.evidence_index)
    self.assertIn("../README.md", self.evidence_index)
    self.assertNotIn("Current boundary and next board gates", self.evidence_index)
    self.assertIn("as of this run", self.latest_evidence.lower())
```

Remove exact-text tests that require the archived HTML to repeat the current
stage, current hero chips, current next gate, or the count of current evidence
cards. Keep structural, accessibility, Mermaid, safety-boundary, and browser
rendering tests.

Parse the archived evidence cards and lock the frozen commit-to-environment
mapping: `6df0f28f` and `3ef99e6bd` are board evidence; `593d5bb19`,
`70734c14e`, and `7f691c479` are QEMU evidence.

Update the repository integration test so the exact bootargs and QEMU profiles
are owned by `tools/riscv/README.md`; the live status page should link to that
command reference instead of repeating frozen QEMU identities and commands.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_readme_is_the_only_live_status_entry \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_archived_explainers_redirect_to_live_status \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_evidence_index_is_append_only_not_live_status
```

Expected: failures for missing live-status markers, archive notices, and the
remaining evidence-index current-state section.

### Task 2: Make the porting README the one-screen debug card

**Files:**
- Modify: `docs/porting/README.md`
- Test: `tools/riscv/tests/test_megrez_boot_flow_html.py`

- [ ] **Step 1: Replace the long handoff narrative with the live status card**

Keep the page below roughly 140 lines and use this section order:

```markdown
# Milk-V Megrez RISC-V 移植状态

> 本页是唯一实时状态入口。
> 可执行命令只维护在 `tools/riscv/README.md`；冻结结果只维护在 evidence。

## 当前状态
## 最后真机边界
## 最近 QEMU 边界
## 第一缺失边界
## 当前单变量假设
## 尚未解决的问题
## 下一次 QEMU 门禁
## 下一次真机门禁
## 简化调试记录
## 文档地图与历史归档
```

Record `3ef99e6bd15341578b32256c897050e873ca2547` as the latest tested
physical candidate, with these evidence limits:

```text
PID 1 entered userspace and write(fd=1, requested=50) returned 50.
The UART log did not contain the hello.
Asterinas received no framebuffer, so it had no HDMI output path.
A later full DDR/OpenSBI/U-Boot epoch was observed; timer attribution also
depends on the controlled-session observation.
```

Set the single active hypothesis to reuse the U-Boot-initialized scanout through
an explicit framebuffer handoff. State that QEMU and board execution are paused
until the user and agent agree on the detailed test design.

Keep a compact `最近 QEMU 边界` section for the frozen `7f691c479` recovery
result and the `70734c14e` 16-GiB direct envelope. Make clear that neither is the
same artifact or environment as the `3ef99e6bd` board run.

Include the compact debug template from the approved design and links to the
command reference, evidence index, latest evidence, archived guide, archived
HTML, and historical `docs/superpowers/` material.

- [ ] **Step 2: Run the README contract test**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_readme_is_the_only_live_status_entry
```

Expected: PASS.

### Task 3: Make evidence append-only

**Files:**
- Modify: `docs/porting/evidence/megrez-history-index.md`
- Create: `docs/porting/evidence/2026-07-20-megrez-pid1-recovery.md`
- Test: `tools/riscv/tests/test_megrez_boot_flow_html.py`

- [ ] **Step 1: Keep only chronological evidence in the index**

Retain the stage table and local-evidence provenance.
Add the `3ef99e6bd` row with the Image, initramfs, and raw-log identities linked
through the dated evidence page.
Preserve the `70734c14e` local result directory plus the `911ab4c...` result and
`f58fe7c...` manifest hashes in the tracked ledger.
Replace `Current boundary and next board gates` with:

```markdown
## Current status

This file is an append-only evidence ledger, not a live status page.
See [`../README.md`](../README.md) for the current boundary and next gate.
```

- [ ] **Step 2: Keep the dated page scoped to its run**

The dated page must contain candidate identity, observed boundaries, direct
versus inferred evidence, and the local raw-evidence policy.
Rename its forward-looking section to `Unresolved at the end of this run` and
include the phrase `as of this run`.
Do not present its display approach as a current command or approved test plan.

- [ ] **Step 3: Run the evidence contract test**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_evidence_index_is_append_only_not_live_status
```

Expected: PASS.

### Task 4: Freeze the long guide and HTML

**Files:**
- Modify: `docs/porting/megrez-asterinas-boot-guide.md`
- Modify: `docs/porting/megrez-boot-flow.html`
- Modify: `tools/riscv/tests/test_megrez_boot_flow_html.py`

- [ ] **Step 1: Add an archive notice to the long guide**

Insert immediately below the title:

```markdown
> **历史快照（冻结于 2026-07-20）：** 本文保留概念、实验过程和工程复盘，
> 不再维护实时状态或当前命令。请从[唯一实时状态入口](README.md)继续。
```

Do not move the file or rewrite its historical commands.

- [ ] **Step 2: Add a visible archive notice to the HTML**

Add a banner near the top of `<body>` containing:

```html
<aside class="archive-notice">
  历史快照（冻结于 2026-07-20）。实时状态请查看
  <a href="/docs/porting/README.md">docs/porting/README.md</a>。
</aside>
```

Keep the six diagrams and existing browser behavior unchanged.

- [ ] **Step 3: Run the archive contract and complete HTML unit suite**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_flow_html.MegrezBootFlowHtmlTests.test_archived_explainers_redirect_to_live_status
python3 -m unittest tools.riscv.tests.test_megrez_boot_flow_html
```

Expected: both commands PASS.

### Task 5: Verify, review, and publish the cleanup

**Files:**
- Verify all files modified in Tasks 1–4.

- [ ] **Step 1: Run the full relevant unit suite**

Run:

```bash
make test_riscv_megrez_preflight_unit
```

Expected: all tests PASS; the cross-compiler-dependent reproducibility test may
remain SKIP outside the development container.

- [ ] **Step 2: Run the static-page browser regression**

Run:

```bash
make test_riscv_megrez_boot_flow_browser
```

Expected: six desktop diagrams, six mobile diagrams, and an empty
`console_errors` array.

- [ ] **Step 3: Run repository hygiene checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the planned documentation, evidence, and
HTML-test files are modified.

- [ ] **Step 4: Review the final diff against the approved design**

Confirm:

```text
one live status page
one command reference
one append-only evidence ledger
archive notices on the guide and HTML
no exact live-status duplication in archived HTML tests
no QEMU, serial, or physical-board action
```

- [ ] **Step 5: Create the atomic cleanup commit**

Run:

```bash
git add \
  docs/porting/README.md \
  docs/porting/evidence/megrez-history-index.md \
  docs/porting/evidence/2026-07-20-megrez-pid1-recovery.md \
  docs/porting/megrez-asterinas-boot-guide.md \
  docs/porting/megrez-boot-flow.html \
  tools/riscv/tests/test_megrez_boot_flow_html.py \
  tools/riscv/tests/test_megrez_preflight_repository.py \
  docs/superpowers/plans/2026-07-20-megrez-documentation-consolidation.md
git diff --cached --check
git commit -m "Consolidate Megrez bring-up documentation"
```

Expected: one commit containing only the planned cleanup and this plan.

- [ ] **Step 6: Push the handoff branch through the configured SSH key**

Fetch the exact remote branch and prove it remains an ancestor:

```bash
GIT_SSH_COMMAND='ssh -o BatchMode=yes -o IdentitiesOnly=yes -i /home/ubuntu/xaj/ssh_config/xaj_id_ed25519' \
  git fetch git@github.com:TankTechnology/asterinas-riscv.git \
  refs/heads/codex/megrez-porting-handoff:refs/remotes/origin/codex/megrez-porting-handoff
git merge-base --is-ancestor origin/codex/megrez-porting-handoff HEAD
```

Expected: both commands exit zero.

Then push without force:

```bash
GIT_SSH_COMMAND='ssh -o BatchMode=yes -o IdentitiesOnly=yes -i /home/ubuntu/xaj/ssh_config/xaj_id_ed25519' \
  git push -u git@github.com:TankTechnology/asterinas-riscv.git \
  codex/megrez-porting-handoff
```

Expected: `codex/megrez-porting-handoff` advances without force-push.
