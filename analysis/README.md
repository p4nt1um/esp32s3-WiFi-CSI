# CSI 分析管线（M1）

Python 端 WiFi CSI 呼吸监测分析管线 —— **M2 端上算法的黄金参考实现**（方案 §5 M2a：端上 C 实现须与本管线对拍，逐窗 bpm 差 ≤ 0.2）。

## 环境

```bash
cd analysis
python -m venv .venv            # 已建好（Python 3.12）
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## 用法

```bash
PY=.venv/Scripts/python.exe

# 合成测试数据（无需硬件）
$PY -m csi_pipeline synth out.csv --duration 300 --bpm 15 --motion 180:210

# 呼吸管线（打印采样率/丢包/中位呼吸率/窗口明细）
$PY -m csi_pipeline breath out.csv

# 瀑布图（清洗后幅度热图 + 呼吸率曲线，红线为选中的敏感子载波）
$PY -m csi_pipeline waterfall out.csv out.png

# 测试（12 项：精度/门控/解析健壮性/PCA）
$PY tests/test_pipeline.py

# M2a 对拍：C 端 csi_core vs 本 Python 实现（判定一致率/bpm 差）
$PY tests/parity_check.py
```

## 管线结构（对应方案 §6，参数域见 dsp.py 注释）

```
parse.py    esp-csi CSV → (ts, seq, rssi, I/Q)；丢包率按 seq 差分
dsp.py      ⓪丢包重采样(ts插值回100Hz) ①hampel@100Hz(窗15,β3) → ②抗混叠降采样10Hz → ③去DC
            → ④聚合(top-K 频段SNR / PCA-PC1) → ⑤带通0.1–0.6Hz(butter4)
            → ⑥30s滑窗FFT(抛物线插值)+自相关交叉验证
            → ⑦功率门控（填充 power；旧快速判据）
            → ⑧相干轨迹确认（宽进严出，最终 valid：仰卧覆盖72%/侧卧可用/走动0%误报）
pipeline.py 编排 + ⑨丢包降级守卫(loss>20%挂起) + 瀑布图
synth.py    合成 CSI（已知呼吸率/体动段/野点/漂移，端到端测解析+管线）
cli.py      synth / breath / waterfall 子命令
```

## 已验证（2026-10-03，12/12 通过）

| 项 | 结果 |
|----|------|
| 15 bpm 还原 | 15.04 bpm，有效窗 96% |
| 21 bpm 还原 | 21.03 bpm |
| 体动段(180–210s)门控 | 100% 挂起；静止段 91% 保持 |
| HT40 64 子载波解析 | 通过 |
| PCA 聚合路线 | 15.04 bpm（与 top-K 相当） |

演示产物：`output/demo_15bpm.csv` + `output/demo_15bpm_waterfall.png`。

## 待硬件联调项

- 真实 CSI 的幅度分布/野点形态与 synth 的差异 → 调 Hampel 参数
- 门控基线：部署期 ≥10min 无人基线（方案 §6 校准规程），当前用功率分位自标定
- 真值对照协议（节拍器/视频计数）与 ±2bpm 命中率统计（M2b 验收）
