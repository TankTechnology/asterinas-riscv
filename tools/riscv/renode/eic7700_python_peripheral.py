# SPDX-License-Identifier: MPL-2.0
"""IronPython peripheral adapter used by eic7700_fault_matrix.resc.

Renode loads this file for each bus transaction.  A control write at offset
0x1ffff0 selects fault bits; the GMAC-1 registers then expose the same
fail-closed values as the standalone scenario model.
"""

CONTROL = 0x1FFFF0
GMAC1 = 0x410000
MAC_VERSION = 0x0110
DMA_STATUS = 0x1008

if request.IsInit:
    faults = 0
elif request.IsWrite:
    if request.Offset == CONTROL:
        faults = request.Value
elif request.IsRead:
    if request.Offset == GMAC1 + MAC_VERSION:
        request.Value = 0xFFFFFFFF if faults & 0xB else 0x1052
    elif request.Offset == GMAC1 + DMA_STATUS:
        request.Value = 0 if not faults & 0xB else 0xFFFFFFFF
    else:
        request.Value = 0
