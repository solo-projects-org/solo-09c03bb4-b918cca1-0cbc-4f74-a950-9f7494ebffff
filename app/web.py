"""服务端 HTML 渲染（无前端框架，所有动态值均转义）。"""
from __future__ import annotations

import json
from html import escape

from .logic import COVERAGE_FACTOR, DECISION_LABELS, THRESHOLD_UG_L
from . import qc

_STYLE = """
<style>
 body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;margin:2rem auto;
      max-width:1150px;padding:0 1rem;color:#222}
 table{border-collapse:collapse;width:100%;margin:.8rem 0 1.6rem}
 th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;font-size:14px}
 th{background:#f0f0f0}
 .badge{padding:2px 10px;border-radius:10px;color:#fff;font-size:13px;white-space:nowrap}
 .red{background:#c0392b}.green{background:#27ae60}.orange{background:#e67e22}
 .gray{background:#7f8c8d}.blue{background:#2980b9}
 form.inline{display:inline}
 fieldset{border:1px solid #ccc;border-radius:6px;margin-bottom:1.2rem}
 .msg{padding:8px 12px;border-radius:6px;background:#eaf3ff;border:1px solid #b6d4fe;margin-bottom:1rem}
 .err{background:#fdecea;border-color:#f5c6cb}
 button{cursor:pointer;padding:4px 14px}
 nav a{margin-right:1rem}
 .muted{color:#777;font-size:13px}
 .qc-block{background:#fdecea;border:1px solid #f5c6cb;border-radius:6px;
           padding:.6rem 1.4rem}
 .qc-block li{margin:.25rem 0}
 tr.qc-hit{background:#fdecea}
</style>
"""


def fmt(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.6g}"
    return escape(str(x))


def decision_badge(decision: str | None) -> str:
    if decision is None:
        return '<span class="badge blue">待重算</span>'
    label = DECISION_LABELS.get(decision, decision)
    cls = {
        "exceeds": "red",
        "not_exceeds": "green",
        "undetermined": "orange",
        "missing_calibration": "gray",
        "qc_failed": "red",
    }.get(decision, "gray")
    return f'<span class="badge {cls}">{escape(label)}</span>'


def qc_badge(status: str) -> str:
    label = qc.QC_STATUS_LABELS.get(status, status)
    cls = {
        qc.QC_NONE: "gray",
        qc.QC_PASSED: "green",
        qc.QC_FAILED: "red",
    }.get(status, "gray")
    return f'<span class="badge {cls}">{escape(label)}</span>'


def layout(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)} · 水样铅浓度判定</title>"
        + _STYLE
        + "</head><body>"
        + "<nav><a href=\"/\">总览</a></nav>"
        + f"<h1>{escape(title)}</h1>"
        + body
        + "</body></html>"
    )


def _msg_html(msg: str, error: str) -> str:
    out = []
    if msg:
        out.append(f'<div class="msg">{escape(msg)}</div>')
    if error:
        out.append(f'<div class="msg err">{escape(error)}</div>')
    return "".join(out)


def dashboard(station_days: list[dict], batches: list[dict],
              msg: str = "", error: str = "") -> str:
    rows = []
    for d in station_days:
        concluded = d.get("conclusion_id") is not None
        decision = d.get("decision") if concluded else None
        station = escape(d["station_id"])
        date = escape(d["sample_date"])
        qc_warn = ""
        if d.get("qc_failed_batches"):
            qc_warn = (
                '<br><span class="badge red">涉及质控失败批次：'
                + escape(", ".join(d["qc_failed_batches"])) + "</span>"
            )
        rows.append(
            "<tr>"
            f"<td>{station}</td><td>{date}</td><td>{d['sample_count']}</td>"
            f"<td>{decision_badge(decision)}{qc_warn}</td>"
            f"<td>{fmt(d.get('mean'))}</td>"
            f"<td>{fmt(d.get('interval_low'))} ~ {fmt(d.get('interval_high'))}</td>"
            f"<td class=\"muted\">{escape(d.get('created_at') or '—')}</td>"
            f"<td><form class=\"inline\" method=\"post\" action=\"/web/recompute\">"
            f"<input type=\"hidden\" name=\"station_id\" value=\"{station}\">"
            f"<input type=\"hidden\" name=\"sample_date\" value=\"{date}\">"
            f"<button type=\"submit\">重算</button></form> "
            f"<a href=\"/station-days/{station}/{date}\">详情</a></td>"
            "</tr>"
        )
    station_table = (
        "<table><tr><th>站号</th><th>UTC 日期</th><th>样本数</th><th>判定</th>"
        "<th>均值 (μg/L)</th><th>95% 测量区间 (μg/L)</th><th>结论时间</th><th>操作</th></tr>"
        + ("".join(rows) if rows else '<tr><td colspan="8" class="muted">暂无样本数据</td></tr>')
        + "</table>"
    )

    batch_rows = []
    for b in batches:
        bid = escape(b["batch_id"])
        qc_cell = qc_badge(b.get("qc_status", qc.QC_NONE))
        if b.get("qc_reading_count"):
            qc_cell += (
                f' <a href="/batches/{bid}">'
                f"{b['qc_reading_count']} 条读数"
                + (f"（{b['qc_trigger_count']} 条触发）" if b.get("qc_trigger_count") else "")
                + "</a>"
            )
        else:
            qc_cell += f' <a href="/batches/{bid}">录入质控</a>'
        batch_rows.append(
            f"<tr><td>{bid}</td><td>{fmt(b['systematic_unc'])}</td>"
            f"<td>{qc_cell}</td>"
            f"<td class=\"muted\">{escape(b['updated_at'])}</td></tr>"
        )
    batch_table = (
        "<table><tr><th>校准批号</th><th>系统标准不确定度 (μg/L)</th>"
        "<th>质控状态</th><th>更新时间</th></tr>"
        + ("".join(batch_rows) if batch_rows
           else '<tr><td colspan="4" class="muted">尚未录入校准批</td></tr>')
        + "</table>"
    )

    return (
        _msg_html(msg, error)
        + '<fieldset><legend>上传样本 CSV（同号样本再次提交视为更正）</legend>'
        + '<form method="post" action="/web/upload" enctype="multipart/form-data">'
        + '<input type="file" name="file" accept=".csv" required> '
        + '<button type="submit">上传</button>'
        + '<div class="muted">必需列：sample_id, station_id, date(UTC, YYYY-MM-DD), '
          "concentration, random_unc, batch_id；浓度与不确定度单位 μg/L</div>"
        + "</form></fieldset>"
        + '<fieldset><legend>录入 / 更正校准批系统标准不确定度（同批次再次提交视为更正）</legend>'
        + '<form method="post" action="/web/batches">'
        + '批号 <input name="batch_id" required> '
        + '系统标准不确定度 (μg/L) <input name="systematic_unc" type="number" step="any" min="0" required> '
        + '<button type="submit">保存</button></form></fieldset>'
        + '<fieldset><legend>录入 / 更正参考物质控复测（同批号同序号再次提交视为更正；'
          "偏差&gt;3σ 或相邻同向两次各&gt;2σ 判该批质控失败）</legend>"
        + '<form method="post" action="/web/qc-readings">'
        + '批号 <input name="batch_id" required> '
        + '测量序号 <input name="seq" type="number" step="1" min="1" required> '
        + "测得值 <input name=\"measured\" type=\"number\" step=\"any\" required> "
        + "证书值 <input name=\"certified\" type=\"number\" step=\"any\" required> "
        + "标准差 <input name=\"std_dev\" type=\"number\" step=\"any\" min=\"0\" required> "
        + '<button type="submit">保存读数</button></form>'
        + '<div class="muted">标准差须为正数；单位与浓度一致（μg/L）。'
          "批次质控首次失败会立即使相关站日现行结论失效，且重算不得给出浓度判定。</div>"
        + "</fieldset>"
        + "<h2>站日判定"
        + ' <form class="inline" method="post" action="/web/recompute-pending">'
        + "<button type=\"submit\">重算全部待重算</button></form></h2>"
        + f'<p class="muted">判定规则：区间完全高于 {THRESHOLD_UG_L:g} μg/L 判超标，完全低于判未超标，'
        + f"否则无法确定；区间 = 均值 ± {COVERAGE_FACTOR} × 合成标准不确定度。"
        + "更正后受影响站日的旧结论立即失效，需重算；"
        + "涉及质控失败批次的站日只能得到「质控失败，不得判定」结论。</p>"
        + station_table
        + "<h2>校准批</h2>"
        + batch_table
    )


def detail_page(station_id: str, sample_date: str, samples: list[dict],
                live_batches: list[dict], current: dict | None,
                history: list[dict]) -> str:
    station = escape(station_id)
    date = escape(sample_date)

    sample_rows = "".join(
        f"<tr><td>{escape(s['sample_id'])}</td><td>{fmt(s['concentration'])}</td>"
        f"<td>{fmt(s['random_unc'])}</td><td>{escape(s['batch_id'])}</td>"
        f"<td class=\"muted\">{escape(s['updated_at'])}</td></tr>"
        for s in samples
    )
    sample_table = (
        "<table><tr><th>样本号</th><th>浓度 (μg/L)</th><th>随机标准不确定度 (μg/L)</th>"
        "<th>校准批号</th><th>更新时间</th></tr>" + sample_rows + "</table>"
    )

    live_rows = "".join(
        f"<tr><td>{escape(b['batch_id'])}</td><td>{b['sample_count']}</td>"
        f"<td>{fmt(b['systematic_unc'])}</td>"
        f"<td>{qc_badge(b.get('qc_status', qc.QC_NONE))} "
        f'<a href="/batches/{escape(b["batch_id"])}">质控详情</a></td></tr>'
        for b in live_batches
    )
    live_table = (
        "<table><tr><th>校准批号</th><th>该站日样本数</th><th>系统标准不确定度 (μg/L)</th>"
        "<th>质控状态</th></tr>"
        + live_rows + "</table>"
    )

    if current is None:
        current_html = ('<p><span class="badge blue">待重算</span> '
                        "当前没有现行结论（尚未计算或已被更正失效）。</p>")
    else:
        used_rows = "".join(
            f"<tr><td>{escape(b['batch_id'])}</td><td>{b['sample_count']}</td>"
            f"<td>{fmt(b['systematic_unc'])}</td></tr>"
            for b in current["batches"]
        )
        used_table = (
            "<table><tr><th>校准批号</th><th>样本数</th><th>系统标准不确定度 (μg/L)</th></tr>"
            + used_rows + "</table>"
        )
        qc_block = ""
        if current["decision"] == "qc_failed" and current.get("qc"):
            lines = []
            for bid, detail in current["qc"]["failed_batches"].items():
                for t in detail["triggers"]:
                    pair = f"（与序号 {t['paired_seq']} 相邻同向）" if t.get("paired_seq") else ""
                    lines.append(
                        f"<li>批号 {escape(bid)} 序号 {t['seq']}{pair}："
                        f"{escape(t['rule_label'])}，偏差 {fmt(t['deviation'])}，"
                        f"门限 ±{fmt(t['limit'])}</li>")
            qc_block = ('<h3>质控失败触发读数（快照）</h3><ul class="qc-block">'
                        + "".join(lines) + "</ul>")
        if current["decision"] == "qc_failed":
            current_html = (
                f"<p>判定：{decision_badge(current['decision'])}　"
                f"样本数：{current['sample_count']}　"
                f"均值（仅供参考）：{fmt(current['mean'])} μg/L　"
                f"95% 测量区间：—（不得给出浓度判定）　"
                f"<span class=\"muted\">计算于 {escape(current['created_at'])}</span></p>"
                + qc_block
            )
        else:
            current_html = (
                f"<p>判定：{decision_badge(current['decision'])}　"
                f"样本数：{current['sample_count']}　"
                f"均值：{fmt(current['mean'])} μg/L　"
                f"合成标准不确定度：{fmt(current['combined_unc'])} μg/L　"
                f"95% 测量区间：[{fmt(current['interval_low'])}, {fmt(current['interval_high'])}] μg/L　"
                f"<span class=\"muted\">计算于 {escape(current['created_at'])}</span></p>"
                "<h3>本次判定所用批次</h3>" + used_table
            )

    hist_rows = "".join(
        f"<tr><td>{h['id']}</td><td>{decision_badge(h['decision'])}</td>"
        f"<td>{fmt(h['mean'])}</td>"
        f"<td>{fmt(h['interval_low'])} ~ {fmt(h['interval_high'])}</td>"
        f"<td class=\"muted\">{escape(h['created_at'])}</td></tr>"
        for h in history
    )
    hist_table = (
        "<table><tr><th>ID</th><th>判定</th><th>均值</th><th>95% 测量区间</th><th>计算时间</th></tr>"
        + (hist_rows if hist_rows else '<tr><td colspan="5" class="muted">无历史结论</td></tr>')
        + "</table>"
    )

    return (
        f'<p><form class="inline" method="post" action="/web/recompute">'
        f'<input type="hidden" name="station_id" value="{station}">'
        f'<input type="hidden" name="sample_date" value="{date}">'
        f'<button type="submit">立即重算</button></form></p>'
        "<h2>现行结论</h2>" + current_html
        + "<h2>样本</h2>" + sample_table
        + "<h2>当前样本涉及的批次（实时）</h2>" + live_table
        + "<h2>历史结论（已失效）</h2>" + hist_table
    )


def parse_batches_json(conclusion_row: dict) -> dict:
    """把 conclusions 行的 batches_json/qc_json 解析为结构，供页面/JSON 使用。"""
    out = dict(conclusion_row)
    out["batches"] = json.loads(out.pop("batches_json"))
    raw_qc = out.pop("qc_json", None)
    out["qc"] = json.loads(raw_qc) if raw_qc else None
    return out


def batch_qc_page(batch_id: str, batch_row: dict | None, status: str,
                  evaluation, users: list[dict], msg: str = "", error: str = "") -> str:
    """单个校准批的质控详情页：录入表单、全部读数、触发明细与使用该批的站日。"""
    bid = escape(batch_id)
    reading_rows = []
    hit_seqs = {t.seq for t in evaluation.triggers}
    paired_seqs = {t.paired_seq for t in evaluation.triggers if t.paired_seq is not None}
    for r in evaluation.readings:
        d = r.measured - r.certified
        z = d / r.std_dev
        cls = ' class="qc-hit"' if r.seq in hit_seqs or r.seq in paired_seqs else ""
        reading_rows.append(
            f"<tr{cls}>"
            f"<td>{r.seq}</td><td>{fmt(r.measured)}</td><td>{fmt(r.certified)}</td>"
            f"<td>{fmt(r.std_dev)}</td><td>{fmt(d)}</td><td>{fmt(z)}</td>"
            "</tr>"
        )
    reading_table = (
        "<table><tr><th>测量序号</th><th>测得值</th><th>证书值</th><th>标准差</th>"
        "<th>偏差</th><th>偏差/σ</th></tr>"
        + ("".join(reading_rows) if reading_rows
           else '<tr><td colspan="6" class="muted">暂无质控读数（未启用质控）</td></tr>')
        + "</table>"
    )

    trigger_lines = []
    for t in evaluation.triggers:
        pair = f"（与序号 {t.paired_seq} 相邻同向）" if t.paired_seq is not None else ""
        trigger_lines.append(
            f"<li>序号 {t.seq}{pair}：{escape(qc.RULE_LABELS[t.rule])}；"
            f"偏差 {fmt(t.deviation)}，门限 ±{fmt(t.limit)}</li>")
    if trigger_lines:
        trigger_html = ('<h3>触发读数</h3><ul class="qc-block">'
                        + "".join(trigger_lines) + "</ul>")
    else:
        trigger_html = '<p class="muted">未触发任何规则。</p>'

    if batch_row is None:
        batch_info = ('<p class="muted">该批号尚未录入系统标准不确定度；'
                      "质控读数可先录入，但使用该批的站日仍会因缺校准数据而无法判定。</p>")
    else:
        batch_info = (
            f"<p>系统标准不确定度：{fmt(batch_row['systematic_unc'])} μg/L　"
            f"<span class=\"muted\">更新于 {escape(batch_row['updated_at'])}</span></p>"
        )

    user_rows = "".join(
        f"<tr><td><a href=\"/station-days/{escape(u['station_id'])}/{escape(u['sample_date'])}\">"
        f"{escape(u['station_id'])} / {escape(u['sample_date'])}</a></td>"
        f"<td>{u['sample_count']}</td></tr>"
        for u in users
    )
    user_table = (
        "<table><tr><th>使用该批的站日</th><th>样本数</th></tr>"
        + (user_rows if user_rows
           else '<tr><td colspan="2" class="muted">当前没有样本使用该批</td></tr>')
        + "</table>"
    )

    return (
        _msg_html(msg, error)
        + f"<p>当前质控状态：{qc_badge(status)}（共 {evaluation.reading_count} 条读数）</p>"
        + batch_info
        + '<fieldset><legend>录入 / 更正质控读数（同序号重录视为更正，提交后重判整批）</legend>'
        + '<form method="post" action="/web/qc-readings">'
        + f'<input type="hidden" name="batch_id" value="{bid}">'
        + '测量序号 <input name="seq" type="number" step="1" min="1" required> '
        + '测得值 <input name="measured" type="number" step="any" required> '
        + '证书值 <input name="certified" type="number" step="any" required> '
        + '标准差 <input name="std_dev" type="number" step="any" min="0" required> '
        + '<button type="submit">保存读数</button></form></fieldset>'
        + "<h3>全部读数（按序号排序）</h3>" + reading_table
        + trigger_html
        + "<h3>影响范围</h3>" + user_table
    )
