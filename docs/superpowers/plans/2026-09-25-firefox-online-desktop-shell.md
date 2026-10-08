# Firefox Online Desktop Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing `browser-web` Debian image show an interactive wallpaper, desktop, and bottom launcher/task bar alongside Firefox.

**Architecture:** Keep the existing Xorg fbdev owner and Firefox service. Install a separate online desktop profile using the already packaged PCManFM and LXPanel, then start both as UID 1000 after X readiness. Keep desktop launchers wired to the browser-web Firefox profile and preserve the root serial debug console.

**Tech Stack:** Bash rootfs builder and session script, Debian PCManFM/LXPanel/Openbox, Python `unittest`, RISC-V QEMU browser-web gate.

---

### Task 1: Fail on the current online image

**Files:** `tools/riscv/tests/test_debian_rootfs.py`

- [x] Add a focused staging test beside `test_configures_desktop_m4_basic_applications`. Call `install_online_desktop_shell` directly so the test does not require the unrelated online trust-check fixture.
- [x] Assert that the staged image contains the wallpaper, PCManFM and LXPanel profiles owned by UID 1000, and Firefox/Files/Terminal launchers with executable browser command. Assert the panel chooses the bottom edge and the session starts PCManFM and LXPanel as UID 1000.
- [x] Run `python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianRootfsBuilderTests.test_configures_browser_web_desktop_shell`; it failed before the helper existed and passes after the change.

### Task 2: Install a real online desktop profile

**Files:** `tools/riscv/debian/rootfs/build_rootfs.sh`, `tools/riscv/debian/rootfs/desktop_online_lxpanel.conf`, `tools/riscv/debian/rootfs/asterinas_firefox.desktop`

- [x] In the `generation=m5, browser_mode=online` branch, install the existing M4 wallpaper and PCManFM configuration into the online image, create `Desktop` and profile directories owned by UID 1000, and install the new bottom LXPanel profile.
- [x] Install three launchers to both `/usr/share/applications` and the user's desktop. The browser launcher must use Firefox's existing browser-web profile, not the M4 NetSurf command.
- [x] Keep the M4 image's existing top panel and NetSurf launcher unchanged.
- [x] Run the focused test from Task 1; it passes.

### Task 3: Start the shell after X is ready

**Files:** `tools/riscv/debian/rootfs/desktop_m5_session.sh`, `tools/riscv/tests/test_debian_browser_web.py`

- [x] Add focused assertions for the online session's X-ready branch and UID-1000 launches for Openbox, PCManFM desktop, and LXPanel.
- [x] Start PCManFM and LXPanel after `x-socket-ready` in the existing Xorg session; do not start a second Firefox service.
- [x] Run the focused test, the complete rootfs/browser-web unit suites, and `bash -n tools/riscv/debian/rootfs/desktop_m5_session.sh`.

### Task 4: Verify the image and interaction

**Files:** `tools/riscv/debian/rootfs/browser_web_desktop_shell_qemu_gate.py`, `tools/riscv/tests/test_debian_browser_web.py`. The existing web gate cannot retain the requested desktop screenshots.

- [ ] Run the existing browser-web QEMU gate with a frozen rootfs and RISC-V release kernel. Preserve a screenshot with Firefox open and one with Firefox minimized; check wallpaper, desktop icons, and bottom launcher/task bar visually.
- [x] Click Firefox, Files, and Terminal launchers, switch windows, and minimize/restore Firefox; use a short bounded interaction gate rather than a long load run. Six QEMU screenshots were reviewed.
- [x] Preserve the browser functional result, artifact identities, command transcript, and whether the run is QEMU. The broader browser gate failed at `probe-fixture-home`, including a no-shell control; the physical minimize check also failed and remains open.

### Task 5: Physical gate and performance handoff

**Files:** a dated record under `docs/porting/evidence/`, and the existing daily-use capture output.

- [ ] Deploy only after QEMU passes and confirm fresh nonce-framed UID-0 serial control before and after closing/reopening the host serial connection. Check reboot persistence separately.
- [ ] Re-run the same bounded desktop and seven-group Firefox functional gate on Megrez. Record a short uninstrumented startup/input/scroll/video baseline with Image, rootfs, display provider, resolution, and clip identity.
- [ ] If the shell adds a measurable regression, compare the same boot with shell on/off before selecting a code change. Profile the dominant boundary with an overhead control; select one optimization variable and qualify it with baseline/candidate/baseline runs.

This plan implements the first independent milestone in [the daily-use objective](../../porting/2026-09-25-firefox-desktop-daily-use-objective.md). The later performance change is chosen from the physical attribution result, so it is not preselected here.
