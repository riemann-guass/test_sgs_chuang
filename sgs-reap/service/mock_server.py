"""确定性假服务：实现 docs/api-contract.md 的 v1 契约，不依赖任何第三方库。

用途：让 Lean 侧在完全不联网、不花钱的前提下完成端到端验证。

用法：
    python mock_server.py --port 8765
    python mock_server.py --mode noisy      # 混入非法候选，用于负例测试
    python mock_server.py --mode empty      # 永远返回空候选

环境变量：
    MOCK_CANDIDATES   JSON 数组，覆盖默认候选命题池
    MOCK_RELEVANT     JSON 数组，命中者 relevance=5，否则 relevance=2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "v1"

DEFAULT_CANDIDATES = ["True", "1 = 1", "∀ (n : ℕ), n = n"]
DEFAULT_RELEVANT = ["True"]

NON_ELABORATING = "NotARealType"
UNPARSABLE = "∀ (n : ℕ), n = "


def sub_scores_to_review(relevance: float, complexity: float, redundancy: float) -> float:
    """SGS 原式：sgs/models/guide/llm_judge_guide.py"""
    if complexity in (3, 4):
        return 0.0
    return max(0.0, relevance + (2 - complexity) + (1 - redundancy))


class MockState:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.candidates = json.loads(os.environ.get("MOCK_CANDIDATES", "null")) or list(
            DEFAULT_CANDIDATES
        )
        self.relevant = json.loads(os.environ.get("MOCK_RELEVANT", "null")) or list(
            DEFAULT_RELEVANT
        )

    def conjecture(self, req: dict) -> tuple[list[dict], dict]:
        n = int(req.get("num_samples", 3) or 3)
        n = max(1, min(8, n))
        start = time.perf_counter()

        if self.mode == "empty":
            pool: list[str] = []
        elif self.mode == "noisy":
            pool = [UNPARSABLE, NON_ELABORATING, *self.candidates]
        else:
            pool = list(self.candidates)

        candidates = []
        for i, typ in enumerate(pool[:n]):
            candidates.append(
                {
                    "index": i,
                    "type": typ,
                    "raw": f"<mock>{typ}</mock>",
                    "review": self.review_of(typ),
                }
            )
        meta = {
            "backend": f"mock:{self.mode}",
            "model": "mock-1",
            "latency_ms": round((time.perf_counter() - start) * 1000, 3),
            "cache_hit": False,
        }
        return candidates, meta

    def review_of(self, typ: str) -> float:
        relevance = 5.0 if typ in self.relevant else 2.0
        return sub_scores_to_review(relevance=relevance, complexity=1, redundancy=0)

    def guide(self, req: dict) -> tuple[list[dict], dict]:
        start = time.perf_counter()
        scores = []
        for i, cand in enumerate(req.get("candidates", [])):
            typ = cand.get("type", "")
            relevant = typ in self.relevant
            relevance = 5.0 if relevant else 2.0
            complexity = 1.0
            redundancy = 0.0
            scores.append(
                {
                    "index": cand.get("index", i),
                    "relevance": relevance,
                    "redundancy": redundancy,
                    "complexity": complexity,
                    "review": sub_scores_to_review(relevance, complexity, redundancy),
                }
            )
        meta = {
            "backend": f"mock:{self.mode}",
            "model": "mock-1",
            "latency_ms": round((time.perf_counter() - start) * 1000, 3),
            "cache_hit": False,
        }
        return scores, meta

    @staticmethod
    def cache_key(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "sgs-reap-mock/" + VERSION
    protocol_version = "HTTP/1.1"

    # 注入点，避免在每个方法里重复 self.server.state
    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover
        sys.stderr.write("[mock] " + fmt % args + "\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err: str, message: str) -> None:
        self._send(code, {"error": {"code": err, "message": message}})

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._error(400, "bad_request", "empty body")
            return None
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, "bad_request", f"invalid JSON: {exc}")
            return None
        if not isinstance(req, dict):
            self._error(400, "bad_request", "body must be a JSON object")
            return None
        return req

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/health":
            self._error(404, "not_found", f"unknown path {self.path}")
            return
        self._send(200, {"status": "ok", "backend": f"mock:{self.state.mode}", "version": VERSION})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        req = self._read_json()
        if req is None:
            return
        request_id = req.get("request_id")
        if path == "/conjecture":
            if "goal_state" not in req:
                self._error(422, "invalid_params", "goal_state is required")
                return
            candidates, meta = self.state.conjecture(req)
            payload = {"candidates": candidates, "meta": meta}
        elif path == "/guide":
            if not isinstance(req.get("candidates"), list):
                self._error(422, "invalid_params", "candidates must be a list")
                return
            scores, meta = self.state.guide(req)
            payload = {"scores": scores, "meta": meta}
        else:
            self._error(404, "not_found", f"unknown path {path}")
            return
        if request_id is not None:
            payload["request_id"] = request_id
        self._send(200, payload)


def build_server(host: str, port: int, mode: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.state = MockState(mode)  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="sgs-reap 假服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", default="normal", choices=["normal", "noisy", "empty"])
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.mode)
    print(f"[mock] listening on http://{args.host}:{args.port} mode={args.mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
