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

include ../../common/Makefile

$(OBJ_OUTPUT_DIR)/stop_continue_pending: EXTRA_C_FLAGS += -pthread
