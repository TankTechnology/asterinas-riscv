#!/bin/sh

# Asterinas' serial console currently cannot complete util-linux login(1)'s
# controlling-terminal handoff. agetty supports a small login helper, so
# keep the user-visible username/password flow while starting the shell on
# the tty that agetty already owns.
# agetty has already consumed the username before invoking the helper. This
# profile intentionally exposes only the provisioned Debian account.
printf 'Password: '
IFS= read -r password || exit 1
printf '\n'
if [ "$password" != asterinas ]; then
    printf 'Login incorrect\n'
    exit 1
fi

printf 'Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent\npermitted by applicable law.\n'
exec /usr/bin/setpriv --reuid=1000 --regid=1000 --init-groups \
    env HOME=/home/debian USER=debian LOGNAME=debian SHELL=/bin/bash \
    /bin/bash -i
