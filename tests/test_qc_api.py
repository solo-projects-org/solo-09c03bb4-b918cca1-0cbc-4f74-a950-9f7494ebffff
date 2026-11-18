"""质控端到端自测：录入/更正、失败闸门、失效语义、交错与重启、未启用批次不受影响。"""
import io

import pytest
from fastapi.testclient import TestClient

from app import logic, qc, services
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


def add_qc(client, batch, seq, measured, certified, std_dev):
    return client.post(f"/api/batches/{batch}/qc-readings",
                       json={"seq": seq, "measured": measured,
                             "certified": certified, "std_dev": std_dev})


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def seed(client):
    """ST-A/2026-11-01 用 B01+B02；ST-B/2026-11-01 只用 B01；ST-C 用 B03。"""
    r = upload(client, [
        ("S1", "ST-A", "2026-11-01", 12.0, 0.4, "B01"),
        ("S2", "ST-A", "2026-11-01", 12.4, 0.4, "B01"),
        ("S3", "ST-A", "2026-11-01", 12.2, 0.4, "B02"),
        ("S4", "ST-B", "2026-11-01", 8.0, 0.3, "B01"),
        ("S5", "ST-C", "2026-11-01", 5.0, 0.2, "B03"),
    ])
    assert r.status_code == 200, r.text
    for b, u in (("B01", 0.3), ("B02", 0.5), ("B03", 0.1)):
        assert client.post("/api/batches",
                           json={"batch_id": b, "systematic_unc": u}).status_code == 200
    client.post("/api/recompute-pending")


def test_qc_crud_and_status(client):
    seed(client)
    # 未录入读数 → 未启用
    d = client.get("/api/batches/B01/qc-readings").json()
    assert d["qc_status"] == qc.QC_NONE and d["readings"] == []
    # 两条合格读数 → 通过
    assert add_qc(client, "B01", 1, 10.1, 10.0, 0.5).json()["qc"]["status"] == qc.QC_PASSED
    assert add_qc(client, "B01", 2, 9.9, 10.0, 0.5).json()["qc"]["status"] == qc.QC_PASSED
    d = client.get("/api/batches/B01/qc-readings").json()
    assert d["qc_status"] == qc.QC_PASSED and d["reading_count"] == 2
    # 同序号重录＝更正
    r = add_qc(client, "B01", 2, 10.0, 10.0, 0.5).json()
    assert r["corrected"] is True
    assert len(r["qc"]["readings"]) == 2


def test_qc_validation_rejects_bad_inputs(client):
    seed(client)
    base = {"seq": 1, "measured": 10.0, "certified": 10.0, "std_dev": 0.5}
    for bad in [
        {"seq": 0}, {"seq": -1}, {"seq": 1.5}, {"seq": "x"},
        {"std_dev": 0}, {"std_dev": -0.1},
        {"measured": "Infinity"}, {"certified": "NaN"},
    ]:
        payload = {**base, **bad}
        r = client.post("/api/batches/B01/qc-readings", json=payload)
        assert r.status_code == 400, bad
    # 全部拒绝：没有任何读数写入
    assert client.get("/api/batches/B01/qc-readings").json()["reading_count"] == 0


def test_3s_failure_blocks_concentration_decision(client):
    seed(client)
    before_a = client.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
    before_b = client.get("/api/station-days/ST-B/2026-11-01").json()["current_conclusion"]
    assert before_a["decision"] == logic.EXCEEDS

    # 首次录入即 4σ 失败 → 使用 B01 的两个站日现行结论立即失效
    r = add_qc(client, "B01", 1, 14.0, 10.0, 1.0).json()
    assert r["qc"]["status"] == qc.QC_FAILED
    assert r["state_changed"] is True
    invalidated = {(x["station_id"], x["sample_date"]) for x in r["invalidated"]}
    assert invalidated == {("ST-A", "2026-11-01"), ("ST-B", "2026-11-01")}
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"] is None
    assert client.get("/api/station-days/ST-B/2026-11-01").json()[
        "current_conclusion"] is None

    # 重算不得给出浓度判定
    r = client.post("/api/station-days/ST-A/2026-11-01/recompute").json()
    assert r["decision"] == logic.QC_FAILED
    assert r["interval_low"] is None and r["interval_high"] is None
    assert r["combined_unc"] is None
    assert r["mean"] == pytest.approx((12.0 + 12.4 + 12.2) / 3)  # 均值仅供参考
    assert "B01" in r["qc"]["failed_batches"]
    trig = r["qc"]["failed_batches"]["B01"]["triggers"]
    assert trig[0]["rule"] == "3s" and trig[0]["seq"] == 1

    # 即使“重算全部待重算”，失败批次的站日也不会得到超标/未超标结论
    client.post("/api/recompute-pending")
    for sd in ("ST-A", "ST-B"):
        cur = client.get(f"/api/station-days/{sd}/2026-11-01").json()["current_conclusion"]
        assert cur["decision"] == logic.QC_FAILED
        assert cur["interval_low"] is None


def test_2s_2of2_failure_and_trigger_reading(client):
    seed(client)
    add_qc(client, "B02", 1, 11.5, 10.0, 0.5)   # +3σ 边界内（=3σ，不触发 3s）
    r = add_qc(client, "B02", 2, 11.2, 10.0, 0.5).json()  # +2.4σ，与前一次同向
    assert r["qc"]["status"] == qc.QC_FAILED
    t = r["qc"]["triggers"][0]
    assert t["rule"] == "2s_2of2" and t["paired_seq"] == 1 and t["seq"] == 2
    # 只有用 B02 的 ST-A 失效，ST-B 不受影响
    invalidated = {(x["station_id"], x["sample_date"]) for x in r["invalidated"]}
    assert invalidated == {("ST-A", "2026-11-01")}
    stb = client.get("/api/station-days/ST-B/2026-11-01").json()["current_conclusion"]
    assert stb["decision"] in (logic.EXCEEDS, logic.NOT_EXCEEDS, logic.UNDETERMINED)


def test_correcting_early_reading_rejudges_and_recovers(client):
    seed(client)
    add_qc(client, "B01", 1, 12.5, 10.0, 1.0)
    add_qc(client, "B01", 2, 12.6, 10.0, 1.0)   # 相邻同向 >2σ → 失败
    assert client.get("/api/batches/B01/qc-readings").json()["qc_status"] == qc.QC_FAILED
    client.post("/api/recompute-pending")
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"]["decision"] == logic.QC_FAILED

    # 更正早期序号 1 的读数 → 整批重判恢复通过，现行结论再次失效，须重新重算
    r = add_qc(client, "B01", 1, 10.5, 10.0, 1.0).json()  # +0.5σ
    assert r["qc"]["status"] == qc.QC_PASSED and r["state_changed"] is True
    assert client.get("/api/station-days/ST-A/2026-11-01").json()[
        "current_conclusion"] is None
    # 恢复通过后重新重算 → 才能重新给出浓度判定
    r = client.post("/api/station-days/ST-A/2026-11-01/recompute").json()
    assert r["decision"] == logic.EXCEEDS and r["interval_low"] is not None
    assert "qc" not in r


def test_failed_batch_keeps_blocking_after_recompute_pending(client):
    seed(client)
    add_qc(client, "B01", 1, 14.0, 10.0, 1.0)  # 失败
    r = client.post("/api/recompute-pending").json()
    for item in r["results"]:
        if item["sample_date"] == "2026-11-01" and item["station_id"] in ("ST-A", "ST-B"):
            assert item["decision"] == logic.QC_FAILED
    # 未启用质控的 B03 → ST-C 行为不变，正常判定
    stc = client.get("/api/station-days/ST-C/2026-11-01").json()["current_conclusion"]
    assert stc["decision"] == logic.NOT_EXCEEDS and stc["interval_high"] is not None


def test_interleaved_upload_and_calibration_correction(client):
    """质控录入与样本上传、校准更正交错：任何时刻都不能有基于失败批次的现行判定。"""
    seed(client)
    add_qc(client, "B01", 1, 14.0, 10.0, 1.0)  # B01 失败并失效相关结论
    # 交错 1：再上传使用 B01 的新样本（新站日），重算 → 仍被闸门拦截
    upload(client, [("S9", "ST-D", "2026-11-03", 20.0, 0.2, "B01")])
    r = client.post("/api/station-days/ST-D/2026-11-03/recompute").json()
    assert r["decision"] == logic.QC_FAILED
    # 交错 2：校准批系统不确定度更正（与质控无关），失效后重算仍被质控拦截
    client.post("/api/batches", json={"batch_id": "B01", "systematic_unc": 0.9})
    assert client.get("/api/station-days/ST-D/2026-11-03").json()[
        "current_conclusion"] is None
    r = client.post("/api/station-days/ST-D/2026-11-03/recompute").json()
    assert r["decision"] == logic.QC_FAILED and r["interval_low"] is None
    # 交错 3：恢复通过后，重算才能给出浓度判定（且使用更正后的系统不确定度）
    add_qc(client, "B01", 1, 10.2, 10.0, 1.0)
    assert client.get("/api/station-days/ST-D/2026-11-03").json()[
        "current_conclusion"] is None
    r = client.post("/api/station-days/ST-D/2026-11-03/recompute").json()
    assert r["decision"] == logic.EXCEEDS
    assert {b["batch_id"]: b["systematic_unc"] for b in r["batches"]}["B01"] == 0.9


def test_qc_failure_survives_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c:
        seed(c)
        add_qc(c, "B01", 1, 14.0, 10.0, 1.0)
        c.post("/api/recompute-pending")
        cur = c.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
        assert cur["decision"] == logic.QC_FAILED
        blocked_id = cur["id"]
    # 模拟服务重启：同库新建应用，质控状态与闸门结论均持久化
    with TestClient(create_app(db)) as c2:
        d = c2.get("/api/batches/B01/qc-readings").json()
        assert d["qc_status"] == qc.QC_FAILED
        cur = c2.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
        assert cur["id"] == blocked_id and cur["decision"] == logic.QC_FAILED
        # 重启后重算仍被拦截
        r = c2.post("/api/station-days/ST-A/2026-11-01/recompute").json()
        assert r["decision"] == logic.QC_FAILED


def test_qc_failure_priority_over_missing_calibration(client):
    # 站日同时缺校准数据且涉及失败质控批 → 优先报质控失败
    upload(client, [("S1", "ST-X", "2026-11-05", 12.0, 0.4, "B99")])
    add_qc(client, "B99", 1, 14.0, 10.0, 1.0)
    r = client.post("/api/station-days/ST-X/2026-11-05/recompute").json()
    assert r["decision"] == logic.QC_FAILED


def test_qc_failed_does_not_invalidate_on_every_failed_reading(client):
    # 失败状态下再录入一条仍失败的读数：状态未翻转，不重复失效其他站日
    seed(client)
    r1 = add_qc(client, "B01", 1, 14.0, 10.0, 1.0).json()
    assert r1["state_changed"] is True and len(r1["invalidated"]) == 2
    client.post("/api/recompute-pending")
    r2 = add_qc(client, "B01", 2, 6.0, 10.0, 1.0).json()  # -4σ，仍失败
    assert r2["qc"]["status"] == qc.QC_FAILED and r2["state_changed"] is False
    assert r2["invalidated"] == []
    # 现行闸门结论不受影响，仍然存在
    cur = client.get("/api/station-days/ST-A/2026-11-01").json()["current_conclusion"]
    assert cur["decision"] == logic.QC_FAILED


def test_web_qc_form_and_batch_page(client):
    seed(client)
    r = client.post("/web/qc-readings", data={
        "batch_id": "B01", "seq": 1, "measured": 14.0,
        "certified": 10.0, "std_dev": 1.0}, follow_redirects=False)
    assert r.status_code == 303 and "/batches/B01" in r.headers["location"]
    page = client.get("/batches/B01")
    assert page.status_code == 200
    assert "质控失败" in page.text and "偏差超过 3 倍标准差" in page.text
    # 总览页显示质控状态与站日警告
    index = client.get("/")
    assert "质控失败" in index.text and "涉及质控失败批次" in index.text
    # 详情页展示触发快照
    client.post("/api/recompute-pending")
    detail = client.get("/station-days/ST-A/2026-11-01")
    assert "质控失败触发读数（快照）" in detail.text
    # 非法输入回跳错误
    r = client.post("/web/qc-readings", data={
        "batch_id": "B01", "seq": 1, "measured": 10.0,
        "certified": 10.0, "std_dev": 0}, follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]


def test_services_directly_serialize_evaluation(client):
    # 直接调用服务层，验证读数按序号排序与派生状态一致
    from app.db import connect
    db = client.app.state.db_path
    conn = connect(db)
    try:
        for seq, m in [(3, 10.1), (1, 10.2), (2, 9.8)]:
            services.upsert_qc_reading(conn, "B77", seq, m, 10.0, 0.5)
        ev = services.evaluate_batch_qc(conn, "B77")
        assert [r.seq for r in ev.readings] == [1, 2, 3]
        assert ev.status == qc.QC_PASSED
    finally:
        conn.close()
