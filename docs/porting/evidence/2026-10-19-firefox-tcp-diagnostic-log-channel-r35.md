# TCP diagnostic log channel r35

The opt-in TCP trace was changed from `info!` to `error!` so
`asterinas.tcp_diagnostic_port=2828` remains visible at the probe's
`loglevel=4` without enabling the full kernel info stream.  The kernel build
completed successfully.

The subsequent runtime attempt r34 was inconclusive because the host QEMU
container lifetime expired before serial output; no TCP runtime claim is made.

