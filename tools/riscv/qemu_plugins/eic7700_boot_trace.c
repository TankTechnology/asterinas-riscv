/* SPDX-License-Identifier: GPL-2.0-or-later */

/*
 * Small QEMU TCG plugin for bounded Asterinas boot traces.
 *
 * It intentionally reports only aggregate translation, instruction, idle and
 * resume counters.  It does not inspect guest memory or claim to be a timing
 * model; use it to find an execution loop or an unexpected idle boundary.
 */

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <qemu-plugin.h>

#define TRACE_OUTPUT_PATH_MAX 4096

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

static uint64_t tb_count;
static uint64_t insn_count;
static uint64_t idle_count;
static uint64_t resume_count;
static char trace_output_path[TRACE_OUTPUT_PATH_MAX];

static void tb_exec(unsigned int cpu_index, void *userdata)
{
    (void)cpu_index;
    __atomic_fetch_add(&tb_count, 1, __ATOMIC_RELAXED);
    __atomic_fetch_add(&insn_count, (uintptr_t)userdata, __ATOMIC_RELAXED);
}

static void tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t insns = qemu_plugin_tb_n_insns(tb);

    (void)id;
    qemu_plugin_register_vcpu_tb_exec_cb(tb, tb_exec, QEMU_PLUGIN_CB_NO_REGS,
                                         (void *)(uintptr_t)insns);
}

static void vcpu_idle(qemu_plugin_id_t id, unsigned int cpu_index)
{
    (void)id;
    (void)cpu_index;
    __atomic_fetch_add(&idle_count, 1, __ATOMIC_RELAXED);
}

static void vcpu_resume(qemu_plugin_id_t id, unsigned int cpu_index)
{
    (void)id;
    (void)cpu_index;
    __atomic_fetch_add(&resume_count, 1, __ATOMIC_RELAXED);
}

static int build_report(char *report, size_t capacity)
{
    return snprintf(report, capacity,
                    "ASTERINAS_QEMU_TCG_TRACE tb=%" PRIu64
                    " insns=%" PRIu64 " idle=%" PRIu64
                    " resume=%" PRIu64 "\n",
                    __atomic_load_n(&tb_count, __ATOMIC_RELAXED),
                    __atomic_load_n(&insn_count, __ATOMIC_RELAXED),
                    __atomic_load_n(&idle_count, __ATOMIC_RELAXED),
                    __atomic_load_n(&resume_count, __ATOMIC_RELAXED));
}

static void write_report_file(const char *report)
{
    FILE *stream;

    if (trace_output_path[0] == '\0') {
        return;
    }
    stream = fopen(trace_output_path, "w");
    if (stream == NULL) {
        qemu_plugin_outs("ASTERINAS_QEMU_TCG_TRACE_FILE status=open-failed\n");
        return;
    }
    if (fputs(report, stream) == EOF || fclose(stream) != 0) {
        qemu_plugin_outs("ASTERINAS_QEMU_TCG_TRACE_FILE status=write-failed\n");
        return;
    }
    qemu_plugin_outs("ASTERINAS_QEMU_TCG_TRACE_FILE status=written\n");
}

static void plugin_exit(qemu_plugin_id_t id, void *userdata)
{
    char report[256];
    int length;

    (void)id;
    (void)userdata;
    length = build_report(report, sizeof(report));
    if (length <= 0 || (size_t)length >= sizeof(report)) {
        qemu_plugin_outs("ASTERINAS_QEMU_TCG_TRACE_FILE status=report-overflow\n");
        return;
    }
    write_report_file(report);
    qemu_plugin_outs(report);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    const char *output;

    (void)info;
    (void)argv;
    if (argc != 0) {
        return -1;
    }
    output = getenv("ASTERINAS_QEMU_TCG_TRACE_OUTPUT");
    if (output != NULL && *output != '\0') {
        size_t length = strlen(output);
        if (length >= sizeof(trace_output_path)) {
            return -1;
        }
        memcpy(trace_output_path, output, length + 1);
    }
    fprintf(stderr, "ASTERINAS_QEMU_TCG_TRACE_START\n");
    qemu_plugin_outs("ASTERINAS_QEMU_TCG_TRACE_START\n");
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans);
    qemu_plugin_register_vcpu_idle_cb(id, vcpu_idle);
    qemu_plugin_register_vcpu_resume_cb(id, vcpu_resume);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}

