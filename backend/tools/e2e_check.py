"""M3 骨架端到端联调（无硬件）：broker → 模拟设备 → 后端 → API 验证。

运行：.venv/Scripts/python.exe tools/e2e_check.py
退出码 0=全部通过。结束后自动清理子进程与临时库。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    db_path = os.path.join(tempfile.gettempdir(), "csi_e2e.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    procs: list[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen([PY, os.path.join(ROOT, "tools", "dev_broker.py")]))
        time.sleep(3)
        env = dict(os.environ, DB_PATH=db_path)
        procs.append(subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--port", "18000"],
            cwd=ROOT, env=env))
        time.sleep(4)

        h = get("http://127.0.0.1:18000/api/health")
        check("health", h.get("status") == "ok", f"broker={h.get('broker_connected')}")
        check("broker 连接", h.get("broker_connected") is True)

        sim = subprocess.run([PY, os.path.join(ROOT, "tools", "sim_device.py"),
                              "--device", "rx1", "--duration", "40", "--seed", "7"],
                             cwd=ROOT, capture_output=True, text=True, timeout=90)
        check("模拟器运行", sim.returncode == 0, sim.stdout.strip().splitlines()[-1] if sim.stdout else "")

        time.sleep(2)
        h = get("http://127.0.0.1:18000/api/health")
        check("呼吸数据入库", h["db"]["breath"] >= 3, f"breath={h['db']['breath']}")
        check("统计数据入库", h["db"]["stat"] >= 1, f"stat={h['db']['stat']}")

        devs = get("http://127.0.0.1:18000/api/devices")
        check("设备列表", any(d["device"] == "rx1" for d in devs), f"{len(devs)} 台")

        latest = get("http://127.0.0.1:18000/api/breath/latest?device=rx1")
        check("latest 有值", latest.get("ts") is not None, f"bpm={latest.get('bpm')}")

        hist = get("http://127.0.0.1:18000/api/breath/history?device=rx1&hours=1&limit=100")
        check("history 非空", len(hist["t"]) >= 3, f"{len(hist['t'])} 点")
        valid_vals = [b for b, v in zip(hist["bpm"], hist["valid"]) if v]
        check("bpm 量级合理", all(10 < b < 25 for b in valid_vals),
              f"min={min(valid_vals):.1f} max={max(valid_vals):.1f}" if valid_vals else "无有效样本")

        with urllib.request.urlopen("http://127.0.0.1:18000/", timeout=5) as r:
            html = r.read().decode()
        check("仪表盘页", "chart" in html and "CSI" in html)
    except Exception as e:  # noqa: BLE001
        checks.append(("异常", False, str(e)))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for _ in range(5):                       # Windows 句柄异步释放，best-effort 清理
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
                break
            except PermissionError:
                time.sleep(1)

    n_fail = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
        n_fail += not ok
    print(f"\n{n_fail} failed / {len(checks)} total")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
