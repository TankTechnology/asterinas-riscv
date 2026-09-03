# SPDX-License-Identifier: MPL-2.0

SUBDIRS := \
	mmap \

ifeq ($(HOST_PLATFORM), riscv64-linux)
SUBDIRS += riscv_icache
endif

include ../common/Makefile
