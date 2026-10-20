# Porting status verifier: relative-path gate

## Scope

This bounded host-side step fixes `docs/porting/verify.py` so that relative
`--manifest`, `--receipts`, and `--output` arguments are resolved once before
loading receipts and rendering evidence paths. It does not start QEMU,
Firefox, a guest, a serial device, or a network command.

## Gate

`status-verifier-relative-paths`: invoke the read-only verifier with repository
relative paths and require a generated `STATUS.md` without a traceback. The
exit status remains non-zero while independent E3/E5 claims are blocked; that
is expected and is not converted into a pass.

## Verification

Commands:

```text
python3 -m py_compile docs/porting/verify.py
python3 docs/porting/verify.py \
  --manifest docs/porting/manifest.lock \
  --receipts docs/porting/receipts \
  --output docs/porting/STATUS.md
python3 docs/porting/verify.py \
  --manifest /mnt/shared/xaj/Program/asterinas/docs/porting/manifest.lock \
  --receipts /mnt/shared/xaj/Program/asterinas/docs/porting/receipts \
  --output /mnt/shared/xaj/Program/asterinas/docs/porting/STATUS.md
```

Both invocations completed without a traceback and returned `1` solely because
the existing E3 (`official_firefox_scene`) and E5 (`uid0_nonce` /
`fallback_recovery`) receipts remain blocked. The generated status reports E1,
E2 and E4 as `✅`, and its diagnostics line remains:

```text
✅ Receipt schema, one-assertion rule, manifest binding and evidence paths passed.
```

## Inputs

- `verify.py` SHA-256: `eacb5ee6deaaad6a0c561512381f081e04f660dcf2449508e17056fb7f597f0e`
- `STATUS.md` SHA-256 after generation: `2df3bd9e87054f5dee1ab09e3e45fb7a4cec38e5d37d1de9d4a5eafbfc7cf037`

## Result

The relative-path verifier failure is closed. E3 and E5 remain independent
blocked claims; no QEMU or hardware result is inferred from this host-only
gate.
