"""连续捕获两个设备的 amp 缓冲到 JSONL（快照重叠，按时间戳去重后拼接）。

用法: python capture_session.py <输出.jsonl> <总秒数> [轮询间隔秒=20]
"""
import json, sys, time, urllib.request

out, total, poll = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 20
t_end = time.time() + total
n = 0
with open(out, "w", encoding="utf-8") as f:
    while time.time() < t_end:
        for dev in ("rx1", "rx2"):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:18000/api/amp/waterfall?device={dev}&seconds=59", timeout=5) as r:
                    d = json.loads(r.read())
                f.write(json.dumps({"wall": time.time(), "device": dev,
                                    "t": d.get("t", []), "amp": d.get("amp", [])}) + "\n")
            except Exception as e:
                f.write(json.dumps({"wall": time.time(), "device": dev, "error": str(e)}) + "\n")
        n += 1
        f.flush()
        time.sleep(poll)
print(f"done: {n} polls -> {out}")
