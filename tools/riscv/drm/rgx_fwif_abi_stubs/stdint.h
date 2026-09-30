// SPDX-License-Identifier: MPL-2.0
#ifndef RGX_PROBE_STDINT_H
#define RGX_PROBE_STDINT_H
typedef __UINT8_TYPE__ uint8_t;
typedef __INT8_TYPE__ int8_t;
typedef __UINT16_TYPE__ uint16_t;
typedef __INT16_TYPE__ int16_t;
typedef __UINT32_TYPE__ uint32_t;
typedef __INT32_TYPE__ int32_t;
typedef __UINT64_TYPE__ uint64_t;
typedef __INT64_TYPE__ int64_t;
typedef __UINTPTR_TYPE__ uintptr_t;
typedef __INTPTR_TYPE__ intptr_t;
#define UINT8_C(x) x##U
#define UINT16_C(x) x##U
#define UINT32_C(x) x##U
#define UINT64_C(x) x##UL
#define INT8_C(x) x
#define INT16_C(x) x
#define INT32_C(x) x
#define INT64_C(x) x##L
#endif
