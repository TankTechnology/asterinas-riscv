#!/busybox sh
# SPDX-License-Identifier: MPL-2.0

/busybox mount -t proc proc /proc || exit 1
# The kernel has already populated /dev before executing init.
echo KLOG_MICRO_BEGIN
/syslog-probe
klog_probe_status=$?
echo KLOG_MICRO_PROBE_EXIT=$klog_probe_status
/console-probe
klog_console_status=$?
echo KLOG_CONSOLE_EXIT=$klog_console_status
echo KLOG_DMESG_BEGIN
/busybox dmesg
klog_dmesg_status=$?
echo KLOG_DMESG_EXIT=$klog_dmesg_status
/dmesg --version
/dmesg --raw
klog_util_status=$?
echo KLOG_UTIL_DMESG_EXIT=$klog_util_status
/dmesg-follow-probe
klog_follow_status=$?
echo KLOG_FOLLOW_EXIT=$klog_follow_status
echo KLOG_MICRO_END
/busybox poweroff -f
