"""N1 的需求挖掘：`d(g) = freq(g) × cost(g)`（P2）。

对每条轨迹（一条候选证明的逐步记录）里的**子目标签名**做聚合，得到"需求强度"：

* `freq` 分层统计：出现在成功轨迹（`verified=true`）里算 `freq_success`，
  出现在失败轨迹里算 `freq_failure` —— SGS 的教训是"失败里出现的子目标"与
  "成功里出现的子目标"含义完全不同，混在一起算会把死路伪影当成需求；
* `cost` = 该子目标在轨迹里出现位置的平均序号（越靠后越"下游"、越难），
  与规格 5.2 节一致（**不是** `goalsLeft`）；
* `score = freq_total × avg_cost`，按分数排序；
* 四条分桶规则（`min_targets` 默认 2）：

| 跨 ≥min_targets 个目标 | 有成功轨迹证据 | bucket | 含义 |
|---|---|---|---|
| 是 | 是 | `demand` | 真需求：够广且被成功证明支持 → N1 的输入 |
| 是 | 否 | `suspect_failure_only` | 出现很广但从没出现在成功轨迹里 → 可疑（共同的死路），不当需求 |
| 否 | 是 | `local` | 局部需求：有成功证据但不够广 → 留着观察 |
| 否 | 否 | `artifact` | 只在不成功的轨迹里、且只在一个目标上 → 死路伪影的代理判据 |

所有被过滤掉的条目都保留在报告里（带 `bucket` 标签），不允许静默丢弃——
否则"过滤前后差别"这件事就无从检验。

## P0 修复：`freq` 按**不同目标**计数

规格 5.2 节的 `freq(g)` 定义是"出现过 g 的**不同题目**数"，而 `min_targets` 那道门槛
也是跨目标的门槛。早先的实现把**候选作业 id** 当目标用（一条目标的 k 篇候选 = k 个目标），
于是门槛永远能过、"需求"退化成"同一个目标的候选里反复出现的子目标"。

现在统计口径是：

* `targets` = 出现过该签名的**不同目标**集合（来自 `trace["target"]`）；
* `freq_total` = `len(targets)`（跨目标频次，规格定义的那个量）；
* `freq_success` / `freq_failure` = 出现在多少个不同目标的**成功 / 失败**轨迹里
  （同一目标既有成功又有失败轨迹时两边都计，这正是"有成功证据"的含义）；
* `occurrences` = 签名出现的总次数（诊断用，**不参与**打分）；
* `steps` / `avg_position` = 出现位置（从 1 开始）的计数与均值，即 `cost(g)`。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Iterable

from sgsr.data.schema import normalize_sig


@dataclass
class DemandEntry:
    sig: str
    #: 出现过该签名的不同**成功**目标
    targets_success: set[str] = field(default_factory=set)
    #: 出现过该签名的不同**失败**目标
    targets_failure: set[str] = field(default_factory=set)
    #: 该签名出现的总次数（诊断用；一条目标的多篇候选会被多次计入）
    occurrences: int = 0
    #: 出现位置之和（位置从 1 开始），除以 `position_count` 得 `avg_cost`
    position_sum: int = 0
    position_count: int = 0
    bucket: str = "local"

    @property
    def targets(self) -> set[str]:
        return self.targets_success | self.targets_failure

    @property
    def num_targets(self) -> int:
        return len(self.targets)

    @property
    def freq_total(self) -> int:
        """规格定义的 `freq(g)`：出现过 g 的**不同目标**数。"""
        return self.num_targets

    @property
    def freq_success(self) -> int:
        return len(self.targets_success)

    @property
    def freq_failure(self) -> int:
        return len(self.targets_failure)

    @property
    def avg_cost(self) -> float:
        return self.position_sum / self.position_count if self.position_count else 0.0

    @property
    def score(self) -> float:
        return self.freq_total * self.avg_cost

    def to_dict(self) -> dict:
        return {
            "sig": self.sig,
            "freq_total": self.freq_total,
            "freq_success": self.freq_success,
            "freq_failure": self.freq_failure,
            "occurrences": self.occurrences,
            "avg_cost": round(self.avg_cost, 3),
            "targets": sorted(self.targets),
            "num_targets": self.num_targets,
            "score": round(self.score, 3),
            "bucket": self.bucket,
        }


def mine(traces: Iterable[dict], min_targets: int = 2) -> dict:
    """挖掘需求。返回报告字典（含 `entries` 与分桶统计）。

    `traces` 的每条必须带 `target`（不同**数学目标**的标识）与 `id`（候选作业标识）。
    只有 `id` 的旧数据会让统计退回"候选当目标"的老错，因此这里**显式检查**并把问题
    记进报告的 `problems` 字段，而不是静默算出一个好看的数字。
    """
    table: dict[str, DemandEntry] = {}
    trace_count = 0
    verified_count = 0
    step_count = 0
    problems: list[str] = []
    missing_target = 0

    for trace in traces:
        trace_count += 1
        verified = trace.get("verified") is True
        verified_count += 1 if verified else 0
        target = str(trace.get("target") or "").strip()
        if not target:
            missing_target += 1
            target = f"<no-target:{trace.get('id')}>"
        for position, step in enumerate(trace.get("steps", []), start=1):
            sig = normalize_sig(step.get("signature", ""))
            if not sig:
                continue
            step_count += 1
            entry = table.setdefault(sig, DemandEntry(sig=sig))
            entry.occurrences += 1
            entry.position_sum += position
            entry.position_count += 1
            if verified:
                entry.targets_success.add(target)
            else:
                entry.targets_failure.add(target)

    if missing_target:
        problems.append(
            f"{missing_target}/{trace_count} 条轨迹没有 target 字段："
            "统计会把它们当成互不相同的目标（见 schema.trace_from_job）"
        )

    for entry in table.values():
        repeat_ok = entry.num_targets >= min_targets
        if not repeat_ok:
            entry.bucket = ("artifact"
                            if (entry.freq_success == 0 and entry.freq_failure > 0) else "local")
        elif entry.freq_success == 0:
            # 跨目标重复但只在失败轨迹里出现：可疑（可能是共同的死路），不直接当需求
            entry.bucket = "suspect_failure_only"
        else:
            entry.bucket = "demand"

    entries = sorted(table.values(), key=lambda e: (-e.score, e.sig))
    buckets = collections.Counter(e.bucket for e in entries)
    distinct_targets = {
        str(t.get("target") or "").strip() for t in traces if str(t.get("target") or "").strip()
    }
    return {
        "min_targets": min_targets,
        "traces": trace_count,
        "verified_traces": verified_count,
        "distinct_targets": len(distinct_targets),
        "steps": step_count,
        "signatures": len(entries),
        "buckets": dict(buckets),
        "demand_count": buckets.get("demand", 0),
        "problems": problems,
        "entries": [e.to_dict() for e in entries],
        "top_demand": [e.to_dict() for e in entries if e.bucket == "demand"][:20],
    }
