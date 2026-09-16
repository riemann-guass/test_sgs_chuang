"""对后端做一次最小 chat 调用，确认模型名、响应结构、usage 字段与 Lean 输出能力。

用法：
    python smoke_chat.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import config


def chat(messages: list[dict], temperature: float = 1.0, max_tokens: int = 512) -> dict:
    payload = {
        "model": config.model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{config.base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {config.api_key()}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    config.ensure_utf8_stdout()
    model = config.model()
    print(f"[smoke] model = {model}")

    # 用例 1：中文回答，确认基本可用
    res = chat([{"role": "user", "content": "用一句话说明 Lean4 的 `have` 策略做什么。"}])
    choice = res["choices"][0]
    print("[smoke] usage =", json.dumps(res.get("usage", {}), ensure_ascii=False))
    print("[smoke] 回答 =", choice["message"]["content"].strip().replace("\n", " ")[:200])

    # 用例 2：按 SGS conjecturer 的口径输出 Lean4 命题，确认格式可控
    prompt = (
        "You are given a Lean 4 goal:\n"
        "```lean4\nx : ℝ\nhx : 0 < x\n⊢ x ≤ x * x\n```\n"
        "Propose ONE auxiliary lemma that would help prove it, referencing only variables "
        "already in scope. Output only the proposition (a Lean 4 type), no `theorem` keyword, "
        "no proof, between ```lean4 and ``` tags."
    )
    # deepseek-flash 是推理模型：max_tokens 必须同时覆盖 reasoning 与可见回答，
    # 否则会在 reasoning 阶段就 finish_reason=length，content 为空。
    res2 = chat([{"role": "user", "content": prompt}], max_tokens=8192)
    choice2 = res2["choices"][0]
    msg = choice2["message"]
    print("[smoke] finish_reason =", choice2.get("finish_reason"))
    print("[smoke] usage2 =", json.dumps(res2.get("usage", {}), ensure_ascii=False))
    print("[smoke] message 字段 =", sorted(msg.keys()))
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    print("[smoke] content 长度 =", len(content), " reasoning 长度 =", len(reasoning))
    print("[smoke] 候选引理原始输出 =", content.strip().replace("\n", " ")[:400])
    if not content.strip() and reasoning:
        print("[smoke] reasoning 片段 =", reasoning.strip().replace("\n", " ")[:300])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        config.ensure_utf8_stdout()
        print(f"[smoke] HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:400]}")
        raise SystemExit(1)
