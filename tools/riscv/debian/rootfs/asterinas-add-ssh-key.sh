#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Authorize a public key for the desktop user over the serial console.
#
#   add-ssh-key 'ssh-ed25519 AAAAC3... you@laptop'
#
# Dropbear reads ~/.ssh/authorized_keys exactly like OpenSSH, and the desktop
# user has no password (the image ships with a locked one), so a key is the
# only way in. Keeping the file root-owned until the append is the simplest
# way to keep it 0600 and owned by the right user.

set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: add-ssh-key 'ssh-ed25519 AAAAC3... comment'" >&2
    exit 2
fi

user=asterinas
home=/home/asterinas
key_file="$home/.ssh/authorized_keys"

install -d -m 0700 -o "$user" -g "$user" "$home/.ssh"
touch "$key_file"
grep -qxF -- "$1" "$key_file" || printf '%s\n' "$1" >>"$key_file"
chown "$user:$user" "$key_file"
chmod 0600 "$key_file"
echo "authorized $(printf '%s' "$1" | cut -d' ' -f1,3) for $user"
