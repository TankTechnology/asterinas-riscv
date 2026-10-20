# Firefox Marionette probe r34 host lifetime boundary

r34 was launched with the opt-in TCP trace enabled.  The persistent QEMU
container reached the guest launch phase, but its fixed `sleep 3600` lifetime
expired while QEMU was still running.  The container stopped before producing a
serial log or result, so no Firefox or TCP conclusion is drawn.

The lifecycle file remains `launch/running` and is intentionally preserved as
an incomplete host-runner artifact.  This is the second host-lifetime issue
seen in this line; future runs must use a container with a lifetime exceeding
the gate timeout or an external supervisor.

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r34-container/`

