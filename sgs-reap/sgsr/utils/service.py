"""本地服务（模型代理 / 假服务）的启动辅助。

`proxy.py` / `mock_server.py` 都是**独立进程**，几个实验脚本要各自把它们拉起来。
这里收拢三件重复的事：

* `wait_health`：轮询 `/health` 直到就绪（或超时）；
* `dump_log`：服务起不来时把它的 stdout 尾巴打出来——**必须容错**：
  进程可能还没来得及建日志文件，直接 `read_text` 会抛异常，把真正的失败原因盖掉
  （phase23 重构后就踩过：三个脚本在"proxy 未就绪"分支上崩在 `read_text`）。
* `parse_port`：统一 `--port`，顺带让脚本支持 `--help`。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def parse_port(description: str, default: int) -> int:
    # 先把控制台切成 UTF-8：argparse 的 help 里常有 `↔` 这类字符，
    # 中文 Windows 默认 GBK，会在 `--help` 上直接抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--port", type=int, default=default,
                        help=f"本地服务端口（默认 {default}）")
    return parser.parse_args().port


def wait_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                import json
                if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.4)
    return False


def dump_log(path: Path, tail: int = 1500, label: str = "service") -> None:
    """打印服务日志尾巴；文件不存在或不可读时**静默跳过**（不要让诊断代码自己崩）。"""
    try:
        if path.exists():
            print(f"[{label}] 日志 {path}（末 {tail} 字符）:")
            print(path.read_text(encoding="utf-8", errors="replace")[-tail:])
    except OSError as exc:
        print(f"[{label}] 无法读取日志 {path}：{exc}")
