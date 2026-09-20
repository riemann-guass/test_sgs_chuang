"""N1 的需求挖掘：`d(g) = freq(g) × cost(g)`（P2）。

对每条轨迹（一条候选证明的逐步记录）里的**子目标签名**做聚合，得到"需求强度"：

* `freq` 分层统计：出现在成功轨迹（`verified=true`）里算 `freq_success`，
  出现在失败轨迹里算 `freq_failure` —— SGS 的教训是"失败里出现的子目标"与
  "成功里出现的子目标"含义完全不同，混在一起算会把死路伪影当成需求；
* `cost` 用"该步之后剩余的子目标数"作代理（v1，见 `docs/phase8-log.md` 的局限说明）；
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
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Iterable

from sgsr.data.schema import normalize_sig


@dataclass
class DemandEntry:
    sig: str
    freq_total: int = 0
    freq_success: int = 0
    freq_failure: int = 0
    cost_sum: int = 0
    targets: set[str] = field(default_factory=set)
    bucket: str = "local"

    @property
    def avg_cost(self) -> float:
        return self.cost_sum / self.freq_total if self.freq_total else 0.0

    @property
    def score(self) -> float:
        return self.freq_total * self.avg_cost

    def to_dict(self) -> dict:
        return {
            "sig": self.sig,
            "freq_total": self.freq_total,
            "freq_success": self.freq_success,
            "freq_failure": self.freq_failure,
            "avg_cost": round(self.avg_cost, 3),
            "targets": sorted(self.targets),
            "num_targets": len(self.targets),
            "score": round(self.score, 3),
            "bucket": self.bucket,
        }


def mine(traces: Iterable[dict], min_targets: int = 2) -> dict:
    """挖掘需求。返回报告字典（含 `entries` 与分桶统计）。"""
    table: dict[str, DemandEntry] = {}
    trace_count = 0
    verified_count = 0
    step_count = 0

    for trace in traces:
        trace_count += 1
        verified = trace.get("verified") is True
        verified_count += 1 if verified else 0
        target = str(trace.get("id"))
        for step in trace.get("steps", []):
            sig = normalize_sig(step.get("signature", ""))
            if not sig:
                continue
            step_count += 1
            entry = table.setdefault(sig, DemandEntry(sig=sig))
            entry.freq_total += 1
            if verified:
                entry.freq_success += 1
            else:
                entry.freq_failure += 1
            entry.cost_sum += int(step.get("goalsLeft", 0) or 0)
            entry.targets.add(target)

    for entry in table.values():
        repeat_ok = len(entry.targets) >= min_targets
        if not repeat_ok:
            entry.bucket = "artifact" if (entry.freq_success == 0 and entry.freq_failure > 0) else "local"
        elif entry.freq_success == 0:
            # 跨目标重复但只在失败轨迹里出现：可疑（可能是共同的死路），不直接当需求
            entry.bucket = "suspect_failure_only"
        else:
            entry.bucket = "demand"

    entries = sorted(table.values(), key=lambda e: (-e.score, e.sig))
    buckets = collections.Counter(e.bucket for e in entries)
    return {
        "min_targets": min_targets,
        "traces": trace_count,
        "verified_traces": verified_count,
        "steps": step_count,
        "signatures": len(entries),
        "buckets": dict(buckets),
        "demand_count": buckets.get("demand", 0),
        "entries": [e.to_dict() for e in entries],
        "top_demand": [e.to_dict() for e in entries if e.bucket == "demand"][:20],
    }
