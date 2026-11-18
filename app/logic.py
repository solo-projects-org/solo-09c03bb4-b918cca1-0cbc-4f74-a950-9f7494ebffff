"""纯计算逻辑：均值、合成标准不确定度、95% 测量区间与超标判定。

模型约定（题目要求）：
- 样本等权：均值 = 浓度算术平均；
- 随机误差互相独立：均值方差贡献 = Σ u_random,i² / n²；
- 同批校准误差完全相关、不同批次独立：
  均值方差贡献 = Σ_b (n_b · u_sys,b)² / n²；
- 95% 测量区间 = 均值 ± 1.96 · 合成标准不确定度（正态假设）；
- 区间完全高于 10 μg/L 判超标，完全低于 10 μg/L 判未超标，否则无法确定；
- 缺少任一涉及批次的系统标准不确定度时不得给出结论。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

THRESHOLD_UG_L = 10.0   # 判定阈值 μg/L
COVERAGE_FACTOR = 1.96  # 正态分布 95% 包含因子

# 判定结果
EXCEEDS = "exceeds"                        # 超标
NOT_EXCEEDS = "not_exceeds"                # 未超标
UNDETERMINED = "undetermined"              # 无法确定（区间跨过阈值）
MISSING_CALIBRATION = "missing_calibration"  # 缺少校准数据，不得给出结论
QC_FAILED = "qc_failed"                    # 涉及批次质控失败，不得给出浓度判定

DECISION_LABELS = {
    EXCEEDS: "超标",
    NOT_EXCEEDS: "未超标",
    UNDETERMINED: "无法确定",
    MISSING_CALIBRATION: "缺少校准数据，无法判定",
    QC_FAILED: "质控失败，不得判定",
}


class MissingCalibrationError(Exception):
    """涉及的校准批中尚有未录入系统标准不确定度的批次。"""

    def __init__(self, missing_batches):
        self.missing_batches = list(missing_batches)
        super().__init__("缺少校准批系统标准不确定度: " + ", ".join(self.missing_batches))


@dataclass(frozen=True)
class SampleInput:
    concentration: float  # 浓度 μg/L
    random_unc: float     # 随机标准不确定度 μg/L
    batch_id: str         # 校准批号


@dataclass(frozen=True)
class BatchUsage:
    batch_id: str
    sample_count: int
    systematic_unc: float | None  # None 表示该批尚未录入系统标准不确定度


@dataclass(frozen=True)
class Computation:
    sample_count: int
    mean: float
    combined_unc: float
    interval_low: float
    interval_high: float
    decision: str
    batches: tuple[BatchUsage, ...]


def batch_usage(samples: Sequence[SampleInput],
                systematic_uncs: Mapping[str, float]) -> tuple[BatchUsage, ...]:
    """统计样本涉及的批次及每批样本数，并附上已录入的系统标准不确定度。"""
    counts: dict[str, int] = {}
    for s in samples:
        counts[s.batch_id] = counts.get(s.batch_id, 0) + 1
    return tuple(
        BatchUsage(batch_id=b, sample_count=counts[b],
                   systematic_unc=systematic_uncs.get(b))
        for b in sorted(counts)
    )


def compute_mean(samples: Sequence[SampleInput]) -> float:
    return sum(s.concentration for s in samples) / len(samples)


def compute(samples: Sequence[SampleInput],
            systematic_uncs: Mapping[str, float]) -> Computation:
    """计算均值、合成标准不确定度、95% 测量区间并给出判定。

    systematic_uncs: batch_id -> 系统标准不确定度。
    缺少任一涉及批次时抛出 MissingCalibrationError（调用方不得给出结论）。
    """
    if not samples:
        raise ValueError("样本为空，无法计算")
    n = len(samples)
    mean = compute_mean(samples)
    usage = batch_usage(samples, systematic_uncs)
    missing = [u.batch_id for u in usage if u.systematic_unc is None]
    if missing:
        raise MissingCalibrationError(missing)

    # 随机误差互相独立
    var_random = sum(s.random_unc ** 2 for s in samples) / (n * n)
    # 同批完全相关（按 n_b·u_sys 线性相加），不同批次独立（方差相加）
    var_systematic = sum((u.sample_count * u.systematic_unc) ** 2 for u in usage) / (n * n)

    combined = math.sqrt(var_random + var_systematic)
    half = COVERAGE_FACTOR * combined
    low, high = mean - half, mean + half
    if low > THRESHOLD_UG_L:
        decision = EXCEEDS
    elif high < THRESHOLD_UG_L:
        decision = NOT_EXCEEDS
    else:
        decision = UNDETERMINED
    return Computation(sample_count=n, mean=mean, combined_unc=combined,
                       interval_low=low, interval_high=high,
                       decision=decision, batches=usage)
