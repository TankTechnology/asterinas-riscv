# Asterinas Development Docker Images

Asterinas development Docker images are provided to facilitate developing and testing Asterinas project. These images can be found in the [asterinas/asterinas](https://hub.docker.com/r/asterinas/asterinas/) repository on DockerHub.

## Building Docker Images

Asterinas development Docker image is based on an OSDK development Docker image. To build an Asterinas development Docker image and test it on your local machine, navigate to the root directory of the Asterinas source code tree and execute the following command:

```bash
cd <asterinas dir>
# Build Docker image
docker buildx build \
    -f tools/docker/Dockerfile \
    --platform linux/amd64,linux/arm64 \
    --build-arg ASTER_RUST_VERSION=$(grep "channel" rust-toolchain.toml | awk -F '"' '{print $2}') \
    --build-arg BASE_VERSION=$(cat DOCKER_IMAGE_VERSION) \
    -t asterinas/asterinas:$(cat DOCKER_IMAGE_VERSION) \
    .
```

## Tagging and Uploading Docker Images

The Docker images are tagged according to the version specified
in the `DOCKER_IMAGE_VERSION` file at the project root.
Check out the [version bump](https://asterinas.github.io/book/to-contribute/version-bump.html) documentation
on how new versions of the Docker images are released.

## Building the RISC-V Debian rootfs builder image

The general development image intentionally does not include the packages
needed to construct a Debian `riscv64` rootfs. Build the dedicated derived
image when working on the Debian, NetSurf, or Firefox gates:

```bash
make build_riscv_rootfs_image
```

The target defaults to the locally validated
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` base and produces
`asterinas/asterinas:0.18.0-20260702-riscv-rootfs`. Both values can be pinned or
overridden without changing the general image:

```bash
make build_riscv_rootfs_image \
  RISCV_ROOTFS_BASE_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  RISCV_ROOTFS_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-rootfs
```

Run the image with `--privileged --network=host`. The entrypoint validates the
host-provided `binfmt_misc` registration before executing any long build. Keep
the content-addressed rootfs cache in a named volume mounted at
`/root/asterinas/target/debian-riscv/cache`; it can be reused safely because
the rootfs builder admits entries only after SHA-256 verification. See
`tools/docker/riscv-rootfs/README.md` for the complete run and proxy examples.
