"""串口工具：发控制台命令 + 采集原始输出（esp-csi 固件联调用）。

用法：
  serial_io.py COM13 2000000 5 --cmd info                 # 短交互，打印全部输出
  serial_io.py COM12 2000000 60 --out cap.csv             # 采集 CSI 到文件
  serial_io.py COM12 2000000 20 --cmd stats --interval 8  # 周期发命令
"""
from __future__ import annotations

import argparse
import sys
import time

import serial


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("baud", type=int)
    ap.add_argument("seconds", type=float)
    ap.add_argument("--cmd", default=None, help="启动后发送的控制台命令")
    ap.add_argument("--interval", type=float, default=0, help="周期重发命令的间隔秒数")
    ap.add_argument("--out", default=None, help="原始输出保存文件")
    ap.add_argument("--reset", action="store_true", help="打开端口后拉 DTR/RTS 复位芯片（抓启动日志）")
    args = ap.parse_args()

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.5
    ser.rts = False          # 打开前压低 DTR/RTS：S3 USB-Serial-JTAG 模拟自动复位电路，
    ser.dtr = False          # 默认的 DTR/RTS 置位会把 TX 板复位（信道等 RAM 配置丢失）
    ser.open()
    time.sleep(0.3)
    ser.reset_input_buffer()             # 先清残留，再复位——否则会把启动日志洗掉
    if args.reset:                       # esptool 硬复位序列：仅拉 RTS（DTR+RTS 同拉会进下载模式）
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.1)
        ser.setRTS(False)

    if args.cmd:
        ser.write((args.cmd + "\r\n").encode())
        ser.flush()

    buf = bytearray()
    t0 = time.time()
    next_cmd = t0 + args.interval if args.interval > 0 else None
    try:
        while time.time() - t0 < args.seconds:
            chunk = ser.read(65536)
            if chunk:
                buf.extend(chunk)
            if next_cmd and time.time() >= next_cmd:
                ser.write((args.cmd + "\r\n").encode())
                ser.flush()
                next_cmd += args.interval
    finally:
        ser.close()

    raw = bytes(buf)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(raw)
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    csv_lines = [l for l in lines if l.startswith("CSI_DATA,")]
    other = [l for l in lines if not l.startswith("CSI_DATA,")]
    print(f"[total {len(raw)}B, {len(lines)} lines, CSI_DATA {len(csv_lines)}, other {len(other)}]")
    for l in other[:40]:
        print(l)
    if not args.out:
        for l in csv_lines[:3]:
            print(l[:160] + ("..." if len(l) > 160 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
