"""探测 deepseek-flash 的推理预算行为：输出上限、可否降低推理强度。

用法：
    python probe_reasoning.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import config
import prompts

GOAL = "n : ℕ\n⊢ 2 ∣ n ^ 2 + n"


def call(payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{config.base_url()}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {config.api_key()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}


def summarise(label: str, status: int, body: dict) -> None:
    if status != 200:
        print(f"[probe] {label}: HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
        return
    choice = body["choices"][0]
    usage = body.get("usage", {}) or {}
    details = usage.get("completion_tokens_details", {}) or {}
    content = (choice.get("message", {}).get("content") or "").strip()
    print(
        f"[probe] {label}: finish={choice.get('finish_reason')} "
        f"completion={usage.get('completion_tokens')} reasoning={details.get('reasoning_tokens')} "
        f"content_len={len(content)}"
    )
    if content:
        print(f"          content = {content[:200]!r}")


def main() -> int:
    config.ensure_utf8_stdout()
    model = config.model()
    base = {
        "model": model,
        "messages": [{"role": "user", "content": prompts.conjecture_prompt(GOAL, 3)}],
        "temperature": 1.0,
        "stream": False,
    }
    print(f"[probe] model={model}")

    for max_tokens in (16384, 32768):
        status, body = call({**base, "max_tokens": max_tokens})
        summarise(f"max_tokens={max_tokens}", status, body)

    # 是否接受推理强度 / 关闭思考类参数
    for extra in ({"reasoning_effort": "low"}, {"thinking": {"type": "disabled"}}):
        status, body = call({**base, "max_tokens": 16384, **extra})
        summarise(f"+{json.dumps(extra)}", status, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
