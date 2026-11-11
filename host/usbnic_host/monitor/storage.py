from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Config


class Storage:
    """SQLite 历史数据。写操作串行化，读操作复用同一连接。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.path: Path = config.data_dir / "host.db"
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._last_prune = 0.0

    def init(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS interface_samples (
                    ts REAL NOT NULL,
                    iface TEXT NOT NULL,
                    rx_bytes INTEGER,
                    tx_bytes INTEGER,
                    rx_rate REAL,
                    tx_rate REAL,
                    rx_errors INTEGER,
                    tx_errors INTEGER,
                    PRIMARY KEY (ts, iface)
                );
                CREATE INDEX IF NOT EXISTS idx_interface_samples_ts
                    ON interface_samples(ts);

                CREATE TABLE IF NOT EXISTS summary_samples (
                    ts REAL PRIMARY KEY,
                    online_devices INTEGER,
                    total_devices INTEGER,
                    rx_rate REAL,
                    tx_rate REAL
                );

                CREATE TABLE IF NOT EXISTS events (
                    ts REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

                -- 使用时长：按「本地日期 + 应用」分桶，只存应用名
                CREATE TABLE IF NOT EXISTS app_usage (
                    day TEXT NOT NULL,
                    app TEXT NOT NULL,
                    seconds REAL NOT NULL DEFAULT 0,
                    last_seen REAL,
                    PRIMARY KEY (day, app)
                );
                CREATE INDEX IF NOT EXISTS idx_app_usage_day ON app_usage(day);

                -- 仅在 usage_record_window_title 打开时才写入
                CREATE TABLE IF NOT EXISTS app_titles (
                    day TEXT NOT NULL,
                    app TEXT NOT NULL,
                    title TEXT NOT NULL,
                    seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, app, title)
                );

                -- 系统健康曲线：CPU / 内存 / swap / 磁盘 / 温度
                CREATE TABLE IF NOT EXISTS system_health_samples (
                    ts REAL PRIMARY KEY,
                    cpu_pct REAL,
                    mem_pct REAL,
                    mem_used INTEGER,
                    mem_total INTEGER,
                    swap_pct REAL,
                    disk_pct REAL,
                    disk_free INTEGER,
                    disk_total INTEGER,
                    temp_c REAL
                );
                CREATE INDEX IF NOT EXISTS idx_system_health_ts
                    ON system_health_samples(ts);
                """
            )
            self.conn.commit()

    def record(self, snapshot: dict[str, Any]) -> None:
        ts = float(snapshot.get("ts") or time.time())
        interfaces = snapshot.get("interfaces") or []
        devices = snapshot.get("devices") or {}
        uplink = snapshot.get("uplink") or {}

        with self.lock:
            for iface in interfaces:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO interface_samples
                    (ts, iface, rx_bytes, tx_bytes, rx_rate, tx_rate, rx_errors, tx_errors)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        iface.get("name"),
                        int(iface.get("rx_bytes") or 0),
                        int(iface.get("tx_bytes") or 0),
                        float(iface.get("rx_rate") or 0),
                        float(iface.get("tx_rate") or 0),
                        int(iface.get("rx_errors") or 0),
                        int(iface.get("tx_errors") or 0),
                    ),
                )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO summary_samples
                (ts, online_devices, total_devices, rx_rate, tx_rate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    int((devices.get("summary") or {}).get("online") or 0),
                    int((devices.get("summary") or {}).get("total") or 0),
                    float(uplink.get("rx_rate") or 0),
                    float(uplink.get("tx_rate") or 0),
                ),
            )
            self.conn.commit()
            self._maybe_prune(ts)

    def _maybe_prune(self, now_ts: float) -> None:
        if now_ts - self._last_prune < 60:
            return
        self._last_prune = now_ts
        cutoff = now_ts - self.config.history_minutes * 60
        event_cutoff = now_ts - 7 * 24 * 3600
        self.conn.execute("DELETE FROM interface_samples WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM summary_samples WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM events WHERE ts < ?", (event_cutoff,))
        self.conn.commit()

    def history(
        self, iface: str | None, minutes: int = 30, max_points: int = 240
    ) -> dict[str, Any]:
        minutes = max(1, min(int(minutes), self.config.history_minutes))
        since = time.time() - minutes * 60
        with self.lock:
            if iface:
                rows = self.conn.execute(
                    """
                    SELECT ts, rx_rate, tx_rate FROM interface_samples
                    WHERE iface = ? AND ts >= ?
                    ORDER BY ts
                    """,
                    (iface, since),
                ).fetchall()
            else:
                rows = []
        points = [
            {
                "ts": float(row["ts"]),
                "rx": float(row["rx_rate"] or 0),
                "tx": float(row["tx_rate"] or 0),
            }
            for row in rows
        ]
        points = _downsample(points, max_points)
        return {"iface": iface or "", "minutes": minutes, "points": points}

    def add_event(self, level: str, message: str, ts: float | None = None) -> None:
        ts = ts or time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (ts, level, message) VALUES (?, ?, ?)",
                (ts, level, message),
            )
            self.conn.commit()

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, level, message FROM events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"ts": float(row["ts"]), "level": row["level"], "message": row["message"]}
            for row in rows
        ]

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---------- 使用时长 ----------
    def add_usage(self, entries: list[tuple[str, str, float, float]]) -> None:
        """批量累加 (day, app, seconds, last_seen)，同一应用当天只保留一行。"""
        if not entries:
            return
        with self.lock:
            self.conn.executemany(
                """
                INSERT INTO app_usage (day, app, seconds, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, app) DO UPDATE SET
                    seconds = seconds + excluded.seconds,
                    last_seen = MAX(COALESCE(last_seen, 0), excluded.last_seen)
                """,
                entries,
            )
            self.conn.commit()

    def add_usage_titles(
        self, entries: list[tuple[str, str, str, float]]
    ) -> None:
        if not entries:
            return
        with self.lock:
            self.conn.executemany(
                """
                INSERT INTO app_titles (day, app, title, seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, app, title) DO UPDATE SET
                    seconds = seconds + excluded.seconds
                """,
                entries,
            )
            self.conn.commit()

    def usage_day(self, day: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT app, seconds FROM app_usage WHERE day = ? ORDER BY seconds DESC",
                (day,),
            ).fetchall()
        return [
            {"app": row["app"], "seconds": float(row["seconds"] or 0.0)}
            for row in rows
        ]

    def usage_day_totals(self, start: str, end: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT day, SUM(seconds) AS total FROM app_usage
                WHERE day >= ? AND day <= ?
                GROUP BY day ORDER BY day
                """,
                (start, end),
            ).fetchall()
        return [
            {"day": row["day"], "seconds": float(row["total"] or 0.0)} for row in rows
        ]

    def usage_app_totals(
        self, start: str, end: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT app, SUM(seconds) AS total FROM app_usage
                WHERE day >= ? AND day <= ?
                GROUP BY app ORDER BY total DESC LIMIT ?
                """,
                (start, end, max(1, int(limit))),
            ).fetchall()
        return [
            {"app": row["app"], "seconds": float(row["total"] or 0.0)} for row in rows
        ]

    def prune_usage(self, before_day: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM app_usage WHERE day < ?", (before_day,))
            self.conn.execute("DELETE FROM app_titles WHERE day < ?", (before_day,))
            self.conn.commit()

    # ---------- 系统健康 ----------
    def add_health_sample(self, sample: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO system_health_samples
                (ts, cpu_pct, mem_pct, mem_used, mem_total, swap_pct,
                 disk_pct, disk_free, disk_total, temp_c)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(sample.get("ts") or time.time()),
                    float(sample.get("cpu_pct") or 0.0),
                    float(sample.get("mem_pct") or 0.0),
                    int(sample.get("mem_used") or 0),
                    int(sample.get("mem_total") or 0),
                    float(sample.get("swap_pct") or 0.0),
                    float(sample.get("disk_pct") or 0.0),
                    int(sample.get("disk_free") or 0),
                    int(sample.get("disk_total") or 0),
                    _float_or_none(sample.get("temp_c")),
                ),
            )
            self.conn.commit()

    def health_latest(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT ts, cpu_pct, mem_pct, mem_used, mem_total, swap_pct,
                       disk_pct, disk_free, disk_total, temp_c
                FROM system_health_samples ORDER BY ts DESC LIMIT 1
                """
            ).fetchone()
        return _health_row(row) if row else None

    def health_series(self, since_ts: float, max_points: int = 240) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT ts, cpu_pct, mem_pct, mem_used, mem_total, swap_pct,
                       disk_pct, disk_free, disk_total, temp_c
                FROM system_health_samples WHERE ts >= ? ORDER BY ts
                """,
                (since_ts,),
            ).fetchall()
        return _downsample_avg([_health_row(row) for row in rows], max_points)

    def prune_health(self, before_ts: float) -> None:
        with self.lock:
            self.conn.execute(
                "DELETE FROM system_health_samples WHERE ts < ?", (before_ts,)
            )
            self.conn.commit()


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    sampled = [points[min(len(points) - 1, int(index * step))] for index in range(max_points)]
    if points and sampled[-1] is not points[-1]:
        sampled[-1] = points[-1]
    return sampled


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _health_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ts": float(row["ts"]),
        "cpu_pct": float(row["cpu_pct"] or 0.0),
        "mem_pct": float(row["mem_pct"] or 0.0),
        "mem_used": int(row["mem_used"] or 0),
        "mem_total": int(row["mem_total"] or 0),
        "swap_pct": float(row["swap_pct"] or 0.0),
        "disk_pct": float(row["disk_pct"] or 0.0),
        "disk_free": int(row["disk_free"] or 0),
        "disk_total": int(row["disk_total"] or 0),
        "temp_c": _float_or_none(row["temp_c"]),
    }


_HEALTH_NUMERIC_KEYS = (
    "cpu_pct",
    "mem_pct",
    "swap_pct",
    "disk_pct",
    "mem_used",
    "mem_total",
    "disk_free",
    "disk_total",
)


def _downsample_avg(
    points: list[dict[str, Any]], max_points: int
) -> list[dict[str, Any]]:
    """健康曲线按桶取平均，避免抽点丢掉尖峰。"""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    output: list[dict[str, Any]] = []
    for index in range(max_points):
        start = int(index * step)
        end = max(start + 1, int((index + 1) * step))
        chunk = points[start:end]
        if not chunk:
            continue
        sample: dict[str, Any] = {"ts": chunk[-1]["ts"]}
        for key in _HEALTH_NUMERIC_KEYS:
            sample[key] = round(
                sum(float(item.get(key) or 0.0) for item in chunk) / len(chunk), 3
            )
        temperatures = [
            item["temp_c"] for item in chunk if item.get("temp_c") is not None
        ]
        sample["temp_c"] = (
            round(sum(temperatures) / len(temperatures), 2) if temperatures else None
        )
        output.append(sample)
    if points and output:
        output[-1]["ts"] = points[-1]["ts"]
    return output
