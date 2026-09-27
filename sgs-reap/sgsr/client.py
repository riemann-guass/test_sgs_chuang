"""对外通信的唯一入口：HTTP JSON 客户端 + 服务配置 + OpenAI 兼容 chat 后端。

合并自旧 `utils/http_client.py` + `models/config.py` + `models/backend.py`。
以前 HTTP 有两套（这里的 `post_json` 与 `backend._post`），配置与后端又各占一个模块，
调用方要记三处；现在"与外部服务说话"只有这一个文件。

为什么要有这个文件：同一段 20 行的 POST 逻辑此前在仓库里存在 7 份副本，
而且**语义不一致**：有的把网络错误抛出去，有的吞成 `{"error": ...}` 返回。
同一件事两套语义可以防止“服务坏了却被记录成数学失败”——调用方拿到的
空列表到底是"模型没生成"还是"服务挂了"，取决于它恰好调的是哪一份副本。

这里只保留两种**显式命名**的语义，调用方必须选一个，不能"忘了处理"：

* `post_json(...)`：网络 / HTTP / 非 JSON 响应一律抛 `BackendUnavailable`。
  用于"失败必须记账"的地方（在线求解、repair）；调用方要么重试，要么把
  `backend_error` 写进记录。
* `soft_post_json(...)`：把错误收进 `{"error": {...}}` 返回。用于确实要把错误当
  数据处理的老脚本；调用方**必须**在报告里把它单列，不许与"模型没产出"混在一起。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 服务配置（`.env`）的位置：密钥与模型名都在这里，**不入库**。
ENV_PATH = Path(__file__).resolve().parent / "models" / ".env"

#: 默认超时（秒）。模型侧单次调用实测中位 6.9 s、最长 61.7 s，留足余量。
DEFAULT_TIMEOUT = 300.0


class BackendUnavailable(RuntimeError):
    """后端不可用（503 / 超时 / 网络 / 非 JSON 响应）。

    **必须**与“模型证不出”分开记：前者是服务故障，后者是这道题对模型太难。
    """


def _error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def soft_post_json(url: str, payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST 一个 JSON，错误收进 `{"error": {...}}` 返回（**不抛**）。

    返回体的形状与真代理的错误响应一致，调用方可以统一用 `response.get("error")`
    判断这次调用是不是服务问题。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return _error_payload(f"http_{exc.code}", detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _error_payload("network", str(exc))
    except ValueError as exc:
        return _error_payload("bad_json", f"响应不是合法 JSON：{exc}")


def post_json(url: str, payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST 一个 JSON，任何失败都抛 `BackendUnavailable`（**不吞错**）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise BackendUnavailable(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BackendUnavailable(f"网络错误（{url}）：{exc}") from exc
    except ValueError as exc:
        raise BackendUnavailable(f"响应不是合法 JSON（{url}）：{exc}") from exc
    # 代理以 HTTP 200 + `{"error": ...}` 报告后端故障（503 是给 HTTP 层的），
    # 这里也要转成异常，否则"服务挂了"会退化成"模型没产出"。
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        raise BackendUnavailable(
            f"{error.get('code', 'error')}: {error.get('message', '')}"
        )
    return data


# ─────────────────────── 服务配置（旧 models/config.py） ───────────────────────


def ensure_utf8_stdout() -> None:
    """Windows 控制台默认可能是 GBK，中文日志会乱码；统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释。不覆盖已存在的环境变量。"""
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


#: 默认 OpenAI 兼容端点（可用 `DEEPSEEK_BASE_URL` 覆盖）。
DEFAULT_BASE_URL = "https://api.deepseek.com"


def api_key() -> str:
    load_env()
    return get("DEEPSEEK_API_KEY")


def base_url() -> str:
    load_env()
    return get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def model() -> str:
    load_env()
    return get("DEEPSEEK_MODEL", "")


# ─────────────────────── OpenAI 兼容 chat 后端（旧 models/backend.py） ───────────────────────

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class BackendError(RuntimeError):
    pass


class Backend:
    """带缓存、重试与 token 统计的 chat 客户端（仅用标准库）。"""

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None) -> None:
        load_env()
        # 用 `get` 直接读，避免与参数名 `model` / `base_url` / `api_key` 相撞
        # （同名函数在函数体里被参数遮蔽，早先在其它模块里踩过）。
        self.model = model or get("DEEPSEEK_MODEL")
        self.base_url = (base_url or get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise BackendError("缺少 DEEPSEEK_API_KEY（见 sgsr/models/.env）")
        if not self.model:
            raise BackendError("缺少 DEEPSEEK_MODEL（见 sgsr/models/.env）")
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
        max_retries: int = 5,
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
            # 命中缓存 = **没有真的调后端** = 没有花 token。
            # 旧实现把上一次的 usage 原样返回，于是同一道题重跑会让
            # `CostPerSolved` 翻倍（成本指标算的是钱，不是"如果重算一遍要多少钱"）。
            # 原值留在 `cached_usage` 里供审计，但 `usage` 必须清零。
            return cached["text"], {
                **cached["meta"],
                "cache_hit": True,
                "usage": {},
                "cached_usage": cached["meta"].get("usage", {}),
            }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        # 重试策略：可重试状态码与网络层错误都退避重试。
        # phase28 实测：一轮 20 题里有 3 题连续 6 次（代理 3 次 × 客户端 1 次重试）
        # 都拿到 `SSL: UNEXPECTED_EOF_WHILE_READING`——那是 API 侧的连接被掐断，
        # 几十秒后自己恢复。3 次重试（2+4 s）不够，改成 5 次（2+4+8+16 s）。
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
