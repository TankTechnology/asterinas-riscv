# Remote access and API-driven harnesses

Date: 2026-11-04

## Outcome

The board is reachable over SSH with a key and can move files with scp, sftp
and rsync in both directions. The Python stack needed by an API-driven
DeepSeek harness installs from PyPI wheels and reaches `api.deepseek.com`
over TLS.

## SSH: the OpenSSH stall was a `poll(2)` bug

OpenSSH used to stop right after authentication: the server logged
"Accepted publickey" and sent `SSH2_MSG_CHANNEL_OPEN_CONFIRMATION`, the
client never received it, and the remote command never ran (a `touch` side
effect never appeared on the board). Both ends then slept in `ppoll`
forever.

Root cause, in the kernel: `poll(2)` allows one descriptor to appear more
than once in the array with different masks, and OpenSSH does exactly that —
it polls its connection once with `POLLIN` and once with an empty mask (it
only wants `ERR`/`HUP` from that entry). `Pollee::register_poller` registered
the poll handle's single observer on the same pollee twice, and `SyncSubject`
keeps one filter per observer, so the second registration *replaced* the
first entry's `POLLIN` with `ALWAYS_POLL` (`ERR|HUP`). Readiness for data
then never woke the waiter even though the bytes were already queued.

The whole bug fits in thirty lines (a socketpair, one writer thread):

    struct pollfd fds[2] = {
        { .fd = fd, .events = POLLIN },
        { .fd = fd, .events = 0 },
    };
    ppoll(fds, 2, NULL, NULL);

On Linux this returns `revents=0x1` when the peer writes. On the unfixed
kernel it blocked until an external timeout killed it. That is also why
plain `poll`/`select` tests always passed: they never repeated a descriptor.

Fix: `Pollee::register_poller` remembers the mask registered per pollee and
registers the union of every registration of the same handle on that pollee
(`kernel/src/process/signal/poll.rs`, with a ktest). A union can only cause
spurious wakeups for a handle that later narrows its interest, which the
pollee contract already allows.

Verified on a QEMU boot of the board's own kernel plus this rootfs: the
reproducer returns `revents=0x1`, and
`ssh 127.0.0.1 'touch /tmp/RAN; echo REMOTE_ECHO_OK'` exits 0 with both the
marker file and the remote output. The pty path needs `devpts` mounted
(systemd mounts it; a stage1 interactive shell does not), so a `-tt` run
there stops at `openpty: No such file or directory`, not in `ppoll`.

The image still ships dropbear on port 22 because it was verified first and
keeps working; the OpenSSH server can now be unmasked if desired.

dropbear's pipe transport is reliable: `ssh -T host cmd`, scp, sftp and
rsync work, including a 20 MB round trip (11 s up / 16 s down, matching
SHA-256). The image therefore ships `dropbear-bin` on port 22 and masks the
OpenSSH server unit:

    /etc/systemd/system/asterinas-dropbear.service
    WantedBy=asterinas-debug-console.target

The target matters: this image never reaches `basic.target` or
`multi-user.target`, so a unit wanted by those never starts. The first
attempt (`WantedBy=sysinit.target`) stayed inactive on the board.

### The fake-pty dropbear

A client that is given a pty and then has the request refused treats it as
fatal (`PTY allocation request failed` then exit 255), so simply marking
keys `no-pty` breaks interactive use. Instead the image installs a dropbear
built from the same 2025.89 source with
`dropbear-asterinas-fake-pty.patch`: `sessionpty()` parses the request and
replies success, but allocates nothing, and `sessioncommand()` runs the
session over pipes (`master == -1`). The client keeps its normal terminal
handling while the bytes travel over the transport that works.

`/usr/local/bin/asterinas-login-shell` (the account's shell, listed in
`/etc/shells` or dropbear rejects the user) then runs
`/usr/bin/script -q -c '/bin/bash -l' /dev/null` when stdin is not a tty,
so an interactive login gets a board-side pty with a prompt and job
control. `ssh board 'cmd'` runs bash directly.

The fake pty existed only to avoid the poll bug above; with it fixed, a
dropbear built with real pty support can `openpty` again. The image keeps the
patched binary because its pipe transport is the path verified on the board.

The patched binary is checked in as
`tools/riscv/debian/rootfs/dropbear-patched-riscv64` (the patch beside it
documents the two edits). It must also resolve the sftp subsystem, whose
upstream default path is `/usr/libexec/sftp-server`; the image adds that
symlink to `/usr/lib/openssh/sftp-server`, otherwise scp and sftp fail with
"Connection closed".

Verified from the host: ten consecutive `ssh -tt board cmd` sessions print
their output, an interactive login reports `/dev/pts/0`, and
`ssh -T board 'sudo -n true'` succeeds (the image now installs sudo).

Pitfall: running `ssh -tt` from the *serial console* without a timeout lets
it put ttyS0 into raw mode. If it hangs, the console is captured and the
root shell stops responding. Recover over the dropbear channel by killing
the server-side sessions (`pkill -9 -u asterinas -x dropbear`); a detached
`while ...; do pkill ...; done` drains a shell loop that spawned them.

## rsync and friends

    rsync -e "ssh -i ~/.ssh/board" local.bin asterinas@<board>:/tmp/
    rsync -e "ssh -i ~/.ssh/board" asterinas@<board>:/tmp/remote.bin .

scp (SFTP protocol; `openssh-server` supplies `/usr/lib/openssh/sftp-server`
even though its own server unit is masked) and sftp were verified as well.

## DeepSeek harness via the API

Platform pieces, all verified on the board:

* Python 3.13 with `python3-pip` and `python3-venv`;
* PyPI reachable, with riscv64 wheels for the OpenAI SDK's native
  dependencies (`pydantic-core`, `jiter`) — `pip install openai` installs
  without a source build;
* `https://api.deepseek.com` reachable over TLS (HTTP 401 without a key).

A harness is then a normal venv:

    python3 -m venv ~/deepseek-env
    ~/deepseek-env/bin/pip install openai
    DEEPSEEK_API_KEY=... ~/deepseek-env/bin/python harness.py

Never write the key into the image or a repository file; read it from the
environment. A minimal check (replace the key at run time):

    from openai import OpenAI
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                    base_url="https://api.deepseek.com")
    print(client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "ping"}]).choices[0].message.content)

With a deliberately invalid key the SDK raises `AuthenticationError` with
status 401, which is the expected proof that DNS, TLS, HTTP and the client
library all work up to the API.
