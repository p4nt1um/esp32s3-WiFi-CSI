"""SQLite 存储：呼吸结果/设备统计/事件。单连接 + 锁（量级：每设备 1 行/10s）。"""
from __future__ import annotations

import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS breath(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,          -- 服务器收到时刻（epoch 秒；设备时钟不可信）
    device TEXT NOT NULL,
    bpm REAL, valid INTEGER, conf REAL
);
CREATE INDEX IF NOT EXISTS idx_breath_dev_ts ON breath(device, ts);

CREATE TABLE IF NOT EXISTS stat(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, device TEXT, rx INTEGER, drop_cnt INTEGER
);

CREATE TABLE IF NOT EXISTS event(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, device TEXT, kind TEXT, msg TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # 增量迁移：睡眠报告所需逐窗指标（老库/老行该列恒 NULL）
            for col in ("ratio", "band_power", "raw_std", "loss"):
                try:
                    self._conn.execute(f"ALTER TABLE breath ADD COLUMN {col} REAL")
                except sqlite3.OperationalError:
                    pass    # 列已存在

    def add_breath(self, device: str, bpm: float | None, valid: bool, conf: float | None,
                   metrics: dict | None = None) -> None:
        m = metrics or {}
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO breath(ts, device, bpm, valid, conf, ratio, band_power, raw_std, loss) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (int(time.time()), device, bpm, int(valid), conf,
                 m.get("ratio"), m.get("band_power"), m.get("raw_std"), m.get("loss")))

    def add_stat(self, device: str, rx: int, drop_cnt: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO stat(ts, device, rx, drop_cnt) VALUES (?,?,?,?)",
                (int(time.time()), device, rx, drop_cnt))

    def add_event(self, device: str, kind: str, msg: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO event(ts, device, kind, msg) VALUES (?,?,?,?)",
                (int(time.time()), device, kind, msg))

    def latest_breath(self, device: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, bpm, valid, conf FROM breath WHERE device=? ORDER BY id DESC LIMIT 1",
                (device,)).fetchone()
        return dict(row) if row else None

    def history(self, device: str, hours: float, limit: int, valid_only: bool) -> dict:
        since = int(time.time() - hours * 3600)
        q = ("SELECT ts, bpm, valid, conf FROM breath WHERE device=? AND ts>=?")
        args: list = [device, since]
        if valid_only:
            q += " AND valid=1"
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(q, args).fetchall()]
        rows.reverse()
        return {"t": [r["ts"] for r in rows],
                "bpm": [r["bpm"] for r in rows],
                "valid": [r["valid"] for r in rows]}

    def devices(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT device FROM breath "
                "UNION SELECT DISTINCT device FROM stat").fetchall()
        return [r["device"] for r in rows]

    def count(self, table: str) -> int:
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
