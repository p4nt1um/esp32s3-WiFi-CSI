# CSI 后端（M3 骨架）

FastAPI + SQLite + MQTT 订阅的自建轻量后端（方案 §3/§9）。本地已用 amqtt broker + 设备模拟器完成端到端验证（10/10），VPS 部署换环境变量即可。

## 快速开始（本地开发）

```bash
cd backend
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt

# 三个终端（或跑 tools/e2e_check.py 一键联调）
.venv/Scripts/python.exe tools/dev_broker.py          # amqtt broker :11883
.venv/Scripts/python.exe -m uvicorn app.main:app --port 18000
.venv/Scripts/python.exe tools/sim_device.py --device rx1 --duration 600

# 打开 http://127.0.0.1:18000/ 仪表盘
```

> 本机端口注意：Hyper-V 保留了 1883/8123 等端口段（`netsh interface ipv4 show excludedportrange protocol=tcp`），故开发用 **11883**（broker）/ **18000**（API）。

## 一键验证

```bash
.venv/Scripts/python.exe tools/e2e_check.py    # 10 项检查：入库/设备/latest/history/量级/仪表盘
```

## 结构

```
app/config.py       环境变量配置（BROKER_URI/MQTT_USERNAME/DB_PATH/TOPIC_*）
app/db.py           SQLite（breath/stat/event 三表，单连接+锁）
app/mqtt_client.py  paho v2 订阅线程：csi/+/breath 落库、csi/+/stat 落库、csi/+/amp 保活
app/api.py          REST：/api/health /api/devices /api/breath/latest /api/breath/history
app/static/         仪表盘（原生 canvas 折线，无效窗断线+灰带标记）
tools/dev_broker.py amqtt 本地 broker（生产换 VPS Mosquitto+TLS）
tools/sim_device.py 设备模拟器（呼吸模型+随机体动段+amp 数组）
tools/e2e_check.py  端到端联调脚本
```

## 主题契约（与固件 mqtt_pub.c 一致）

| 主题 | 载荷 | 处理 |
|------|------|------|
| `csi/<dev>/breath` | `{"t":ms,"bpm":15.1,"valid":1,"conf":0.8}` | breath 表（ts 用服务器时刻，设备时钟不可信） |
| `csi/<dev>/stat` | `{"t":ms,"rx":N,"drop":M}` | stat 表 |
| `csi/<dev>/amp` | `{"t":ms,"n":c,"amp":[52]}` | 仅刷新 last_seen（不落库） |

## VPS 部署（对应方案 §9）

```bash
BROKER_URI=mqtts://broker.example.com:8883 MQTT_USERNAME=rx1 MQTT_PASSWORD=*** \
DB_PATH=/data/csi.db .venv/bin/uvicorn app.main:app --port 18000 --host 0.0.0.0
```

要求：Mosquitto 8883 TLS + 每设备独立凭据；API 置于 Caddy 反代后（TLS 终止+鉴权），不直接暴露公网；SQLite 落加密盘、每日快照出 VPS。呼吸数据属健康敏感数据。

## 待办（随硬件/M3 后续里程碑）

- amp 落库策略（按分钟降采样入 stat）与存在性/链路质量视图
- 体动事件表（event）的写入端（端上门控上行为 M2b）
- 活动识别推理接入（M3 后半，需自采数据）
- Grafana 备选接入（当前仪表盘已覆盖呼吸主视图）
