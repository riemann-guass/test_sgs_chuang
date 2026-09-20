"""探测后端可用性并列出模型 ID。只打印模型名，不打印密钥。

用法：
    python check_backend.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from sgsr.models import config


def http_json(url: str, key: str, payload: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:400]}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": str(exc)}


def main() -> int:
    config.ensure_utf8_stdout()
    key = config.api_key()
    base = config.base_url()
    if not key:
        print("[check] 未找到 DEEPSEEK_API_KEY（请检查 MODELS/.env）")
        return 2

    print(f"[check] base_url = {base}")
    print(f"[check] api_key  length = {len(key)}, prefix = {key[:6]}…（其余不打印）")

    status, body = http_json(f"{base}/models", key)
    print(f"[check] GET /models -> HTTP {status}")
    if status == 200 and isinstance(body.get("data"), list):
        ids = sorted(str(item.get("id", "?")) for item in body["data"])
        print(f"[check] 可用模型 {len(ids)} 个：")
        for mid in ids:
            print(f"         - {mid}")
    else:
        print(f"[check] 响应：{json.dumps(body, ensure_ascii=False)[:600]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
