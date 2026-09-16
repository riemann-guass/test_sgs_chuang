"""服务层配置：从 .env 读取，不依赖第三方库。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


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


def api_key() -> str:
    load_env()
    return get("DEEPSEEK_API_KEY")


def base_url() -> str:
    load_env()
    return get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")


def model() -> str:
    load_env()
    return get("DEEPSEEK_MODEL", "")
