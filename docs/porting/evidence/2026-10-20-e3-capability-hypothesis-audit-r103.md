# E3 Firefox capability hypothesis audit r103

- gate: `e3-firefox-capability-hypothesis-audit`
- claim: `E3`
- status: `pass` for the diagnostic hypothesis only; E3 application acceptance remains unmet.

## Assertion

The A/B probe used the same kernel, DTB, rootfs image, rootfs manifest and
package inputs. Only the WebDriver NewSession capability object differed.
Both arms reached the same boundary: Marionette greeting, virgl GLX and
`/dev/dri/renderD128` passed, but no NewSession response arrived within the
bounded 360-second run. Therefore capability shape is not sufficient to
explain the current E3 failure.

## Evidence

- A/B runtime evidence: `docs/porting/evidence/2026-10-20-e3-firefox-newsession-capability-ab-r101.md`
- A/B receipt: `docs/porting/receipts/2026-10-20-e3-firefox-newsession-capability-ab-r101.json`
- Current wait-boundary evidence: `docs/porting/evidence/2026-10-20-firefox-ppoll-runtime-r99.md`
- Current wait-boundary receipt: `docs/porting/receipts/2026-10-20-e3-firefox-ppoll-runtime-r99.json`

## Decision

Do not rerun either capability arm and do not revert the scene gate to the
legacy three-field object on this evidence. The next bounded investigation is
the guest IPC wait path (`ppoll` in the Firefox parent and `futex` in a worker),
with no full 900-second scene run until that boundary changes.
