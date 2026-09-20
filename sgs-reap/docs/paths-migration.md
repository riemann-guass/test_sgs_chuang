# 目录重构对照表（phase24）

为了让结构与上游 SGS（`D:\bianma\code\SGS`）对齐，Python 侧做了一次**纯搬迁式**重构：
包名从散落的 `service/` `graph/` `tests/` 收拢成 `sgsr/`，实验入口收进 `scripts/`。
**没有任何功能改动**（思路框架与协议不变），所有阶段日志里的旧路径按本表理解。

## 旧 → 新

| 旧路径 | 新路径 | 说明 |
|---|---|---|
| `service/config.py` | `sgsr/models/config.py` | 配置（读同目录 `.env`） |
| `service/backend.py` | `sgsr/models/backend.py` | OpenAI 兼容后端客户端 |
| `service/prompts.py` | `sgsr/models/prompts.py` | 提示词与解析器 |
| `service/proxy.py` | `sgsr/models/proxy.py` | 模型代理（`/conjecture` `/guide` `/solve`） |
| `service/mock_server.py` | `sgsr/models/mock_server.py` | 确定性假服务（离线测试用） |
| `service/.env(.example)` | `sgsr/models/.env(.example)` | 密钥（不入库） |
| `service/probe_*.py`、`smoke_chat.py`、`check_backend.py` | `tools/` 同名 | 一次性探针与后端自检 |
| `graph/schema.py` | `sgsr/data/schema.py` | 轨迹/需求的数据模式 |
| `graph/demand.py` | `sgsr/pipeline/demand.py` | N1 需求挖掘 |
| `graph/conjecture.py` | `sgsr/pipeline/conjecture.py` | 猜想（N1 下游） |
| `graph/library.py` | `sgsr/pipeline/library.py` | 引理库读写 |
| `graph/coverage.py` | `sgsr/pipeline/coverage.py` | N2 覆盖度与子模贪心 |
| `graph/runner.py` | `sgsr/pipeline/runner.py` | **闭环编排** |
| `tests/lean_server.py` | `sgsr/verification/client.py` | Lean 服务常驻客户端 |
| `tests/run_*.py`、`build_library.py`、`diagnose_exceptions.py`、`test_prompts.py`、`verify_lemma_refs.py` | `scripts/` 同名 | 实验入口（对应 SGS 的 `scripts/`） |
| `tools/minif2f_to_jsonl.py` | 不变 | 数据集转换 |
| `service/README.md` | `sgsr/models/README.md`（重写） | — |
| `tests/README.md` | `scripts/README.md` | — |

## 删除的东西（及理由）

| 删除项 | 理由 |
|---|---|
| `tests/run_gate_g3.py` | **空转装置**：它的候选池被定义成"各目标轨迹签名的并集"，于是每条候选按构造至少覆盖一个目标，贪心比必然 1.000、子模性必然 0 违例——测的是一个恒真命题。真 cover 在 `scripts/run_gate_g3_real.py`（两臂测量）。 |
| `graph/coverage.py::sig_cover` / `dependency_cover` | 同上，是那套空转代理的实现；选择层现在用 `parent_cover`（父目标覆盖），评测层用两臂真 cover。 |
| `sgslean/generated/` | 物化目标已改到 `sgslean/SgsLean/GeneratedLibrary.lean`（必须在 lib 内才能被 `import`）。 |
| `service/`、`tests/`、`graph/` 目录本身 | 文件已按上表搬走，剩下的只有日志与 `__pycache__`。 |
| 本地 `__pycache__`、`*.log`、`experiments/runs/`、`data/raw/` | 可再生产物，本来就没入库（见 `.gitignore`）。 |

## 重构中踩到的坑（同类问题以后会再遇到）

1. **`Path(__file__).resolve().parents[N]` 对层级敏感**：`sgsr/verification/client.py` 下移一层后
   `parents[1]` 指向 `sgsr/` 而不是仓库根，表现为 `NotADirectoryError: [WinError 267]`
   （cwd 被设成不存在的目录）。已改为 `parents[2]` 并加注释。
2. **脚本的 `sys.path.insert` 目标要跟着变**：原来插的是 `tests/`（为了 import 同级模块），
   现在要插仓库根（为了 import `sgsr` 包）。
3. **docstring 里的 Windows 路径会触发 `SyntaxWarning: invalid escape sequence`**：
   `python tests\diagnose...` 里的 `\d` 被当成转义。模块 docstring 加 `r` 前缀即可。
4. **中文 Windows 默认 GBK**：argparse 的 `--help` 里带 `↔` 就会抛 `UnicodeEncodeError`。
   已在 `sgsr/utils/service.py::parse_port` 里统一先切 UTF-8。
