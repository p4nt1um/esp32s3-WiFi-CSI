"""MQTT 订阅：paho v2 API，后台线程，回调写库。

主题契约（与固件 mqtt_pub.c / 模拟器一致）：
  csi/<device>/breath  {"t":ms,"bpm":15.1,"valid":1,"conf":0.8}   → breath 表
  csi/<device>/stat    {"t":ms,"rx":N,"drop":M}                    → stat 表
  csi/<device>/amp     {"t":ms,"n":cnt,"amp":[...]}                → 仅刷新 last_seen（不落库）
"""
from __future__ import annotations

import json
import threading

import paho.mqtt.client as mqtt

from .config import settings


class MqttBridge:
    def __init__(self, db, on_status=None):
        self._db = db
        self._on_status = on_status or (lambda connected: None)
        self._latest: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._engines: dict[str, "BreathEngine"] = {}   # device → engine（多 RX 分流）
        self._client = self._make_client()
        self._breath_timer = None

    # ---------- paho 回调（broker 线程） ----------

    def _make_client(self) -> mqtt.Client:
        uri = settings.broker_uri
        # mqtts:// 与 ssl:// 均启用 TLS（证书校验由系统 CA；自签需设环境变量，见 README）
        use_tls = uri.startswith("mqtts://") or uri.startswith("ssl://")
        host = uri.split("://", 1)[1].rsplit(":", 1)[0] if "://" in uri else "localhost"
        port = int(uri.rsplit(":", 1)[1]) if uri.rsplit(":", 1)[-1].isdigit() else (8883 if use_tls else 1883)

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="csi-backend", protocol=mqtt.MQTTv311)
        if settings.mqtt_username:
            c.username_pw_set(settings.mqtt_username, settings.mqtt_password or "")
        if use_tls:
            c.tls_set()
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._host, self._port, = host, port
        return c

    def _on_connect(self, client, userdata, flags, rc, props=None):
        ok = (rc == 0)          # paho v2: rc 是 ReasonCode，支持与 int 比较
        self._on_status(ok)
        if ok:
            client.subscribe([(settings.topic_breath, 0),
                              (settings.topic_stat, 0),
                              (settings.topic_amp, 0)])

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self._on_status(False)

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        _, device, kind = parts
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return
        with self._lock:
            self._latest.setdefault(device, {})["last_seen"] = payload.get("t")
        if kind == "breath":
            self._db.add_breath(device, payload.get("bpm"),
                                bool(payload.get("valid", 0)), payload.get("conf"))
        elif kind == "stat":
            self._db.add_stat(device, int(payload.get("rx", 0)), int(payload.get("drop", 0)))
        elif kind == "amp":
            # 批量格式：{"t0": ms, "samples": [[52f], [52f], ...]}
            # 兼容旧格式：{"t": ms, "amp": [52f]}
            eng = self._engines.get(device)
            if eng is None:
                from .breath_engine import BreathEngine
                eng = BreathEngine()
                self._engines[device] = eng
            if "samples" in payload:
                t0 = payload.get("t0", 0) / 1000.0
                for i, sample in enumerate(payload["samples"]):
                    eng.feed(t0 + i * 0.1, sample)   # 每 100ms 一个样本
            elif "amp" in payload:
                t_s = payload.get("t", 0) / 1000.0
                eng.feed(t_s, payload["amp"])

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._client.connect_async(self._host, self._port)
        self._breath_timer = threading.Timer(10.0, self._breath_tick)
        self._breath_timer.daemon = True
        self._breath_timer.start()

    def _breath_tick(self) -> None:
        """每 10s 对所有设备的呼吸引擎各跑一次，结果入 breath 表。异常不能杀死定时器链。"""
        try:
            for device, eng in list(self._engines.items()):
                try:
                    result = eng.compute()
                    if result:
                        self._db.add_breath(device, result.get("bpm"),
                                            result.get("valid", False), result.get("conf"),
                                            metrics=result)
                except Exception as e:
                    print(f"[breath_tick] {device} compute error: {e}")
        except Exception as e:
            print(f"[breath_tick] outer error: {e}")
        finally:
            self._breath_timer = threading.Timer(10.0, self._breath_tick)
            self._breath_timer.daemon = True
            self._breath_timer.start()
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    # ---------- 查询 ----------

    def device_status(self) -> list[dict]:
        out = []
        with self._lock:
            for device, info in self._latest.items():
                out.append({"device": device, "last_msg_ms": info.get("last_seen")})
        return out

    @property
    def connected(self) -> bool:
        return self._client.is_connected()
