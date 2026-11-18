"""FastAPI 应用：操作网页 + JSON API。

运行：uvicorn app.main:app --host 0.0.0.0 --port 8000
数据库路径由环境变量 APP_DB_PATH 指定（默认 ./data/app.db）。
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import logic, qc, services, web
from .db import connect, init_db

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_COLUMNS = ["sample_id", "station_id", "date",
                    "concentration", "random_unc", "batch_id"]
COLUMN_ALIASES = {"utc_date": "date"}  # 兼容别名表头
MAX_REPORTED_ERRORS = 20


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("APP_DB_PATH", "./data/app.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.db_path)
        yield

    app = FastAPI(title="水样铅浓度超标判定系统", lifespan=lifespan)
    app.state.db_path = db_path

    def get_conn(request: Request):
        conn = connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ---------------- CSV 解析 ----------------

    def parse_samples_csv(text: str):
        """返回 (rows, errors)。任何一行有错都整体拒绝，不产生部分写入。"""
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return [], ["CSV 为空或缺少表头"]
        headers = {}
        for raw in reader.fieldnames:
            name = (raw or "").strip()
            headers[name] = COLUMN_ALIASES.get(name, name)
        missing = [c for c in REQUIRED_COLUMNS if c not in headers.values()]
        if missing:
            return [], ["缺少必需列: " + ", ".join(missing)]

        rows, errors = [], []
        for lineno, raw in enumerate(reader, start=2):
            rec = {}
            for k, v in raw.items():
                key = headers.get((k or "").strip())
                if key:
                    rec[key] = v
            row, errs = _parse_row(rec, lineno)
            if errs:
                errors.extend(errs)
            else:
                rows.append(row)
        return rows, errors

    def _parse_row(rec: dict, lineno: int):
        errs = []
        sample_id = (rec.get("sample_id") or "").strip()
        station_id = (rec.get("station_id") or "").strip()
        date = (rec.get("date") or "").strip()
        batch_id = (rec.get("batch_id") or "").strip()
        if not sample_id:
            errs.append(f"第{lineno}行: sample_id 为空")
        if not station_id:
            errs.append(f"第{lineno}行: station_id 为空")
        if not batch_id:
            errs.append(f"第{lineno}行: batch_id 为空")
        if not DATE_RE.match(date):
            errs.append(f"第{lineno}行: date 须为 YYYY-MM-DD（UTC），得到 {date!r}")
        else:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                errs.append(f"第{lineno}行: date 不是有效日期: {date!r}")
        try:
            concentration = float((rec.get("concentration") or "").strip())
            if not math.isfinite(concentration):
                raise ValueError
        except ValueError:
            concentration = None
            errs.append(f"第{lineno}行: concentration 不是有效数值")
        try:
            random_unc = float((rec.get("random_unc") or "").strip())
            if not math.isfinite(random_unc) or random_unc < 0:
                raise ValueError
        except ValueError:
            random_unc = None
            errs.append(f"第{lineno}行: random_unc 须为非负有限数值")
        if errs:
            return None, errs
        return {
            "sample_id": sample_id, "station_id": station_id, "sample_date": date,
            "concentration": concentration, "random_unc": random_unc,
            "batch_id": batch_id,
        }, []

    def _read_csv_text(data: bytes) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "文件不是有效的 UTF-8 文本")

    def _parse_or_400(text: str):
        rows, errors = parse_samples_csv(text)
        if errors:
            raise HTTPException(400, detail={
                "message": "CSV 校验失败，未写入任何数据",
                "errors": errors[:MAX_REPORTED_ERRORS],
                "total_errors": len(errors),
            })
        if not rows:
            raise HTTPException(400, "CSV 中没有数据行")
        return rows

    def _parse_qc_payload(payload: dict):
        """校验质控读数表单/JSON：序号为正整数，测得值/证书值为有限数，标准差为正。"""
        raw_seq = payload.get("seq")
        if isinstance(raw_seq, bool):
            raise HTTPException(400, "测量序号 seq 须为正整数")
        if isinstance(raw_seq, float):
            if not math.isfinite(raw_seq) or not raw_seq.is_integer():
                raise HTTPException(400, "测量序号 seq 须为正整数")
            seq = int(raw_seq)
        else:
            try:
                seq = int(raw_seq)
            except (TypeError, ValueError):
                raise HTTPException(400, "测量序号 seq 须为正整数")
        if seq < 1:
            raise HTTPException(400, "测量序号 seq 须为正整数")

        def _finite(name: str, allow_negative: bool = True):
            try:
                v = float(payload.get(name))
                if not math.isfinite(v):
                    raise ValueError
                if not allow_negative and v <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"{name} 须为有限数值" + ("且严格为正" if not allow_negative else ""))
            return v

        measured = _finite("measured")
        certified = _finite("certified")
        std_dev = _finite("std_dev", allow_negative=False)
        return seq, measured, certified, std_dev

    # ---------------- JSON API ----------------

    @app.post("/api/samples/upload")
    async def api_upload_samples(file: UploadFile = File(...),
                                 conn: sqlite3.Connection = Depends(get_conn)):
        rows = _parse_or_400(_read_csv_text(await file.read()))
        result = services.upsert_samples(conn, rows)
        return {"rows": len(rows), **result}

    @app.post("/api/batches")
    def api_upsert_batch(payload: dict = Body(...),
                         conn: sqlite3.Connection = Depends(get_conn)):
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            raise HTTPException(400, "batch_id 不能为空")
        try:
            unc = float(payload.get("systematic_unc"))
            if not math.isfinite(unc) or unc < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "systematic_unc 须为非负有限数值")
        return services.upsert_batch(conn, batch_id, unc)

    @app.get("/api/batches")
    def api_list_batches(conn: sqlite3.Connection = Depends(get_conn)):
        rows = conn.execute(
            "SELECT batch_id, systematic_unc, updated_at"
            " FROM calibration_batches ORDER BY batch_id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ev = services.evaluate_batch_qc(conn, d["batch_id"])
            d["qc_status"] = (qc.QC_NONE if ev.reading_count == 0 else ev.status)
            d["qc_reading_count"] = ev.reading_count
            d["qc_triggers"] = [qc.trigger_as_dict(t) for t in ev.triggers]
            out.append(d)
        return out

    @app.get("/api/batches/{batch_id}/qc-readings")
    def api_get_qc(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        ev = services.evaluate_batch_qc(conn, batch_id)
        return {
            "batch_id": batch_id,
            "qc_status": (qc.QC_NONE if ev.reading_count == 0 else ev.status),
            **qc.evaluation_as_dict(ev),
        }

    @app.post("/api/batches/{batch_id}/qc-readings")
    def api_upsert_qc(batch_id: str, payload: dict = Body(...),
                      conn: sqlite3.Connection = Depends(get_conn)):
        batch_id = (batch_id or "").strip()
        if not batch_id:
            raise HTTPException(400, "batch_id 不能为空")
        seq, measured, certified, std_dev = _parse_qc_payload(payload)
        return services.upsert_qc_reading(conn, batch_id, seq,
                                          measured, certified, std_dev)

    @app.get("/api/station-days")
    def api_station_days(conn: sqlite3.Connection = Depends(get_conn)):
        return _station_day_list(conn)

    @app.get("/api/station-days/{station_id}/{sample_date}")
    def api_station_day_detail(station_id: str, sample_date: str,
                               conn: sqlite3.Connection = Depends(get_conn)):
        detail = _station_day_detail(conn, station_id, sample_date)
        if detail is None:
            raise HTTPException(404, "该站日没有样本")
        return detail

    @app.post("/api/station-days/{station_id}/{sample_date}/recompute")
    def api_recompute(station_id: str, sample_date: str,
                      conn: sqlite3.Connection = Depends(get_conn)):
        return services.recompute_station_day(conn, station_id, sample_date)

    @app.post("/api/recompute-pending")
    def api_recompute_pending(conn: sqlite3.Connection = Depends(get_conn)):
        results = services.recompute_all_pending(conn)
        return {"recomputed": len(results), "results": results}

    # ---------------- 网页 ----------------

    @app.get("/", response_class=HTMLResponse)
    def page_index(request: Request, msg: str = "", error: str = "",
                   conn: sqlite3.Connection = Depends(get_conn)):
        batches = [dict(r) for r in conn.execute(
            "SELECT batch_id, systematic_unc, updated_at"
            " FROM calibration_batches ORDER BY batch_id").fetchall()]
        for b in batches:
            ev = services.evaluate_batch_qc(conn, b["batch_id"])
            b["qc_status"] = qc.QC_NONE if ev.reading_count == 0 else ev.status
            b["qc_reading_count"] = ev.reading_count
            b["qc_trigger_count"] = len(ev.triggers)
        body = web.dashboard(_station_day_list(conn), batches, msg, error)
        return HTMLResponse(web.layout("水样铅浓度判定 · 总览", body))

    @app.get("/station-days/{station_id}/{sample_date}", response_class=HTMLResponse)
    def page_station_day(station_id: str, sample_date: str,
                         conn: sqlite3.Connection = Depends(get_conn)):
        detail = _station_day_detail(conn, station_id, sample_date)
        if detail is None:
            raise HTTPException(404, "该站日没有样本")
        for b in detail["live_batches"]:
            ev = services.evaluate_batch_qc(conn, b["batch_id"])
            b["qc_status"] = qc.QC_NONE if ev.reading_count == 0 else ev.status
            b["qc_trigger_count"] = len(ev.triggers)
        body = web.detail_page(
            station_id, sample_date,
            samples=detail["samples"],
            live_batches=detail["live_batches"],
            current=detail["current_conclusion"],
            history=detail["history"],
        )
        return HTMLResponse(web.layout(f"{station_id} · {sample_date}", body))

    @app.post("/web/upload")
    async def web_upload(file: UploadFile = File(...),
                         conn: sqlite3.Connection = Depends(get_conn)):
        try:
            text = _read_csv_text(await file.read())
            rows, errors = parse_samples_csv(text)
            if errors:
                shown = "；".join(errors[:5])
                if len(errors) > 5:
                    shown += f" 等 {len(errors)} 处错误"
                raise ValueError("CSV 校验失败，未写入任何数据：" + shown)
            if not rows:
                raise ValueError("CSV 中没有数据行")
            result = services.upsert_samples(conn, rows)
            msg = (f"已接收 {result['inserted']} 条新样本、{result['corrected']} 条更正；"
                   f"{len(result['invalidated'])} 个站日的现行结论已失效，请重算")
            return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)
        except ValueError as e:
            return RedirectResponse(f"/?error={quote(str(e))}", status_code=303)

    @app.post("/web/batches")
    def web_upsert_batch(batch_id: str = Form(...), systematic_unc: str = Form(...),
                         conn: sqlite3.Connection = Depends(get_conn)):
        batch_id = batch_id.strip()
        try:
            unc = float(systematic_unc)
            if not math.isfinite(unc) or unc < 0:
                raise ValueError
            if not batch_id:
                raise ValueError
        except ValueError:
            return RedirectResponse(
                f"/?error={quote('批号不能为空，系统标准不确定度须为非负有限数值')}",
                status_code=303)
        result = services.upsert_batch(conn, batch_id, unc)
        kind = "更正" if result["corrected"] else "录入"
        msg = (f"校准批 {batch_id} 已{kind}；"
               f"{len(result['invalidated'])} 个站日的现行结论已失效，请重算")
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

    @app.post("/web/qc-readings")
    def web_upsert_qc(batch_id: str = Form(...), seq: str = Form(...),
                      measured: str = Form(...), certified: str = Form(...),
                      std_dev: str = Form(...),
                      conn: sqlite3.Connection = Depends(get_conn)):
        batch_id = batch_id.strip()
        if not batch_id:
            return RedirectResponse(
                f"/?error={quote('批号不能为空')}", status_code=303)
        try:
            seq_i = int(seq)
            if seq_i < 1:
                raise ValueError
            m, c, s = float(measured), float(certified), float(std_dev)
            if not (math.isfinite(m) and math.isfinite(c)) or not math.isfinite(s) or s <= 0:
                raise ValueError
        except ValueError:
            return RedirectResponse(
                f"/?error={quote('质控录入校验失败：序号须为正整数，测得值/证书值为有限数值，标准差须为正数')}",
                status_code=303)
        result = services.upsert_qc_reading(conn, batch_id, seq_i, m, c, s)
        ev_status = result["qc"]["status"]
        kind = "更正" if result["corrected"] else "录入"
        if ev_status == qc.QC_FAILED:
            trig = "；".join(
                f"序号 {t['seq']} 触发「{t['rule_label']}」"
                for t in result["qc"]["triggers"])
            msg = (f"批次 {batch_id} 质控读数已{kind}，判定质控失败（{trig}）；"
                   f"{len(result['invalidated'])} 个站日现行结论已失效，"
                   "重算也不会给出浓度判定")
        elif result["state_changed"]:
            msg = (f"批次 {batch_id} 质控恢复通过；"
                   f"{len(result['invalidated'])} 个站日现行结论已失效，须重新重算")
        else:
            msg = f"批次 {batch_id} 质控读数已{kind}，质控通过"
        return RedirectResponse(
            f"/batches/{quote(batch_id)}?msg={quote(msg)}", status_code=303)

    @app.get("/batches/{batch_id}", response_class=HTMLResponse)
    def page_batch_qc(batch_id: str, request: Request, msg: str = "", error: str = "",
                      conn: sqlite3.Connection = Depends(get_conn)):
        ev = services.evaluate_batch_qc(conn, batch_id)
        status = qc.QC_NONE if ev.reading_count == 0 else ev.status
        brow = conn.execute(
            "SELECT systematic_unc, updated_at FROM calibration_batches"
            " WHERE batch_id=?", (batch_id,)).fetchone()
        users = [dict(r) for r in conn.execute(
            "SELECT station_id, sample_date, COUNT(*) AS sample_count FROM samples"
            " WHERE batch_id=? GROUP BY station_id, sample_date"
            " ORDER BY sample_date, station_id", (batch_id,)).fetchall()]
        body = web.batch_qc_page(batch_id, dict(brow) if brow else None,
                                 status, ev, users, msg, error)
        return HTMLResponse(web.layout(f"校准批 {batch_id} · 质控", body))

    @app.post("/web/recompute")
    def web_recompute(station_id: str = Form(...), sample_date: str = Form(...),
                      conn: sqlite3.Connection = Depends(get_conn)):
        result = services.recompute_station_day(conn, station_id, sample_date)
        if result.get("status") == "no_samples":
            return RedirectResponse(f"/?error={quote('该站日没有样本')}", status_code=303)
        label = logic.DECISION_LABELS.get(result["decision"], result["decision"])
        msg = f"{station_id} {sample_date} 重算完成：{label}"
        return RedirectResponse(
            f"/station-days/{quote(station_id)}/{quote(sample_date)}?msg={quote(msg)}",
            status_code=303)

    @app.post("/web/recompute-pending")
    def web_recompute_pending(conn: sqlite3.Connection = Depends(get_conn)):
        results = services.recompute_all_pending(conn)
        msg = f"已重算 {len(results)} 个待重算站日"
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

    # ---------------- 查询组装 ----------------

    def _station_day_list(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT s.station_id, s.sample_date, COUNT(*) AS sample_count,"
            " c.id AS conclusion_id, c.decision, c.mean,"
            " c.interval_low, c.interval_high, c.created_at"
            " FROM samples s"
            " LEFT JOIN conclusions c"
            "   ON c.station_id=s.station_id AND c.sample_date=s.sample_date"
            "  AND c.status='current'"
            " GROUP BY s.station_id, s.sample_date"
            " ORDER BY s.sample_date DESC, s.station_id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["status"] = "concluded" if d["conclusion_id"] is not None else "pending"
            # 实时质控状态：即使尚未重算，涉及失败批次的站日也明确标注
            batch_ids = [x["batch_id"] for x in conn.execute(
                "SELECT DISTINCT batch_id FROM samples"
                " WHERE station_id=? AND sample_date=?",
                (d["station_id"], d["sample_date"])).fetchall()]
            d["qc_failed_batches"] = sorted(services.failed_qc_batches(conn, batch_ids))
            out.append(d)
        return out

    def _station_day_detail(conn: sqlite3.Connection, station_id: str, sample_date: str):
        samples = [dict(r) for r in conn.execute(
            "SELECT sample_id, station_id, sample_date, concentration, random_unc,"
            " batch_id, updated_at FROM samples"
            " WHERE station_id=? AND sample_date=? ORDER BY sample_id",
            (station_id, sample_date)).fetchall()]
        if not samples:
            return None
        live_batches = [dict(r) for r in conn.execute(
            "SELECT s.batch_id, COUNT(*) AS sample_count, b.systematic_unc"
            " FROM samples s LEFT JOIN calibration_batches b ON b.batch_id=s.batch_id"
            " WHERE s.station_id=? AND s.sample_date=?"
            " GROUP BY s.batch_id ORDER BY s.batch_id",
            (station_id, sample_date)).fetchall()]
        for b in live_batches:
            ev = services.evaluate_batch_qc(conn, b["batch_id"])
            b["qc_status"] = qc.QC_NONE if ev.reading_count == 0 else ev.status
        current_row = conn.execute(
            "SELECT * FROM conclusions"
            " WHERE station_id=? AND sample_date=? AND status='current'",
            (station_id, sample_date)).fetchone()
        history_rows = conn.execute(
            "SELECT * FROM conclusions"
            " WHERE station_id=? AND sample_date=? AND status='superseded'"
            " ORDER BY id DESC",
            (station_id, sample_date)).fetchall()
        return {
            "station_id": station_id,
            "sample_date": sample_date,
            "samples": samples,
            "live_batches": live_batches,
            "current_conclusion": web.parse_batches_json(current_row) if current_row else None,
            "history": [web.parse_batches_json(r) for r in history_rows],
        }

    return app


app = create_app()
