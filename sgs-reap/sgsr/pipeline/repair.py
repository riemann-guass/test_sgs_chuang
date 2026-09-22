"""repair 定向重试（P1，规格第 3.7 节）。

核心思路：内核返回的是**结构化失败码**加 Lean 错误原文，比一句「失败」信息量大得多。
把 `[失败码 + 错误原文 + 原命题]` 按固定模板回灌给求解器，要求它针对这个错误重写。

停止条件：轮数上限 R（默认 2）或 token 预算耗尽——两者都由 `prover.Prover` 掌握，
本模块只负责"给定失败记录，产出下一轮 k 篇候选"这一件事。
若上一轮全部是 `mvar_or_sorry`，提示词里追加「不要使用 sorry / admit」的硬指令
（在 `prompts.repair_prompt` 里实现）。

## 与"重新采样"的区别

直接把 `/solve` 再调一次的期望收益很低：温度相同、提示词相同，分布也相同。
repair 把 Lean 的诊断变成提示词的一部分，才是"从失败里学到东西"的那一步。
这也是**唯一**一条在"不做梯度更新"前提下能利用错误信号的通道。
"""

from __future__ import annotations

from sgsr.utils.http_client import BackendUnavailable, post_json
from sgsr.models import prompts


#: 后端不可用。以前这里另立了一个 `RepairBackendError`，结果调用方得同时 catch 两个
#: 异常类；现在与在线主线共用 `BackendUnavailable`（`sgsr/models/http.py`）。
RepairBackendError = BackendUnavailable


def solve_with_prompt(
    prompt: str,
    k: int,
    endpoint: str,
    timeout: float = 300.0,
    stmt_hint: str = "",
) -> tuple[list[str], dict]:
    """把一段**已经拼好的**提示词发给 `/solve`，取回 k 篇候选证明。

    为什么要这个函数：`/solve` 端点自己会拼 `solve_prompt`，而 repair 需要换成
    `repair_prompt`。与其在代理里开第二个端点（多一处协议面），不如让代理支持
    "调用方直接给提示词"——见 `proxy.handle_solve` 的 `prompt` 字段。

    返回 `(proofs, meta)`；`meta` 含 usage 与 parsed 数，供成本记账。
    后端不可用时抛 `RepairBackendError`（**不**返回空列表——空列表的含义是
    "模型这次没生成出东西"，与"服务挂了"必须区分，否则失败率会被污染）。
    """
    payload = {"prompt": prompt, "num_samples": k}
    if stmt_hint:
        # 真代理的 repair 入口要 `statement`（它自己会拼 repair 提示词）；
        # 我们走 `prompt` 直接送拼好的提示词，但仍把原命题带上：同一个语句文本
        # 在两处出现，让"模型看到的命题"与"内核验证的命题"逐字一致。
        payload["statement"] = stmt_hint
    data = post_json(endpoint, payload, timeout=timeout)
    proofs = [
        str(item.get("proof", "")).strip()
        for item in (data.get("proofs") or [])
        if str(item.get("proof", "")).strip()
    ]
    return proofs, (data.get("meta") or {})


def repair(
    stmt: str,
    failed: list[dict],
    k: int = 4,
    endpoint: str | None = None,
    library: list[dict] | None = None,
    timeout: float = 300.0,
) -> tuple[list[str], dict]:
    """给定失败记录，产出新一轮 k 篇候选证明脚本。

    `failed` 的每项形如 `{"proof": str, "reason": str, "detail": str}`（规格附录 B 的 `Attempt`）。
    `endpoint=None` 时抛 `ValueError`：repair 必须有后端，静默返回空列表会让
    "后端没配"伪装成"修不出来"。
    """
    if not endpoint:
        raise ValueError("repair 需要 endpoint（/solve 的 URL）")
    prompt = prompts.repair_prompt(stmt, failed, num_samples=k, library=library)
    return solve_with_prompt(prompt, k, endpoint, timeout=timeout, stmt_hint=stmt)


def error_summary(failed: list[dict]) -> dict[str, int]:
    """失败码直方图，供报告与日志用（"这一轮主要栽在哪类错误上"）。"""
    summary: dict[str, int] = {}
    for attempt in failed:
        reason = str(attempt.get("reason") or "").strip() or "unknown"
        summary[reason] = summary.get(reason, 0) + 1
    return summary
