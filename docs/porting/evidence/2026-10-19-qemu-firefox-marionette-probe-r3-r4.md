# Firefox Marionette short probe r3/r4

## r3: boundary reached

r3 used a 300-second probe budget and reached the exact target boundary:

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PHASE name=probe-new-session
A_WEB_MARIONETTE_TRANSPORT ... command=WebDriver:NewSession ... send_complete=true
```

The service timeout was still 180 seconds in the r3 image, so systemd killed
the probe before its `finally` block could publish the process receipt. This
configuration defect was corrected to 420 seconds for r4.

## r4: service timeout coverage

r4 reached the same NewSession send boundary but the desktop lifecycle timed
out before a complete probe receipt was published. No further runtime retry is
planned in this cycle; r3 already localizes the Firefox/userland boundary, and
r2 provides the thread snapshot format.

Artifacts:

| Run | Serial SHA-256 | Result SHA-256 | Lifecycle SHA-256 |
|---|---|---|---|
| r3 | `060ce9784b20a8e8bb91de55bd14db8e28920ed6220a0ae980f0d663ba162957` | `3325df5ab053793b5c9c7588d2ec7402344773b0ab7b11afee0cf639dcd044ec` | `010b4fbf71c524fdd6518a956a8c05ba4324544f95a51f6d7ae34ffd88d710b9` |
| r4 | `b502420ef0f4d8505980bafdca81b1254f8dc3666de24bc0d354aa61dce7ad70` | `238782f1899c351470c269811ec59cbad6e0f532247372e3147324d06e9c15eb` | `a78f293835f1cd5970b560a385ac0dbd6db4496065dea0240b6aaa52e2d60af9` |

The short-probe chain is therefore complete as a diagnostic: DRM/virgl is
healthy, Marionette greeting eventually arrives, and the Firefox stall begins
after the NewSession frame is sent. E3 application acceptance remains
unproven.
