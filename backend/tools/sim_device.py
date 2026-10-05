"""设备模拟器：无硬件时模拟 RX 板的 MQTT 上行（主题契约同固件 mqtt_pub.c）。

呼吸模型：基线 bpm + 慢波动 + 噪声；随机体动段（valid=0, bpm=None）；
amp 为 52 子载波均值数组（含微弱呼吸纹波）。
用法：python tools/sim_device.py --device rx1 --duration 300
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

HOST = "localhost"
PORT = 11883   # 本地开发 broker 端口（1883 被 Hyper-V 保留段占用）


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="rx1")
    ap.add_argument("--duration", type=float, default=300, help="模拟时长（秒）")
    ap.add_argument("--base-bpm", type=float, default=15.0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    t0 = time.time()
    phase = rng.uniform(0, 2 * math.pi)
    motions = []                        # (start, end) 体动段
    t = 0.0
    while t < args.duration:
        if rng.random() < 0.006:        # 平均每 ~160s 一次体动，持续 20-40s
            motions.append((t + 10, t + 10 + rng.uniform(20, 40)))
        t += 1.0

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{args.device}")
    c.connect(HOST, PORT)
    c.loop_start()
    prefix = f"csi/{args.device}"
    print(f"simulating {args.device} for {args.duration:.0f}s -> {prefix}/#")

    next_breath = next_stat = next_amp = 0.0
    seq = 0
    try:
        while True:
            now = time.time() - t0
            if now >= args.duration:
                break
            if now >= next_breath:                      # 每 10s 一条呼吸结果
                next_breath = now + 10
                in_motion = any(a <= now % max(args.duration, 1) <= b for a, b in motions) \
                    if motions else False
                # 体动判定用绝对时间轴（简单起见对 duration 取模）
                bpm = args.base_bpm + 0.6 * math.sin(2 * math.pi * now / 240 + phase) \
                    + rng.gauss(0, 0.15)
                valid = 0 if in_motion else 1
                c.publish(f"{prefix}/breath", json.dumps({
                    "t": int(now * 1000), "bpm": round(bpm, 2) if valid else None,
                    "valid": valid, "conf": round(rng.uniform(0.7, 0.95) if valid else 0.0, 2)}), qos=0)
                seq += 1
            if now >= next_stat:                        # 每 30s 统计
                next_stat = now + 30
                c.publish(f"{prefix}/stat", json.dumps({
                    "t": int(now * 1000), "rx": seq * 100 + int(now), "drop": rng.randint(0, 3)}), qos=0)
            if now >= next_amp:                         # 每 2s 平均幅度
                next_amp = now + 2
                ripple = 1.5 * math.sin(2 * math.pi * args.base_bpm / 60 * now)
                amps = [round(80 + 20 * math.sin(0.3 * k) + ripple * math.cos(0.11 * k)
                              + rng.gauss(0, 2), 1) for k in range(52)]
                c.publish(f"{prefix}/amp", json.dumps(
                    {"t": int(now * 1000), "n": 200, "amp": amps}), qos=0)
            time.sleep(0.2)
    finally:
        c.loop_stop()
        c.disconnect()
        print(f"done: {seq} breath messages")


if __name__ == "__main__":
    main()
