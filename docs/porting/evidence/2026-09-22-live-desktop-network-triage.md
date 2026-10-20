# Live Megrez desktop network triage

## Later recovery status

The unavailable-control observations below describe earlier Asterinas boots.
At 19:31 CST on the same day, RockOS serial root and existing-key SSH access
were restored and verified across a software reboot and serial reconnect.
The persistent selector was an experimental entry without a debug-console flag;
it has been backed up and replaced with the qualified RockOS-default menu.
See [RockOS control recovery](2026-09-22-rockos-control-recovery.md)
for exact changes, identities, acceptance checks and reusable access paths.
Firefox/Bilibili validation remains pending; the board is now left in RockOS.

## Report and scope

The user reports an already-running Asterinas desktop and Firefox ESR showing
"Hmm. We're having trouble finding that site" for `www.baidu.com`.
The goal is to diagnose this live boot, then validate muted Bilibili playback.
No reboot, board image write, browser-profile modification or proxy restart
was performed during these checks.

## Checks at approximately 18:40 CST, 2026-09-22

| Boundary | Observation | What it establishes |
| --- | --- | --- |
| Host LAN | `enp12s0` has `10.100.19.216/21`; known board neighbour `10.100.19.200` is reachable | Host-side LAN configuration and neighbour reachability, not browser connectivity |
| UART | Stable FTDI device opens and responds to CR with `ubuntu-MS-7D76 login:` | The serial transport works; it does not provide an authenticated command shell |
| SSH | Known board address port 22 returns connection refused | No SSH control channel at that address/port |
| Browser/desktop control | Targeted TCP connections to the known board address on ports 2828 and 6000 return connection refused | Neither remote Marionette nor X11 TCP is available on those ports; this does not establish whether a loopback-only service exists |
| Host DNS tunnel | `dig +tcp +time=3 +tries=1 @10.100.19.216 -p 15354 www.baidu.com A` returns NOERROR and addresses | Existing host listener and its upstream resolver work |
| Host HTTP proxy | Requests through `http://10.100.19.216:17893` to `https://www.baidu.com/` and `https://www.bilibili.com/` return HTTP 200 | Existing proxy can reach both sites from the host side |
| Passive board traffic | A bounded 20-second host capture saw no board traffic on ports 22, 53, 15354 or 17893 | No observed packets in this window only; no coordinated browser reload occurred, so this does not prove the guest never uses those services |

The DNS listener is the existing `socat` bridge to `223.5.5.5:53`.
The HTTP listener is the existing `socat` bridge to the host proxy at
`127.0.0.1:7890`.
Neither process was restarted or replaced.

## Remaining discrimination

The source launcher `browser_web_firefox.sh` uses the dedicated profile
`/home/asterinas/.mozilla/asterinas-browser-web` and writes explicit HTTP/HTTPS
proxy preferences when configured for proxy mode.
A manually launched Firefox ESR may use a different profile.
This is a hypothesis, not an observation of the running browser.

The guest DNS shim requires the configured host/port and only publishes
`nameserver 127.0.0.1` after an upstream probe succeeds.
Host-side DNS success does not show that the shim is installed, running or
configured in this live boot.

## Live control limitation

The user declined the request to open a desktop Terminal and run commands.
Do not repeat this manual-operation request or interpret it as permission to
reset the board. The current desktop has been preserved.

Project-scoped session-history inspection confirms earlier access to RockOS
and earlier debug-console sessions, but did not recover an authenticated
command channel for this live boot. The UART device is accessible and is not
held by another serial-console process; receiving a login prompt is not a
successful shell handshake. Previously attempted documented credentials were
rejected; no further password guessing was performed.

An offline inspection of the expected
`target/debian-riscv/browser-web-dns2/debian-root.ext2` image finds locked
password fields for both `root` and `asterinas`, with no unlocked interactive
account found. Password hashes were not printed. This is evidence about the
local image, not verification of the live board's exact root filesystem.

Source inspection also rules out assuming that UART bytes are desktop keyboard
events: `kernel/src/device/tty/serial.rs` forwards received bytes into the serial
TTY. No serial SysRq recovery handler was found in the inspected console/TTY
path. In `kernel/src/device/tty/vt/keyboard/handler.rs`,
`SpecialHandler::Reboot` is explicitly unimplemented, despite a key mapping
existing. Neither is a verified live software-reboot route.

The existing desktop boot launcher requests `--debug-console=isolated-root`
and masks conflicting gettys; stage1 provides a dedicated root shell for that
explicit development mode. These are next-boot facilities, not a way to enable
a shell retroactively on an unauthenticated live system. The current boot's
arguments and service state remain unverified.

No safe live command route was established in this investigation. Host-side
network repair or another local image build alone cannot change the running
browser's profile or inspect its resolver. A usable login or another actual
management entry point is required to continue those live checks.

## Checks once control is available

After obtaining a live command channel, first capture the boot identity,
Firefox command line/profile, relevant DNS/proxy preferences, and service
states before changing configuration.
Do not infer that this board is running the newly qualified `gp`-fix kernel
merely because that artifact passed QEMU.

After connectivity is restored, use the existing Bilibili playback gate to
record an actual video element, advancing playback time and frame counters,
buffering/errors and muted state.
An HTTP 200 response, codec capability query or visible page alone is not
successful video playback.

## Follow-up: retaining access across sessions and boots

The user reports that the board probably restarted and explicitly requests that
the interaction channel be retained.
The project-scoped history identifies three conditional recovery/control paths:

- RockOS maintenance login followed by authenticated software reboot.
- An Asterinas Stage1-created serial root shell, independent of the browser.
- A fresh U-Boot prompt after a real firmware transition, not a command that
  can be sent to an arbitrary login prompt.

The September 19 recovery transcript at
`firefox-daily-use-perf/target/firefox-daily-use-physical/reinstall-preflight/software-recovery.serial.log`
in the historical worktree shows the userspace reboot deadline, then fresh
OpenSBI and U-Boot. It did not obtain a shell by logging into the locked
Asterinas accounts. That old deadline is not evidence of an armed deadline in
the current boot.

The retained September 12 candidate-2 menu already includes
`--debug-console=root`; the newer fast-boot launcher includes
`--debug-console=isolated-root`.
Thus reboot alone does not explain the missing channel: a correctly selected
entry recreates it. The currently selected entry, Stage1 identity and guest
service state are still unknown. Another bounded check found no unsolicited
UART output or response to a single Ctrl-C, and ports 22, 2828 and 6000 again
refused connections. No login guesses or reboot commands were sent.

Two independently testable retention gaps were fixed in local source, under
the existing debug-console and boot-menu designs:

1. Stage1 generated a conditional guard for `console-getty` but not for
   `serial-getty@ttyS0`. Both normal and isolated debug modes now generate the
   latter guard under `/run`, with the same opt-in enable marker. Existing
   destination files cause preparation to fail before creating the marker;
   `/etc`, passwords and normal non-debug boot policy remain unchanged.
2. Schema-v3 menu validation accepted Desktop arguments with no debug-console
   flag. Such entries are now rejected before use/publication. The retained
   qualified candidate-2 arguments remain valid. Older no-console manifests
   need regeneration and qualification rather than silent acceptance.

Verification performed in the persistent development container:

- RED: both debug modes failed the missing serial-getty guard assertion, and
  the pre-existing-guard test showed preparation incorrectly succeeding.
- RED: the missing-console menu test showed validation incorrectly succeeding.
- GREEN: 98 tests passed across `DebianStage1Tests`,
  `test_debian_debug_console` and `test_megrez_boot_menu` using `--offline`.
- GREEN: all 22 `test_megrez_desktop_boot` tests passed with normal container
  networking. An initial offline combined run had four errors because the
  existing startup preflight opens the host proxy; that offline namespace has
  no route. The proxy preflight was neither bypassed nor changed for the rerun.
- Final combined run: all 120 tests passed in 6.183 seconds with normal
  container networking. The retained candidate-2 manifest also passed the
  tightened validator unchanged.
- Independent scoped code review found no implementation/security issue;
  its documentation clarification about normal versus isolated graphical
  startup was applied to the README.

`AGENTS.md` and the RISC-V README now require fresh identity handshakes before
handoff, after reconnecting the host serial handle, and after an actual reboot.
These are operational acceptance requirements, not a claim that an automated
reconnect gate was added in this patch.

No new Stage1 or selector has been deployed to the board in this follow-up.
The live control channel remains unavailable. Actual reconnect/reboot
acceptance, diagnosis of the live getty/service state, and Firefox/Bilibili
hardware verification remain pending.

## Observed boot after the user reported a reset

The first fresh serial bytes were already from Asterinas/systemd at kernel
uptime approximately 16.5 seconds; no U-Boot countdown was captured.
The bounded acquisition attempt did not obtain a firmware prompt and therefore
sent no firmware commands. It retained the boot output under
`target/megrez-debug/serial-reacquire-20260922-7mmme46l/connection-1.log`.

This boot explicitly reports `Started console-getty.service`, then
`Debian GNU/Linux 13 ubuntu-MS-7D76 console` and a login prompt.
A subsequent empty line at that observed login prompt returns another
`ubuntu-MS-7D76 login:`; the response is retained in `login-response.log` in
the same directory. This proves current bidirectional UART communication,
not authenticated command access. No root debug prompt was observed.

The captured Xorg log includes a truncated kernel command line beginning
`console=ttyS0 console=tty0 loglevel=info init=/init`, unlike the current
fast-boot entry. The full command line and selected persistent selector remain
unknown; absence of a flag in this truncated line is not proof it was omitted.
The network evidence service initially reports `proxy-unavailable`, while a
later clock-sync message reports success through `10.100.19.216:17893`.
Do not interpret that initial network failure as proof the proxy is persistently
unreachable. No board reboot, credential attempt, image write or selector
update was performed by this check.
