# RISC-V Debian Firefox builder

This image is the single build environment for the RISC-V Debian Firefox
workflow. Its base is pinned by digest and it adds the host-side tools used to
finalize the target root filesystem's sysusers, journal, linker, and font
caches before QEMU starts. It also contains FFmpeg so the checked-in Firefox
media fixture is validated instead of silently skipped.
The Python build headers and `setuptools` required by the repository's U-Boot
preparation script are included as well, along with the `bison`/`flex`
generators and OpenSSL development headers used by U-Boot's Kconfig/build.

Ubuntu's base package does not contain the Debian 13 archive keys. The image
therefore installs the fixed Trixie `debian-archive-keyring` package after
checking its published SHA-256 digest; signed release validation remains
fail-closed.

Build it from the repository root:

```bash
docker build \
  --file tools/riscv/debian/docker/Dockerfile \
  --tag asterinas/debian-riscv-firefox:20260829 \
  .
```

Do not install packages interactively into a running container. Update this
Dockerfile and advance the dated tag so the environment remains reproducible.

Mount the canonical clone at `/workspace` and a persistent host backup
directory at `/artifacts`. Repository output under `target/` is a build cache;
copy every expensive final artifact and its source/provenance bundle to
`/artifacts` immediately after it is produced.
