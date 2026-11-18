"""质控纯计算：参考物复测读数的 3s / 相邻同向 2s 规则判定。

无 I/O，可独立单测。每个读数携带其自身的证书值与正标准差。

判定规则（题目要求）：
- **3s**：任一读数偏差绝对值 |measured − certified| > 3 × std_dev → 整批失败；
- **2-of-2 同向**：读数按测量序号排序后，相邻两次偏差同向（同号），
  且两次的偏差绝对值各自 > 2 × 自身标准差 → 整批失败；
- 门限为严格大于：偏差恰好等于 2σ/3σ 不触发；
- 更正任一读数后，对整批序列（按序号排序）重新判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 质控状态
QC_NONE = "none"      # 该批尚未启用质控（没有任何读数）
QC_PASSED = "passed"  # 已启用且未触发任何规则
QC_FAILED = "failed"  # 触发至少一条规则

# 规则标识
RULE_3S = "3s"
RULE_2S_2OF2 = "2s_2of2"

RULE_LABELS = {
    RULE_3S: "偏差超过 3 倍标准差",
    RULE_2S_2OF2: "相邻两次同向偏差各超过 2 倍标准差",
}

QC_STATUS_LABELS = {
    QC_NONE: "未启用质控",
    QC_PASSED: "质控通过",
    QC_FAILED: "质控失败",
}


@dataclass(frozen=True)
class QcReading:
    seq: int            # 测量序号（正整数）
    measured: float     # 测得值
    certified: float    # 证书值（参考值）
    std_dev: float      # 正标准差


@dataclass(frozen=True)
class QcTrigger:
    rule: str               # RULE_3S / RULE_2S_2OF2
    seq: int                # 触发读数序号（2-of-2 取相邻对的后一个）
    paired_seq: int | None  # 2-of-2 相邻对的前一个读数序号；3s 为 None
    deviation: float        # 触发读数的偏差 measured − certified
    limit: float            # 该读数对应的偏差门限（3σ 或 2σ）


@dataclass(frozen=True)
class QcEvaluation:
    status: str                       # passed / failed（空序列由调用方按 none 处理）
    reading_count: int
    triggers: tuple[QcTrigger, ...]
    readings: tuple[QcReading, ...]

    @property
    def failed(self) -> bool:
        return self.status == QC_FAILED


def deviation(reading: QcReading) -> float:
    return reading.measured - reading.certified


def evaluate(readings: Sequence[QcReading]) -> QcEvaluation:
    """对整批读数按序号排序后重新判定，返回触发读数明细。

    空序列视为 passed（无触发）；调用方通过 reading_count==0 区分“未启用”。
    """
    ordered = sorted(readings, key=lambda r: r.seq)
    triggers: list[QcTrigger] = []

    # 规则 1：单点 3s
    for r in ordered:
        d = deviation(r)
        limit3 = 3.0 * r.std_dev
        if abs(d) > limit3:
            triggers.append(QcTrigger(rule=RULE_3S, seq=r.seq, paired_seq=None,
                                      deviation=d, limit=limit3))

    # 规则 2：按序号相邻的两次同向、各自超 2σ
    for prev, cur in zip(ordered, ordered[1:]):
        d_prev, d_cur = deviation(prev), deviation(cur)
        same_direction = (d_prev > 0 and d_cur > 0) or (d_prev < 0 and d_cur < 0)
        if not same_direction:
            continue
        if abs(d_prev) > 2.0 * prev.std_dev and abs(d_cur) > 2.0 * cur.std_dev:
            triggers.append(QcTrigger(rule=RULE_2S_2OF2, seq=cur.seq,
                                      paired_seq=prev.seq,
                                      deviation=d_cur, limit=2.0 * cur.std_dev))

    status = QC_FAILED if triggers else QC_PASSED
    return QcEvaluation(status=status, reading_count=len(ordered),
                        triggers=tuple(triggers), readings=tuple(ordered))


def reading_as_dict(reading: QcReading) -> dict:
    return {"seq": reading.seq, "measured": reading.measured,
            "certified": reading.certified, "std_dev": reading.std_dev,
            "deviation": deviation(reading)}


def trigger_as_dict(trigger: QcTrigger) -> dict:
    return {"rule": trigger.rule, "rule_label": RULE_LABELS[trigger.rule],
            "seq": trigger.seq, "paired_seq": trigger.paired_seq,
            "deviation": trigger.deviation, "limit": trigger.limit}


def evaluation_as_dict(evaluation: QcEvaluation) -> dict:
    return {
        "status": evaluation.status,
        "reading_count": evaluation.reading_count,
        "triggers": [trigger_as_dict(t) for t in evaluation.triggers],
        "readings": [reading_as_dict(r) for r in evaluation.readings],
    }
