"""OpenAI 兼容后端客户端：请求、缓存、重试、token 统计。仅用标准库。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import config

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        config.load_env()
        self.model = model or config.model()
        self.base_url = (base_url or config.base_url()).rstrip("/")
        self.api_key = api_key or config.api_key()
        if not self.api_key:
            raise BackendError("缺少 DEEPSEEK_API_KEY（见 service/.env）")
        if not self.model:
            raise BackendError("缺少 DEEPSEEK_MODEL（见 service/.env）")
        self._cache: dict[str, dict] = {}
        # deepseek-flash 是推理模型：打开 thinking 时单次调用会烧掉 3k–10k reasoning token
        # （实测 8192 的 max_tokens 会被 reasoning 全部吃掉并返回空 content）。
        # 关闭 thinking 后同一次调用只需 ~83 token，输出质量在抽查中相当。
        # 这是一个可消融开关，阶段 4 会做 on/off 对照。
        self.disable_thinking = os.environ.get("DISABLE_THINKING", "1") not in ("0", "false", "False")
        self.calls = 0
        self.cache_hits = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.latency_ms = 0.0

    @staticmethod
    def cache_key(messages: list[dict], max_tokens: int, temperature: float, thinking: bool) -> str:
        blob = json.dumps(
            {"m": messages, "mt": max_tokens, "t": temperature, "thinking": thinking},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 8192,
        temperature: float = 1.0,
        max_retries: int = 3,
    ) -> tuple[str, dict]:
        """返回 (可见回答文本, meta)。

        reasoning 内容不计入返回值，只在 usage 里统计。注意 `deepseek-flash`
        是推理模型：`max_tokens` 同时覆盖 reasoning 与可见回答，预算不足会出现
        `finish_reason=length` 且 content 为空。
        """
        thinking = not self.disable_thinking
        key = self.cache_key(messages, max_tokens, temperature, thinking)
        if key in self._cache:
            self.cache_hits += 1
            cached = self._cache[key]
            return cached["text"], {**cached["meta"], "cache_hit": True}

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        last_error = ""
        start = time.perf_counter()
        for attempt in range(max_retries):
            try:
                body = self._post(payload)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {exc.code}: {raw[:300]}"
                if exc.code in RETRYABLE_STATUS and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise BackendError(last_error) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"网络错误: {exc}"
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise BackendError(last_error) from exc

            latency_ms = (time.perf_counter() - start) * 1000.0
            choice = body["choices"][0]
            message = choice.get("message", {}) or {}
            usage = body.get("usage", {}) or {}
            details = usage.get("completion_tokens_details", {}) or {}

            self.calls += 1
            self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            self.reasoning_tokens += int(details.get("reasoning_tokens", 0) or 0)
            self.latency_ms += latency_ms

            text = (message.get("content") or "").strip()
            meta = {
                "backend": "deepseek",
                "model": self.model,
                "thinking": thinking,
                "latency_ms": round(latency_ms, 1),
                "finish_reason": choice.get("finish_reason"),
                "usage": usage,
                "cache_hit": False,
            }
            if not text and choice.get("finish_reason") == "length":
                raise BackendError(
                    "reasoning 预算耗尽（finish_reason=length 且 content 为空），请提高 max_tokens"
                )
            self._cache[key] = {"text": text, "meta": meta}
            return text, meta
        raise BackendError(last_error or "未知后端错误")

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def stats(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_latency_ms": round(self.latency_ms, 1),
            "avg_latency_ms": round(self.latency_ms / self.calls, 1) if self.calls else 0.0,
        }
