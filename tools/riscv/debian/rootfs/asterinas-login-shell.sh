#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Login shell for the desktop user.
#
# Dropbear's pty relay stalls on this kernel: the client authenticates, the
# input reaches the line discipline, but data written by the session program
# to the pty slave never comes back, so an interactive session looks frozen.
# The pipe transport behind a no-pty session is reliable (scp, rsync and
# `ssh -T host cmd` all work), so authorized_keys carries the `no-pty`
# option and this shell gives the session a real pty of its own on the board
# with `script`. Interactive use then has a controlling terminal, job
# control and a prompt, and the bytes travel over pipes instead of the
# broken pty relay.
#
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
