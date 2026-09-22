"""在线求解主流程（P1，规格文档第 3 节）。

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
  两者的区别是"我们的装置坏了"与"这道题对模型太难"，混在一起会让所有指标失真。
* **所有步骤都要记账**（调用次数、token、Lean 墙钟），否则成本指标失去意义。

## 为什么兜底与求解都产出"候选"，而只有一处放行

兜底很便宜但**没有经过内核终检**，求解很贵但同样只是"候选"。两条路汇合在验证步骤，
由 `Verify.verify` 统一放行——这样"输出的一定是验证过的"这条不变量只有一个实现点，
不会随着路径增加而出现漏洞。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from sgsr.pipeline import repair as repair_module
from sgsr.pipeline import retrieval as retrieval_module
from sgsr.pipeline.retrieval import Premise, RetrievalResult

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

_DECL_RE = re.compile(
    r"(?:^|\n)\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(?:theorem|lemma|example)\s*",
)

#: 声明之前允许出现的一行式命令（`import` / `open` / `namespace` / `set_option` …）。
#: 它们必须被**剔掉**再找声明，否则 `import Mathlib` 会被拼进命题里——审计 P0 第 6 条。
_PRELUDE_RE = re.compile(
    r"(?m)^\s*(?:"
    r"import\s+[^\n]*"
    r"|open\s+[^\n]*"
    r"|namespace\s+[^\n]*"
    r"|end\s+[^\n]*"
    r"|section\s*[^\n]*"
    r"|variable\s+[^\n]*"
    r"|universe\s+[^\n]*"
    r"|set_option\s+[^\n]*"
    r"|local\s+[^\n]*"
    r"|noncomputable\s+section"
    r")\s*$"
)
#: 块注释（含文档注释 `/-- … -/` 与 `/--! … -/`）。
_BLOCK_COMMENT_RE = re.compile(r"/-[-!]?.*?-/", re.DOTALL)


def _strip_prelude(text: str) -> str:
    """去掉声明之前的 import/open/namespace/set_option 与块注释。"""
    cleaned = _BLOCK_COMMENT_RE.sub("\n", text)
    cleaned = _PRELUDE_RE.sub("", cleaned)
    return cleaned


def _find_top_level_colon(text: str) -> int:
    """找**不在括号内**的第一个 `:`。用于切开 `theorem <名字> <绑定> : <类型>`。

    不能直接用 `text.find(":")`：`theorem add_zero (n : Nat) : n + 0 = n` 里第一个冒号
    是绑定变量里的那个，按它切会把名字切成一团乱码（实测踩过）。
    """
    depth = 0
    pairs = {"(": ")", "{": "}", "[": "]"}
    closing = set(pairs.values())
    for index, char in enumerate(text):
        if char in pairs:
            depth += 1
        elif char in closing:
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            return index
    return -1


def _split_binders(text: str) -> tuple[list[str], str]:
    """把 `(x : T) (h : P) {a : U} [inst : C]` 前缀切成绑定列表与剩余文本。"""
    binders: list[str] = []
    rest = text.lstrip()
    while rest[:1] in ("(", "{", "["):
        close_ch = {"(": ")", "{": "}", "[": "]"}[rest[0]]
        depth = 0
        end = -1
        for index, char in enumerate(rest):
            if char == rest[0]:
                depth += 1
            elif char == close_ch:
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            break
        binders.append(rest[: end + 1])
        rest = rest[end + 1 :].lstrip()
    return binders, rest


def _binder_to_forall(binder: str) -> str:
    """`(x : T)` → `∀ x : T,`；`{a : U}` → `∀ {a : U},`；`[inst : C]` → `∀ [inst : C],`。"""
    inner = binder[1:-1].strip()
    opener = "" if binder[0] == "(" else binder[0]
    closer = "" if binder[0] == "(" else {"{": "}", "[": "]"}[binder[0]]
    return f"∀ {opener}{inner}{closer},"


def close_declaration(text: str) -> str:
    """把一条 `theorem`/`example` 声明闭包成自足的命题（协议 v1.2 要求闭式）。

    步骤：剔掉 import/open/namespace/注释 → 定位声明 → 切掉证明体 →
    找**顶层**冒号把"名字 + 绑定"与"命题"分开 → 绑定转 `∀`。
    """
    body = _strip_prelude(_DECL_RE.sub("\n", _strip_prelude(text), count=1)).strip()
    # 截掉证明体
    for marker in (":= by", ":=by", ":="):
        index = body.find(marker)
        if index != -1:
            body = body[:index]
            break
    colon = _find_top_level_colon(body)
    if colon == -1:
        head, rest = body, ""
    else:
        head, rest = body[:colon], body[colon + 1:]
    # head 有两种形状：
    #   `add_zero (n : Nat)` —— 名字 + 绑定
    #   `(n : Nat)`          —— 只有绑定（`example` 形式）
    # 名字是**不以括号开头**的首个词；剥掉它之后再切绑定。
    head = head.strip()
    if head[:1] not in ("(", "{", "["):
        parts = head.split(None, 1)
        head = parts[1].strip() if len(parts) == 2 else ""
    binders, _ = _split_binders(head)
    statement = re.sub(r"\s+", " ", rest.strip())
    if not binders:
        return statement
    return " ".join(_binder_to_forall(binder) for binder in binders) + " " + statement


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
        if not _DECL_RE.search(marked):
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
    decl = _DECL_RE.search(marked)
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


class BackendUnavailable(RuntimeError):
    """后端不可用（503 / 超时 / 网络）。**必须**与"模型证不出"分开记。"""


def post_json(url: str, payload: dict, timeout: float = 300.0) -> dict:
    """POST 一个 JSON，返回解析后的响应。**不吞错**：网络/HTTP 错误一律抛 `BackendUnavailable`。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise BackendUnavailable(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BackendUnavailable(f"network error from {url}: {exc}") from exc
    except ValueError as exc:
        raise BackendUnavailable(f"响应不是合法 JSON：{exc}") from exc


def _usage_of(meta: dict) -> dict:
    usage = meta.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
    }


def _add_usage(total: dict, delta: dict) -> None:
    for key, value in delta.items():
        total[key] = int(total.get(key, 0)) + int(value or 0)


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
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return f"sha256:{digest[:16]}"


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
            self.library = [
                json.loads(line)
                for line in self.library_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
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
        return retrieval_module.retrieve(
            stmt, self.library, budget=budget, mathlib_endpoint=self.mathlib_endpoint
        )

    # ---- 步骤 5 / 7 的模型侧 ----
    def solve(self, stmt: str, k: int, library: list[dict] | None = None) -> tuple[list[str], dict]:
        """调 `/solve`；后端不可用时**重试一次**，仍失败则抛 `BackendUnavailable`。"""
        payload: dict = {"statement": stmt, "num_samples": k}
        if library:
            payload["library"] = library
        try:
            data = post_json(self.endpoint, payload)
        except BackendUnavailable:
            data = post_json(self.endpoint, payload)  # 规格 3.5：重试一次
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            raise BackendUnavailable(f"{error.get('code', 'error')}: {error.get('message', '')}")
        proofs = [
            str(item.get("proof", "")).strip()
            for item in (data.get("proofs") or [])
            if str(item.get("proof", "")).strip()
        ]
        return proofs, (data.get("meta") or {})

    def repair(self, stmt: str, failed: list[dict], k: int,
               library: list[dict] | None = None) -> tuple[list[str], dict]:
        """步骤 7：把失败记录回灌，产出新一轮候选（`prompts.repair_prompt`）。"""
        return repair_module.repair(
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
    def prove(self, stmt: str, budget: Budget | None = None) -> ProofResult:
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

        with self._lean() as lean:
            # ②③ 门检 + 廉价兜底：一批作业一起做（导入是最贵的固定成本）
            before = lean.frontend_ms_total
            jobs = [
                {"id": "gate", "cmd": "check", "stmt": text},
                {"id": "cheap", "cmd": "cheap", "stmt": text},
                # 兜底命中的脚本要过内核终检才放行。把它**塞进同一批**：
                # 命中时（简单题的主要出路）这一步就是最后一个 Lean 作业，
                # 整题只付一次 Mathlib 导入。代价是未命中时白跑一条 verify
                # （毫秒级），换来简单题上省掉一整次 75 s 量级的导入。
                {"id": "cheap_v", "cmd": "verify", "stmt": text, "proof": ""},
            ]
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
            cheap = (responses.get("cheap") or {}).get("result") or {}
            if CHEAP_DISABLED:
                result.notes.append("cheap: 已被 --no-cheap 关闭")
            elif cheap.get("hit") is True and cheap.get("proof"):
                candidate = str(cheap["proof"])
                # 兜底候选取同样的放行口：**必须过内核终检**才能进 proof 字段
                attempt = self._judge(responses.get("cheap_v"), candidate, "cheap", 0)
                result.attempts.append(attempt)
                if attempt.ok:
                    result.solved = True
                    result.proof = candidate
                    result.path = "cheap"
                    result.lean_ms = lean_ms
                    result.wall_ms = int((time.perf_counter() - started) * 1000)
                    return result
                result.notes.append(f"cheap 命中 {cheap.get('tactic')} 但未过终检：{attempt.reason}")

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
                    spent += sum(_usage_of(meta).values())
                    if not (meta.get("usage") or {}):
                        # 后端没回报 usage 时**必须留下痕迹**：否则成本表会显示
                        # 0 token 的"免费证明"，而那是记账缺失，不是真的免费。
                        result.notes.append(f"{source}: 后端未回报 usage（成本按 0 记）")
                except (BackendUnavailable, repair_module.RepairBackendError) as exc:
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
                    f"失败码 {repair_module.error_summary(failed_for_repair)}"
                )
                if round_index >= budget.repair_rounds:
                    result.notes.append(f"repair 轮数达上限 R={budget.repair_rounds}")

        result.usage["model_calls"] = model_calls
        result.lean_ms = lean_ms
        result.wall_ms = int((time.perf_counter() - started) * 1000)
        return result


def _default_lean_factory(imports: str = "Mathlib", stderr_path=None):
    from sgsr.verification.client import LeanServer

    return LeanServer(imports=imports, stderr_path=stderr_path)
