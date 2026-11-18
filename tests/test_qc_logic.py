"""质控规则纯计算自测：3s 与相邻同向 2s 规则、边界严格性、更正后重判。"""
import pytest

from app import qc
from app.qc import QcReading, RULE_2S_2OF2, RULE_3S, QC_FAILED, QC_PASSED


def r(seq, measured, certified=10.0, std_dev=1.0):
    return QcReading(seq=seq, measured=measured,
                     certified=certified, std_dev=std_dev)


def test_empty_series_passes():
    ev = qc.evaluate([])
    assert ev.status == QC_PASSED and ev.reading_count == 0
    assert ev.triggers == ()


def test_within_limits_passes():
    # 偏差恰好 2σ/3σ（相等）不触发，严格大于才触发
    ev = qc.evaluate([r(1, 13.0), r(2, 12.0), r(3, 7.0)])  # +3σ, +2σ, -3σ 边界
    assert ev.status == QC_PASSED and ev.triggers == ()


def test_3s_rule_triggers():
    ev = qc.evaluate([r(1, 11.0), r(2, 14.0)])  # +1σ, +4σ
    assert ev.status == QC_FAILED
    assert len(ev.triggers) == 1
    t = ev.triggers[0]
    assert t.rule == RULE_3S and t.seq == 2 and t.paired_seq is None
    assert t.deviation == pytest.approx(4.0) and t.limit == pytest.approx(3.0)


def test_negative_3s_triggers():
    ev = qc.evaluate([r(1, 6.0)])  # -4σ
    assert ev.status == QC_FAILED
    assert ev.triggers[0].rule == RULE_3S
    assert ev.triggers[0].deviation == pytest.approx(-4.0)


def test_2s_2of2_same_direction_triggers():
    # 两次同向、各超 2σ（但都不超 3σ）→ 仅触发 2-of-2
    ev = qc.evaluate([r(1, 12.5), r(2, 12.6)])  # +2.5σ, +2.6σ
    assert ev.status == QC_FAILED
    assert len(ev.triggers) == 1
    t = ev.triggers[0]
    assert t.rule == RULE_2S_2OF2 and t.seq == 2 and t.paired_seq == 1
    assert t.deviation == pytest.approx(2.6) and t.limit == pytest.approx(2.0)


def test_2s_2of2_negative_direction_triggers():
    ev = qc.evaluate([r(1, 7.4), r(2, 7.3)])  # -2.6σ, -2.7σ
    assert ev.status == QC_FAILED
    t = ev.triggers[0]
    assert t.rule == RULE_2S_2OF2 and t.paired_seq == 1 and t.seq == 2


def test_opposite_direction_does_not_trigger_2of2():
    # 各超 2σ 但方向相反 → 不触发
    ev = qc.evaluate([r(1, 12.5), r(2, 7.5)])
    assert ev.status == QC_PASSED and ev.triggers == ()


def test_boundary_equal_2sigma_not_trigger():
    # 恰好 +2σ 两次（相等而非大于）→ 不触发
    ev = qc.evaluate([r(1, 12.0), r(2, 12.0)])
    assert ev.status == QC_PASSED


def test_adjacency_is_by_seq_order_not_insertion_order():
    # 乱序录入：按序号排序后，序号 1 与 2 相邻同向超 2σ
    ev = qc.evaluate([r(2, 12.5), r(1, 12.4), r(3, 10.0)])
    assert ev.status == QC_FAILED
    t = ev.triggers[0]
    assert t.rule == RULE_2S_2OF2 and t.paired_seq == 1 and t.seq == 2


def test_non_adjacent_pair_does_not_trigger():
    # 序号 1、3 同向超 2σ，但中间有序号 2（不超 2σ）→ 不相邻，不触发
    ev = qc.evaluate([r(1, 12.5), r(2, 10.5), r(3, 12.5)])
    assert ev.status == QC_PASSED


def test_correction_rejudges_whole_series():
    # 初始：序号 1、2 同向超 2σ → 失败
    ev = qc.evaluate([r(1, 12.5), r(2, 12.6)])
    assert ev.failed
    # 更正序号 1 使其回到 1σ → 整批重判通过（模拟同序号重录后的集合）
    ev2 = qc.evaluate([r(1, 11.0), r(2, 12.6)])
    assert ev2.status == QC_PASSED and ev2.triggers == ()


def test_correction_introduces_failure():
    ev = qc.evaluate([r(1, 10.5), r(2, 10.5)])
    assert ev.status == QC_PASSED
    ev2 = qc.evaluate([r(1, 10.5), r(2, 14.0)])  # 更正序号 2 → 4σ
    assert ev2.failed and ev2.triggers[0].rule == RULE_3S


def test_per_reading_std_dev_used():
    # 每读数标准差不同：读数 2 的 σ=2，偏差 4.5 仅 2.25σ；读数 1 的 σ=1，2.5σ
    ev = qc.evaluate([r(1, 12.5, std_dev=1.0), r(2, 14.5, std_dev=2.0)])
    # 读数1: 2.5σ（>2σ），读数2: 2.25σ（>2σ）→ 同向 2-of-2 触发；均未超各自 3σ
    rules = {t.rule for t in ev.triggers}
    assert rules == {RULE_2S_2OF2}


def test_both_rules_can_trigger_and_collect_all():
    # 读数 1 超 3σ；读数 2、3 相邻同向超 2σ
    ev = qc.evaluate([r(1, 14.0), r(2, 12.5), r(3, 12.5)])
    rules = sorted(t.rule for t in ev.triggers)
    assert RULE_3S in rules and RULE_2S_2OF2 in rules
    seqs_3s = [t.seq for t in ev.triggers if t.rule == RULE_3S]
    assert seqs_3s == [1]


def test_serialization_helpers():
    ev = qc.evaluate([r(1, 14.0)])
    d = qc.evaluation_as_dict(ev)
    assert d["status"] == QC_FAILED
    assert d["triggers"][0]["rule_label"] == qc.RULE_LABELS[RULE_3S]
    assert d["readings"][0]["deviation"] == pytest.approx(4.0)
