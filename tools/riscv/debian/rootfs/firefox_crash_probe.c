/*
 * Diagnostic-only crash reporter for the Asterinas E3 Firefox gate.
 *
 * A release-build Firefox that dies from SIGSEGV/SIGBUS usually closes its
 * Marionette socket without writing anything to stderr.  Loaded through
 * LD_PRELOAD, this records the faulting signal, address and the loaded-module
 * offset of every frame so the Debian dbgsym files can name the site offline.
 *
 * This is a debugging aid, not part of the shipped guest image.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <execinfo.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ucontext.h>
#include <unistd.h>

/* sigaltstack is required for stack-overflow faults; SIGSTKSZ is not a
 * constant expression in current glibc, so reserve a fixed generous stack. */
static char alternate_stack[64 * 1024];

/* write(2) may fail on a full pipe; a crash report cannot do more than try. */
static void emit(const char *buffer, int length) {
    if (length <= 0) {
        return;
    }
    ssize_t ignored = write(STDERR_FILENO, buffer, (size_t)length);
    (void)ignored;
}

static void report_module(const char *label, void *address) {
    Dl_info module;
    char line[320];
    if (dladdr(address, &module) != 0 && module.dli_fname != NULL) {
        uintptr_t offset = (uintptr_t)address - (uintptr_t)module.dli_fbase;
        int length = snprintf(
            line,
            sizeof(line),
            "ASTERINAS_CRASH_MODULE %s=%p %s+0x%lx caller=%s\n",
            label,
            address,
            module.dli_fname,
            (unsigned long)offset,
            module.dli_sname != NULL ? module.dli_sname : "-");
        emit(line, length);
    } else {
        int length =
            snprintf(line, sizeof(line), "ASTERINAS_CRASH_MODULE %s=%p unknown\n", label, address);
        emit(line, length);
    }
}

static void report(int signal_number, siginfo_t *info, void *context) {
    void *frames[64];
    char line[320];
    int count = backtrace(frames, (int)(sizeof(frames) / sizeof(frames[0])));
    int length = snprintf(
        line,
        sizeof(line),
        "\nASTERINAS_CRASH_PROBE signal=%d code=%d addr=%p frames=%d\n",
        signal_number,
        info != NULL ? info->si_code : -1,
        info != NULL ? info->si_addr : NULL,
        count);
    emit(line, length);
    /* backtrace() starts at this handler and cannot unwind the signal frame,
     * so the interrupted PC/RA have to come from the ucontext itself. */
    if (context != NULL) {
        const ucontext_t *machine = context;
        uintptr_t pc = machine->uc_mcontext.__gregs[REG_PC];
        uintptr_t ra = machine->uc_mcontext.__gregs[REG_RA];
        uintptr_t sp = machine->uc_mcontext.__gregs[REG_SP];
        length = snprintf(
            line,
            sizeof(line),
            "ASTERINAS_CRASH_REG pc=%p ra=%p sp=%p\n",
            (void *)pc,
            (void *)ra,
            (void *)sp);
        emit(line, length);
        report_module("pc", (void *)pc);
        report_module("ra", (void *)ra);
    }
    for (int index = 0; index < count; index++) {
        Dl_info module;
        if (dladdr(frames[index], &module) != 0 && module.dli_fname != NULL) {
            uintptr_t offset = (uintptr_t)frames[index] - (uintptr_t)module.dli_fbase;
            length = snprintf(
                line,
                sizeof(line),
                "ASTERINAS_CRASH_FRAME #%d %s+0x%lx caller=%s\n",
                index,
                module.dli_fname,
                (unsigned long)offset,
                module.dli_sname != NULL ? module.dli_sname : "-");
        } else {
            length = snprintf(
                line,
                sizeof(line),
                "ASTERINAS_CRASH_FRAME #%d %p caller=-\n",
                index,
                frames[index]);
        }
        emit(line, length);
    }
    /* Restore the default action and re-raise so the exit status still
     * reports the original signal to the supervising gate. */
    signal(signal_number, SIG_DFL);
    raise(signal_number);
}

__attribute__((constructor)) static void install_handlers(void) {
    stack_t stack = {0};
    stack.ss_sp = alternate_stack;
    stack.ss_size = sizeof(alternate_stack);
    sigaltstack(&stack, NULL);
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_sigaction = report;
    action.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_RESETHAND;
    sigemptyset(&action.sa_mask);
    sigaction(SIGSEGV, &action, NULL);
    sigaction(SIGBUS, &action, NULL);
    sigaction(SIGILL, &action, NULL);
    sigaction(SIGFPE, &action, NULL);
    sigaction(SIGABRT, &action, NULL);
    /* Firefox's GPU/content sandboxes are seccomp filters; a misclassified
     * syscall reaches a process as SIGSYS, which is otherwise silent. */
    sigaction(SIGSYS, &action, NULL);
}
