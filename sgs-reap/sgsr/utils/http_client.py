"""与本地服务通信的**唯一** HTTP JSON 客户端。

为什么要有这个文件：同一段 20 行的 POST 逻辑此前在仓库里存在 7 份副本
（`prover.post_json` / `repair.solve_with_prompt` / `conjecture.post` /
`runner._post` / `build_library.http_post` / `run_gate_g1.http_post` /
`run_gate_g2.http_post`），而且**语义不一致**：有的把网络错误抛出去，有的吞成
`{"error": ...}` 返回。同一件事两套语义，正是审计最反对的那一类
"装置坏了却伪装成结果"——调用方拿到的空列表到底是"模型没生成"还是"服务挂了"，
取决于它恰好调的是哪一份副本。

这里只保留两种**显式命名**的语义，调用方必须选一个，不能"忘了处理"：

* `post_json(...)`：网络 / HTTP / 非 JSON 响应一律抛 `BackendUnavailable`。
  用于"失败必须记账"的地方（在线求解、repair）；调用方要么重试，要么把
  `backend_error` 写进记录。
* `soft_post_json(...)`：把错误收进 `{"error": {...}}` 返回。用于确实要把错误当
  数据处理的老脚本；调用方**必须**在报告里把它单列，不许与"模型没产出"混在一起。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

#: 默认超时（秒）。模型侧单次调用实测中位 6.9 s、最长 61.7 s，留足余量。
DEFAULT_TIMEOUT = 300.0


class BackendUnavailable(RuntimeError):
    """后端不可用（503 / 超时 / 网络 / 非 JSON 响应）。

    **必须**与"模型证不出"分开记：前者是装置坏了，后者是这道题对模型太难。
    """


def _error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def soft_post_json(url: str, payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST 一个 JSON，错误收进 `{"error": {...}}` 返回（**不抛**）。

    返回体的形状与真代理的错误响应一致，调用方可以统一用 `response.get("error")`
    判断这次调用是不是装置问题。
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
