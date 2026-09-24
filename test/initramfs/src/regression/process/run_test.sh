#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

if [ "$(uname -m)" = "x86_64" ]; then
    ./arch_prctl/fsgsbase
fi

./clone3/clone_exit_signal
./clone3/clone_files
./clone3/clone_invalid_exit_signal
./clone3/clone_no_exit_signal
./clone3/clone_parent
./clone3/clone_process
./clone3/clone_set_tid

./cpu_affinity/cpu_affinity
./cpu_affinity/inheritance || [ "$?" -eq 77 ]

if [ "$(uname -m)" = "riscv64" ]; then
    if grep -qw 'RISCV_ICACHE_REQUIRE_SMP4=1' /proc/cmdline; then
        ./riscv_flush_icache/riscv_flush_icache --require-smp4
    else
        ./riscv_flush_icache/riscv_flush_icache
    fi
fi

./execve/execve
./execve/execve_comm
./execve/execve_err
./execve/execve_memfd
./execve/execve_shebang_argv
./execve/execve_mt_parent

./exit/exit_code
./exit/exit_procfs

[ "$(uname -m)" = "x86_64" ] && ./fork/fork
./fork_c/fork

./getcpu/getcpu

./getpid/getpid

./personality/personality

./prctl/capbset
./prctl/no_new_privs
./prctl/secure_bits
./prctl/subreaper
./prctl/thread_name

./pthread/pthread_signal_test
./pthread/pthread_cond_handoff
./pthread/pthread_test

./ptrace/ptrace
./ptrace/set_options

if [ "$(uname -m)" = "x86_64" ]; then
    ./ptrace/debugger
    ./ptrace/read_write_regs
fi

./sched/sched_attr_getset
./sched/sched_param_getset
./sched/sched_param_idle
./sched/sched_permissions
./sched/sched_policy

./signal/kill
./signal/parent_death_signal
./signal/pidfd_send_signal
./signal/signal_fd
./signal/signal_test2
./signal/stop_continue
./signal/stop_continue_pending
./signal/sigtimedwait_race
./signal/group_stop
./signal/group_stop_workload
./signal/group_stop_events
./signal/group_stop_disposition
./signal/group_stop_restart
./signal/group_stop_sleep
./signal/group_stop_wait
./signal/group_stop_ppoll
./signal/group_stop_pselect
./signal/group_stop_poll_select
./signal/group_stop_futex
./signal/group_stop_ptrace
./signal/group_stop_ptrace_parent
./signal/group_stop_lifecycle
./signal/group_stop_time_namespace || [ "$?" -eq 77 ]

if [ "$(uname -m)" != "loongarch64" ]; then
    ./signal/signal_restart_context
fi

if [ "$(uname -m)" = "x86_64" ]; then
    ./signal/signal_sigsuspend_ptrace
    ./signal/fault_signals
    ./signal/sigaltstack
    ./signal/signal_fpu
    ./signal/signal_rflags_df
    ./signal/signal_test
    ./signal/sigtrap
fi

./cgroup.sh
./cgroup_events
./syslog/syslog
./syslog/provenance
./group_session
./job_control
./pidfd
./pidfd_getfd
./kcmp
./rseq
./wait4
