"""把未解目标和跨题需求发送给 `/conjecture`，得到候选引理文本。

本模块只负责请求、解析和记录，不提前删除语义上较差的候选。候选是否合法、是否平凡、是否
重复、能否证明以及是否有用，都由后续可审计步骤判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sgsr.client import soft_post_json


@dataclass
class ConjectureResult:
    target_id: str
    statement: str
    demand_sent: list[str]
    seeds_sent: list[str]
    candidates: list[dict]
    meta: dict
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "target": self.target_id,
            "statement": self.statement,
            "demand_sent": self.demand_sent,
            "seeds_sent": self.seeds_sent,
            "candidates": self.candidates,
            "meta": self.meta,
            "error": self.error,
        }


def build_payload(
    target_statement: str,
    demand: list[str],
    seeds: list[str],
    num_samples: int,
) -> dict:
    """组装 `/conjecture` 的请求体（契约 v1.2）。"""
    return {
        "goal_state": target_statement,
        "num_samples": num_samples,
        "demand": list(demand),
        "seeds": list(seeds),
    }


def post(endpoint: str, payload: dict, timeout: float = 300.0) -> dict:
    """软失败 POST：错误收进 `{"error": {...}}`。

    保留软语义是因为调用方要把**每一条目标**的失败原因写进报告；
    但闭环侧（`runner.conjecture`）**必须**把 `error` 计成 `backend_error`，
    不许与"模型没出候选"混在一起——见 `runner.round` 的 `protocol/backend` 计数。
    """
    return soft_post_json(endpoint, payload, timeout=timeout)


def load_demand(demand_path: str | Path, limit: int = 8) -> list[str]:
    """从 `g2_demand.json` 里取 `bucket=demand` 的签名（按 score 排序，已在文件里排好）。

    只取 `demand` 桶：`suspect_failure_only` / `artifact` / `local` 三桶刻意不进提示词——
    前两桶是"疑似死路伪影"，第三桶是"证据不足"，把它们混进去正好破坏 N1 的设计。
    """
    path = Path(demand_path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    mining = payload.get("mining") or {}
    top = mining.get("top_demand") or payload.get("demand_entries") or []
    sigs = [str(item.get("sig", "")).strip() for item in top]
    return [s for s in sigs if s][:limit]


def load_seeds(library_path: str | Path | None, limit: int = 4) -> list[str]:
    """从引理库里取几条语句作为范例（库不存在就返回空——v1 允许空库）。"""
    if library_path is None:
        return []
    path = Path(library_path)
    if not path.exists():
        return []
    stmts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("verified") is True and row.get("stmt"):
            stmts.append(str(row["stmt"]))
    return stmts[:limit]


def generate(
    endpoint: str,
    target: dict,
    demand: list[str],
    seeds: list[str],
    num_samples: int,
    timeout: float = 300.0,
) -> ConjectureResult:
    """对一条目标请求候选引理。网络/协议失败**不抛异常**，记在 `error` 里。"""
    payload = build_payload(target["statement"], demand, seeds, num_samples)
    response = post(endpoint, payload, timeout=timeout)
    if "error" in response:
        err = response["error"]
        return ConjectureResult(
            target_id=target["id"],
            statement=target["statement"],
            demand_sent=demand,
            seeds_sent=seeds,
            candidates=[],
            meta={},
            error=f"{err.get('code')}: {err.get('message')}",
        )
    candidates = response.get("candidates") or []
    return ConjectureResult(
        target_id=target["id"],
        statement=target["statement"],
        demand_sent=demand,
        seeds_sent=seeds,
        candidates=candidates,
        meta=response.get("meta") or {},
    )
