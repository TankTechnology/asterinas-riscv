# Firefox ESR source-package anchor

Date: 2026-10-17

## Scope

The matching Debian Security source descriptor for the pinned ESR source was
downloaded into `/tmp` only. No source archive was downloaded, extracted, or
built. This records the exact input needed for the later reproducible RISC-V
build and does not claim Debian signature verification on this host.

## Verification

Descriptor:

```text
https://security.debian.org/debian-security/pool/updates/main/f/firefox-esr/firefox-esr_140.16.0esr-1~deb13u1.dsc
SHA-256: 721ad7dc3924a0061a0f5893340f97659e2e7c35b65a95f004eb8174cab2f249
Format: 3.0 (quilt)
Version: 140.16.0esr-1~deb13u1
```

The repository anchor is
`tools/riscv/debian/rootfs/firefox_compat/source_package_manifest.json`.
Its two core archives match the descriptor's SHA-256 and size rows:

```text
firefox-esr_140.16.0esr.orig.tar.xz
  size=687986356
  sha256=c0c5b6ff43742f3e2ad5eec004956f33aaedabf457aeb54851dd74f45db0ce22
firefox-esr_140.16.0esr-1~deb13u1.debian.tar.xz
  size=186476
  sha256=61f7977216d310103af70d66918ddf7d47bf60ac50a19016712f0d6c168e9245
```

The local descriptor/manifest comparison emitted:

```text
FIREFOX_SOURCE_PACKAGE_MANIFEST PASS dsc_sha256=721ad7dc3924a0061a0f5893340f97659e2e7c35b65a95f004eb8174cab2f249 archives=2

$ PYTHONPATH=tools/riscv python3 tools/riscv/debian/rootfs/firefox_compat/source_package_gate.py \
    --manifest tools/riscv/debian/rootfs/firefox_compat/source_package_manifest.json \
    --dsc /tmp/firefox-esr_140.16.0esr-1~deb13u1.dsc
FIREFOX_SOURCE_PACKAGE_GATE PASS package=firefox-esr version=140.16.0esr-1~deb13u1 archives=2
```

The reusable gate has three tests covering a matching descriptor, descriptor
hash drift, and archive-row drift; they all pass as part of the 37-test Firefox
host suite.

## Signature check and boundary

The fixed Debian archive-keyring package from the repository Dockerfile was
downloaded to `/tmp` and matched its pinned SHA-256:

```text
9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac
```

The descriptor's signer fingerprint is
`182E161D1130B9FCCD7DB167E42AA04FA6AA8C72`. With that public key imported
into a disposable keyring, `gpgv` reported:

```text
gpgv: 完好的签名，来自于 “Mike Hommey <glandium@debian.org>”
```

This proves the OpenPGP signature under the retrieved signer key; the key was
not added to the repository or the host trust database. The next build step
must still download and hash the two archives, run the source patch gate, and
build inside a fixed RISC-V-capable image before package admission.

The two core archives have since been downloaded to `/tmp`, verified against
the recorded SHA-256 values, and extracted under
`/tmp/firefox-esr-140-source-tree/firefox-140.16.0`. The full source tree
passes the zero-fuzz source patch gate. It is not yet built: the host is
missing the Firefox Debian Build-Depends and has no fixed RISC-V builder image.
