# scripts —— 实验入口

对应上游 SGS 的 `scripts/`：每个脚本是一个**可独立运行、自带判定准则**的实验单元。
所有编排逻辑都在 `sgsr/` 包里，这里只做参数、流程串联与报告落盘。

权威规格是 `docs/SG-Lean思路文档第二版.pdf`；本文件只说明"哪个脚本干哪件事"。

## 按思路的两个时间尺度分组

### 在线求解（证明器本体）

| 脚本 | 作用 | 状态 |
|---|---|---|
| `prove.py` | **单题入口**：一条命题进，一篇过内核终检的证明出 | ✅ P1（phase25） |
| `run_prover_eval.py` | 在一个数据集上批量评测，报 pass@k 与成本指标 | ✅ P1（phase25）；真模型数字待跑 |

### 离线建库（库的建立与管理）

| 脚本 | 作用 | 状态 |
|---|---|---|
| `run_round.py` | 闭环主入口（多轮建库）。**只接受课程集 C**，有硬守卫 | ✅ |
| `build_library.py` | 单轮漏斗：候选 → 硬门 → 求解 → 验证 → 物化 → 入库 | ✅ |
| `run_conjecture.py` | 出题者下游：需求 → 引理候选 → 门检（`--no-demand` 是对照） | ✅ |
| `run_gate_g2.py` | 需求挖掘的检定（分层统计 + 四条分桶） | ✅ |

### 判据与测量

| 脚本 | 作用 | 状态 |
|---|---|---|
| `run_gate_g3_real.py` | **两臂测量**：有库／无库对照，含环境预检与引用计数。复用判据的测量基础 | ✅ |
| `run_closure_tests.py` | **闭环核心协议测试**（46 条断言）：目标身份、`ok/verified` 字段、库来源守卫、角色守卫、`.lean` 解析、选择与淘汰、物化往返 | ✅ phase26 |
| `run_gate_g1.py` | 求解器能力检定（solve_rate 分布）；P2 之后由难度标定吸收 | ✅ |
| `diagnose_exceptions.py` | 判定预算诊断：把 `exception` 样本用放大预算重测 | ✅ |
| `run_server_smoke.py` | Lean 服务协议冒烟（`ping`/`check`/`verify`/`trace`/预算） | ✅ |
| `run_solve_mock.py` | 离线链路：生成 → 验证 → 轨迹落盘 | ✅ |
| `run_materialize.py` | 物化与编译校验 | ✅ |
| `verify_lemma_refs.py` | 校验 G1 引理集里的参考证明 | ✅ |
| `test_prompts.py` | 提示词离线单测（空需求必须等于没有需求） | ✅ |
| `run_m1_calibration.py` | M1 标定：利用率 / token / 延迟（驱动 `reap-fork/tests/Calibrate.lean`） | ✅ |

## 已删除的脚本（2026-09-21 精简）

| 删除项 | 理由 |
|---|---|
| `run_e2e.py` / `run_real_e2e.py` | 测的是"把 SGS 的猜想动作做成 Lean tactic"这条路，当前思路的在线流程不使用它；结论已入 `docs/phase0..2-log.md` |
| `run_proxy_smoke.py` | 代理冒烟，已被 `run_gate_g3_real.py` 的环境预检覆盖 |

## 常用命令

```powershell
cd sgs-reap
$py = "C:\Users\gaosen\anaconda3\python.exe"

# 离线：闭环冒烟（假服务，无 Mathlib，快）
& $py sgsr\models\mock_server.py --port 8765
& $py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 真跑：先起真代理（需网络权限 + sgsr\models\.env）
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"   # 见下：直接跑脚本时 sgsr 包不在 sys.path 上
& $py sgsr\models\proxy.py --port 8770
& $py scripts\run_round.py --rounds 3 --k 2 --n 3

# 两臂测量（开发集上调参；测试集冻结后只跑一次）
& $py scripts\run_gate_g3_real.py --select nearmiss --limit 6 --k 4 --endpoint http://127.0.0.1:8770/solve

# P1 完成后
& $py scripts\prove.py --statement "forall (n : Nat), n + 0 = n" --k 4
& $py scripts\run_prover_eval.py --set D --k 4 --out experiments\results\p1_dev.json
```

> `python sgsr\models\proxy.py` 直接跑时，Python 会把**脚本所在目录**（`sgsr\models`）放进
> `sys.path`，于是 `from sgsr.models import config` 报 `ModuleNotFoundError`。
> 起服务前把仓库根加到 `PYTHONPATH`（如上），或用 `python -m sgsr.models.proxy`。

## 约定

* **判定准则写在脚本里、跑之前定死**（每个脚本的文件头都有），避免事后解释。
* 报告一律带 `mode` 与规模后缀，防止小规模重跑覆盖正式报告。
* Lean 侧测试在 `sgslean/SgsLean/Test/` 与 `reap-fork/Reap/Test/`，用 `lake build` 跑。
