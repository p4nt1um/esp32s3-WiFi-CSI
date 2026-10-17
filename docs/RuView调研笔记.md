# RuView 调研笔记（docs/RuView调研笔记.md）

> 调研日期：2026-10-16 ｜ 方式：稀疏检出源码（python/、firmware/esp32-csi-node/、docs/、references/、v2/ 共 70+ crate）+ 文档精读
> 目的：与本项目 WiFi CSI 呼吸/睡眠管线对拍，评估可借鉴算法（只调研，不引入代码）
> 仓库：github.com/ruvnet/RuView（MIT，~413MB，声称 97.2k stars，1513 commits）

## 0. 一句话结论

RuView 是一个**工程完成度高、算法并不神秘、营销远超实测**的 CSI 感知平台。其核心 DSP（EMA 残差预处理、谐振器带通、过零/自相关计频、圆方差、P95 自适应归一化）均为经典手法，与我们的管线同源同代；**对我们最有价值的是三样：置信度设计（周期规律度×覆盖度）、相位方差空房判别、以及他们"诚实清单"里标出的坑**。他们没有任何呼吸/心率的真值对照精度——**这一点上我们的验证（M2b 验收 MAE 0.46/0.78 bpm、相干轨迹确认真值闭环 MAE 0.30）反而领先**。

## 1. 项目概况与可信度

- 结构：Rust 核心（v2/ 下 70+ crate、3128 个 Rust 测试）+ Python 绑定（PyPI `wifi-densepose`，Rust 编译 wheel）+ ESP32 固件（esp32-csi-node，S3/C6）+ 服务器（sensing-server）+ 一堆 AI 生成痕迹目录（.claude-flow/.swarm/PROOF.md/WITNESS-LOG）
- **有实测支撑**：MM-Fi 姿态 PCK@20 82.69%（公开可复跑）；presence 对比编码器 82.3%（held-out triplet，"100%"已因单类录音撤回）；9 节点 seq 门控配对率 2.3× 实测；C6 占用完整性 fail-closed 真机验证；50Hz self-ping 为硬件实测安全上限
- **自己承认没验证的**（PROOF.md:61-69、ADR-157:145 等）：呼吸/心率 MAE ~2-3bpm 属 DATA-GATED CLAIMED（"需要我们没有的 PPG/ECG 真值"）；WiFi 命名身份测得不可行（gap 0.0005）；边缘医疗技能 UNVALIDATED；多人计数是 slot-capacity 启发式；跨环境 zero-shot 掉到 17-64%
- 呼吸暂停模块大字声明 EXPERIMENTAL / NOT A MEDICAL DEVICE，藏在非默认 feature 后

## 2. 架构对拍总表

| 维度 | 本项目 | RuView | 点评 |
|---|---|---|---|
| 照射源 | 专用 TX + 双 RX 固定信道，相干链路 | 无专用 TX：嗅探环境帧 + 对自家网关 self-ping（50Hz 上限） | 我们的相位质量天花板更高；他们部署门槛更低 |
| 采样率 | 设计值 100Hz（链路保证） | 实测跟踪 8-60Hz EMA（被动源不可控） | 他们的 fs 自适应代码（漂移>15% 重设计滤波器）值得抄思路 |
| 处理位置 | 全服务器端（MQTT→后端） | 固件端分级 DSP（tier0 透传→tier2 压缩） | 他们端上做完全套生命体征；我们暂不需要 |
| 传输 | MQTT JSON | UDP 二进制包（0xC511 头） | 我们带宽富余，暂不构成瓶颈 |
| 输出 | bpm/valid/ratio/功率 落 SQLite | 1Hz vitals 包（BR×100、HR×10000、flags、n_persons） | 字段设计类似 |
| 多人 | 双链"链路主人"+M4 规划服务器多峰 | P95 归一化+滞回查表+dedup_factor；固件 top-k/2 分组 | 他们自己承认只是"子载波多样性"不是真占用——**我们的双链物理交叉思路理论上限更高** |

## 3. 呼吸算法逐项对拍（核心章节）

RuView 有三套实现，主路径是 Rust 核心 `v2/crates/wifi-densepose-vitals/src/breathing.rs`：

| 环节 | 本项目（csi_pipeline/dsp.py） | RuView 主路径 | 对拍结论 |
|---|---|---|---|
| 野点/预处理 | Hampel(窗15,3σ)@100Hz + 滑窗均值去DC(10s) | 逐子载波 EMA 残差（α=0.05，即 ~20s 时间常数） | 目的同（去静态分量）；我们多一层野点剔除。EMA 计算更便宜，效果对拍过再说 |
| 子载波聚合 | 呼吸带能量占比 top-8 均值 / PCA-PC1 | 加权残差融合（权重缺失回退均匀） | 我们的 top-k（WiRM）选择性更强；他们无选择（或固件端 Welford 方差 top-8——**与我们思路一致**） |
| 带通 | Butterworth 4阶 0.1-0.6Hz 零相位双通 | 2阶谐振器 IIR 0.1-0.5Hz（极点半径=带宽控制，NaN 自愈） | 等效；我们的零相位双通无相位畸变，对拍精度更好 |
| 频率估计 | 30s 窗 FFT 峰(抛物线插值) + 自相关 **双估计交叉验证** | 单一过零点计数 f=crossings/(2·T)；出带即整窗重置 | **我们的双估计+agreement 更鲁棒**（过零法对单噪声脉冲敏感，他们靠整窗重置兜底） |
| 置信/门控 | 两级门控（功率 floor×8/×0.3 + 峰值比≥3.5）+ 相干轨迹确认（90s 邻域≥5 候选 ±0.5bpm） | **置信度=周期规律度×覆盖度**：信号与平移一个估计周期的副本做 Pearson 相关 × clamp(周期数-1,0,1) | **最值得借鉴的单点**。我们的 peak_ratio 是谱形态指标，他们的是时域周期性指标——正交维度，可并联增强假峰剔除 |
| 质量分级 | 无（valid 布尔） | conf≥0.7 Valid / ≥0.4 Degraded / else Unreliable | 三级分级比布尔更适合夜间长时段展示，成本低可加 |

**固件端版本**（edge_processing.c，20Hz）：atan2 相位→解缠→Welford 方差选 top-8→双 biquad→过零+**自相关谐波拒斥**（k=1..6、±8% 拒斥带、best/r0<0.2 拒绝）→HR 13 点中值平滑。其中"呼吸谐波拒斥"与我们 FFT+AC 一致性检查目的相同（防把呼吸的 2/3 次谐波当心率）。

## 4. 心率算法（我们暂无此功能）

- 输入不是幅度而是**相位相干加权残差**：相邻子载波相位差越小权越大，coherence=exp(-|θᵢ₊₁-θᵢ|)（heartrate.rs:302-339）
- 两级级联 2 阶谐振器 0.8-2.0Hz（第二级专压呼吸泄漏）+ 自相关峰估频 + **第二级证据封顶置信度**（滤波器不能把噪声"滤成"可信）
- 15s 窗、最少 5s；服务器版则用**跨子载波 Mardia 圆方差的时间序列**当心搏代理信号（V=1-R̄，R̄=|Σe^{jθ}|/n）再 FFT——思路清奇：心跳引起多径微扰→相位弥散度起伏
- **评估结论：值得列 M4.5 实验，但预期要压低**。理由：①RuView 自己零真值验证；②CSI 心率位移量级 ~0.5mm（呼吸的 1/20），对链路几何极敏感；③我们优势是睡眠场景（静止+长观察）恰好是 CSI 心率文献里唯一靠谱的场景。落地路径：在 100Hz 原始流上（不经 10Hz 降采样）加 0.8-2Hz 支路，先只对"相干轨迹确认通过且功率平稳"的深睡窗输出，与第二天晨起脉搏对照

## 5. 呼吸暂停对拍（我们已有探索性代理）

| | 本项目（sleep_report.py） | RuView（med_sleep_apnea.rs） |
|---|---|---|
| 判据 | 呼吸带功率塌缩 ≤30% 滚动中位 + raw_std 不变（非体动）+ 低丢包 | **BPM<4.0 持续≥10s** → 事件；每 300s 汇总 AHI 代理 |
| 生理逻辑 | 信号消失=呼吸停 | 估计器输出低率=呼吸停 |
| 弱点 | 塌缩也可能是侧睡信号弱（我们已在文档标注） | 呼吸停时估计器往往先失效/漂移，BPM 到不了 <4；且 4bpm 边界恰在估计带外缘 |
| 定位 | 探索性代理，非医学 | 大字 EXPERIMENTAL，非默认 feature |

**对拍结论：我们的功率塌缩代理生理上更合理**。可借鉴他们两点：①事件状态机 + **恢复即计事件时长**（我们目前只标候选段）；②AHI 代理口径（事件数/小时）直接对齐医学习惯，报告可读性好。

## 6. 存在检测 / 空房假峰（直接命中我们的痛点）

RuView 无模型回退：最近 20 帧相位的线性方差当 presence_score，前 1200 帧做环境校准（均值+3σ 为阈值），Schmitt 触发迟滞（低阈=0.5×高阈，连续 5 帧低于低阈才清除）。主机侧 600 帧硬下限 Welford + von Mises 圆统计。

**与我们空房假峰签名（ratio<2.5+低功率+频率游走）的关系**：他们看的是"相位方差"这一**原始层特征**，我们看的是"谱形态+轨迹"这一**估计层特征**。两层正交，叠加可显著压假阳性。**落地建议（评估①结论）**：不替换、做并联增强——在 sleep_report 的假峰后验剔除里加一个"相位方差通道"：夜间长窗（如 5min）相位方差低于空房基线上限的窗直接判无效。前提：需要固件或解析层输出 I/Q→wrapped phase（我们的采集器存了原始 I/Q，`parse.py` 已解析幅度，加相位列是中等工作量）。**先做离线回放验证**（用已有的空房基线 jsonl + 两夜数据对比假阳性数）再决定进不进在线管线。

另注：他们的 presence 模型法（AETHER 对比编码器 82.3%）我们不需要——单房间单/双人场景，规则法足够，模型反而引入跨环境迁移坑（他们自己 zero-shot 掉到 17-64%）。

## 7. 多人检测（我们 M4 规划）

- 服务器：4 特征（variance/change_points/motion_band/spectral）各自 P95 自适应归一化→加权 0.40/0.20/0.25/0.15→EMA→**滞回查表**（score>0.85→3 人等）→多节点 count=ceil(Σ/dedup_factor=3.0)
- 固件：top-k 子载波分 k/2 组（≤4 人），能量门 0.35×最强组 + 空间隔≥4 + 连续 3 帧去抖
- **他们 README 自己承认：固件多人是"slot-capacity heuristic，追踪子载波多样性而非真实占用"**；无真正盲源分离
- **对 M4 的启示**：①滞回+去抖这些工程细节直接抄；②他们的"多特征 P95 归一化加权"可作为我们双链交叉判据之外的第三证据；③真分离仍需走我们原计划的服务器端多峰提取（双链各出一峰+交叉验证），RuView 没有解决它

## 8. 固件细节对比（esp32-csi-node vs 本项目 rx_collector）

| 项 | 本项目 | RuView | 备注 |
|---|---|---|---|
| CSI 源 | 专用 TX 定向发包 | self-ping 网关 + 嗅探 | 相位相干性我们占优 |
| 信道 | 固定 | 跳表{1,6,11,36,40,44} dwell 50ms，**默认不跳** | 他们默认单信道印证我们固定信道的稳定性选择 |
| 速率 | 100Hz | self-ping 50Hz 硬上限（超限 WiFi 崩溃，编译期 #error） | 我们的专用链路 100Hz 是差异化优势 |
| 帧消毒 | — | first_word_invalid 时前 4 字节 CSI 置零 | 低成本防御，可加 |
| fs 处理 | 设计值 | 1s 窗计数+EMA(0.25) 实测跟踪，夹 [8,60]Hz；漂移>15% 重设计滤波器 | 若未来引入非专用源可参考 |

## 9. 可采纳清单（按价值/成本排序）

1. **置信度双通道**（高价值/低成本）：在 peak_ratio 之外增加"周期规律度×覆盖度"时域置信度，与现有相干轨迹确认并联 → 假峰剔除与质量分级同时受益。改 `dsp.py` estimate_breath + sleep_report，~半天
2. **暂停事件状态机 + AHI 口径**（高价值/低成本）：sleep_report 的暂停候选段加"恢复即闭合"逻辑与事件/小时统计。~2h
3. **相位方差空房通道**（中价值/中成本）：parse 层加 wrapped phase 列，sleep_report 假峰剔除并联相位方差判据，先离线回放对比假阳性。~1-2 天
4. **质量三级分级**（低成本）：valid → Valid/Degraded/Unreliable，报告与卡片展示更诚实。~2h
5. **HR 实验（M4.5）**（探索）：100Hz 流上 0.8-2Hz 支路+相位相干加权，仅深睡稳窗输出，晨起脉搏对照。预期成功率中低，一周内出 go/no-go
6. 固件帧消毒（first_word_invalid 置零）等小防御项，顺手项

## 10. 明确不建议照搬的

- **被动嗅探/self-ping 架构**：牺牲相位质量换部署便利，与我们已有专用 TX 硬件优势背道而驰
- **AETHER 对比学习 presence**：跨环境迁移差（自己测的 17-64%），单人房场景规则法够用
- **任何姿态/穿墙模型**：他们 PCK 3% 的教训就在那
- **过零单估计**：我们 FFT+AC 双估计已是上位替代
- Rust 核心/UDP 协议等基础设施：为重写而重写，无收益

## 11. 索引

- 源码检出：`%TEMP%\RuView-research\`（python/src、v2/crates/wifi-densepose-vitals|sensing-server、firmware/esp32-csi-node、docs/）
- 关键文件：`v2/crates/wifi-densepose-vitals/src/{breathing,heartrate,preprocessor}.rs`、`v2/crates/wifi-densepose-sensing-server/src/{vital_signs,main,semantic/distress}.rs`、`v2/crates/wifi-densepose-wasm-edge/src/med_sleep_apnea.rs`、`firmware/esp32-csi-node/main/{csi_collector,edge_processing}.c`、`docs/{proof-of-capabilities,calibration-guide,csi-frame-selection-measured}.md`
- 本项目对应物：`analysis/csi_pipeline/dsp.py`、`backend/app/breath_engine.py`、`analysis/tools/sleep_report.py`、`firmware/rx_collector/main/app_main.c`
