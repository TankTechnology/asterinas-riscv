#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Login shell for the desktop user.
#
# The image runs OpenSSH on port 22, so an interactive `ssh -t` session
# already has a real pty and lands in the `bash -l` branch below. The
# `script` fallback covers the fallback server: dropbear ships patched to
# fake the pty (`dropbear-asterinas-fake-pty.patch`), because its pty relay
# used to stall -- the client authenticated, input reached the line
# discipline, but the session's output never came back. A no-pty session
# gets a board-side pty from `script`, which keeps job control and a prompt
# while the bytes travel over the pipe transport that does work.
#
#   ssh -t board                   -> login bash on the server's pty
#   ssh board                      -> script-backed interactive bash
#   ssh board 'cmd'                -> bash -c cmd (no pty)
#   local console (tty present)    -> plain login bash

if [ "$#" -gt 0 ]; then
    exec /bin/bash "$@"
fi

if [ -t 0 ] && [ -t 1 ]; then
    exec /bin/bash -l
fi

exec /usr/bin/script -q -c '/bin/bash -l' /dev/null
