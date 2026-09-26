# 阶段 24 执行日志（目录重构：按 SGS 布局重组，裁撤死代码）

## 为什么做

运行了 23 个阶段之后，Python 侧散成三个平级目录（`service/` 模型服务、`graph/` 数据与优化、
`tests/` 实验脚本），与上游 SGS（`D:\bianma\code\SGS` 用 `sgs/{data,models,pipeline,training,utils,verification}`
+ `scripts/`）对不上，也不好定位"某个问题该改哪个文件夹"。

本次是**纯搬迁式重构**：思路框架（`docs/framework.md`）与数据协议（`docs/data-protocol.md`）**一字未改**。

## 结构对照

```
重构前                                重构后（对齐 SGS）
service/  ────────┐
graph/    ────────┼──►  sgsr/             ← Python 包（对应 SGS 的 sgs/）
tests/*.py ───────┘        ├─ data/           轨迹/需求的数据模式
                           ├─ models/         模型服务：真代理 + 假服务 + 提示词
                           ├─ pipeline/       N1 需求 / 猜想 / 库 / N2 覆盖 / **闭环编排**
                           ├─ verification/   Lean 服务常驻客户端
                           └─ utils/          本地服务启动辅助
      experiments/*.py ──►  scripts/          实验入口（对应 SGS 的 scripts/）
      service/probe_*.py ►  tools/            一次性探针 + 数据集转换
```

完整旧→新对照表在 `docs/paths-migration.md`（含被删项与理由）。

## 删掉的东西

| 删除项 | 理由 |
|---|---|
| `tests/run_gate_g3.py` + `coverage.py` 里的 `sig_cover`/`dependency_cover` | **空转装置**：候选池被定义成"各目标轨迹签名的并集"，于是每条候选按构造至少覆盖一个目标——贪心比必然 1.000、子模性必然 0 违例。真 cover 在 `scripts/run_gate_g3_real.py`（两臂测量）。 |
| `sgslean/generated/` | 物化目标已移到 `sgslean/SgsLean/GeneratedLibrary.lean`（必须在 lib 内才能被 `import`）。 |
| `service/`、`tests/`、`graph/` 三个目录本身 | 文件已搬走，剩下的只有日志与 `__pycache__`。 |
| 本地 `__pycache__` / `*.log` / `reap-fork` 里的测试产物 | 可再生产物，本来就没入库。 |

## 重构中踩到的四个坑（都写进对照表了）

1. **`parents[N]` 对层级敏感**：`sgsr/verification/client.py` 下移一层后 `parents[1]` 指到了 `sgsr/`，
   表现为 `NotADirectoryError: [WinError 267]`（进度好的假象是"server 未启动"）。
2. **脚本的 `sys.path.insert` 目标要跟着变**（原来插 `tests/`，现在插仓库根）。
3. **docstring 里的 Windows 路径触发 `SyntaxWarning: invalid escape sequence`**（`\d`）→ 模块 docstring 加 `r`。
4. **中文 Windows 是 GBK**：argparse 的 `--help` 里带 `↔` 直接抛 `UnicodeEncodeError` →
   在 `sgsr/utils/service.py::parse_port` 里统一先切 UTF-8。
5. 顺带修掉三处**重复且脆弱**的代码：三个 P0 脚本各自读服务日志尾巴、失败即崩
   （日志文件还没建出来时会抛异常，把真正的失败原因盖掉）→ 收拢成 `sgsr/utils/service.py::dump_log`。

## 验证（全部当场重跑）

| 检查 | 结果 |
|---|---|
| `python -m compileall -qf sgsr scripts tools` | ✅ 退出 0（含 `-W error::SyntaxWarning`） |
| 17 个脚本 `--help` | ✅ 全部 OK（含此前崩过的三个 P0 脚本） |
| `scripts/run_server_smoke.py` | ✅ PASS（16 请求 / 17 行纯 JSON，frontend 15.4s） |
| `scripts/run_solve_mock.py` | ✅ PASS（10 目标 × 3 篇，solve_rate mean 0.2666） |
| `scripts/run_round.py --expect-mock --imports none`（2 轮闭环） | ✅ 跑通，111 s |
| `scripts/run_conjecture.py`（假服务） | ✅ 跑通 |
| `lake build SgsLean SgsLean.Test sgslean-server` | ✅ **422 jobs** |

## 现状

目录与上游 SGS 一一对应；每个包/目录都有 README 说明职责；死代码与误导性装置已清除；
所有入口都能跑。**下一步仍按 `docs/data-protocol.md`：在 C（课程集）上跑多轮闭环建库，
再用 miniF2F valid 调参、test 只跑一次。**
