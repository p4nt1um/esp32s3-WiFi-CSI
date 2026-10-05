"""信道切换修复的实机验证脚本（自带超时与端口清理，勿在 REPL 内联跑）。"""
import sys
import time

sys.path.insert(0, ".")

import serial

PY = sys.executable


def cmd(port, baud, command, wait=1.5):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.rts = False
    ser.dtr = False
    out = b""
    try:
        ser.open()
        time.sleep(0.2)
        ser.reset_input_buffer()
        ser.write((command + "\r\n").encode())
        t0 = time.time()
        while time.time() - t0 < wait:
            out += ser.read(8192)
    except Exception as e:  # noqa: BLE001
        return f"<ERR {e}>"
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
    return out.decode(errors="replace")


def rx_flow(seconds=4.0):
    """读 COM12 统计 CSI 行速率"""
    ser = serial.Serial()
    ser.port = "COM12"
    ser.baudrate = 2000000
    ser.timeout = 0.2
    ser.rts = False
    ser.dtr = False
    n = 0
    try:
        ser.open()
        time.sleep(0.2)
        ser.reset_input_buffer()
        t0 = time.time()
        buf = b""
        while time.time() - t0 < seconds:
            buf += ser.read(1 << 16)
        n = buf.count(b"CSI_DATA,")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
    return n, f"{n / seconds:.0f}/s"


if __name__ == "__main__":
    print("1) TX info:")
    print("   " + " | ".join(l for l in cmd("COM13", 115200, "info").splitlines() if "rate=" in l))
    print("2) RX 对齐信道 1:")
    print("   " + " | ".join(l for l in cmd("COM12", 2000000, "channel 1").splitlines() if "channel" in l))
    n, rate = rx_flow()
    print(f"3) ch1 链路: {rate}")
    print("4) TX 切信道 6（关键修复验证）:")
    print("   " + " | ".join(l for l in cmd("COM13", 115200, "channel 6").splitlines() if "channel" in l))
    n, rate = rx_flow(3)
    print(f"   TX在6/RX在1 → 应无包: {rate}")
    print("5) RX 跟到 6:")
    print("   " + " | ".join(l for l in cmd("COM12", 2000000, "channel 6").splitlines() if "channel" in l))
    n, rate = rx_flow(4)
    print(f"   双板在6 → 应恢复: {rate}")
    print("6) 切回 1:")
    cmd("COM13", 115200, "channel 1")
    cmd("COM12", 2000000, "channel 1")
    n, rate = rx_flow(4)
    print(f"   双板回1 → {rate}")
