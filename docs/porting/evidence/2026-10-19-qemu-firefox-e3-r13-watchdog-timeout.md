# Firefox E3 protocol watchdog r13 (2026-10-19)

The Firefox profile boot arguments were changed to
`asterinas.reboot_after=1200` so the previous 300-second guest reboot could
not interrupt Marionette startup. The run was started in the fixed GL
container and reached the Firefox service, but the host protocol did not
return or terminate within the intended 900-second gate budget. The host
process and its QEMU child were stopped manually at approximately 16 minutes
to prevent an unbounded run.

The lifecycle file is retained at
`/mnt/mydisk/qemu-firefox-e3-r13-container/lifecycle.json` with phase
`launch`, status `running` at interruption; no serial log was published.
This is evidence of a host-side bounded-wait defect, not a Firefox pass or a
new guest renderer result. The prior r12 serial receipt remains the
authoritative Firefox boundary: `new-session` followed by repeated
`ConnectionRefusedError`.

