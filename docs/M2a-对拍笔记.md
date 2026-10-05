# M2a 笔记 — C 端呼吸算法核心与黄金实现对拍（2026-10-03）

> 对应《项目实施方案》§5 M2a。**验收结果：4/4 合成用例，判定一致率 100%、逐窗 bpm 差 0.000（逐位一致），远超验收线（≥99% / ≤0.2bpm）。** M1 回归 12/12 仍全绿。M2b（真值实验达标）待硬件。

## 1. 交付物

| 模块 | 位置 | 说明 |
|------|------|------|
| **csi_core 算法核心** | [firmware/components/csi_core](../firmware/components/csi_core/) | 平台无关纯 C（double 精度），七步管线与 `analysis/csi_pipeline/dsp.py` 严格同构；IDF 组件形式接入固件 |
| PC 对拍工具 | `components/csi_core/pc_main.c` → `build/csi_pc.exe` | 读 esp-csi CSV → 输出逐窗结果 CSV；zig cc 编译（`pip install ziglang`，`python -m ziglang cc`） |
| SOS 系数导出 | [analysis/tools/export_sos.py](../analysis/tools/export_sos.py) | 带通系数改参数后重新生成 C 数组 |
| 对拍脚本 | [analysis/tests/parity_check.py](../analysis/tests/parity_check.py) | 4 个合成用例（静止单频/体动/异频/异种子），比对判定一致率与逐窗 bpm 差 |
| 固件 replay 模式 | [firmware/rx_collector/main/replay.c](../firmware/rx_collector/main/replay.c) | UART1（GPIO5，921600）注入 CSV → 板上跑同一份 csi_core → 控制台输出逐窗结果；固件级对拍与参数回归工具 |
| 中间量调试器 | `components/csi_core/dbg_ratios.c` | 直接 include csi_core.c 访问内部步骤，导出各级中间矩阵二进制（对拍排障用） |

## 2. 复现命令

```bash
# PC 工具编译
cd firmware/components/csi_core
analysis/.venv/Scripts/python.exe -m ziglang cc -O2 -Iinclude src/csi_core.c pc_main.c -lm -o build/csi_pc.exe

# 对拍（验收）
cd analysis && .venv/Scripts/python.exe tests/parity_check.py

# 单文件跑 C 端
csi_core/build/csi_pc.exe <in.csv> <out_windows.csv>

# Python 侧同格式导出
python -m csi_pipeline breath <in.csv> --dump-windows out.csv

# 板上回放（到货后）：控制台 replay → UART1 发 CSV → 发 END
```

## 3. 对拍过程揪出的 7 个真实 bug（这就是黄金对拍的价值）

| # | bug | 位置 | 症状 |
|---|-----|------|------|
| 1 | remove_dc 前缀和越界 1 位（读 pre[n+1]） | C | 尾部 ~50 样本读到垃圾，谱被抹平、topk 失效 |
| 2 | **scipy.ndimage 'reflect' ≠ np.pad 'reflect'**（前者重复边缘=pad 的 symmetric，后者不重复） | 概念 | Hampel 边界行为两侧不一致 |
| 3 | 算法弱点：去DC补零边缘瞬态 + 矩形窗泄漏，带内低沿裙边压过呼吸线 | 双侧 | seed42 用例全窗锁死在 7.03bpm（带边沿）|
| 4 | hampel 就地修改，窗口读到已替换样本，级联污染 | C | 与向量化实现全矩阵差异 maxΔ=76 |
| 5 | MAD 定义错位：窗口内相对"中心点中位数"的偏差 vs scipy 的"偏差图再窗口中值" | C | 替换数 250 vs 229 |
| 6 | **Windows 上 `long` 是 32 位**，幅值×1e9 溢出回绕 | C | 峰值选择全乱、全窗无效 |
| 7 | 自相关缓冲区复用，lag>16 的相关读到先前写入的 AC 值（自污染） | C | motion 用例 bpm_ac 选错谐波 |

另修复环境/工程问题：IDF 5.5 组件名为 `console`/`mqtt`（非 esp_console/esp_mqtt）；main 组件加 PRIV_REQUIRES 后失去隐式全依赖需显式列出；一次失败的配置把 build 目录污染成 VS 生成器缓存（删除重建）；Defender 拦截编译进程仍靠 ninja 断点续编+重试循环（本轮 rx 又中一次 cc1 拒绝访问）。

## 4. 为达成可对拍而做的算法决策（双侧对称修改，M1 测试保持绿）

1. **降采样**：块平均（确定性）替代 scipy.decimate IIR。
2. **带通**：双通零相位（正向 sosfilt → 反转 → 再滤 → 反转）替代 sosfiltfilt 奇延拓。
3. **去 DC**：reflect 填充（np.pad `symmetric`，与 scipy.ndimage reflect 同约定）替代补零卷积——修复 bug#3 的边缘瞬态。
4. **topk 比值谱**：加 hann 窗抑制泄漏裙边（bug#3 的另一半）；排序键量化 1e-4、平手按索引升序——消除 FFT 累加次序差（~1e-12 相对）造成的近并列翻转。
5. **峰值选择**（窗口 FFT 与自相关两处）：相对量化（基准=带内最大，粒度 1e-10 相对）并列取低 bin/lag——绝对量化在大幅值窗（体动）粒度细于噪声，必须用相对粒度。
6. **精度**：C 内部全 double（最初 float32 的"真实性"让位于对拍契约；板上呼吸管线计算量 ~1 窗/秒，软双精度开销可忽略；S3 双精度软模拟，实时性由 10Hz 域小窗保证）。

## 5. 结构说明

- csi_core 输入为 **int16 I/Q**（固件/CSV 天然形态），幅度提取在核心内完成。
- 离线块处理 API（`csi_core_run`）；流式化（10Hz 增量喂入）留到 M2b 硬件联调时按 FreeRTOS 任务边界改造。
- replay 缓冲上限 `RX_REPLAY_MAX_SAMPLES`（默认 4000=40s，PSRAM 优先、内部 RAM 兜底）。

## 6. M2b 待办（硬件到货后）

1. 烧录新 rx_collector（含 csi_core+replay），先 replay 一段合成 CSV 在**板上**复现 PC 对拍结果（固件级第三重验证）。
2. 真值实验（节拍器/视频计数）：静止单人 MAE ≤ 2bpm 验收；走动挂起 ≥90%。
3. 校准规程上板：无人基线 ≥10min、阈值"基底+k·σ"入 NVS。
4. 流式化改造 + MQTT 上行呼吸结果（topic `csi/rx1/breath`）。
