"""真实模型代理：实现 docs/api-contract.md 的 v1 契约，后端为 OpenAI 兼容 API。

    python proxy.py --port 8770
    GET  /health    存活与后端信息
    GET  /stats     token / 延迟 / 缓存统计（阶段 4 成本核算的依据）
    POST /conjecture
    POST /guide
    POST /solve      （`statement` 走标准提示词；`prompt` 直接送拼好的提示词，供 repair 用）

只依赖标准库。所有对外调用与解析细节都收敛在这里，Lean 侧只认契约。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sgsr.models import config
from sgsr.models import prompts
from sgsr.models.backend import Backend, BackendError

VERSION = "v1"

# 关闭 thinking 时 16k 绰绰有余；打开 thinking 时实测需要 ≥16k 才不至于被 reasoning 吃光
CONJECTURE_MAX_TOKENS = int(os.environ.get("CONJECTURE_MAX_TOKENS", "16384"))
GUIDE_MAX_TOKENS = int(os.environ.get("GUIDE_MAX_TOKENS", "16384"))
SOLVE_MAX_TOKENS = int(os.environ.get("SOLVE_MAX_TOKENS", "16384"))
CONJECTURE_TEMPERATURE = float(os.environ.get("CONJECTURE_TEMPERATURE", "1.0"))
# Guide 是搜索先验的来源，稳定性比多样性重要：实测 T=1.0 时最优候选出现
# review = [8,8,3,8,8]（stdev 2.0），T=0.0 时为 [8,8,8,8,8]（stdev 0.0）。
GUIDE_TEMPERATURE = float(os.environ.get("GUIDE_TEMPERATURE", "0.0"))
# Solve 要的是**多样性**（solve_rate 是"k 次采样里成功几次"），所以默认给一个正温度；
# 具体取值由闸门 G1 标定。
SOLVE_TEMPERATURE = float(os.environ.get("SOLVE_TEMPERATURE", "0.6"))
LOG_PATH = os.environ.get("PROXY_LOG_PATH", "proxy_log.jsonl")

BACKEND_LOCK = threading.Lock()
BACKEND: Backend | None = None


def log_record(record: dict) -> None:
    record["ts"] = time.time()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # 日志失败不应影响服务
        print(f"[proxy] 日志写入失败: {exc}", file=sys.stderr)


def handle_conjecture(req: dict) -> dict:
    assert BACKEND is not None
    goal_state = str(req.get("goal_state", ""))
    num_samples = max(1, min(8, int(req.get("num_samples", 3) or 3)))
    if not goal_state.strip():
        raise ValueError("goal_state 为空")

    # N1 的两个条件化输入：需求签名（来自真实轨迹的聚合）与库范例。
    # 只做长度截断与去空，**不做语义过滤**——过滤规则属于 Lean 侧的门检/硬门。
    demand = [str(x) for x in (req.get("demand") or []) if str(x).strip()][:8]
    seeds = [str(x) for x in (req.get("seeds") or []) if str(x).strip()][:4]
    prompt = prompts.conjecture_prompt(goal_state, num_samples, demand=demand, seeds=seeds)
    with BACKEND_LOCK:
        text, meta = BACKEND.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=CONJECTURE_MAX_TOKENS,
            temperature=CONJECTURE_TEMPERATURE,
        )
    props = prompts.extract_propositions(text)
    seen: set[str] = set()
    candidates = []
    for prop in props:
        if prop in seen:
            continue
        seen.add(prop)
        if len(candidates) >= num_samples:
            break
        candidates.append(
            {"index": len(candidates), "type": prop, "raw": text[:2000], "review": 0.0}
        )
    log_record(
        {
            "endpoint": "conjecture",
            "num_samples": num_samples,
            "demand": len(demand),
            "seeds": len(seeds),
            "parsed": len(props),
            "kept": len(candidates),
            "usage": meta.get("usage", {}),
            "latency_ms": meta.get("latency_ms"),
            "cache_hit": meta.get("cache_hit"),
        }
    )
    return {
        "candidates": candidates,
        "meta": {
            **meta,
            "parsed_propositions": len(props),
            "demand_used": len(demand),
            "seeds_used": len(seeds),
        },
    }


def handle_guide(req: dict) -> dict:
    assert BACKEND is not None
    target = str(req.get("target", ""))
    raw_candidates = req.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates 必须是列表")
    scores = []
    parse_failures = 0
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    latency = 0.0
    cache_hits = 0
    for position, candidate in enumerate(raw_candidates):
        index = int(candidate.get("index", position))
        conjecture = str(candidate.get("type", ""))
        prompt = prompts.guide_prompt(target, conjecture)
        try:
            with BACKEND_LOCK:
                text, meta = BACKEND.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=GUIDE_MAX_TOKENS,
                    temperature=GUIDE_TEMPERATURE,
                )
        except BackendError as exc:
            parse_failures += 1
            log_record({"endpoint": "guide", "index": index, "error": str(exc)})
            scores.append(
                {"index": index, "relevance": 0.0, "redundancy": 0.0, "complexity": 0.0, "review": 0.0}
            )
            continue
        parsed = prompts.parse_guide_scores(text)
        if parsed is None:
            parse_failures += 1
            parsed = {"relevance": 0.0, "redundancy": 0.0, "complexity": 0.0, "review": 0.0}
        usage = meta.get("usage", {}) or {}
        usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        latency += float(meta.get("latency_ms", 0.0) or 0.0)
        cache_hits += 1 if meta.get("cache_hit") else 0
        scores.append({"index": index, **parsed})

    log_record(
        {
            "endpoint": "guide",
            "candidates": len(raw_candidates),
            "parse_failures": parse_failures,
            "usage": usage_totals,
            "latency_ms": round(latency, 1),
        }
    )
    return {
        "scores": scores,
        "meta": {
            "backend": "deepseek",
            "model": BACKEND.model,
            "latency_ms": round(latency, 1),
            "cache_hits": cache_hits,
            "parse_failures": parse_failures,
            "usage": usage_totals,
        },
    }


def handle_solve(req: dict) -> dict:
    """整篇证明生成：给定 statement 返回 k 篇候选证明（tactic 脚本）。

    与 `/conjecture` 一样是**软失败**：解析不出证明就回 `{"proofs": []}`，
    由调用方把空列表当作"本次没生成出东西"，而不是错误。

    两条入口：
    * `statement`：代理自己拼 `solve_prompt`（常规求解与两臂评测走这条）；
    * `prompt`：**调用方直接给完整提示词**。P1 的 repair 步骤需要把内核的诊断
      （失败码 + 错误原文）按 `repair_prompt` 的模板回灌，拼不出"只有 statement"
      的形式，所以必须能从外面送提示词进来。两条同时给出时 `prompt` 优先。
    """
    assert BACKEND is not None
    statement = str(req.get("statement", ""))
    raw_prompt = str(req.get("prompt", ""))
    if not statement.strip() and not raw_prompt.strip():
        raise ValueError("statement 与 prompt 不能同时为空")
    num_samples = max(1, min(8, int(req.get("num_samples", 4) or 4)))

    # 记忆注入（阶段 D/E）：库里的引理已在 Lean 环境中物化成有名常量，
    # 这里把它们的**名字 + 语句**告诉模型，让它可以直接引用。
    # 空列表 = 提示词里不出现该区块 ⟹ "有库 / 无库"是干净的两臂对照（cover 的真定义靠它算）。
    library = [
        {"name": str(item.get("name", "")), "stmt": str(item.get("stmt", ""))}
        for item in (req.get("library") or [])
        if str(item.get("stmt", "")).strip()
    ][:16]
    # `prompt` 优先于 `statement`：它是 repair 送进来的**完整**提示词（含失败码与错误原文），
    # 用 `statement` 重拼一次会把那些诊断全丢掉——那正是 repair 与"再采样一次"的区别。
    prompt = raw_prompt.strip() or prompts.solve_prompt(statement, num_samples, library=library)
    with BACKEND_LOCK:
        text, meta = BACKEND.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=SOLVE_MAX_TOKENS,
            temperature=SOLVE_TEMPERATURE,
        )
    parsed = prompts.extract_proofs(text)
    seen: set[str] = set()
    proofs = []
    for proof in parsed:
        if proof in seen:
            continue
        seen.add(proof)
        if len(proofs) >= num_samples:
            break
        proofs.append({"index": len(proofs), "proof": proof, "raw": text[:4000]})
    log_record(
        {
            "endpoint": "solve",
            "num_samples": num_samples,
            "library": len(library),
            "custom_prompt": bool(raw_prompt.strip()),
            "parsed": len(parsed),
            "kept": len(proofs),
            "usage": meta.get("usage", {}),
            "latency_ms": meta.get("latency_ms"),
            "cache_hit": meta.get("cache_hit"),
        }
    )
    return {"proofs": proofs, "meta": {**meta, "parsed_proofs": len(parsed)}}


class Handler(BaseHTTPRequestHandler):
    server_version = "sgs-reap-proxy/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover
        sys.stderr.write("[proxy] " + fmt % args + "\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err: str, message: str) -> None:
        self._send(code, {"error": {"code": err, "message": message}})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "backend": "deepseek",
                    "model": BACKEND.model if BACKEND else None,
                    "version": VERSION,
                },
            )
        elif path == "/stats":
            self._send(200, BACKEND.stats() if BACKEND else {})
        else:
            self._error(404, "not_found", f"unknown path {path}")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, "bad_request", f"invalid JSON: {exc}")
            return
        request_id = req.get("request_id") if isinstance(req, dict) else None
        try:
            if path == "/conjecture":
                payload = handle_conjecture(req)
            elif path == "/guide":
                payload = handle_guide(req)
            elif path == "/solve":
                payload = handle_solve(req)
            else:
                self._error(404, "not_found", f"unknown path {path}")
                return
        except ValueError as exc:
            self._error(422, "invalid_params", str(exc))
            return
        except BackendError as exc:
            log_record({"endpoint": path, "error": str(exc)})
            self._error(503, "backend_unavailable", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 保证服务不因单次请求崩溃
            log_record({"endpoint": path, "error": f"{type(exc).__name__}: {exc}"})
            self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")
            return
        if request_id is not None:
            payload["request_id"] = request_id
        self._send(200, payload)


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    return server


def main() -> int:
    global BACKEND
    config.ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="sgs-reap 真实模型代理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    try:
        BACKEND = Backend()
    except BackendError as exc:
        print(f"[proxy] 初始化后端失败: {exc}")
        return 2

    server = build_server(args.host, args.port)
    print(
        f"[proxy] listening on http://{args.host}:{args.port} "
        f"model={BACKEND.model} conjecture_max_tokens={CONJECTURE_MAX_TOKENS}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[proxy] stats:", json.dumps(BACKEND.stats(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
