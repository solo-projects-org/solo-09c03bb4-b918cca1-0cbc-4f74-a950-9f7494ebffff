"""纯计算逻辑自测：均值、合成不确定度（相关模型）、区间与判定边界。"""
import math

import pytest

from app import logic
from app.logic import SampleInput


def make(values, random_unc, batch):
    return [SampleInput(concentration=v, random_unc=random_unc, batch_id=batch)
            for v in values]


def test_single_sample_combines_random_and_systematic():
    # n=1：u_c = sqrt(u_r^2 + u_s^2)
    samples = make([12.0], 0.3, "B1")
    comp = logic.compute(samples, {"B1": 0.4})
    assert comp.mean == pytest.approx(12.0)
    assert comp.combined_unc == pytest.approx(math.hypot(0.3, 0.4))
    assert comp.interval_low == pytest.approx(12.0 - 1.96 * 0.5)
    assert comp.interval_high == pytest.approx(12.0 + 1.96 * 0.5)
    assert comp.decision == logic.EXCEEDS  # 区间 [11.02, 12.98] 完全高于 10


def test_same_batch_systematic_fully_correlated():
    # 同批 4 样本：系统项 = (4·u_s)^2/16 = u_s^2（不随 n 减小）
    samples = make([12.0, 12.0, 12.0, 12.0], 0.5, "B1")
    comp = logic.compute(samples, {"B1": 0.3})
    var_random = 4 * 0.25 / 16
    var_sys = (4 * 0.3) ** 2 / 16
    assert comp.combined_unc == pytest.approx(math.sqrt(var_random + var_sys))
    assert var_sys == pytest.approx(0.09)  # 完全相关：等于单样本系统方差


def test_different_batches_independent():
    # 两批各 2 样本：系统项 = ((2·u1)^2 + (2·u2)^2)/16
    samples = (make([12.0, 12.0], 0.0, "B1") + make([12.0, 12.0], 0.0, "B2"))
    comp = logic.compute(samples, {"B1": 0.3, "B2": 0.4})
    var_sys = ((2 * 0.3) ** 2 + (2 * 0.4) ** 2) / 16
    assert comp.combined_unc == pytest.approx(math.sqrt(var_sys))
    # 若错误地按完全相关合并会得 (2·0.3+2·0.4)^2/16，明显更大
    assert comp.combined_unc < (2 * 0.3 + 2 * 0.4) / 4


def test_random_errors_average_down():
    samples = make([12.0] * 4, 0.5, "B1")
    comp = logic.compute(samples, {"B1": 0.0})
    assert comp.combined_unc == pytest.approx(0.5 / 2)  # u_r / sqrt(n)


def test_decision_boundaries():
    # 区间完全高于 10 → 超标
    comp = logic.compute(make([10.5], 0.1, "B"), {"B": 0.0})
    assert comp.interval_low > 10 and comp.decision == logic.EXCEEDS
    # 区间完全低于 10 → 未超标
    comp = logic.compute(make([9.5], 0.1, "B"), {"B": 0.0})
    assert comp.interval_high < 10 and comp.decision == logic.NOT_EXCEEDS
    # 区间跨过 10 → 无法确定
    comp = logic.compute(make([10.1], 0.5, "B"), {"B": 0.0})
    assert comp.interval_low < 10 < comp.interval_high
    assert comp.decision == logic.UNDETERMINED


def test_decision_boundary_is_strict(monkeypatch):
    # 区间端点恰好等于 10 时不算“完全高于/低于” → 无法确定
    monkeypatch.setattr(logic, "COVERAGE_FACTOR", 2.0)
    comp = logic.compute(make([12.0], 1.0, "B"), {"B": 0.0})
    assert comp.interval_low == 10.0 and comp.decision == logic.UNDETERMINED
    comp = logic.compute(make([8.0], 1.0, "B"), {"B": 0.0})
    assert comp.interval_high == 10.0 and comp.decision == logic.UNDETERMINED


def test_missing_calibration_raises():
    samples = make([12.0], 0.1, "B1") + make([11.0], 0.1, "B2")
    with pytest.raises(logic.MissingCalibrationError) as exc:
        logic.compute(samples, {"B1": 0.2})
    assert exc.value.missing_batches == ["B2"]


def test_batch_usage_counts():
    samples = make([1.0, 2.0], 0.1, "B2") + make([3.0], 0.1, "B1")
    usage = logic.batch_usage(samples, {"B1": 0.5})
    assert [(u.batch_id, u.sample_count, u.systematic_unc) for u in usage] == [
        ("B1", 1, 0.5), ("B2", 2, None),
    ]


def test_empty_samples_rejected():
    with pytest.raises(ValueError):
        logic.compute([], {})
