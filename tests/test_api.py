"""端到端自测：上传、更正失效、重算、缺失校准、失败/重启/并发不复活旧结论。"""
import csv
import io
import threading

import pytest
from fastapi.testclient import TestClient

from app import logic, services
from app.db import connect, init_db
from app.main import create_app

CSV_HEADER = "sample_id,station_id,date,concentration,random_unc,batch_id"


def make_csv(rows):
    buf = io.StringIO()
    buf.write(CSV_HEADER + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode()


def upload(client, rows):
    return client.post("/api/samples/upload",
                       files={"file": ("samples.csv", make_csv(rows), "text/csv")})


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def seed_two_station_days(client):
    """ST-A/2026-11-01 用 B01、B02；ST-B/2026-11-01 只用 B01。"""
    r = upload(client, [
        ("S1", "ST-A", "2026-11-01", 12.0, 0.4, "B01"),
        ("S2", "ST-A", "2026-11-01", 12.4, 0.4, "B01"),
        ("S3", "ST-A", "2026-11-01", 12.2, 0.4, "B02"),
        ("S4", "ST-B", "2026-11-01", 8.0, 0.3, "B01"),
    ])
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 4


def test_full_flow_and_missing_calibration(client):
    seed_two_station_days(client)
    # 未录入任何校准批 → 重算后不得给出结论
    r = client.post("/api/recompute-pending")
    assert r.status_code == 200
    assert r.json()["recomputed"] == 2
    detail = client.get("/api/station-days/ST-A/2026-11-01").json()
    cur = detail["current_conclusion"]
    assert cur["decision"] == logic.MISSING_CALIBRATION
    assert cur["interval_low"] is None and cur["interval_high"] is None
    assert cur["mean"] == pytest.approx((12.0 + 12.4 + 12.2) / 3)
    # 所用批次可见，且 B01/B02 的系统不确定度为空
    assert {b["batch_id"]: b["systematic_unc"] for b in cur["batches"]} == {
        "B01": None, "B02": None}

    # 只录 B01 → 仍缺 B02，不得给出结论
    assert client.post("/api/batches", json={
        "batch_id": "B01", "systematic_unc": 0.3}).status_code == 200
    client.post("/api/station-days/ST-A/2026-11-01/recompute")
    cur = client.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
    assert cur["decision"] == logic.MISSING_CALIBRATION

    # 补齐 B02 → 可判定
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    r = client.post("/api/station-days/ST-A/2026-11-01/recompute").json()
    assert r["decision"] == logic.EXCEEDS
    assert r["interval_low"] > 10.0
    assert r["sample_count"] == 3
    # 合成不确定度核对：随机 3·0.16/9；系统 ((2·0.3)^2+(1·0.5)^2)/9
    var = 3 * 0.16 / 9 + ((2 * 0.3) ** 2 + (1 * 0.5) ** 2) / 9
    assert r["combined_unc"] == pytest.approx(var ** 0.5)
    assert {b["batch_id"]: b["systematic_unc"] for b in r["batches"]} == {
        "B01": 0.3, "B02": 0.5}


def test_correction_invalidates_and_recompute_updates(client):
    seed_two_station_days(client)
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    client.post("/api/recompute-pending")
    before = client.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
    assert before["decision"] == logic.EXCEEDS

    # 同号样本 S3 再次提交（浓度改小）→ 更正，ST-A 当日结论立即失效
    r = upload(client, [("S3", "ST-A", "2026-11-01", 9.0, 0.4, "B02")])
    assert r.json()["corrected"] == 1
    assert {"station_id": "ST-A", "sample_date": "2026-11-01"} in r.json()["invalidated"]
    detail = client.get("/api/station-days/ST-A/2026-11-01").json()
    assert detail["current_conclusion"] is None           # 旧结论失去现行资格
    assert len(detail["history"]) == 1                    # 历史可查但已失效
    assert detail["history"][0]["id"] == before["id"]

    # 重算后得到基于更正数据的新结论
    r = client.post("/api/station-days/ST-A/2026-11-01/recompute").json()
    assert r["mean"] == pytest.approx((12.0 + 12.4 + 9.0) / 3)
    detail = client.get("/api/station-days/ST-A/2026-11-01").json()
    assert detail["current_conclusion"]["id"] != before["id"]
    assert len(detail["history"]) == 1


def test_correction_moving_station_invalidates_both_days(client):
    seed_two_station_days(client)
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    client.post("/api/recompute-pending")
    # S4 从 ST-B 改到 ST-A：两个站日都失效
    r = upload(client, [("S4", "ST-A", "2026-11-01", 8.0, 0.3, "B01")])
    invalidated = {(d["station_id"], d["sample_date"]) for d in r.json()["invalidated"]}
    assert invalidated == {("ST-A", "2026-11-01"), ("ST-B", "2026-11-01")}
    days = {d["station_id"]: d["status"] for d in client.get("/api/station-days").json()}
    assert days == {"ST-A": "pending"}  # ST-B 已无样本，不再出现在站日列表中


def test_batch_correction_invalidates_users_others_unaffected(client):
    seed_two_station_days(client)
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    client.post("/api/recompute-pending")
    # 再加一个只用 B03 的站日
    upload(client, [("S9", "ST-C", "2026-11-02", 5.0, 0.2, "B03")])
    client.post("/api/batches", json={"batch_id": "B03", "systematic_unc": 0.1})
    client.post("/api/recompute-pending")
    assert client.get("/api/station-days/ST-C/2026-11-02").json()[
        "current_conclusion"]["decision"] == logic.NOT_EXCEEDS

    # 同批次 B01 再次提交（更正系统不确定度）→ 用 B01 的站日失效
    r = client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.9})
    assert r.json()["corrected"] is True
    invalidated = {(d["station_id"], d["sample_date"]) for d in r.json()["invalidated"]}
    assert invalidated == {("ST-A", "2026-11-01"), ("ST-B", "2026-11-01")}
    # 其他站日照常可查
    stc = client.get("/api/station-days/ST-C/2026-11-02").json()
    assert stc["current_conclusion"]["decision"] == logic.NOT_EXCEEDS
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"] is None


def test_failed_recompute_does_not_revive(client, monkeypatch):
    seed_two_station_days(client)
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    client.post("/api/recompute-pending")
    # 更正使结论失效
    upload(client, [("S1", "ST-A", "2026-11-01", 11.0, 0.4, "B01")])
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"] is None

    # 重算中途失败 → 旧结论不得复活
    def boom(*a, **k):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr("app.services.logic.compute", boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        client.post("/api/station-days/ST-A/2026-11-01/recompute")
    monkeypatch.undo()
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"] is None
    # 恢复后可正常重算
    r = client.post("/api/station-days/ST-A/2026-11-01/recompute").json()
    assert r["mean"] == pytest.approx((11.0 + 12.4 + 12.2) / 3)


def test_restart_does_not_revive(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c:
        seed_two_station_days(c)
        c.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
        c.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
        c.post("/api/recompute-pending")
        old_id = c.get("/api/station-days/ST-A/2026-11-01").json()[
            "current_conclusion"]["id"]
        upload(c, [("S1", "ST-A", "2026-11-01", 11.0, 0.4, "B01")])
    # 模拟重启：同一数据库文件新建应用
    with TestClient(create_app(db)) as c2:
        detail = c2.get("/api/station-days/ST-A/2026-11-01").json()
        assert detail["current_conclusion"] is None       # 失效状态持久化
        assert [h["id"] for h in detail["history"]] == [old_id]
        r = c2.post("/api/station-days/ST-A/2026-11-01/recompute").json()
        assert r["mean"] == pytest.approx((11.0 + 12.4 + 12.2) / 3)


def test_concurrent_corrections_and_recompute(tmp_path):
    db = str(tmp_path / "concurrent.db")
    init_db(db)
    conn = connect(db)
    try:
        rows = [(f"S{st}-{i}", f"ST-{st}", "2026-11-01", 10.0 + i, 0.4, f"B{st}")
                for st in range(4) for i in range(5)]
        services.upsert_samples(conn, [
            dict(zip(("sample_id", "station_id", "sample_date",
                      "concentration", "random_unc", "batch_id"), r)) for r in rows])
        for st in range(4):
            services.upsert_batch(conn, f"B{st}", 0.3)
        services.recompute_all_pending(conn)
    finally:
        conn.close()

    errors = []

    def worker(k):
        try:
            c = connect(db)
            try:
                for i in range(15):
                    st = (k + i) % 4
                    services.upsert_samples(c, [dict(
                        sample_id=f"S{st}-{i % 5}", station_id=f"ST-{st}",
                        sample_date="2026-11-01", concentration=9.0 + 0.1 * k + 0.01 * i,
                        random_unc=0.4, batch_id=f"B{st}")])
                    services.recompute_station_day(c, f"ST-{st}", "2026-11-01")
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 收敛后：每个站日至多一条现行结论，且与最终数据一致
    conn = connect(db)
    try:
        services.recompute_all_pending(conn)
        dup = conn.execute(
            "SELECT station_id, sample_date, COUNT(*) c FROM conclusions"
            " WHERE status='current' GROUP BY 1,2 HAVING c>1").fetchall()
        assert not dup
        for st in range(4):
            rows = conn.execute(
                "SELECT * FROM samples WHERE station_id=? AND sample_date='2026-11-01'",
                (f"ST-{st}",)).fetchall()
            expected = logic.compute(
                [logic.SampleInput(r["concentration"], r["random_unc"], r["batch_id"])
                 for r in rows], {f"B{st}": 0.3})
            cur = conn.execute(
                "SELECT * FROM conclusions WHERE station_id=?"
                " AND sample_date='2026-11-01' AND status='current'",
                (f"ST-{st}",)).fetchone()
            assert cur is not None
            assert cur["mean"] == pytest.approx(expected.mean)
            assert cur["decision"] == expected.decision
    finally:
        conn.close()


def test_csv_validation_rejects_bad_rows(client):
    r = upload(client, [
        ("S1", "ST-A", "2026-11-01", 12.0, 0.4, "B01"),
        ("S2", "ST-A", "2026-13-40", 12.0, 0.4, "B01"),   # 非法日期
        ("S3", "ST-A", "2026-11-01", "abc", 0.4, "B01"),  # 非法浓度
        ("S4", "ST-A", "2026-11-01", 12.0, -1, "B01"),    # 负不确定度
    ])
    assert r.status_code == 400
    assert r.json()["detail"]["total_errors"] == 3
    # 整体拒绝：一条也不写入
    assert client.get("/api/station-days").json() == []


def test_csv_header_alias_and_bom(client):
    text = "﻿sample_id,station_id,utc_date,concentration,random_unc,batch_id\n" \
           "S1,ST-A,2026-11-01,12.0,0.4,B01\n"
    r = client.post("/api/samples/upload",
                    files={"file": ("s.csv", text.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1


def test_web_pages_render(client):
    seed_two_station_days(client)
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.3})
    client.post("/api/batches", json={"batch_id": "B02", "systematic_unc": 0.5})
    client.post("/api/recompute-pending")
    r = client.get("/")
    assert r.status_code == 200 and "超标" in r.text and "未超标" in r.text
    r = client.get("/station-days/ST-A/2026-11-01")
    assert r.status_code == 200 and "本次判定所用批次" in r.text
    # 网页表单重算
    r = client.post("/web/recompute",
                    data={"station_id": "ST-A", "sample_date": "2026-11-01"},
                    follow_redirects=False)
    assert r.status_code == 303
