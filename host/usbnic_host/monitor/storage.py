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


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    sampled = [points[min(len(points) - 1, int(index * step))] for index in range(max_points)]
    if points and sampled[-1] is not points[-1]:
        sampled[-1] = points[-1]
    return sampled
