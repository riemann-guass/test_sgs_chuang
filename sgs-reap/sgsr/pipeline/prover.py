"""在线证明主流程。

九个步骤（多出来的第 7 步是 repair，它本身是一条循环）：

```
① 输入解析     parse_input            0 成本
② 门检         Gate.check             Lean 1 作业
③ 廉价兜底     Trivial.tryCheapTactics 0 次模型调用
④ 分层检索     retrieval.retrieve     库（0 成本）+ 外部（可降级）
⑤ 求解 k 篇    /solve                 1 次模型调用（k 条候选）
⑥ 内核验证     Verify.verify          1 批 Lean 作业
⑦ repair       /solve（repair_prompt） 最多 R 轮
⑧ 输出与记账   RunRecord
⑨ 预算与停止   预算耗尽 / 某步命中即停
```

硬规则（不可协商）：

* **未通过内核终检的脚本一律不出现在 `ProofResult.proof` 中**。兜底命中的 tactic 也一样——
  它必须再过一次 `Verify.verify`，否则一条带残余元变量的 `decide` 会绕过整个验收层。
* **后端不可用（503／超时）必须记为 `backend_error`**，不得当成「模型证不出」。
  服务故障与数学失败混在一起会让所有指标失真。
* **所有步骤都要记账**（调用次数、token、Lean 墙钟），否则成本指标失去意义。

## 为什么兜底与求解都产出"候选"，而只有一处放行

兜底很便宜但**没有经过内核终检**，求解很贵但同样只是"候选"。两条路汇合在验证步骤，
由 `Verify.verify` 统一放行——这样"输出的一定是验证过的"这条不变量只有一个实现点，
不会随着路径增加而出现漏洞。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sgsr.data import DECL_RE, close_declaration  # noqa: F401  （对外复用）
from sgsr.client import BackendUnavailable, post_json
from sgsr.models import prompts
from sgsr.pipeline import selection as selection_module
from sgsr.pipeline.selection import RetrievalResult

# 消融开关（`scripts/prove.py --no-cheap`）：关掉第 3 步廉价 tactic 兜底。
# 它是**模块级**的，因为兜底清单在 Lean 侧、跨进程；Python 侧只需要让"这张清单为空"，
# 不必再为消融另立一条代码路径（主线保持唯一）。
CHEAP_DISABLED = False


@dataclass
class Budget:
    """单题的预算上限。默认值见规格文档 3.9 节：k=4，R=2，CTX_LEMMA_BUDGET=1200。"""

    total_tokens: int = 20_000
    k: int = 4
    repair_rounds: int = 2
    ctx_lemma_tokens: int = 1_200


@dataclass
class Attempt:
    """一次候选证明及其判定结果。"""

    proof: str
    reason: str = ""
    detail: str = ""
    source: str = ""          # "cheap" | "solve" | "repair"
    round_index: int = 0      # 第几轮（0 = 首次求解，1..R = 第几轮 repair）
    ok: bool = False


@dataclass
class ProofResult:
    """单题求解的完整记录，字段见规格文档附录 A 的 RunRecord。"""

    solved: bool
    stmt: str = ""
    proof: str | None = None
    path: str = ""            # cheap | solve | repair | failed
    attempts: list[Attempt] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    lean_ms: int = 0
    wall_ms: int = 0
    retrieval: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: 报告元数据（提交号、库版本号）——规格附录 A 的 RunRecord 要求可回溯
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stmt": self.stmt,
            "solved": self.solved,
            "proof": self.proof,
            "path": self.path,
            "attempts": [
                {"proof": a.proof, "reason": a.reason, "detail": a.detail,
                 "source": a.source, "round": a.round_index, "ok": a.ok}
                for a in self.attempts
            ],
            "usage": self.usage,
            "lean_ms": self.lean_ms,
            "wall_ms": self.wall_ms,
            "retrieval": self.retrieval,
            "notes": self.notes,
            "meta": self.meta,
        }


# ─────────────────────────── 输入解析 ───────────────────────────
#
# 具体的解析规则放在 `sgsr/data/lean_parse.py`（全仓库唯一一份实现）；
# 这里只负责"闭式命题 + 来源标注"这一层，供 CLI 与评测脚本使用。

def parse_input(stmt: str | None = None, lean_file: str | Path | None = None) -> dict:
    """抽取命题并闭包成全称量化形式（规格 3.1 节）。

    返回 `{"stmt": str, "origin": str, "binders": []}`；无法抽取时返回
    `{"stmt": "", "origin": ..., "error": "parse_error", "detail": ...}`——
    调用方必须把它记成**输入问题**，不得计进解出率的分母之外（规格 3.1 的失败处理）。
    """
    if lean_file is not None:
        path = Path(lean_file)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"stmt": "", "origin": str(path), "error": "parse_error",
                    "detail": f"读取失败：{exc}", "binders": []}
        # `_DECL_RE` 的 `(?:^|\n)` 需要行首位置；文件可能以 `theorem` 直接开头，
        # 所以统一在前面补一个换行再匹配（并把这个补过的文本交给下游解析）。
        marked = "\n" + raw
        if not DECL_RE.search(marked):
            return {"stmt": "", "origin": str(path), "error": "parse_error",
                    "detail": "文件里没有 theorem/lemma/example 声明", "binders": []}
        if not re.search(r":=\s*by\b|:=", marked):
            # 声明没有证明体（比如是 `axiom`/`def` 或纯注释）：不是可证的命题，
            # 如实报 parse_error，而不是抽出一个空串往下走。
            return {"stmt": "", "origin": str(path), "error": "parse_error",
                    "detail": "声明没有 `:= by` 证明体", "binders": []}
        closed = close_declaration(marked)
        if not closed:
            return {"stmt": "", "origin": str(path), "error": "parse_error",
                    "detail": "声明已定位但抽不出命题", "binders": []}
        return {"stmt": closed, "origin": str(path), "binders": []}

    text = (stmt or "").strip()
    if not text:
        return {"stmt": "", "origin": "string", "error": "parse_error",
                "detail": "命题字符串为空", "binders": []}
    # 同文件路径：补一个换行让 `(?:^|\n)` 能匹配到行首
    marked = "\n" + text
    decl = DECL_RE.search(marked)
    if decl:
        # **像一份 .lean 文件**才按声明解析：声明之后必须还有 `:=`（证明体）。
        # 否则一段恰好含 `theorem` 字样的文本会被误当成文件（实测踩过：
        # `import Mathlib\n#check Nat.add_comm` 因为含 `:=` 而被当成声明）。
        if re.search(r":=", marked[decl.end():]):
            closed = close_declaration(marked)
            if closed:
                return {"stmt": closed, "origin": "declaration", "binders": []}
            return {"stmt": "", "origin": "declaration", "error": "parse_error",
                    "detail": "定位到声明但抽不出命题", "binders": []}
        # 有声明关键字却没有证明体：这是"看起来像 Lean 文件但没有可证命题"，
        # 如实报 parse_error（规格 3.1 的失败处理：输入问题不计入解出率）。
        return {"stmt": "", "origin": "string", "error": "parse_error",
                "detail": "文本含声明关键字但没有 `:= by` 证明体，无法抽取命题", "binders": []}
    return {"stmt": re.sub(r"\s+", " ", text), "origin": "string", "binders": []}


# ─────────────────────────── HTTP ───────────────────────────
#
# HTTP 客户端统一在 `sgsr/models/http.py`（`post_json` 抛 `BackendUnavailable`，
# `soft_post_json` 把错误当数据返回）。本模块只用前者，且 `BackendUnavailable`
# 从这里继续对外暴露，避免调用方多一个 import 面。


def _usage_of(meta: dict) -> dict:
    usage = meta.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
    }


def usage_total(usage: dict) -> int:
    """**计费**口径：prompt + completion。

    `reasoning_tokens` 是 `completion_tokens_details` 里的一个**子集**
    （OpenAI 兼容接口的定义），把它与前两类相加会把推理 token 算两遍——
    thinking 打开时这会虚增 30%–50% 的成本。报告里三类都留，求和只用这个函数。
    """
    return (int(usage.get("prompt_tokens", 0) or 0)
            + int(usage.get("completion_tokens", 0) or 0))


def _add_usage(total: dict, delta: dict) -> None:
    for key, value in delta.items():
        total[key] = int(total.get(key, 0)) + int(value or 0)


def solve_candidates(
    endpoint: str,
    stmt: str,
    k: int,
    library: list[dict] | None = None,
    *,
    prompt_mode: str = "product",
    sample_salt: str | None = None,
) -> tuple[list[str], dict]:
    """全仓库唯一的 ``/solve`` 客户端。

    在线证明、离线建库和对照实验必须经过这里，才能统一后端错误、重试、候选解析，
    以及测量提示词/轮次盐字段。调用方不得把 503 吞成“零候选”。
    """
    payload: dict = {
        "statement": stmt,
        "num_samples": k,
        "prompt_mode": prompt_mode,
    }
    if library:
        payload["library"] = library
    if sample_salt:
        payload["sample_salt"] = sample_salt
    try:
        data = post_json(endpoint, payload)
    except BackendUnavailable:
        data = post_json(endpoint, payload)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        raise BackendUnavailable(f"{error.get('code', 'error')}: {error.get('message', '')}")
    if not isinstance(data, dict):
        raise BackendUnavailable(f"/solve 响应不是对象：{str(data)[:200]}")
    proofs = [
        str(item.get("proof", "")).strip()
        for item in (data.get("proofs") or [])
        if isinstance(item, dict) and str(item.get("proof", "")).strip()
    ]
    return proofs, (data.get("meta") or {})


def library_hash(path: str | Path | None) -> str:
    """库文件的 sha256（前 16 位）。规格附录 A 的 `library_hash`。

    **报告必须带它**：硬约束 6 要求"正式数字能由 `experiments/results/` 回溯"，
    而回溯"给库之后涨了多少"必须能确定当时的库是哪一个版本。只记大小是不够的——
    大小相同的两份库可以是完全不同的内容。
    """
    if path is None:
        return "none"
    target = Path(path)
    if not target.exists():
        return "missing"
    from sgsr.pipeline.library import active_hash

    return active_hash(target)


def git_commit() -> str:
    """当前提交号（短）。取不到时返回 `"unknown"`，**不抛异常**——
    报告缺一个元数据字段不该让整次实验失败。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ─────────────────────────── 证明器 ───────────────────────────


class Prover:
    """证明器本体。构造参数与 `prove` 的签名见规格文档附录 B。"""

    def __init__(
        self,
        endpoint: str,
        backend: str = "deepseek",
        library_path: str | Path | None = None,
        imports: str = "Mathlib",
        lean_server_factory=None,
        mathlib_endpoint: str | None = None,
        stderr_path: str | Path | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.backend = backend
        self.library_path = Path(library_path) if library_path else None
        self.imports = imports
        self.mathlib_endpoint = mathlib_endpoint
        self.stderr_path = Path(stderr_path) if stderr_path else None
        self._factory = lean_server_factory or _default_lean_factory
        self.library: list[dict] = []
        if self.library_path is not None and self.library_path.exists():
            from sgsr.pipeline.library import active_rows

            self.library = active_rows(self.library_path)
        #: 库版本号：报告里必须带它，否则"给库前后的差"无法回溯到具体哪一版库。
        self.library_hash = library_hash(self.library_path)

    # ---- Lean 服务 ----
    def _lean(self):
        return self._factory(imports=self.imports, stderr_path=self.stderr_path)

    # ---- 步骤 1 ----
    def parse_input(self, stmt: str | None = None, lean_file: str | None = None) -> dict:
        """抽取命题并闭包成全称量化形式（规格 3.1）。"""
        return parse_input(stmt=stmt, lean_file=lean_file)

    # ---- 步骤 4 ----
    def retrieve(self, stmt: str, budget: int) -> RetrievalResult:
        return selection_module.retrieve(
            stmt, self.library, budget=budget, mathlib_endpoint=self.mathlib_endpoint
        )

    # ---- 步骤 5 / 7 的模型侧 ----
    def solve(self, stmt: str, k: int, library: list[dict] | None = None) -> tuple[list[str], dict]:
        return solve_candidates(self.endpoint, stmt, k, library)

    def repair(self, stmt: str, failed: list[dict], k: int,
               library: list[dict] | None = None) -> tuple[list[str], dict]:
        """步骤 7：把失败记录回灌，产出新一轮候选（`prompts.repair_prompt`）。"""
        return repair(
            stmt, failed, k=k, endpoint=self.endpoint, library=library
        )

    # ---- 步骤 6 ----
    def verify(self, items: list[dict], server=None) -> tuple[dict[str, dict], int]:
        """内核验证一批 `{id, stmt, proof}`；返回 `(响应表, 子进程毫秒)`。

        验证走 `Verify.verify`（含 `checkProof` 内核终检）。**这里不解析结果**——
        放行判断集中在 `_judge`，避免同一份判定逻辑散在两处。
        """
        if not items:
            return {}, 0
        if server is None:
            with self._lean() as lean:
                before = lean.frontend_ms_total
                responses = lean.batch(
                    [{"id": it["id"], "cmd": "verify", "stmt": it["stmt"], "proof": it["proof"]}
                     for it in items]
                )
                return responses, lean.frontend_ms_total - before
        before = server.frontend_ms_total
        responses = server.batch(
            [{"id": it["id"], "cmd": "verify", "stmt": it["stmt"], "proof": it["proof"]}
             for it in items]
        )
        return responses, server.frontend_ms_total - before

    def _cheap_two_step(self, lean, text: str) -> tuple[dict, dict, Attempt | None]:
        """老服务端（没有 `cheap_verify`）上的兜底：先问清单，命中后再单独送终检。

        多付一次 Mathlib 导入，但"廉价兜底命中的脚本也必须过内核终检"这条不变量
        在**任何**服务端版本上都成立——这正是旧实现丢掉的那一条。
        """
        responses = lean.batch([{"id": "cheap", "cmd": "cheap", "stmt": text}])
        cheap = (responses.get("cheap") or {}).get("result") or {}
        if cheap.get("hit") is not True or not cheap.get("proof"):
            return cheap, {}, None
        candidate = str(cheap["proof"])
        verdicts = lean.batch(
            [{"id": "cheap_v", "cmd": "verify", "stmt": text, "proof": candidate}]
        )
        verdict = (verdicts.get("cheap_v") or {}).get("result") or {}
        return cheap, verdict, self._judge(verdicts.get("cheap_v"), candidate, "cheap", 0)

    @staticmethod
    def _judge(response: dict | None, proof: str, source: str,
               round_index: int) -> Attempt:
        result = (response or {}).get("result") or {}
        error = (response or {}).get("error") or {}
        if error:
            return Attempt(proof=proof, reason=str(error.get("code") or "protocol_error"),
                           detail=str(error.get("message") or ""), source=source,
                           round_index=round_index, ok=False)
        if response is None:
            return Attempt(proof=proof, reason="missing_response",
                           detail="服务端没有返回这一条的判定", source=source,
                           round_index=round_index, ok=False)
        return Attempt(
            proof=proof,
            reason=str(result.get("reason") or ""),
            detail=str(result.get("detail") or ""),
            source=source,
            round_index=round_index,
            ok=result.get("ok") is True,
        )

    # ---- 九步主线 ----
    def session(self):
        """开一个 Lean 会话（`LeanServer` 上下文管理器）。

        批量评测应当**复用同一个会话**：子进程现在跨批常驻（`SgsLean/Server.lean` 的
        `serveLoop`），一次导入 Mathlib 就能服务整批题目；每题各开一个会话会把
        导入按题数重复付掉（实测 20 题 5 小时 → 复用后 2 分钟级）。
        """
        return self._lean()

    def prove(self, stmt: str, budget: Budget | None = None, lean=None) -> ProofResult:
        """九步主线。`lean` 给出时复用该会话（批量评测走这条），否则自开一个。"""
        if lean is not None:
            return self._prove_in(stmt, budget or Budget(), lean)
        with self._lean() as session:
            return self._prove_in(stmt, budget or Budget(), session)

    def _prove_in(self, stmt: str, budget: Budget, lean) -> ProofResult:
        budget = budget or Budget()
        started = time.perf_counter()
        parsed = self.parse_input(stmt)
        text = parsed.get("stmt", "")
        if not text:
            return ProofResult(
                solved=False, path="failed", notes=[f"parse_error: {parsed.get('detail', '')}"],
                wall_ms=int((time.perf_counter() - started) * 1000),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                       "model_calls": 0},
            )
        result = ProofResult(solved=False, stmt=text, path="failed")
        result.usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                        "model_calls": 0}
        result.meta = {
            "commit": git_commit(),
            "library_hash": self.library_hash,
            "library_size": len(self.library),
            "imports": self.imports,
            "endpoint": self.endpoint,
        }
        lean_ms = 0
        model_calls = 0

        # ②③ 门检 + 廉价兜底：一批作业一起做（导入是最贵的固定成本）
        before = lean.frontend_ms_total
        jobs = [{"id": "gate", "cmd": "check", "stmt": text}]
        if not CHEAP_DISABLED:
            jobs.append({"id": "cheap", "cmd": "cheap_verify", "stmt": text})
            # `cheap_verify` = 先试三批廉价 tactic，命中就**当场**把命中的那条
            # tactic 送去内核终检，返回 `{cheap, verify}`。
            #
            # 为什么不是"把 verify 一起塞进这一批"：批是在知道命中哪条 tactic
            # **之前**发出去的，那时唯一的证法只有空串 `proof=""`（恒 parse_error）。
            # 旧实现正是这么写的，于是命中结果被自己的判定否掉、兜底彻底失效——
            # 实测 `cheap` 报 `hit=true, tactic=simp`，而 `verify(proof="")` 报
            # `ok=false, reason=parse_error`。判定必须发生在拿到 tactic 之后。
        responses = lean.batch(jobs)
        lean_ms += lean.frontend_ms_total - before
        gate_response = responses.get("gate") or {}
        gate = gate_response.get("result") or {}
        if gate.get("ok") is not True:
            # 门检没通过时 `result` 可能是空的（协议错误），此时用 `error.code`；
            # 两者都没有才算 `unknown`。**不要**把协议错误写成"输入不合法"——
            # 审计指出旧代码在这条路径上记的是恒定的 "gate: unknown"，无法定位。
            reason = (gate.get("reason")
                      or (gate_response.get("error") or {}).get("code")
                      or "unknown")
            detail = (gate.get("detail")
                      or (gate_response.get("error") or {}).get("message") or "")
            result.notes.append(f"gate: {reason}" + (f" — {detail[:200]}" if detail else ""))
            # 输入/语句问题**不计入解出率的分母**（规格 3.1 的失败处理）：
            # 用 `path="gate_rejected"` 标出来，让评测脚本能把它单列。
            result.path = "gate_rejected"
            result.lean_ms = lean_ms
            result.wall_ms = int((time.perf_counter() - started) * 1000)
            return result
        # `cheap_verify` 的 `result` 是 `{"cheap": …, "verify": …}` 两层结构：
        # 外层是这条命令的响应体，内层才是廉价兜底的结果与**对命中的那条 tactic**
        # 的终检结果。别把外壳当结果用（实测：外壳上取 `hit` 恒为 None，
        # 兜底会静默失效、直接掉进模型路径）。
        payload_result = (responses.get("cheap") or {}).get("result") or {}
        cheap = payload_result.get("cheap") or {}
        cheap_verify = payload_result.get("verify") or {}
        if CHEAP_DISABLED:
            result.notes.append("cheap: 已被 --no-cheap 关闭")
        else:
            if not payload_result and (responses.get("cheap") or {}).get("error"):
                # 老服务端不认识 `cheap_verify`：退回两步式（多付一次导入，
                # 但"命中的脚本必须过终检"这条不变量在任何服务端上都成立）。
                cheap, cheap_verify, attempt = self._cheap_two_step(lean, text)
                if attempt is not None:
                    result.attempts.append(attempt)
                    if attempt.ok:
                        result.solved = True
                        result.proof = attempt.proof
                        result.path = "cheap"
                        result.lean_ms = lean.frontend_ms_total
                        result.wall_ms = int((time.perf_counter() - started) * 1000)
                        return result
            if cheap.get("hit") is True and cheap.get("proof"):
                candidate = str(cheap["proof"])
                # 兜底候选取同样的放行口：**必须过内核终检**才能进 proof 字段。
                # 判定来自 `cheap_verify` 里对**这条** tactic 的 verify 结果。
                attempt = self._judge({"result": cheap_verify}, candidate, "cheap", 0)
                result.attempts.append(attempt)
                if attempt.ok:
                    result.solved = True
                    result.proof = candidate
                    result.path = "cheap"
                    result.lean_ms = lean_ms
                    result.wall_ms = int((time.perf_counter() - started) * 1000)
                    return result
                result.notes.append(
                    f"cheap 命中 {cheap.get('tactic')} 但未过终检：{attempt.reason}"
                )
            elif cheap.get("exhausted") is True:
                # "没试完"与"试过但都不行"是两件事：前者说明清扫被整条墙钟上限截断，
                # 兜底是否本来能解出是**未知**的，不能记成兜底失败。
                result.notes.append(
                    f"cheap 清扫达到墙钟上限（{cheap.get('elapsedMs')} ms），未试完清单"
                )
        # ④ 分层检索：**放到门检之后**（规格 3.4 节的顺序）。
        # 审计指出旧实现先检索再门检——不合法/不合式的输入也会先去打一次外部检索 API，
        # 既浪费又让"检索层"的调用统计失真。
        retrieved = self.retrieve(text, budget.ctx_lemma_tokens)
        result.retrieval = {
            "premises": len(retrieved.premises),
            "library": retrieved.library_candidates,
            "mathlib": retrieved.mathlib_candidates,
            "degraded": retrieved.degraded,
            "tokens": sum(p.tokens for p in retrieved.premises),
            "names": [p.name for p in retrieved.premises],
            "notes": retrieved.notes,
        }
        prompt_library = retrieved.as_prompt_items()

        # ⑤⑥⑦ 求解 → 验证 → repair（同一批会话内循环，避免重复导入）
        spent = 0
        failed_for_repair: list[dict] = []
        for round_index in range(0, budget.repair_rounds + 1):
            if spent >= budget.total_tokens:
                result.notes.append(f"token 预算耗尽（{spent}/{budget.total_tokens}）")
                break
            source = "solve" if round_index == 0 else "repair"
            try:
                if round_index == 0:
                    proofs, meta = self.solve(text, budget.k, library=prompt_library)
                else:
                    proofs, meta = self.repair(
                        text, failed_for_repair, budget.k, library=prompt_library
                    )
                model_calls += 1
                _add_usage(result.usage, _usage_of(meta))
                spent += usage_total(_usage_of(meta))
                if meta.get("cache_hit"):
                    # 缓存命中是按 0 token 计费的（`Backend.chat` 不再回上一次的
                    # usage），单列出来，免得报告里出现"0 token 的调用"却看不出原因。
                    result.usage["cache_hits"] = int(result.usage.get("cache_hits", 0)) + 1
                    result.notes.append(f"{source}: 命中后端缓存（本次不计费）")
                elif not (meta.get("usage") or {}):
                    # 后端没回报 usage 时**必须留下痕迹**：否则成本表会显示
                    # 0 token 的"免费证明"，而那是记账缺失，不是真的免费。
                    result.notes.append(f"{source}: 后端未回报 usage（成本按 0 记）")
            except BackendUnavailable as exc:
                result.notes.append(f"backend_error: {exc}")
                result.attempts.append(
                    Attempt(proof="", reason="backend_error", detail=str(exc),
                            source=source, round_index=round_index, ok=False)
                )
                break
            if not proofs:
                result.notes.append(f"{source}: 本轮没有解析出候选")
                break

            before = lean.frontend_ms_total
            items = [
                {"id": f"{source}:{round_index}:{index}", "stmt": text, "proof": proof}
                for index, proof in enumerate(proofs)
            ]
            verdicts = lean.batch(
                [{"id": it["id"], "cmd": "verify", "stmt": it["stmt"], "proof": it["proof"]}
                 for it in items]
            )
            lean_ms += lean.frontend_ms_total - before

            round_attempts: list[Attempt] = []
            winner: str | None = None
            for index, proof in enumerate(proofs):
                attempt = self._judge(
                    verdicts.get(f"{source}:{round_index}:{index}"),
                    proof, source, round_index,
                )
                round_attempts.append(attempt)
                if attempt.ok and winner is None:
                    winner = proof
            result.attempts.extend(round_attempts)
            if winner is not None:
                result.solved = True
                result.proof = winner
                result.path = source
                break
            failed_for_repair = [
                {"proof": a.proof, "reason": a.reason, "detail": a.detail}
                for a in round_attempts
            ]
            result.notes.append(
                f"{source}: 第 {round_index} 轮 {len(proofs)} 篇全部未通过，"
                f"失败码 {error_summary(failed_for_repair)}"
            )
            if round_index >= budget.repair_rounds:
                result.notes.append(f"repair 轮数达上限 R={budget.repair_rounds}")

        result.usage["model_calls"] = model_calls
        result.lean_ms = lean_ms
        result.wall_ms = int((time.perf_counter() - started) * 1000)
        return result


def _default_lean_factory(imports: str = "Mathlib", stderr_path=None):
    from sgsr.lean import LeanServer

    # **必须显式给单作业心跳预算**：不给就用服务端的默认值（4,000,000），
    # 而离线脚本（run_round / build_library / g3 / calibrate）都传 `budget_for_jobs()`
    # （= 400M）。两条路不一致的后果实测过：D 上的 `aime_1984_p15` 在 4M 下门检
    # 直接 `exception`（`whnf` 超时），而同一条语句在 400M 下能过门检——
    # 于是"在线九步"的 pass@k 里混进了一个纯预算伪影。
    from sgsr.lean import budget_for_jobs

    return LeanServer(imports=imports, heartbeats=budget_for_jobs(), stderr_path=stderr_path)


# ═══════════════════════ repair 定向重试（规格 3.7；旧 pipeline/repair.py） ═══════════════════════
#
# 核心思路：内核返回的是**结构化失败码**加 Lean 错误原文，比一句「失败」信息量大得多。
# 把 `[失败码 + 错误原文 + 原命题]` 按固定模板回灌给求解器，要求它针对这个错误重写。
#
# 停止条件：轮数上限 R（默认 2）或 token 预算耗尽——两者都由 `Prover` 掌握，
# 这一段只负责"给定失败记录，产出下一轮 k 篇候选"。
# 若上一轮全部是 `mvar_or_sorry`，提示词里追加「不要使用 sorry / admit」的硬指令
# （在 `prompts.repair_prompt` 里实现）。
#
# ## 与"重新采样"的区别
#
# 直接把 `/solve` 再调一次的期望收益很低：温度相同、提示词相同，分布也相同。
# repair 把 Lean 的诊断变成提示词的一部分，才是"从失败里学到东西"的那一步。
# 这也是**唯一**一条在"不做梯度更新"前提下能利用错误信号的通道。
#
# 注意与 `Prover.repair`（上面那个方法）的分工：方法是实例上的步骤 7，
# 把 `Prover` 的状态（endpoint）接上；这里的模块级函数只做"拼提示词 + 调后端 + 解析"。


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
    后端不可用时抛 `BackendUnavailable`（**不**返回空列表——空列表的含义是
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
