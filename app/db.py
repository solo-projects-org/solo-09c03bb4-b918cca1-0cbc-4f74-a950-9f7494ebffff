"""SQLite 连接与 schema。所有状态都持久化在 SQLite 中，重启不丢、不失效。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_id     TEXT PRIMARY KEY,          -- 样本号
    station_id    TEXT NOT NULL,             -- 站号
    sample_date   TEXT NOT NULL,             -- UTC 日期 YYYY-MM-DD
    concentration REAL NOT NULL,             -- 浓度 μg/L
    random_unc    REAL NOT NULL CHECK (random_unc >= 0),  -- 随机标准不确定度
    batch_id      TEXT NOT NULL,             -- 校准批号
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_station_day ON samples(station_id, sample_date);
CREATE INDEX IF NOT EXISTS idx_samples_batch ON samples(batch_id);

CREATE TABLE IF NOT EXISTS calibration_batches (
    batch_id       TEXT PRIMARY KEY,         -- 校准批号
    systematic_unc REAL NOT NULL CHECK (systematic_unc >= 0),  -- 系统标准不确定度
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conclusions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id    TEXT NOT NULL,
    sample_date   TEXT NOT NULL,
    sample_count  INTEGER NOT NULL,
    mean          REAL,                      -- 缺少校准数据时仍记录均值（仅供参考）
    combined_unc  REAL,
    interval_low  REAL,
    interval_high REAL,
    decision      TEXT NOT NULL,             -- exceeds / not_exceeds / undetermined / missing_calibration
    batches_json  TEXT NOT NULL,             -- 计算时所用批次及系统标准不确定度（快照）
    status        TEXT NOT NULL CHECK (status IN ('current', 'superseded')),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conclusions_station_day
    ON conclusions(station_id, sample_date, status);
-- 每个站日至多一条现行结论：并发重算也无法产生两个“现行”。
CREATE UNIQUE INDEX IF NOT EXISTS uq_conclusions_current
    ON conclusions(station_id, sample_date) WHERE status = 'current';
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False：每个请求/线程使用各自独立的连接，
    # 线程安全由 SQLite 事务（BEGIN IMMEDIATE）与 busy_timeout 保证。
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 立即取得写锁，序列化并发写者。

    更正与失效在同一事务内提交；重算只插入新结论，绝不把
    superseded 改回 current，因此失败/重启/并发都不会复活旧结论。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
