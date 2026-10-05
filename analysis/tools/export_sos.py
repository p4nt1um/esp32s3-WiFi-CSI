"""导出带通 SOS 系数为 C 数组（嵌入 firmware/components/csi_core/src/csi_core.c）。

改带通参数（fs/频带/阶数）后运行本脚本，把输出贴回 csi_core.c 的 k_sos，并重跑对拍。
"""
from scipy.signal import butter

FS = 10.0
BAND = (0.1, 0.6)
ORDER = 4

if __name__ == "__main__":
    sos = butter(ORDER, BAND, btype="bandpass", fs=FS, output="sos")
    print(f"/* butter({ORDER}, {BAND}, fs={FS}) */")
    for row in sos:
        print("    {" + ", ".join(f"{v:.17g}" for v in row) + "},")
