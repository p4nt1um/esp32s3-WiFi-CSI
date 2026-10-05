# ESP32-S3 × WiFi CSI 家庭呼吸/活动监测系统

非接触、无摄像头、无穿戴的家庭人体感知：利用 WiFi CSI（Channel State Information）监测室内 1–2 人的**呼吸率**与**活动状态**。整套系统仅由 3 块 ESP32 开发板（1 发 2 收，只需供电）+ 一台运行 MQTT broker 与轻量后端的机器组成，呼吸率精度经真人真值对照验收：**MAE 0.46–0.85 bpm**（验收线 ≤ 2 bpm）。

> 定位：日常趋势监测，非医疗级。走动/体动期间正确挂起输出（97%）而非给出错误读数，是本系统的设计原则之一。

## 1. 系统架构

```
┌───────────────── 家庭现场（仅供电，无上位机）─────────────────┐
│                                                              │
│  [TX 板 ESP32-S3]        [RX1 板 ESP32-S3]   [RX2 板 ESP32]  │
│  ESP-NOW 100Hz 广播  →   CSI 提取+增益补偿    CSI 提取        │
│  HT20 / ch1-11           环形缓冲→10Hz 聚合   （对照链路）     │
│  信道 NVS 持久化               │                    │        │
└────────────────────────────────┼────────────────────┼────────┘
                        WiFi（经家用路由器，仅出站连接）
                                 │                    │
                                 ▼                    ▼
                    Mosquitto broker（批量 MQTT：10 样本/条 × 1Hz）
                                 │
                                 ▼
              FastAPI 后端： BreathEngine（60s 滑窗呼吸管线）
                 ├─ SQLite 落库（呼吸结果 + 有效性标记）
                 └─ Web 仪表盘：双 RX 呼吸曲线 + CSI 瀑布图
```

**呼吸算法双实现**：同一套九级管线既有板端 C 版（`firmware/components/csi_core`，可脱网自治），也有服务器 Python 版（`backend/app/breath_engine.py`）；两者通过合成数据对拍验收（4/4 用例逐位一致），C 版兼作固件回归基准。

## 2. 仓库结构

```
├── firmware/
│   ├── tx_sender/          TX 板固件：ESP-NOW 100Hz 发包，rate/channel 控制台命令
│   ├── rx_collector/       RX1 固件（S3）：CSI 采集 + 增益补偿 + MQTT 批量上行 + replay 对拍
│   ├── rx2_collector/      RX2 固件（原版 ESP32）：简化对照链路
│   └── components/csi_core/  平台无关纯 C 呼吸算法（与 Python 金标准严格同构）
├── analysis/               Python DSP 管线（金标准实现）+ 合成数据测试 + 对拍脚本
├── backend/                FastAPI + SQLite + paho-mqtt + 原生 canvas 仪表盘
├── docs/                   项目方案 v1.1 与 M0–M3 里程碑笔记（含完整排障记录）
└── idf.bat                 ESP-IDF 构建包装脚本（Windows）
```

## 3. 开发历程

项目从方案定稿到真人验收通过历时 3 天（2026-10-03 → 10-06），按里程碑推进，全过程笔记在 `docs/`。

### M0 环境与上游验证（10-03）
ESP-IDF v5.5.2 Windows 环境踩坑（Git Bash 的 MSYSTEM 泄漏、ccache 间歇失败 → `--no-ccache` 固化进 `idf.bat`）；编译 espressif/esp-csi 官方例程确认工具链可用；从上游源码定稿 CSI 配置口径。

### M1 数据工具链（10-03，硬件未到先用合成数据）
自研三份固件骨架；Python 分析管线用合成数据 12/12 测试通过（含 15→15.04 bpm、21→21.03 bpm、体动段 100% 挂起）。

### M2a 板端算法与黄金对拍（10-03–10-05）
管线移植为纯 C（double 精度、量化平局判决保证 C/Python 一致）。**对拍过程揪出 7 个真实 bug**——包括 `long` 在 Windows 是 32 位导致幅值溢出、Hampel 就地修改级联污染、scipy 与 numpy 的 'reflect' 填充语义差异等，最终 4/4 用例判定一致率 100%、逐窗 bpm 差 0.000（验收线 ≥99% / ≤0.2 bpm）。

### M2b 真人验收（10-05–10-06，床上场景）
- 上板排障：`CONFIG_ESP_WIFI_CSI_ENABLED` 不设则 CSI API 全部裸 ESP_FAIL（原厂例程 defaults 里有但文档无说明，本次最大排障成果）；实测定稿子载波口径（HT20 → 64 复数值，学术口径 52 有效）；TX 板"疑似硬件损坏"实为 flash 数据损坏，`erase_flash` 完全恢复。
- 摆位三次才成功，验证了 Fresnel 区几何约束：人不在收发连线上 → 门控零误报；连线过胸腹 → 峰值比 3.87、65% 有效窗。
- **验收结果**：静止单人 MAE 0.74 bpm（仰卧 0.49 / 薄被 1.02）；走动挂起 97%。
- 验收后新增 **⑧相干轨迹确认 + ⑨丢包守卫** 两级管线：仰卧有效率 26%→72%，侧卧从不可用变为可用（MAE 0.85），走动特异性 100%。
- 教训沉淀：灵敏度参数（峰值比 3.5→3.0）调低后覆盖率翻倍但走动挂起率破线——**调灵敏度必须同时复检特异性**，已回退并留档。

### M3 后端与无线化（10-03 骨架，10-06 联调）
FastAPI + SQLite + 仪表盘先以设备模拟器端到端打通（10/10 通过）；真机联调解决 MQTT 三大问题：**10Hz 直发与 100Hz CSI 抢空口导致 46% 丢消息 → 批量打包 1Hz×2KB 后 100% 到达**；**发布任务 8KB 栈上缓冲区溢出 → 改 malloc + 栈扩至 12KB**；**看门狗误重启 → 改为"有 CSI 数据且发布失败"才计数**（TX 关机不应触发）。仪表盘扩展为双 RX 对照（呼吸曲线 + Z-score 归一化 CSI 瀑布图），本地 broker 由 amqtt 换为 Mosquitto。

## 4. 核心算法管线（九级）

输入 CSI（100Hz × 64 子载波，幅度）：

```
① 丢包重采样     按时间戳线性插值回均匀网格（真实链路 6–25% 随机丢包，
                 块平均跨洞会混叠波形展宽 FFT 谱线）
② Hampel 去野点  窗 15（0.15s@100Hz）× β=3 —— 必须先去野点后滤波，
                 冲激野点被低通展宽后无法剔除
③ 低通 + 降采样  100Hz → 10Hz（呼吸带宽 < 0.6Hz）
④ 去 DC + 剔死载波  滑窗均值扣除；std==0 的填充子载波剔除
⑤ 子载波聚合     呼吸频段 DFT 信噪比 top-K 求平均
⑥ 带通           0.1–0.6 Hz（6–36 bpm），4 阶 Butterworth
⑦ 频率估计       30s 滑窗 FFT（抛物线插值峰值）+ 自相关周期交叉验证，
                 双法一致才输出
⑧ 相干轨迹确认   宽进（两法一致 & 峰值比≥2.0 & 功率非尖峰）
                 → 严出（±45s 邻域候选≥5 且与中位差≤0.5bpm）
                 —— 呼吸窗锁定同一频率轨迹，体动/干扰伪窗散乱出局
⑨ 体动门控 + 丢包守卫  峰值比≥3.5（真人标定）；loss>20% 挂起输出
                 （拥塞链路的重采样伪影会形成假相干轨）
```

输出：bpm + valid + confidence，每 10s 经 MQTT 上报。

## 5. 论文技术点

本项目的算法与工程决策均有论文出处，关键借鉴如下（完整 15 篇知识库见上游调研文档）：

| 技术点 | 来源论文 | 借鉴内容 |
|--------|----------|----------|
| 呼吸管线主体 | **Serene**（Kamran Ali, 2020）：真实家庭 80 晚 >550h、55% NLOS，全夜中位误差 1.19 bpm | 差分/去 DC 消静态多径、10–40bpm 带通、峰值检测 + 60s 滑窗输出、体动期间挂起而非硬算的哲学；其"睡姿/距离/同住者干扰"失效边界分析直接塑造了本项目的门控设计 |
| 体动门控 | **Vital-Radio**（Fadel Adib, CHI 2015）：MIT 隔墙生命体征监测 | "准静态窗口"思想——只在信号周期性显著时输出呼吸率，其余时间挂起；FFT 峰值功率 ≥ k×谱均值的显著性判据（本项目实测标定 k=3.5） |
| 子载波聚合 | **WiRM**（James Rhodes, 2025） | 按呼吸频段 DFT 强度选子载波（top-K 思路）；其 9.9Hz 降采样证明 10Hz 对呼吸任务足够 |
| FFT+自相关双估计 | **TensorBeat**（Xuyu Wang, ACM TIST 2017） | 频域/时域双法交叉验证；其 CP 张量分解多人呼吸分离留作本项目 M4 双人场景方案 |
| Hampel 参数域 | **Avola**（Danilo Avola, 2025）：ESP32 @100Hz/52 子载波实测 | 窗 15 / β=3 参数（100Hz 域）；验证了 ESP32 平台 CSI 感知可行性 |
| 硬件参数定标 | **CSI-Bench**（Guozhen Zhu, NeurIPS 2025）、**CrossFi**（Zijian Zhao） | ESP32-S3 @20MHz 64 CSI 值的口径（本项目实测证实）；"仅幅度足以支撑呼吸任务"策略；OOD 跨域骤降的教训 → 本项目评估集强制含跨天/跨位置样本 |
| 感知几何 | **Tan 十年综述**（IEEE IoT-J 2022）、**Yousefi 综述**（IEEE Comm. Mag. 2017） | Fresnel 区模型指导 TX/RX/人体摆位（连线过胸腹、人不在链路上则零有效窗——本项目实测印证）；小尺度活动 = 呼吸监测的信号处理路线索引 |
| 后续路线（M3 后半/M4/M5） | **SenseFi**（Patterns 2023，浅层 CNN-5/GRU 精度-效率最优）、**CrossFi**（1-shot 跨域重建模板 91.72%）、**DATTA**（WACV 2026 TTA）、**WiMANS**（ECCV 2024，多人 HAR 仅 60–65% → 收紧类别）、**Wi-Vi**（SIGCOMM 2013，空间方差人数粗判）、**Fei Wang 泛化综述**（IEEE COMST 2026） | 活动识别模型选型、少样本现场适配、多人场景预期管理 |

开源参考：[espressif/esp-csi](https://github.com/espressif/esp-csi)（发包/接收例程与 CSI 配置口径、esp_csi_gain_ctrl 增益补偿组件）、[ESP32-CSI-Tool](https://github.com/StevenMHernandez/ESP32-CSI-Tool)（CSV 格式惯例）。

## 6. 快速开始

### 硬件
- 2× ESP32-S3（TX + RX1），1× 任意原版 ESP32（RX2，可选对照链路）
- TX/RX 间距 3–5m，监测对象在连线上（Fresnel 区约束，详见 docs/ 上板笔记）
- 信道须避开家用路由器占用的信道（实测晚高峰同信道干扰可达 63% 空口丢包）

### 固件（ESP-IDF v5.5.x）

```bash
cd firmware/rx_collector
# WiFi 凭据不入库：创建 sdkconfig.secrets（被 .gitignore 排除）
#   CONFIG_RX_WIFI_SSID="..."
#   CONFIG_RX_WIFI_PASSWORD="..."
idf.py set-target esp32s3 && idf.py build flash monitor
```

### 后端

```bash
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
# broker: mosquitto -c tools/mosquitto.conf   （端口 11883）
.venv/Scripts/python -m uvicorn app.main:app --port 18000
# 仪表盘: http://127.0.0.1:18000/
```

### 算法离线复算

```bash
cd analysis && pip install -r requirements.txt
python -m csi_pipeline breath input.csv     # 九级管线 + 逐窗结果
```

## 7. 已知限制与路线图

| 事项 | 状态 |
|------|------|
| 静止单人呼吸 MAE ≤ 2 bpm | ✅ 0.46–0.85 bpm |
| 走动期间挂起 ≥ 90% | ✅ 97–100% |
| 侧卧（弱信号）| ✅ 相干轨迹确认后可用（拥塞信道下的干扰轨迹问题归入 M3 模型判别） |
| 有效窗覆盖率 | 58–72%（趋势监测达标，连续监测待侧卧干扰轨问题解决） |
| 活动识别（无人/静止/走动/跌倒）| M3 后半，待自采数据（每类 ≥30min×3 天×2 位置） |
| 双人呼吸 / 人数粗判 | M4（TensorBeat CP 分解 + Wi-Vi 空间方差） |
| 跨环境泛化、OTA、TLS | M5（CrossFi 少样本模板 + esp_https_OTA） |

## 8. 文档索引

- [docs/项目实施方案.md](docs/项目实施方案.md) — 完整方案 v1.1（架构/指标/算法规格/风险）
- [docs/M0-构建与环境笔记.md](docs/M0-构建与环境笔记.md) / [M1](docs/M1-数据工具链笔记.md) / [M2a](docs/M2a-对拍笔记.md) / [M2b 验收报告](docs/M2b-验收报告.md) / [M3](docs/M3-后端骨架笔记.md)
- [docs/上板笔记.md](docs/上板笔记.md) — 双板联调全记录（CSI 开关、子载波口径、信道拥塞、TX 板恢复）
- [analysis/README.md](analysis/README.md) / [backend/README.md](backend/README.md)

---

*项目周期 2026-10-03 起 · ESP-IDF v5.5.2 · 3 块 ESP32（1×S3 TX + 1×S3 RX + 1×ESP32 RX）*
