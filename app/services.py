"""业务事务：样本/校准批更正、质控复测、结论失效与重算。

关键不变式：
1. 更正（同号样本、同批次或同序号质控读数再次提交）与“受影响站日现行结论失效”
   在同一事务提交；
2. 重算只会把旧的 current 置为 superseded 并插入新 current，
   任何失败都只会回滚本次重算，不会复活已失效结论；
3. 所有读写都在 BEGIN IMMEDIATE 事务内完成，并发更正/重算被序列化；
4. 涉及质控失败批次的站日重算只能得到 qc_failed 闸门结论（无区间、无浓度判定），
   任何时刻都不会存在基于失败批次的浓度判定。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from . import logic, qc
from .db import utcnow_iso, write_tx


def load_qc_readings(conn: sqlite3.Connection, batch_id: str) -> list[qc.QcReading]:
    """读取某批的全部质控读数（持久化数据，重启后一致）。"""
    return [
        qc.QcReading(seq=r["seq"], measured=r["measured"],
                     certified=r["certified"], std_dev=r["std_dev"])
        for r in conn.execute(
            "SELECT seq, measured, certified, std_dev FROM qc_readings"
            " WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
    ]


def evaluate_batch_qc(conn: sqlite3.Connection, batch_id: str) -> qc.QcEvaluation:
    return qc.evaluate(load_qc_readings(conn, batch_id))


def failed_qc_batches(conn: sqlite3.Connection, batch_ids) -> dict[str, qc.QcEvaluation]:
    """在给定批号集合中返回质控失败的批次及其判定明细。"""
    out = {}
    for b in sorted(set(batch_ids)):
        ev = evaluate_batch_qc(conn, b)
        if ev.failed:
            out[b] = ev
    return out


def _invalidate_station_days(conn: sqlite3.Connection, station_days) -> list[dict]:
    """把受影响站日的现行结论置为 superseded（须在写事务内调用）。"""
    invalidated = []
    for station_id, sample_date in sorted(station_days):
        cur = conn.execute(
            "UPDATE conclusions SET status='superseded' "
            "WHERE status='current' AND station_id=? AND sample_date=?",
            (station_id, sample_date),
        )
        if cur.rowcount:
            invalidated.append({"station_id": station_id, "sample_date": sample_date})
    return invalidated


def upsert_samples(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """写入样本。同号样本再次提交视为更正（整行替换）。

    受影响站日 = 样本原站日（若已存在）∪ 新站日；
    这些站日的现行结论在同一事务内失效。
    """
    now = utcnow_iso()
    affected: set[tuple[str, str]] = set()
    inserted = corrected = 0
    with write_tx(conn):
        for r in rows:
            old = conn.execute(
                "SELECT station_id, sample_date FROM samples WHERE sample_id=?",
                (r["sample_id"],),
            ).fetchone()
            if old is not None:
                corrected += 1
                affected.add((old["station_id"], old["sample_date"]))
            else:
                inserted += 1
            conn.execute(
                "INSERT INTO samples(sample_id, station_id, sample_date,"
                " concentration, random_unc, batch_id, updated_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(sample_id) DO UPDATE SET"
                "   station_id=excluded.station_id,"
                "   sample_date=excluded.sample_date,"
                "   concentration=excluded.concentration,"
                "   random_unc=excluded.random_unc,"
                "   batch_id=excluded.batch_id,"
                "   updated_at=excluded.updated_at",
                (r["sample_id"], r["station_id"], r["sample_date"],
                 r["concentration"], r["random_unc"], r["batch_id"], now),
            )
            affected.add((r["station_id"], r["sample_date"]))
        invalidated = _invalidate_station_days(conn, affected)
    return {
        "inserted": inserted,
        "corrected": corrected,
        "affected_station_days": [
            {"station_id": s, "sample_date": d} for s, d in sorted(affected)
        ],
        "invalidated": invalidated,
    }


def upsert_batch(conn: sqlite3.Connection, batch_id: str, systematic_unc: float) -> dict:
    """录入/更正校准批系统标准不确定度。同批次再次提交视为更正。

    使用该批次的所有站日的现行结论在同一事务内失效。
    """
    now = utcnow_iso()
    with write_tx(conn):
        existed = conn.execute(
            "SELECT 1 FROM calibration_batches WHERE batch_id=?", (batch_id,)
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO calibration_batches(batch_id, systematic_unc, updated_at)"
            " VALUES(?,?,?)"
            " ON CONFLICT(batch_id) DO UPDATE SET"
            "   systematic_unc=excluded.systematic_unc,"
            "   updated_at=excluded.updated_at",
            (batch_id, systematic_unc, now),
        )
        affected = {
            (row["station_id"], row["sample_date"])
            for row in conn.execute(
                "SELECT DISTINCT station_id, sample_date FROM samples WHERE batch_id=?",
                (batch_id,),
            )
        }
        invalidated = _invalidate_station_days(conn, affected)
    return {
        "batch_id": batch_id,
        "systematic_unc": systematic_unc,
        "corrected": existed,
        "invalidated": invalidated,
    }


def upsert_qc_reading(conn: sqlite3.Connection, batch_id: str, seq: int,
                      measured: float, certified: float, std_dev: float) -> dict:
    """录入/更正某校准批的一条质控复测读数。

    同 (批号, 测量序号) 再次提交视为更正（整行替换）；写入后对整批序列重新判定。
    当批次的质控状态发生变化时（首次启用、通过→失败、失败→通过），
    使用该批的所有站日现行结论在同一事务内失效；状态不变（如失败后又一条
    失败读数）则不动其他站日。
    """
    now = utcnow_iso()
    with write_tx(conn):
        before = evaluate_batch_qc(conn, batch_id)
        old = conn.execute(
            "SELECT 1 FROM qc_readings WHERE batch_id=? AND seq=?",
            (batch_id, seq)).fetchone() is not None
        conn.execute(
            "INSERT INTO qc_readings(batch_id, seq, measured, certified, std_dev, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(batch_id, seq) DO UPDATE SET"
            "   measured=excluded.measured,"
            "   certified=excluded.certified,"
            "   std_dev=excluded.std_dev,"
            "   updated_at=excluded.updated_at",
            (batch_id, seq, measured, certified, std_dev, now),
        )
        after = evaluate_batch_qc(conn, batch_id)
        state_changed = before.failed != after.failed
        if state_changed:
            affected = {
                (row["station_id"], row["sample_date"])
                for row in conn.execute(
                    "SELECT DISTINCT station_id, sample_date FROM samples"
                    " WHERE batch_id=?", (batch_id,))
            }
            invalidated = _invalidate_station_days(conn, affected)
        else:
            invalidated = []
    return {
        "batch_id": batch_id,
        "corrected": old,
        "state_changed": state_changed,
        "invalidated": invalidated,
        "qc": qc.evaluation_as_dict(after),
    }


def recompute_station_day(conn: sqlite3.Connection, station_id: str, sample_date: str) -> dict:
    """重算单个站日结论。

    读取样本与批次、写入新结论在同一个 BEGIN IMMEDIATE 事务内完成，
    因此并发更正要么先于本事务（被读到），要么后于本事务
    （随后把本结论失效），不会产生基于过期数据的“现行”结论。
    """
    with write_tx(conn):
        rows = conn.execute(
            "SELECT * FROM samples WHERE station_id=? AND sample_date=?"
            " ORDER BY sample_id",
            (station_id, sample_date),
        ).fetchall()
        if not rows:
            return {"station_id": station_id, "sample_date": sample_date,
                    "status": "no_samples"}

        samples = [logic.SampleInput(concentration=r["concentration"],
                                     random_unc=r["random_unc"],
                                     batch_id=r["batch_id"]) for r in rows]
        batch_ids = sorted({r["batch_id"] for r in rows})
        systematic: dict[str, float] = {}
        for b in batch_ids:
            brow = conn.execute(
                "SELECT systematic_unc FROM calibration_batches WHERE batch_id=?", (b,)
            ).fetchone()
            if brow is not None:
                systematic[b] = brow["systematic_unc"]

        # 质控闸门优先：只要任一涉及批次质控失败，就不得给出浓度判定。
        # 读取与写入在同一写事务内，交错的质控更正要么先于本事务（被读到），
        # 要么后于本事务（随后把本结论失效），不会留下基于失败批次的现行判定。
        failed_batches = failed_qc_batches(conn, batch_ids)
        qc_snapshot = None

        if failed_batches:
            usage = logic.batch_usage(samples, systematic)
            payload = {
                "sample_count": len(samples),
                "mean": logic.compute_mean(samples),
                "combined_unc": None,
                "interval_low": None,
                "interval_high": None,
                "decision": logic.QC_FAILED,
                "batches": [asdict(u) for u in usage],
            }
            qc_snapshot = {
                "failed_batches": {
                    b: qc.evaluation_as_dict(ev) for b, ev in failed_batches.items()
                }
            }
        else:
            try:
                comp = logic.compute(samples, systematic)
                payload = {
                    "sample_count": comp.sample_count,
                    "mean": comp.mean,
                    "combined_unc": comp.combined_unc,
                    "interval_low": comp.interval_low,
                    "interval_high": comp.interval_high,
                    "decision": comp.decision,
                    "batches": [asdict(u) for u in comp.batches],
                }
            except logic.MissingCalibrationError:
                # 缺少校准数据：不得给出结论。记录均值仅供参考，区间留空。
                usage = logic.batch_usage(samples, systematic)
                payload = {
                    "sample_count": len(samples),
                    "mean": logic.compute_mean(samples),
                    "combined_unc": None,
                    "interval_low": None,
                    "interval_high": None,
                    "decision": logic.MISSING_CALIBRATION,
                    "batches": [asdict(u) for u in usage],
                }

        now = utcnow_iso()
        conn.execute(
            "UPDATE conclusions SET status='superseded'"
            " WHERE status='current' AND station_id=? AND sample_date=?",
            (station_id, sample_date),
        )
        cur = conn.execute(
            "INSERT INTO conclusions(station_id, sample_date, sample_count, mean,"
            " combined_unc, interval_low, interval_high, decision, batches_json,"
            " qc_json, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?, 'current',?)",
            (station_id, sample_date, payload["sample_count"], payload["mean"],
             payload["combined_unc"], payload["interval_low"], payload["interval_high"],
             payload["decision"], json.dumps(payload["batches"], ensure_ascii=False),
             json.dumps(qc_snapshot, ensure_ascii=False) if qc_snapshot else None,
             now),
        )
    result = {"station_id": station_id, "sample_date": sample_date,
              "status": "computed", "conclusion_id": cur.lastrowid, **payload}
    if qc_snapshot is not None:
        result["qc"] = qc_snapshot
    return result


def pending_station_days(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """有样本但没有现行结论的站日（待重算）。"""
    return conn.execute(
        "SELECT DISTINCT s.station_id, s.sample_date FROM samples s"
        " WHERE NOT EXISTS (SELECT 1 FROM conclusions c"
        "                   WHERE c.station_id=s.station_id"
        "                     AND c.sample_date=s.sample_date"
        "                     AND c.status='current')"
        " ORDER BY s.sample_date, s.station_id"
    ).fetchall()


def recompute_all_pending(conn: sqlite3.Connection) -> list[dict]:
    """重算所有待重算站日。每个站日独立事务，单个失败不影响其他。"""
    results = []
    for row in pending_station_days(conn):
        results.append(recompute_station_day(conn, row["station_id"], row["sample_date"]))
    return results
