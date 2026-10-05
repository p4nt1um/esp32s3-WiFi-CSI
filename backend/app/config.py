"""后端配置：环境变量驱动，本地开发与 VPS 部署同一份代码。

本地开发（默认）:  mqtt://localhost:1883 匿名
VPS 部署示例:
  BROKER_URI=mqtts://broker.example.com:8883
  MQTT_USERNAME=rx1 MQTT_PASSWORD=***
  DB_PATH=/data/csi.db
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    broker_uri: str = field(default_factory=lambda: os.environ.get("BROKER_URI", "mqtt://localhost:11883"))
    mqtt_username: str | None = field(default_factory=lambda: os.environ.get("MQTT_USERNAME") or None)
    mqtt_password: str | None = field(default_factory=lambda: os.environ.get("MQTT_PASSWORD") or None)
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "csi.db"))
    topic_breath: str = os.environ.get("TOPIC_BREATH", "csi/+/breath")
    topic_stat: str = os.environ.get("TOPIC_STAT", "csi/+/stat")
    topic_amp: str = os.environ.get("TOPIC_AMP", "csi/+/amp")
    history_limit_max: int = int(os.environ.get("HISTORY_LIMIT_MAX", "5000"))


settings = Settings()
