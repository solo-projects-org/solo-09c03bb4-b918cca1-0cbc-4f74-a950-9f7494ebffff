"""服务端 HTML 渲染（无前端框架，所有动态值均转义）。"""
from __future__ import annotations

import json
from html import escape

from .logic import COVERAGE_FACTOR, DECISION_LABELS, THRESHOLD_UG_L

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
    }.get(decision, "gray")
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
        rows.append(
            "<tr>"
            f"<td>{station}</td><td>{date}</td><td>{d['sample_count']}</td>"
            f"<td>{decision_badge(decision)}</td>"
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
        batch_rows.append(
            f"<tr><td>{escape(b['batch_id'])}</td><td>{fmt(b['systematic_unc'])}</td>"
            f"<td class=\"muted\">{escape(b['updated_at'])}</td></tr>"
        )
    batch_table = (
        "<table><tr><th>校准批号</th><th>系统标准不确定度 (μg/L)</th><th>更新时间</th></tr>"
        + ("".join(batch_rows) if batch_rows else '<tr><td colspan="3" class="muted">尚未录入校准批</td></tr>')
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
        + "<h2>站日判定"
        + ' <form class="inline" method="post" action="/web/recompute-pending">'
        + "<button type=\"submit\">重算全部待重算</button></form></h2>"
        + f'<p class="muted">判定规则：区间完全高于 {THRESHOLD_UG_L:g} μg/L 判超标，完全低于判未超标，'
        + f"否则无法确定；区间 = 均值 ± {COVERAGE_FACTOR} × 合成标准不确定度。"
        + "更正后受影响站日的旧结论立即失效，需重算。</p>"
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
        f"<td>{fmt(b['systematic_unc'])}</td></tr>"
        for b in live_batches
    )
    live_table = (
        "<table><tr><th>校准批号</th><th>该站日样本数</th><th>系统标准不确定度 (μg/L)</th></tr>"
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
    """把 conclusions 行的 batches_json 解析为列表，供页面/JSON 使用。"""
    out = dict(conclusion_row)
    out["batches"] = json.loads(out.pop("batches_json"))
    return out
