# SPDX-License-Identifier: MPL-2.0

ifneq ($(HOST_PLATFORM), x86_64-linux)
C_OBJS_FILTER += \
	fault_signals \
	sigaltstack \
	signal_fpu \
	signal_rflags_df \
	signal_test \
	sigtrap
endif

ifeq ($(HOST_PLATFORM), loongarch64-linux)
# The saved-register assertion currently covers x86-64 and RISC-V 64 layouts.
C_OBJS_FILTER += signal_restart_context
endif

include ../../common/Makefile

$(OBJ_OUTPUT_DIR)/stop_continue_pending: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_workload: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_restart: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_sleep: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_wait: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_futex: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_ptrace_parent: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/group_stop_lifecycle: EXTRA_C_FLAGS += -pthread
$(OBJ_OUTPUT_DIR)/sigtimedwait_race: EXTRA_C_FLAGS += -pthread
