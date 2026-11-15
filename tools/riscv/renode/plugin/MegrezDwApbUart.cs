// SPDX-License-Identifier: MPL-2.0
// Status: prototype -- Renode peripheral for the Megrez platform.

using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.UART;

namespace Megrez
{
    /// <summary>
    /// DesignWare APB UART with the register layout the Megrez board exposes.
    ///
    /// The stock NS16550 model in wide-register mode matches the data path but
    /// has no way to inject input or a break from the monitor, which is what
    /// the serial SysRq path needs. This model exists to be driven:
    ///
    ///   sysbus WriteDoubleWord 0x10000020 1      -- inject a break
    ///   sysbus WriteDoubleWord 0x10000024 0x62   -- inject 'b'
    ///
    /// The hooks live at offsets 0x20/0x24 inside the 0x1000 window, which the
    /// kernel driver never touches.
    /// </summary>
    public class MegrezDwApbUart : UARTBase, IDoubleWordPeripheral, IKnownSize
    {
        public MegrezDwApbUart(IMachine machine) : base(machine)
        {
            IRQ = new GPIO();
        }

        public GPIO IRQ { get; }

        public long Size => 0x1000;

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case RbrThrOffset:
                    if(breakPending)
                    {
                        // The break's zero byte is a line condition; report it
                        // once and clear the cause.
                        breakPending = false;
                        UpdateInterrupts();
                        return 0;
                    }
                    if(TryGetCharacter(out var character))
                    {
                        UpdateInterrupts();
                        return character;
                    }
                    return 0;

                case IerOffset:
                    return interruptEnable;

                case IirFcrOffset:
                    return InterruptId();

                case LcrOffset:
                    return lineControl;

                case McrOffset:
                    return modemControl;

                case LsrOffset:
                {
                    uint status = (1u << 5) | (1u << 6); // THRE | TEMT
                    if(Count > 0)
                    {
                        status |= 1u; // DR
                    }
                    if(breakPending)
                    {
                        status |= 1u << 4; // BI
                        // Reading LSR clears the break indication, exactly as
                        // the real 16550 does; the driver relies on that.
                        breakPending = false;
                        UpdateInterrupts();
                    }
                    return status;
                }

                case UsrOffset:
                    return 0;

                default:
                    this.Log(LogLevel.Warning, "unhandled UART read at 0x{0:X}", offset);
                    return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case RbrThrOffset:
                    TransmitCharacter((byte)(value & 0xFF));
                    break;

                case IerOffset:
                    interruptEnable = value;
                    UpdateInterrupts();
                    break;

                case IirFcrOffset:
                    // FIFO control; this model has no FIFO.
                    break;

                case LcrOffset:
                    lineControl = value;
                    break;

                case McrOffset:
                    modemControl = value;
                    break;

                case ScrOffset:
                    break;

                case InjectBreakOffset:
                    InjectBreak();
                    break;

                case InjectByteOffset:
                    WriteChar((byte)(value & 0xFF));
                    UpdateInterrupts();
                    break;

                default:
                    this.Log(LogLevel.Warning, "unhandled UART write 0x{0:X} at 0x{1:X}", value, offset);
                    break;
            }
        }

        public void InjectBreak()
        {
            lock(innerLock)
            {
                breakPending = true;
            }
            this.InfoLog("serial break injected");
            UpdateInterrupts();
        }

        public override void Reset()
        {
            base.Reset();
            interruptEnable = 0;
            lineControl = 0;
            modemControl = 0;
            breakPending = false;
            IRQ.Unset();
        }

        public override uint BaudRate => 115200;

        public override Bits StopBits => Bits.One;

        public override Parity ParityBit => Parity.None;

        protected override void CharWritten()
        {
            UpdateInterrupts();
        }

        protected override void QueueEmptied()
        {
            UpdateInterrupts();
        }

        private uint InterruptId()
        {
            if((interruptEnable & (1u << 2)) != 0 && breakPending)
            {
                return 0x06; // receiver line status (break)
            }
            if((interruptEnable & 1u) != 0 && Count > 0)
            {
                return 0x04; // received data available
            }
            return 0x01; // no interrupt pending
        }

        private void UpdateInterrupts()
        {
            var dataReady = (interruptEnable & 1u) != 0 && Count > 0;
            var lineStatus = (interruptEnable & (1u << 2)) != 0 && breakPending;
            IRQ.Set(dataReady || lineStatus);
        }

        private uint interruptEnable;
        private uint lineControl;
        private uint modemControl;
        private bool breakPending;

        private const long RbrThrOffset = 0x00;
        private const long IerOffset = 0x04;
        private const long IirFcrOffset = 0x08;
        private const long LcrOffset = 0x0C;
        private const long McrOffset = 0x10;
        private const long LsrOffset = 0x14;
        private const long ScrOffset = 0x1C;
        private const long UsrOffset = 0x7C;
        private const long InjectBreakOffset = 0x20;
        private const long InjectByteOffset = 0x24;
    }
}
